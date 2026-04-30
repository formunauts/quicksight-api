import argparse
import datetime
import json
import os
import re
from typing import Any, Dict, List, Optional

import boto3

from qs_migrate_paymentattempt_datasets import (
    LOG_DIR,
    QS_ACCOUNT_ID,
    REGION,
    ROOT_DIR,
    Logger,
    PAYMENTATTEMPT_PATTERN,
    get_all_summaries,
    is_unsupported_file_source_error,
    require_env,
)


TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
BUREAU_PAYMENTATTEMPT_PATTERN = re.compile(r"\bbureau_paymentattempt\b", re.IGNORECASE)


def build_text_report_path() -> str:
    return os.path.join(LOG_DIR, f"paymentattempt_failed_refresh_audit_{TIMESTAMP}.txt")


def build_json_report_path() -> str:
    return os.path.join(LOG_DIR, f"paymentattempt_failed_refresh_audit_{TIMESTAMP}.json")


def build_target_plan_path() -> str:
    return os.path.join(LOG_DIR, f"paymentattempt_failed_refresh_targets_{TIMESTAMP}.json")


def list_all_ingestions(qs_client, dataset_id: str) -> List[Dict[str, Any]]:
    paginator = qs_client.get_paginator("list_ingestions")
    ingestions: List[Dict[str, Any]] = []
    for page in paginator.paginate(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
        PaginationConfig={"PageSize": 100},
    ):
        ingestions.extend(page.get("Ingestions", []))
    return ingestions


def pick_latest_ingestion(ingestions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not ingestions:
        return None
    return max(
        ingestions,
        key=lambda item: item.get("CreatedTime")
        or item.get("UpdatedTime")
        or datetime.datetime.min,
    )


def describe_dataset(qs_client, dataset_id: str) -> Dict[str, Any]:
    response = qs_client.describe_data_set(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
    )
    return response["DataSet"]


def find_bureau_paymentattempt_references(dataset: Dict[str, Any]) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []
    physical_map = dataset.get("PhysicalTableMap", {})
    if not isinstance(physical_map, dict):
        return matches

    for physical_table_id, physical_table in physical_map.items():
        custom_sql = physical_table.get("CustomSql")
        if not custom_sql:
            continue
        sql_query = custom_sql.get("SqlQuery", "")
        if not isinstance(sql_query, str):
            continue
        if not BUREAU_PAYMENTATTEMPT_PATTERN.search(sql_query):
            continue
        matches.append(
            {
                "physical_table_id": physical_table_id,
                "custom_sql_name": custom_sql.get("Name", physical_table_id),
                "sql_query": sql_query,
            }
        )

    return matches


def build_target_row(
    dataset: Dict[str, Any],
    latest_ingestion: Dict[str, Any],
    paymentattempt_matches: List[Dict[str, str]],
) -> Dict[str, Any]:
    error_info = latest_ingestion.get("ErrorInfo", {}) if isinstance(latest_ingestion, dict) else {}
    return {
        "name": dataset["Name"],
        "data_set_id": dataset["DataSetId"],
        "arn": dataset.get("Arn"),
        "import_mode": dataset.get("ImportMode"),
        "latest_ingestion": {
            "ingestion_id": latest_ingestion.get("IngestionId"),
            "status": latest_ingestion.get("IngestionStatus"),
            "created_time": (
                latest_ingestion.get("CreatedTime").isoformat()
                if hasattr(latest_ingestion.get("CreatedTime"), "isoformat")
                else latest_ingestion.get("CreatedTime")
            ),
            "error_type": error_info.get("Type"),
            "error_message": error_info.get("Message"),
        },
        "bureau_paymentattempt_matches": paymentattempt_matches,
    }


def write_json(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)


def write_target_plan(path: str, rows: List[Dict[str, Any]]) -> None:
    payload = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": REGION,
        "source": "failed_refresh_paymentattempt_audit",
        "datasets": rows,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)


def summarize_failed_status(latest_ingestion: Dict[str, Any], include_cancelled: bool) -> bool:
    status = latest_ingestion.get("IngestionStatus")
    if status == "FAILED":
        return True
    if include_cancelled and status == "CANCELLED":
        return True
    return False


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", REGION)
    os.makedirs(LOG_DIR, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="Find datasets whose latest SPICE refresh failed and that reference bureau_paymentattempt."
    )
    parser.add_argument(
        "--dataset-name-contains",
        help="Optional case-insensitive substring filter before checking ingestions.",
    )
    parser.add_argument(
        "--include-cancelled",
        action="store_true",
        help="Also treat CANCELLED as a failed latest refresh.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional limit on the number of datasets to inspect after filtering.",
    )
    args = parser.parse_args()

    logger = Logger(build_text_report_path())
    json_report_path = build_json_report_path()
    target_plan_path = build_target_plan_path()

    qs = boto3.client("quicksight", region_name=REGION)
    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {REGION})")
    logger.log("Mode: READ ONLY")

    all_datasets = get_all_summaries(qs.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
    if args.dataset_name_contains:
        needle = args.dataset_name_contains.lower()
        all_datasets = [
            item for item in all_datasets if needle in item.get("Name", "").lower()
        ]
    if args.limit:
        all_datasets = all_datasets[: args.limit]

    logger.log(f"Datasets selected for ingestion audit: {len(all_datasets)}")

    inspected = 0
    failed_latest_refresh = 0
    failed_with_paymentattempt = 0
    unsupported_failed_datasets = 0
    skipped_no_ingestions = 0
    skipped_non_failed = 0
    failed_to_inspect = 0

    matching_rows: List[Dict[str, Any]] = []
    unsupported_rows: List[Dict[str, Any]] = []
    inspection_errors: List[Dict[str, str]] = []

    for summary in all_datasets:
        inspected += 1
        dataset_id = summary["DataSetId"]
        dataset_name = summary["Name"]

        if inspected == 1 or inspected % 25 == 0:
            logger.log(f"Progress: inspected {inspected}/{len(all_datasets)} datasets")

        try:
            ingestions = list_all_ingestions(qs, dataset_id)
        except Exception as exc:
            failed_to_inspect += 1
            inspection_errors.append(
                {
                    "name": dataset_name,
                    "data_set_id": dataset_id,
                    "stage": "list_ingestions",
                    "error": str(exc),
                }
            )
            continue

        latest_ingestion = pick_latest_ingestion(ingestions)
        if not latest_ingestion:
            skipped_no_ingestions += 1
            continue

        if not summarize_failed_status(latest_ingestion, args.include_cancelled):
            skipped_non_failed += 1
            continue

        failed_latest_refresh += 1

        try:
            dataset = describe_dataset(qs, dataset_id)
        except Exception as exc:
            if is_unsupported_file_source_error(exc):
                unsupported_failed_datasets += 1
                unsupported_rows.append(
                    {
                        "name": dataset_name,
                        "data_set_id": dataset_id,
                        "latest_ingestion_status": latest_ingestion.get("IngestionStatus"),
                        "latest_ingestion_id": latest_ingestion.get("IngestionId"),
                        "reason": "QuickSight public DescribeDataSet does not support this uploaded-file dataset.",
                    }
                )
                continue
            failed_to_inspect += 1
            inspection_errors.append(
                {
                    "name": dataset_name,
                    "data_set_id": dataset_id,
                    "stage": "describe_data_set",
                    "error": str(exc),
                }
            )
            continue

        paymentattempt_matches = find_bureau_paymentattempt_references(dataset)
        if not paymentattempt_matches:
            continue

        failed_with_paymentattempt += 1
        row = build_target_row(dataset, latest_ingestion, paymentattempt_matches)
        matching_rows.append(row)

        logger.log("")
        logger.log(f"DATASET: {dataset_name} ({dataset_id})")
        logger.log(f"  Import mode: {dataset.get('ImportMode')}")
        logger.log(
            f"  Latest ingestion: {latest_ingestion.get('IngestionStatus')} ({latest_ingestion.get('IngestionId')})"
        )
        error_info = latest_ingestion.get("ErrorInfo", {})
        if error_info.get("Message"):
            logger.log(f"  Error: {error_info.get('Message')}")
        for match in paymentattempt_matches:
            logger.log(
                f"  bureau_paymentattempt SQL: {match['custom_sql_name']} [{match['physical_table_id']}]"
            )

    report_payload = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": REGION,
        "root_dir": ROOT_DIR,
        "filters": {
            "dataset_name_contains": args.dataset_name_contains,
            "include_cancelled": args.include_cancelled,
            "limit": args.limit,
        },
        "summary": {
            "datasets_selected": len(all_datasets),
            "datasets_inspected": inspected,
            "datasets_with_no_ingestions": skipped_no_ingestions,
            "datasets_whose_latest_refresh_failed": failed_latest_refresh,
            "failed_refresh_datasets_with_bureau_paymentattempt": failed_with_paymentattempt,
            "unsupported_failed_datasets": unsupported_failed_datasets,
            "inspection_errors": failed_to_inspect,
        },
        "datasets": matching_rows,
        "unsupported_failed_datasets": unsupported_rows,
        "inspection_errors": inspection_errors,
    }

    write_json(json_report_path, report_payload)
    write_target_plan(target_plan_path, matching_rows)

    logger.log("")
    logger.log(f"Datasets inspected: {inspected}")
    logger.log(f"Datasets with no ingestions: {skipped_no_ingestions}")
    logger.log(f"Datasets whose latest refresh failed: {failed_latest_refresh}")
    logger.log(f"Failed-refresh datasets using bureau_paymentattempt: {failed_with_paymentattempt}")
    logger.log(f"Unsupported failed datasets skipped: {unsupported_failed_datasets}")
    logger.log(f"Inspection errors: {failed_to_inspect}")
    logger.log(f"JSON report: {json_report_path}")
    logger.log(f"Target plan: {target_plan_path}")


if __name__ == "__main__":
    main()
