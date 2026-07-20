import argparse
import sys
from boto3.session import Session
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError
from typing import Any, Dict, Iterable, List, Optional

from qs_common import (
    LOG_DIR,
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


DEFAULT_DATASETS = [
    "Marketplace_Dach_Billing_AT_DE_ONLY_GAAT/DE",
    "Marketplace_Dach_Billing_AT_DE_NOT_GA_ATDE",
    "Marketplace_Dach_Billing_ONLY_GA_CH",
    "Marketplace_Dach_Billing_CH_NOT_GACH",
]
DEFAULT_ENTITY_NAME = "Quality One-Pager"
LOOKUP_TYPE_CONFIGS = {
    "dataset": {
        "func_name": "list_data_sets",
        "key_name": "DataSetSummaries",
        "id_key": "DataSetId",
        "extra_keys": ["ImportMode"],
    },
    "analysis": {
        "func_name": "list_analyses",
        "key_name": "AnalysisSummaryList",
        "id_key": "AnalysisId",
        "extra_keys": ["Status"],
    },
    "dashboard": {
        "func_name": "list_dashboards",
        "key_name": "DashboardSummaryList",
        "id_key": "DashboardId",
        "extra_keys": ["PublishedVersionNumber"],
    },
    "datasource": {
        "func_name": "list_data_sources",
        "key_name": "DataSources",
        "id_key": "DataSourceId",
        "extra_keys": ["Type"],
    },
    "folder": {
        "func_name": "list_folders",
        "key_name": "FolderSummaryList",
        "id_key": "FolderId",
        "extra_keys": ["FolderType"],
    },
    "template": {
        "func_name": "list_templates",
        "key_name": "TemplateSummaryList",
        "id_key": "TemplateId",
        "extra_keys": ["VersionNumber"],
    },
    "theme": {
        "func_name": "list_themes",
        "key_name": "ThemeSummaryList",
        "id_key": "ThemeId",
        "extra_keys": ["VersionNumber"],
    },
}


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


def select_datasets(
    all_datasets: List[Dict[str, str]],
    target_ids: Optional[Iterable[str]] = None,
    target_names: Optional[Iterable[str]] = None,
    name_contains: Optional[str] = None,
) -> List[Dict[str, str]]:
    selected = []
    exact_ids = set(target_ids or [])
    exact_names = set(target_names or [])
    substring = name_contains.lower() if name_contains else None

    for dataset in all_datasets:
        dataset_id = dataset["DataSetId"]
        name = dataset["Name"]
        exact_id_match = dataset_id in exact_ids if exact_ids else True
        exact_match = name in exact_names if exact_names else True
        substring_match = substring in name.lower() if substring else True
        if exact_id_match and exact_match and substring_match:
            selected.append(dataset)

    return selected


def extract_calculated_columns(dataset_details: Dict[str, object]) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []
    logical_map = dataset_details.get("DataSet", {}).get("LogicalTableMap", {})
    for value in logical_map.values():
        for transform in value.get("DataTransforms", []):
            operation = transform.get("CreateColumnsOperation")
            if not operation:
                continue
            for column in operation.get("Columns", []):
                matches.append(
                    {
                        "column_name": column.get("ColumnName", ""),
                        "expression": column.get("Expression", ""),
                    }
                )
    return matches


def search_datasets(
    qs_client,
    logger: Logger,
    target_ids: Optional[List[str]] = None,
    target_names: Optional[List[str]] = None,
    name_contains: Optional[str] = None,
    show_calc_fields: bool = False,
) -> None:
    logger.log("-" * 40)
    logger.log("DATASET SEARCH")
    if target_ids:
        logger.log(f"Looking for exact dataset IDs: {target_ids}")
    if target_names:
        logger.log(f"Looking for exact dataset names: {target_names}")
    if name_contains:
        logger.log(f"Looking for dataset names containing: '{name_contains}'")
    logger.log("-" * 40)

    all_datasets = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
    logger.log(f"Datasets discovered: {len(all_datasets)}")

    selected_datasets = select_datasets(
        all_datasets,
        target_ids=target_ids,
        target_names=target_names,
        name_contains=name_contains,
    )
    exact_id_matches = {dataset["DataSetId"] for dataset in selected_datasets}
    exact_name_matches = {dataset["Name"] for dataset in selected_datasets}

    if target_ids:
        missing_dataset_ids = sorted(set(target_ids) - exact_id_matches)
        for missing_id in missing_dataset_ids:
            logger.log(f"Dataset ID not found: {missing_id}")

    if target_names:
        missing_datasets = sorted(set(target_names) - exact_name_matches)
        for missing_name in missing_datasets:
            logger.log(f"Dataset not found: {missing_name}")

    if not selected_datasets:
        logger.log("No matching datasets found.")
        return

    for dataset in selected_datasets:
        logger.log(f"FOUND: {dataset['Name']} (ID: {dataset['DataSetId']})")

    if not show_calc_fields:
        return

    logger.log("")
    logger.log("-" * 40)
    logger.log("EXTRACTING CALCULATED FIELDS")
    logger.log("-" * 40)
    for dataset in selected_datasets:
        try:
            details = qs_client.describe_data_set(
                AwsAccountId=QS_ACCOUNT_ID,
                DataSetId=dataset["DataSetId"],
            )
        except Exception as exc:
            logger.log(f"Error describing dataset '{dataset['Name']}': {exc}")
            continue

        logger.log("")
        logger.log(f"DATASET: {dataset['Name']}")
        calculated_columns = extract_calculated_columns(details)
        if not calculated_columns:
            logger.log("(No calculated fields)")
            continue
        for column in calculated_columns:
            logger.log(f"- {column['column_name']}")
            logger.log(f"  = {column['expression']}")


def search_calculated_fields_by_name(
    qs_client,
    logger: Logger,
    field_name_contains: str,
    dataset_ids: Optional[List[str]] = None,
    dataset_names: Optional[List[str]] = None,
    dataset_name_contains: Optional[str] = None,
) -> None:
    logger.log("-" * 40)
    logger.log("CALCULATED FIELD SEARCH")
    logger.log(f"Searching for calculated field names containing: '{field_name_contains}'")
    if dataset_ids:
        logger.log(f"Restricting search to exact dataset IDs: {dataset_ids}")
    if dataset_names:
        logger.log(f"Restricting search to exact dataset names: {dataset_names}")
    if dataset_name_contains:
        logger.log(f"Restricting search to dataset names containing: '{dataset_name_contains}'")
    if not dataset_ids and not dataset_names and not dataset_name_contains:
        logger.log("Scanning all datasets")
    logger.log("-" * 40)

    all_datasets = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
    datasets_to_scan = select_datasets(
        all_datasets,
        target_ids=dataset_ids,
        target_names=dataset_names,
        name_contains=dataset_name_contains,
    )

    if dataset_ids:
        matched_exact_ids = {dataset["DataSetId"] for dataset in datasets_to_scan}
        missing_ids = sorted(set(dataset_ids) - matched_exact_ids)
        for missing_id in missing_ids:
            logger.log(f"Dataset ID not found: {missing_id}")

    if dataset_names:
        matched_exact_names = {dataset["Name"] for dataset in datasets_to_scan}
        missing_datasets = sorted(set(dataset_names) - matched_exact_names)
        for missing_name in missing_datasets:
            logger.log(f"Dataset not found: {missing_name}")

    logger.log(f"Datasets to scan: {len(datasets_to_scan)}")
    search_term = field_name_contains.lower()
    found = False

    for dataset in datasets_to_scan:
        try:
            details = qs_client.describe_data_set(
                AwsAccountId=QS_ACCOUNT_ID,
                DataSetId=dataset["DataSetId"],
            )
        except Exception as exc:
            logger.log(f"Error describing dataset '{dataset['Name']}': {exc}")
            continue

        matches = [
            column
            for column in extract_calculated_columns(details)
            if search_term in column["column_name"].lower()
        ]
        if not matches:
            continue

        found = True
        logger.log("")
        logger.log(f"DATASET: {dataset['Name']} (ID: {dataset['DataSetId']})")
        for match in matches:
            logger.log(f"- {match['column_name']}")
            logger.log(f"  = {match['expression']}")

    if not found:
        logger.log(f"No calculated fields found matching '{field_name_contains}'")


def search_assets(qs_client, logger: Logger, search_term: str, label: str, func, key_name: str, id_key: str, extra_key: Optional[str] = None) -> None:
    logger.log("-" * 40)
    logger.log(f"{label.upper()} SEARCH")
    logger.log(f"Searching for name containing: '{search_term}'")
    logger.log("-" * 40)

    items = get_all_summaries(func, QS_ACCOUNT_ID, key_name)
    found = False
    for item in items:
        if search_term.lower() not in item["Name"].lower():
            continue
        logger.log(f"FOUND: '{item['Name']}'")
        logger.log(f"ID: {item[id_key]}")
        if extra_key:
            logger.log(f"{extra_key}: {item.get(extra_key, 'N/A')}")
        found = True

    if not found:
        logger.log(f"No {label.lower()}s found matching '{search_term}'")


def search_assets_by_filters(
    qs_client,
    logger: Logger,
    label: str,
    func,
    key_name: str,
    id_key: str,
    search_term: Optional[str] = None,
    target_ids: Optional[List[str]] = None,
    extra_key: Optional[str] = None,
) -> None:
    logger.log("-" * 40)
    logger.log(f"{label.upper()} SEARCH")
    if target_ids:
        logger.log(f"Searching for exact {label} IDs: {target_ids}")
    if search_term:
        logger.log(f"Searching for name containing: '{search_term}'")
    if not target_ids and not search_term:
        logger.log("Scanning all items")
    logger.log("-" * 40)

    items = get_all_summaries(func, QS_ACCOUNT_ID, key_name)
    selected = []
    id_filter = set(target_ids or [])

    for item in items:
        item_id = item.get(id_key, "")
        item_name = item.get("Name", "")

        id_match = item_id in id_filter if target_ids else True
        name_match = search_term.lower() in item_name.lower() if search_term else True
        if id_match and name_match:
            selected.append(item)

    if target_ids:
        matched_ids = {item.get(id_key, "") for item in selected}
        missing_ids = sorted(set(target_ids) - matched_ids)
        for missing_id in missing_ids:
            logger.log(f"{label.title()} ID not found: {missing_id}")

    if not selected:
        logger.log(f"No matching {label.lower()}s found.")
        return

    for item in selected:
        logger.log(f"FOUND: '{item.get('Name', '')}'")
        logger.log(f"ID: {item.get(id_key, '')}")
        if extra_key:
            logger.log(f"{extra_key}: {item.get(extra_key, 'N/A')}")


def lookup_objects_by_id(
    qs_client,
    logger: Logger,
    target_ids: List[str],
    target_types: Optional[List[str]] = None,
) -> None:
    if not target_ids:
        return

    selected_types = target_types or list(LOOKUP_TYPE_CONFIGS.keys())
    ids_to_find = list(dict.fromkeys(target_ids))
    matches_by_id: Dict[str, List[Dict[str, Any]]] = {asset_id: [] for asset_id in ids_to_find}

    logger.log("-" * 40)
    logger.log("OBJECT LOOKUP BY ID")
    logger.log(f"Target IDs: {ids_to_find}")
    logger.log(f"Object types: {selected_types}")
    logger.log("-" * 40)

    id_filter = set(ids_to_find)
    for object_type in selected_types:
        config = LOOKUP_TYPE_CONFIGS.get(object_type)
        if not config:
            logger.log(f"Unsupported object type skipped: {object_type}")
            continue

        func = getattr(qs_client, config["func_name"])
        try:
            items = get_all_summaries(func, QS_ACCOUNT_ID, config["key_name"])
        except Exception as exc:
            if is_auth_error(exc) or is_credentials_error(exc):
                raise
            logger.log(f"Could not list {object_type}s: {exc}")
            continue

        for item in items:
            item_id = item.get(config["id_key"])
            if item_id not in id_filter:
                continue

            match: Dict[str, Any] = {
                "type": object_type,
                "name": item.get("Name", ""),
                "id": item_id,
            }
            for key in config["extra_keys"]:
                if key in item:
                    match[key] = item.get(key)
            matches_by_id[item_id].append(match)

    found_any = False
    for asset_id in ids_to_find:
        matches = matches_by_id.get(asset_id, [])
        if not matches:
            logger.log(f"ID not found in selected object types: {asset_id}")
            continue

        found_any = True
        logger.log("")
        logger.log(f"ID: {asset_id}")
        for match in matches:
            logger.log(f"- TYPE: {match.get('type', '')}")
            logger.log(f"  NAME: {match.get('name', '')}")
            for key, value in match.items():
                if key in {"type", "name", "id"}:
                    continue
                logger.log(f"  {key}: {value}")

    if not found_any:
        logger.log("No matching objects found for the requested IDs.")


def main() -> None:
    parser = argparse.ArgumentParser(description="QuickSight audit CLI tool.")
    parser.add_argument("--run-all", action="store_true", help="Run the default dataset, analysis, and dashboard checks.")
    parser.add_argument("--dataset-ids", nargs="+", help="List of exact dataset IDs to search for.")
    parser.add_argument("--datasets", nargs="+", help="List of exact dataset names to search for.")
    parser.add_argument("--dataset-name-contains", help="Search datasets by substring.")
    parser.add_argument("--calc-fields", action="store_true", help="List calculated fields for matching datasets.")
    parser.add_argument("--calc-field-name-contains", help="Search calculated field names across matching datasets.")
    parser.add_argument("--analysis-ids", nargs="+", help="List of exact analysis IDs to search for.")
    parser.add_argument("--analysis", help="Search analyses by name substring.")
    parser.add_argument("--dashboard-ids", nargs="+", help="List of exact dashboard IDs to search for.")
    parser.add_argument("--dashboard", help="Search dashboards by name substring.")
    parser.add_argument("--lookup-ids", nargs="+", help="Lookup object name/type by ID across QuickSight object types.")
    parser.add_argument(
        "--lookup-types",
        nargs="+",
        choices=sorted(LOOKUP_TYPE_CONFIGS.keys()),
        help="Restrict --lookup-ids search to specific object types.",
    )
    parser.add_argument(
        "--all-regions",
        action="store_true",
        help="Run the selected checks in every account-active AWS region that supports QuickSight.",
    )
    args = parser.parse_args()

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)
    log_path = build_log_path("quicksight_audit_report")
    logger = Logger(log_path, "QUICKSIGHT AUDIT REPORT")

    try:
        target_regions = get_target_regions(args.all_regions)

        logger.log(f"Command: {' '.join(sys.argv)}")
        logger.log(f"Log file: {log_path}")
        logger.log(f"Regions selected: {target_regions}")
        logger.log("")

        for region in target_regions:
            logger.log("=" * 80)
            logger.log(f"REGION: {region}")
            logger.log("=" * 80)

            try:
                qs_client = create_quicksight_client(region=region)
                logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {region})")

                if args.run_all:
                    search_datasets(qs_client, logger, DEFAULT_DATASETS, show_calc_fields=True)
                    logger.log("")
                    search_assets(
                        qs_client,
                        logger,
                        DEFAULT_ENTITY_NAME,
                        "analysis",
                        qs_client.list_analyses,
                        "AnalysisSummaryList",
                        "AnalysisId",
                        "Status",
                    )
                    logger.log("")
                    search_assets(
                        qs_client,
                        logger,
                        DEFAULT_ENTITY_NAME,
                        "dashboard",
                        qs_client.list_dashboards,
                        "DashboardSummaryList",
                        "DashboardId",
                        "PublishedVersionNumber",
                    )
                else:
                    if args.calc_field_name_contains:
                        search_calculated_fields_by_name(
                            qs_client,
                            logger,
                            args.calc_field_name_contains,
                            dataset_ids=args.dataset_ids,
                            dataset_names=args.datasets,
                            dataset_name_contains=args.dataset_name_contains,
                        )
                    elif args.dataset_ids or args.datasets or args.dataset_name_contains:
                        search_datasets(
                            qs_client,
                            logger,
                            target_ids=args.dataset_ids,
                            target_names=args.datasets,
                            name_contains=args.dataset_name_contains,
                            show_calc_fields=args.calc_fields,
                        )

                    if args.analysis or args.analysis_ids:
                        logger.log("")
                        search_assets_by_filters(
                            qs_client,
                            logger,
                            "analysis",
                            qs_client.list_analyses,
                            "AnalysisSummaryList",
                            "AnalysisId",
                            search_term=args.analysis,
                            target_ids=args.analysis_ids,
                            extra_key="Status",
                        )

                    if args.dashboard or args.dashboard_ids:
                        logger.log("")
                        search_assets_by_filters(
                            qs_client,
                            logger,
                            "dashboard",
                            qs_client.list_dashboards,
                            "DashboardSummaryList",
                            "DashboardId",
                            search_term=args.dashboard,
                            target_ids=args.dashboard_ids,
                            extra_key="PublishedVersionNumber",
                        )

                    if args.lookup_ids:
                        logger.log("")
                        lookup_objects_by_id(
                            qs_client,
                            logger,
                            target_ids=args.lookup_ids,
                            target_types=args.lookup_types,
                        )

                    if not any(
                        [
                            args.dataset_ids,
                            args.datasets,
                            args.dataset_name_contains,
                            args.analysis,
                            args.analysis_ids,
                            args.dashboard,
                            args.dashboard_ids,
                            args.lookup_ids,
                            args.calc_field_name_contains,
                        ]
                    ):
                        logger.log("No action selected. Use --run-all or specific flags. Use --help for info.")
            except Exception as region_exc:
                logger.log(f"REGION ERROR ({region}): {region_exc}")
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

        logger.log("")
        logger.log(f"DONE. Output saved to {log_path}")
    except Exception as exc:
        logger.log(f"FATAL ERROR: {exc}")


if __name__ == "__main__":
    main()
