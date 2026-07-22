import argparse
import copy
import datetime
import json
from typing import Any, Dict, List, Optional, Tuple

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Update PhysicalTableMap DataSourceArn values in selected QuickSight datasets "
            "from one data source name to another."
        )
    )
    parser.add_argument(
        "--dataset-ids",
        nargs="+",
        required=True,
        help="One or more QuickSight dataset IDs to inspect/update.",
    )
    parser.add_argument(
        "--legacy-data-source-name",
        required=True,
        help="Exact QuickSight data source name currently used by physical tables (for example: LiveDataBase).",
    )
    parser.add_argument(
        "--target-data-source-name",
        required=True,
        help="Exact QuickSight data source name that should replace the legacy source.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates. Without this flag, the script runs in dry-run mode.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue with remaining datasets when one dataset fails.",
    )
    return parser.parse_args()


def find_data_source_by_name(qs_client, account_id: str, name: str) -> Dict[str, Any]:
    summaries = get_all_summaries(qs_client.list_data_sources, account_id, "DataSources")
    matches = [row for row in summaries if row.get("Name") == name]

    if not matches:
        raise SystemExit(f"No QuickSight data source found with exact name: {name}")

    if len(matches) > 1:
        duplicate_ids = ", ".join(sorted(str(row.get("DataSourceId")) for row in matches))
        raise SystemExit(
            "Multiple QuickSight data sources share this name. "
            f"Name: {name}. Matching DataSourceIds: {duplicate_ids}"
        )

    return matches[0]


def describe_dataset(qs_client, account_id: str, dataset_id: str) -> Dict[str, Any]:
    response = qs_client.describe_data_set(
        AwsAccountId=account_id,
        DataSetId=dataset_id,
    )
    return response["DataSet"]


def build_update_payload(account_id: str, dataset: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "AwsAccountId": account_id,
        "DataSetId": dataset["DataSetId"],
        "Name": dataset["Name"],
        "PhysicalTableMap": dataset["PhysicalTableMap"],
        "ImportMode": dataset["ImportMode"],
    }

    optional_keys = [
        "LogicalTableMap",
        "ColumnGroups",
        "FieldFolders",
        "RowLevelPermissionDataSet",
        "RowLevelPermissionTagConfiguration",
        "ColumnLevelPermissionRules",
        "DataSetUsageConfiguration",
        "DatasetParameters",
        "PerformanceConfiguration",
        "DataPrepConfiguration",
        "SemanticModelConfiguration",
    ]
    for key in optional_keys:
        if key in dataset and dataset[key]:
            payload[key] = dataset[key]

    return payload


def patch_physical_table_data_sources(
    dataset: Dict[str, Any], legacy_data_source_arn: str, target_data_source_arn: str
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    updated_dataset = copy.deepcopy(dataset)
    changes: List[Dict[str, str]] = []

    physical_table_map = updated_dataset.get("PhysicalTableMap", {})
    if not isinstance(physical_table_map, dict):
        return updated_dataset, changes

    for physical_table_id, physical_table in physical_table_map.items():
        if not isinstance(physical_table, dict):
            continue

        for source_type in ["CustomSql", "RelationalTable", "S3Source"]:
            source_obj = physical_table.get(source_type)
            if not isinstance(source_obj, dict):
                continue

            current_arn = source_obj.get("DataSourceArn")
            if current_arn != legacy_data_source_arn:
                continue

            source_obj["DataSourceArn"] = target_data_source_arn
            changes.append(
                {
                    "physical_table_id": str(physical_table_id),
                    "source_type": source_type,
                    "source_name": str(source_obj.get("Name") or ""),
                    "from_data_source_arn": str(current_arn),
                    "to_data_source_arn": target_data_source_arn,
                }
            )

    return updated_dataset, changes


def json_default(value: Any) -> str:
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    raise TypeError(f"Unsupported type: {type(value)!r}")


def main() -> None:
    args = parse_args()
    account_id = require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    dataset_ids = list(dict.fromkeys(args.dataset_ids))

    text_log_path = build_log_path("custom_sql_datasource_migration", "txt")
    json_log_path = build_log_path("custom_sql_datasource_migration", "json")
    logger = Logger(text_log_path, "QUICKSIGHT PHYSICAL TABLE DATASOURCE MIGRATION")

    qs_client = create_quicksight_client()

    legacy_data_source = find_data_source_by_name(qs_client, account_id, args.legacy_data_source_name)
    target_data_source = find_data_source_by_name(qs_client, account_id, args.target_data_source_name)

    legacy_arn = legacy_data_source.get("Arn")
    target_arn = target_data_source.get("Arn")

    if not isinstance(legacy_arn, str) or not legacy_arn:
        raise SystemExit(f"Could not resolve ARN for legacy data source: {args.legacy_data_source_name}")
    if not isinstance(target_arn, str) or not target_arn:
        raise SystemExit(f"Could not resolve ARN for target data source: {args.target_data_source_name}")

    logger.log(f"Connected to QuickSight (Account: {account_id}, Region: {QS_REGION})")
    logger.log(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    logger.log(f"Legacy source: {args.legacy_data_source_name} ({legacy_data_source.get('DataSourceId')})")
    logger.log(f"Target source: {args.target_data_source_name} ({target_data_source.get('DataSourceId')})")
    logger.log(f"Dataset targets: {len(dataset_ids)}")
    logger.log("")

    plan: Dict[str, Any] = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": account_id,
        "region": QS_REGION,
        "mode": "APPLY" if args.apply else "DRY_RUN",
        "legacy_data_source": {
            "name": args.legacy_data_source_name,
            "id": legacy_data_source.get("DataSourceId"),
            "arn": legacy_arn,
        },
        "target_data_source": {
            "name": args.target_data_source_name,
            "id": target_data_source.get("DataSourceId"),
            "arn": target_arn,
        },
        "datasets": [],
        "summary": {},
    }

    changed_datasets = 0
    changed_physical_tables = 0
    applied_datasets = 0
    failed_datasets = 0

    for index, dataset_id in enumerate(dataset_ids, start=1):
        logger.log(f"{index}. Inspecting dataset {dataset_id}")

        try:
            original_dataset = describe_dataset(qs_client, account_id, dataset_id)
            updated_dataset, changes = patch_physical_table_data_sources(
                original_dataset,
                legacy_data_source_arn=legacy_arn,
                target_data_source_arn=target_arn,
            )

            dataset_row: Dict[str, Any] = {
                "data_set_id": dataset_id,
                "name": original_dataset.get("Name"),
                "arn": original_dataset.get("Arn"),
                "import_mode": original_dataset.get("ImportMode"),
                "physical_table_rewrites": len(changes),
                "changes": changes,
                "status": "NO_CHANGE",
            }

            if changes:
                changed_datasets += 1
                changed_physical_tables += len(changes)
                dataset_row["status"] = "PLANNED"
                logger.log(f"  Planned rewrites: {len(changes)}")
                for change in changes:
                    logger.log(
                        "    "
                        f"{change['physical_table_id']}"
                        f" [{change['source_type']}]"
                        f" ({change['source_name'] or 'unnamed source'})"
                        " -> target data source ARN"
                    )

                if args.apply:
                    payload = build_update_payload(account_id, updated_dataset)
                    qs_client.update_data_set(**payload)
                    applied_datasets += 1
                    dataset_row["status"] = "APPLIED"
                    logger.log("  Update applied.")
                else:
                    logger.log("  Dry run only (use --apply to persist).")
            else:
                logger.log("  No matching PhysicalTableMap DataSourceArn entries found in this dataset.")

            plan["datasets"].append(dataset_row)

        except Exception as exc:
            failed_datasets += 1
            logger.log(f"  Failed: {exc}")
            plan["datasets"].append(
                {
                    "data_set_id": dataset_id,
                    "status": "FAILED",
                    "error": str(exc),
                }
            )
            if not args.continue_on_error:
                break

        logger.log("")

    plan["summary"] = {
        "requested_datasets": len(dataset_ids),
        "changed_datasets": changed_datasets,
        "changed_physical_tables": changed_physical_tables,
        "applied_datasets": applied_datasets,
        "failed_datasets": failed_datasets,
    }

    with open(json_log_path, "w", encoding="utf-8") as handle:
        json.dump(plan, handle, indent=2, default=json_default)

    logger.log("Summary")
    logger.log(f"  Requested datasets: {len(dataset_ids)}")
    logger.log(f"  Changed datasets: {changed_datasets}")
    logger.log(f"  Changed physical tables: {changed_physical_tables}")
    logger.log(f"  Applied datasets: {applied_datasets}")
    logger.log(f"  Failed datasets: {failed_datasets}")
    logger.log(f"Text log: {text_log_path}")
    logger.log(f"JSON plan/result: {json_log_path}")


if __name__ == "__main__":
    main()
