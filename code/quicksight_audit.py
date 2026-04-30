import argparse
import sys
from typing import Dict, Iterable, List, Optional

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


def select_datasets(
    all_datasets: List[Dict[str, str]],
    target_names: Optional[Iterable[str]] = None,
    name_contains: Optional[str] = None,
) -> List[Dict[str, str]]:
    selected = []
    exact_names = set(target_names or [])
    substring = name_contains.lower() if name_contains else None

    for dataset in all_datasets:
        name = dataset["Name"]
        exact_match = name in exact_names if exact_names else True
        substring_match = substring in name.lower() if substring else True
        if exact_match and substring_match:
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
    target_names: Optional[List[str]] = None,
    name_contains: Optional[str] = None,
    show_calc_fields: bool = False,
) -> None:
    logger.log("-" * 40)
    logger.log("DATASET SEARCH")
    if target_names:
        logger.log(f"Looking for exact dataset names: {target_names}")
    if name_contains:
        logger.log(f"Looking for dataset names containing: '{name_contains}'")
    logger.log("-" * 40)

    all_datasets = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
    logger.log(f"Datasets discovered: {len(all_datasets)}")

    selected_datasets = select_datasets(all_datasets, target_names=target_names, name_contains=name_contains)
    exact_name_matches = {dataset["Name"] for dataset in selected_datasets}

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
    dataset_names: Optional[List[str]] = None,
    dataset_name_contains: Optional[str] = None,
) -> None:
    logger.log("-" * 40)
    logger.log("CALCULATED FIELD SEARCH")
    logger.log(f"Searching for calculated field names containing: '{field_name_contains}'")
    if dataset_names:
        logger.log(f"Restricting search to exact dataset names: {dataset_names}")
    if dataset_name_contains:
        logger.log(f"Restricting search to dataset names containing: '{dataset_name_contains}'")
    if not dataset_names and not dataset_name_contains:
        logger.log("Scanning all datasets")
    logger.log("-" * 40)

    all_datasets = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
    datasets_to_scan = select_datasets(
        all_datasets,
        target_names=dataset_names,
        name_contains=dataset_name_contains,
    )

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


def main() -> None:
    parser = argparse.ArgumentParser(description="QuickSight audit CLI tool.")
    parser.add_argument("--run-all", action="store_true", help="Run the default dataset, analysis, and dashboard checks.")
    parser.add_argument("--datasets", nargs="+", help="List of exact dataset names to search for.")
    parser.add_argument("--dataset-name-contains", help="Search datasets by substring.")
    parser.add_argument("--calc-fields", action="store_true", help="List calculated fields for matching datasets.")
    parser.add_argument("--calc-field-name-contains", help="Search calculated field names across matching datasets.")
    parser.add_argument("--analysis", help="Search analyses by name substring.")
    parser.add_argument("--dashboard", help="Search dashboards by name substring.")
    args = parser.parse_args()

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)
    log_path = build_log_path("quicksight_audit_report")
    logger = Logger(log_path, "QUICKSIGHT AUDIT REPORT")

    try:
        qs_client = create_quicksight_client()
        logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
        logger.log(f"Command: {' '.join(sys.argv)}")
        logger.log(f"Log file: {log_path}")
        logger.log("")

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
                    dataset_names=args.datasets,
                    dataset_name_contains=args.dataset_name_contains,
                )
            elif args.datasets or args.dataset_name_contains:
                search_datasets(
                    qs_client,
                    logger,
                    target_names=args.datasets,
                    name_contains=args.dataset_name_contains,
                    show_calc_fields=args.calc_fields,
                )

            if args.analysis:
                logger.log("")
                search_assets(
                    qs_client,
                    logger,
                    args.analysis,
                    "analysis",
                    qs_client.list_analyses,
                    "AnalysisSummaryList",
                    "AnalysisId",
                    "Status",
                )

            if args.dashboard:
                logger.log("")
                search_assets(
                    qs_client,
                    logger,
                    args.dashboard,
                    "dashboard",
                    qs_client.list_dashboards,
                    "DashboardSummaryList",
                    "DashboardId",
                    "PublishedVersionNumber",
                )

            if not any(
                [
                    args.datasets,
                    args.dataset_name_contains,
                    args.analysis,
                    args.dashboard,
                    args.calc_field_name_contains,
                ]
            ):
                logger.log("No action selected. Use --run-all or specific flags. Use --help for info.")

        logger.log("")
        logger.log(f"DONE. Output saved to {log_path}")
    except Exception as exc:
        logger.log(f"FATAL ERROR: {exc}")


if __name__ == "__main__":
    main()
