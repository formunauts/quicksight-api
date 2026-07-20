import argparse
import json
from typing import Any, Dict, List, Optional, Set

from boto3.session import Session
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


AUTH_ERROR_CODES = {
    "UnrecognizedClientException",
    "InvalidClientTokenId",
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidSignatureException",
}


def is_auth_error(exc: Exception) -> bool:
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code", "")
    return code in AUTH_ERROR_CODES


def is_credentials_error(exc: Exception) -> bool:
    return isinstance(exc, (NoCredentialsError, PartialCredentialsError))


def get_target_regions(all_regions: bool) -> List[str]:
    if not all_regions:
        return [QS_REGION]

    session = Session()
    qs_supported = set(session.get_available_regions("quicksight"))
    if not qs_supported:
        return [QS_REGION]

    try:
        ec2 = session.client("ec2", region_name="us-east-1")
        response = ec2.describe_regions(AllRegions=True)
        active = {
            row.get("RegionName")
            for row in response.get("Regions", [])
            if row.get("OptInStatus") in {"opt-in-not-required", "opted-in"}
        }
        selected = sorted(region for region in qs_supported if region in active)
        selected = selected or sorted(qs_supported)
        if QS_REGION in selected:
            selected = [QS_REGION] + [region for region in selected if region != QS_REGION]
        return selected
    except Exception:
        selected = sorted(qs_supported)
        if QS_REGION in selected:
            selected = [QS_REGION] + [region for region in selected if region != QS_REGION]
        return selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect PhysicalTableMap and LogicalTableMap for datasets in QuickSight."
        )
    )
    parser.add_argument(
        "--dataset-ids",
        nargs="+",
        help="Optional exact dataset IDs to inspect.",
    )
    parser.add_argument(
        "--dataset-name-contains",
        help="Optional case-insensitive dataset name filter before deep scan.",
    )
    parser.add_argument(
        "--physical-table-ids",
        nargs="+",
        help="Optional exact PhysicalTableId values to match.",
    )
    parser.add_argument(
        "--logical-table-ids",
        nargs="+",
        help="Optional exact LogicalTableId values to match.",
    )
    parser.add_argument(
        "--table-id-contains",
        help="Optional case-insensitive substring filter applied to both physical and logical table IDs.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional limit on datasets to inspect after filters.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue scanning after describe errors. Default behavior is fail-fast.",
    )
    parser.add_argument(
        "--all-regions",
        action="store_true",
        help="Run the scan in every account-active AWS region that supports QuickSight.",
    )
    return parser.parse_args()


def _truncate(value: str, limit: int = 200) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def extract_physical_entries(dataset: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    physical_map = dataset.get("PhysicalTableMap", {})
    if not isinstance(physical_map, dict):
        return entries

    for physical_table_id, table in physical_map.items():
        if not isinstance(table, dict):
            continue

        base = {"physical_table_id": physical_table_id}

        relational = table.get("RelationalTable")
        if isinstance(relational, dict):
            entries.append(
                {
                    **base,
                    "source_type": "RelationalTable",
                    "name": relational.get("Name"),
                    "schema": relational.get("Schema"),
                    "catalog": relational.get("Catalog"),
                    "data_source_arn": relational.get("DataSourceArn"),
                    "input_columns": len(relational.get("InputColumns", [])) if isinstance(relational.get("InputColumns"), list) else None,
                }
            )

        custom_sql = table.get("CustomSql")
        if isinstance(custom_sql, dict):
            sql_query = custom_sql.get("SqlQuery")
            entries.append(
                {
                    **base,
                    "source_type": "CustomSql",
                    "name": custom_sql.get("Name"),
                    "data_source_arn": custom_sql.get("DataSourceArn"),
                    "sql_query_preview": _truncate(sql_query) if isinstance(sql_query, str) else None,
                    "input_columns": len(custom_sql.get("Columns", [])) if isinstance(custom_sql.get("Columns"), list) else None,
                }
            )

        s3_source = table.get("S3Source")
        if isinstance(s3_source, dict):
            upload = s3_source.get("UploadSettings")
            entries.append(
                {
                    **base,
                    "source_type": "S3Source",
                    "data_source_arn": s3_source.get("DataSourceArn"),
                    "input_columns": len(s3_source.get("InputColumns", [])) if isinstance(s3_source.get("InputColumns"), list) else None,
                    "upload_settings": upload if isinstance(upload, dict) else None,
                }
            )

    return entries


def extract_logical_entries(dataset: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    logical_map = dataset.get("LogicalTableMap", {})
    if not isinstance(logical_map, dict):
        return entries

    for logical_table_id, table in logical_map.items():
        if not isinstance(table, dict):
            continue

        source = table.get("Source", {})
        source_kind = "Unknown"
        source_ref: Optional[str] = None
        join_type: Optional[str] = None

        if isinstance(source, dict):
            if source.get("PhysicalTableId"):
                source_kind = "PhysicalTableId"
                source_ref = source.get("PhysicalTableId")
            elif source.get("DataSetArn"):
                source_kind = "DataSetArn"
                source_ref = source.get("DataSetArn")
            elif isinstance(source.get("JoinInstruction"), dict):
                source_kind = "JoinInstruction"
                join_instruction = source.get("JoinInstruction", {})
                left = join_instruction.get("LeftOperand")
                right = join_instruction.get("RightOperand")
                join_type = join_instruction.get("Type")
                source_ref = f"{left} <> {right}"

        transforms = table.get("DataTransforms", [])
        entries.append(
            {
                "logical_table_id": logical_table_id,
                "alias": table.get("Alias"),
                "source_kind": source_kind,
                "source_ref": source_ref,
                "join_type": join_type,
                "data_transforms": len(transforms) if isinstance(transforms, list) else None,
            }
        )

    return entries


def entry_matches(
    physical_entries: List[Dict[str, Any]],
    logical_entries: List[Dict[str, Any]],
    physical_ids: Optional[Set[str]],
    logical_ids: Optional[Set[str]],
    table_id_contains: Optional[str],
) -> bool:
    if not physical_ids and not logical_ids and not table_id_contains:
        return True

    contains = table_id_contains.lower() if table_id_contains else None
    physical_hits = [entry for entry in physical_entries if (entry.get("physical_table_id") in physical_ids if physical_ids else True)]
    logical_hits = [entry for entry in logical_entries if (entry.get("logical_table_id") in logical_ids if logical_ids else True)]

    if contains:
        physical_hits = [entry for entry in physical_hits if contains in str(entry.get("physical_table_id", "")).lower()]
        logical_hits = [entry for entry in logical_hits if contains in str(entry.get("logical_table_id", "")).lower()]

    return bool(physical_hits or logical_hits)


def filter_entries(
    physical_entries: List[Dict[str, Any]],
    logical_entries: List[Dict[str, Any]],
    physical_ids: Optional[Set[str]],
    logical_ids: Optional[Set[str]],
    table_id_contains: Optional[str],
) -> Dict[str, List[Dict[str, Any]]]:
    contains = table_id_contains.lower() if table_id_contains else None

    filtered_physical = physical_entries
    filtered_logical = logical_entries

    if physical_ids:
        filtered_physical = [entry for entry in filtered_physical if entry.get("physical_table_id") in physical_ids]
    if logical_ids:
        filtered_logical = [entry for entry in filtered_logical if entry.get("logical_table_id") in logical_ids]

    if contains:
        filtered_physical = [
            entry for entry in filtered_physical if contains in str(entry.get("physical_table_id", "")).lower()
        ]
        filtered_logical = [
            entry for entry in filtered_logical if contains in str(entry.get("logical_table_id", "")).lower()
        ]

    return {"physical": filtered_physical, "logical": filtered_logical}


def main() -> None:
    args = parse_args()
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    text_log_path = build_log_path("dataset_table_map_audit", "txt")
    json_log_path = build_log_path("dataset_table_map_audit", "json")
    logger = Logger(text_log_path, "QUICKSIGHT DATASET TABLE MAP AUDIT")

    try:
        target_regions = get_target_regions(args.all_regions)

        dataset_ids = set(args.dataset_ids or [])
        physical_ids = set(args.physical_table_ids or [])
        logical_ids = set(args.logical_table_ids or [])

        logger.log(f"Connected to QuickSight account: {QS_ACCOUNT_ID}")
        logger.log(f"Regions selected: {target_regions}")

        rows: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        total_datasets_scanned = 0
        region_statuses: List[Dict[str, str]] = []

        for region in target_regions:
            logger.log("")
            logger.log("=" * 80)
            logger.log(f"REGION: {region}")
            logger.log("=" * 80)

            try:
                qs_client = create_quicksight_client(region=region)
                summaries = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
                if dataset_ids:
                    summaries = [summary for summary in summaries if summary.get("DataSetId") in dataset_ids]
                if args.dataset_name_contains:
                    needle = args.dataset_name_contains.lower()
                    summaries = [summary for summary in summaries if needle in summary.get("Name", "").lower()]
                if args.limit:
                    summaries = summaries[: args.limit]

                logger.log(f"Datasets selected for scan: {len(summaries)}")
                total_datasets_scanned += len(summaries)

                for index, summary in enumerate(summaries, start=1):
                    if index == 1 or index % 25 == 0:
                        logger.log(f"Progress: inspected {index}/{len(summaries)} datasets")

                    dataset_id = summary.get("DataSetId", "")
                    dataset_name = summary.get("Name", "")

                    try:
                        response = qs_client.describe_data_set(
                            AwsAccountId=QS_ACCOUNT_ID,
                            DataSetId=dataset_id,
                        )
                    except Exception as exc:
                        error_row = {
                            "region": region,
                            "dataset_id": dataset_id,
                            "dataset_name": dataset_name,
                            "stage": "describe_data_set",
                            "error": str(exc),
                        }
                        errors.append(error_row)
                        if not args.continue_on_error:
                            raise RuntimeError(
                                f"Failed on dataset {dataset_name} ({dataset_id}): {exc}"
                            )
                        continue

                    dataset = response.get("DataSet", {})
                    physical_entries = extract_physical_entries(dataset)
                    logical_entries = extract_logical_entries(dataset)

                    if not entry_matches(
                        physical_entries,
                        logical_entries,
                        physical_ids if physical_ids else None,
                        logical_ids if logical_ids else None,
                        args.table_id_contains,
                    ):
                        continue

                    filtered = filter_entries(
                        physical_entries,
                        logical_entries,
                        physical_ids if physical_ids else None,
                        logical_ids if logical_ids else None,
                        args.table_id_contains,
                    )

                    row = {
                        "region": region,
                        "dataset_id": dataset.get("DataSetId", dataset_id),
                        "dataset_name": dataset.get("Name", dataset_name),
                        "dataset_arn": dataset.get("Arn"),
                        "import_mode": dataset.get("ImportMode"),
                        "physical_table_count": len(filtered["physical"]),
                        "logical_table_count": len(filtered["logical"]),
                        "physical_tables": filtered["physical"],
                        "logical_tables": filtered["logical"],
                    }
                    rows.append(row)

                region_statuses.append({"region": region, "status": "ok"})
            except Exception as region_exc:
                errors.append(
                    {
                        "region": region,
                        "dataset_id": "",
                        "dataset_name": "",
                        "stage": "region_scan",
                        "error": str(region_exc),
                    }
                )
                region_statuses.append({"region": region, "status": "error", "error": str(region_exc)})
                logger.log(f"REGION ERROR: {region_exc}")
                if is_credentials_error(region_exc):
                    raise RuntimeError(
                        "AWS credentials are missing or incomplete. "
                        "Refresh your AWS credentials and retry."
                    ) from region_exc
                if is_auth_error(region_exc):
                    if args.all_regions:
                        logger.log(f"Skipping region due to auth token rejection: {region}")
                        continue
                    raise RuntimeError(
                        "Authentication token was rejected while scanning regions. "
                        "Refresh your AWS credentials and retry."
                    ) from region_exc
                if not args.all_regions:
                    raise

        payload = {
            "account_id": QS_ACCOUNT_ID,
            "regions_selected": target_regions,
            "datasets_scanned": total_datasets_scanned,
            "datasets_with_matches": len(rows),
            "dataset_rows": rows,
            "errors": errors,
            "region_statuses": region_statuses,
            "summary": {
                "datasets_scanned": total_datasets_scanned,
                "datasets_with_matches": len(rows),
                "errors": len(errors),
            },
        }

        with open(json_log_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)

        logger.log("")
        logger.log("SUMMARY")
        logger.log(f"Datasets scanned: {total_datasets_scanned}")
        logger.log(f"Datasets with matches: {len(rows)}")
        logger.log(f"Errors: {len(errors)}")
        logger.log(f"Text report: {text_log_path}")
        logger.log(f"JSON report: {json_log_path}")
    except Exception as exc:
        logger.log(f"FATAL ERROR: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()