import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Set

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
            "Find datasets that use one or more QuickSight data sources, and analyses that consume those datasets."
        )
    )
    parser.add_argument("--data-source-id", help="QuickSight data source id to match.")
    parser.add_argument("--data-source-name", help="Exact QuickSight data source name to match.")
    parser.add_argument(
        "--data-source-name-contains",
        help="Case-insensitive contains match for QuickSight data source name.",
    )
    parser.add_argument(
        "--skip-analyses",
        action="store_true",
        help="Skip analysis consumer lookup and only list matching datasets.",
    )
    parser.add_argument(
        "--dataset-name-contains",
        help="Optional case-insensitive dataset name filter before deep scan.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional limit on dataset count to inspect after filters.",
    )
    args = parser.parse_args()

    if not any([args.data_source_id, args.data_source_name, args.data_source_name_contains]):
        raise SystemExit(
            "Provide one of --data-source-id, --data-source-name, or --data-source-name-contains."
        )
    return args


def select_target_sources(sources: List[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []

    for source in sources:
        source_id = source.get("DataSourceId", "")
        source_name = source.get("Name", "")

        if args.data_source_id and source_id != args.data_source_id:
            continue
        if args.data_source_name and source_name != args.data_source_name:
            continue
        if args.data_source_name_contains and args.data_source_name_contains.lower() not in source_name.lower():
            continue

        selected.append(source)

    return selected


def extract_data_source_arns_from_dataset(dataset: Dict[str, Any]) -> Set[str]:
    arns: Set[str] = set()
    physical_map = dataset.get("PhysicalTableMap", {})
    if not isinstance(physical_map, dict):
        return arns

    for table in physical_map.values():
        if not isinstance(table, dict):
            continue

        for entry in ("RelationalTable", "CustomSql", "S3Source"):
            block = table.get(entry)
            if not isinstance(block, dict):
                continue
            arn = block.get("DataSourceArn")
            if isinstance(arn, str) and arn:
                arns.add(arn)

    return arns


def extract_dataset_arns_from_analysis_definition(definition: Dict[str, Any]) -> Set[str]:
    arns: Set[str] = set()
    declarations = definition.get("DataSetIdentifierDeclarations", [])
    if not isinstance(declarations, list):
        return arns

    for declaration in declarations:
        if not isinstance(declaration, dict):
            continue
        arn = declaration.get("DataSetArn")
        if isinstance(arn, str) and arn:
            arns.add(arn)

    return arns


def scan_datasets(
    qs_client,
    logger: Logger,
    target_source_arns: Set[str],
    dataset_name_contains: Optional[str],
    limit: Optional[int],
) -> Dict[str, Any]:
    dataset_summaries = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")

    if dataset_name_contains:
        needle = dataset_name_contains.lower()
        dataset_summaries = [item for item in dataset_summaries if needle in item.get("Name", "").lower()]

    if limit:
        dataset_summaries = dataset_summaries[:limit]

    logger.log(f"Datasets selected for scan: {len(dataset_summaries)}")

    matched_datasets: List[Dict[str, Any]] = []
    inspection_errors: List[Dict[str, str]] = []

    for index, summary in enumerate(dataset_summaries, start=1):
        if index == 1 or index % 25 == 0:
            logger.log(f"Progress: inspected {index}/{len(dataset_summaries)} datasets")

        dataset_id = summary.get("DataSetId", "")
        dataset_name = summary.get("Name", "")

        try:
            response = qs_client.describe_data_set(
                AwsAccountId=QS_ACCOUNT_ID,
                DataSetId=dataset_id,
            )
        except Exception as exc:
            inspection_errors.append(
                {
                    "data_set_id": dataset_id,
                    "name": dataset_name,
                    "error": str(exc),
                }
            )
            continue

        dataset = response.get("DataSet", {})
        source_arns = extract_data_source_arns_from_dataset(dataset)
        matches = sorted(target_source_arns.intersection(source_arns))
        if not matches:
            continue

        matched_row = {
            "data_set_id": dataset.get("DataSetId", dataset_id),
            "name": dataset.get("Name", dataset_name),
            "arn": dataset.get("Arn"),
            "import_mode": dataset.get("ImportMode"),
            "matched_data_source_arns": matches,
            "analyses": [],
        }
        matched_datasets.append(matched_row)

        logger.log("")
        logger.log(f"DATASET: {matched_row['name']} ({matched_row['data_set_id']})")
        logger.log(f"  ARN: {matched_row.get('arn')}")
        logger.log(f"  Import mode: {matched_row.get('import_mode')}")
        logger.log(f"  Matched data sources: {', '.join(matches)}")

    return {
        "datasets_scanned": len(dataset_summaries),
        "matched_datasets": matched_datasets,
        "dataset_errors": inspection_errors,
    }


def scan_analyses_for_datasets(qs_client, logger: Logger, matched_dataset_arns: Set[str]) -> Dict[str, Any]:
    analysis_summaries = get_all_summaries(qs_client.list_analyses, QS_ACCOUNT_ID, "AnalysisSummaryList")
    logger.log("")
    logger.log(f"Analyses selected for scan: {len(analysis_summaries)}")

    analyses_by_dataset_arn: Dict[str, List[Dict[str, Any]]] = {arn: [] for arn in matched_dataset_arns}
    analysis_errors: List[Dict[str, str]] = []

    for index, summary in enumerate(analysis_summaries, start=1):
        if index == 1 or index % 25 == 0:
            logger.log(f"Progress: inspected {index}/{len(analysis_summaries)} analyses")

        analysis_id = summary.get("AnalysisId", "")
        analysis_name = summary.get("Name", "")

        try:
            response = qs_client.describe_analysis_definition(
                AwsAccountId=QS_ACCOUNT_ID,
                AnalysisId=analysis_id,
            )
        except Exception as exc:
            analysis_errors.append(
                {
                    "analysis_id": analysis_id,
                    "name": analysis_name,
                    "error": str(exc),
                }
            )
            continue

        dataset_arns = extract_dataset_arns_from_analysis_definition(response.get("Definition", {}))
        for dataset_arn in sorted(dataset_arns.intersection(matched_dataset_arns)):
            analyses_by_dataset_arn[dataset_arn].append(
                {
                    "analysis_id": analysis_id,
                    "name": analysis_name,
                    "status": summary.get("Status"),
                }
            )

    return {
        "analyses_scanned": len(analysis_summaries),
        "analyses_by_dataset_arn": analyses_by_dataset_arn,
        "analysis_errors": analysis_errors,
    }


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    args = parse_args()

    text_report_path = build_log_path("datasource_dependency_audit", "txt")
    json_report_path = build_log_path("datasource_dependency_audit", "json")
    logger = Logger(text_report_path, "QUICKSIGHT DATA SOURCE DEPENDENCY AUDIT")

    qs_client = create_quicksight_client()
    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
    logger.log("Mode: READ ONLY")
    logger.log(f"Command: {' '.join(sys.argv)}")
    logger.log(f"Text report: {text_report_path}")
    logger.log(f"JSON report: {json_report_path}")
    logger.log("")

    all_sources = get_all_summaries(qs_client.list_data_sources, QS_ACCOUNT_ID, "DataSources")
    target_sources = select_target_sources(all_sources, args)

    if not target_sources:
        raise SystemExit("No data sources matched your filters.")

    logger.log("Matched data source(s):")
    for source in target_sources:
        logger.log(
            f"- {source.get('Name')} ({source.get('DataSourceId')}) [{source.get('Type', 'UNKNOWN')}] ARN={source.get('Arn')}"
        )

    target_source_arns = {
        source.get("Arn")
        for source in target_sources
        if isinstance(source.get("Arn"), str) and source.get("Arn")
    }

    dataset_scan = scan_datasets(
        qs_client,
        logger,
        target_source_arns,
        dataset_name_contains=args.dataset_name_contains,
        limit=args.limit,
    )

    matched_dataset_arns = {
        row.get("arn")
        for row in dataset_scan["matched_datasets"]
        if isinstance(row.get("arn"), str) and row.get("arn")
    }

    analysis_scan: Dict[str, Any] = {
        "analyses_scanned": 0,
        "analyses_by_dataset_arn": {},
        "analysis_errors": [],
    }
    if matched_dataset_arns and not args.skip_analyses:
        analysis_scan = scan_analyses_for_datasets(qs_client, logger, matched_dataset_arns)
        analyses_by_dataset_arn = analysis_scan["analyses_by_dataset_arn"]

        for row in dataset_scan["matched_datasets"]:
            dataset_arn = row.get("arn")
            row["analyses"] = analyses_by_dataset_arn.get(dataset_arn, [])

            logger.log("")
            logger.log(f"ANALYSES USING DATASET: {row['name']} ({row['data_set_id']})")
            if not row["analyses"]:
                logger.log("  None found.")
                continue
            for analysis in row["analyses"]:
                logger.log(
                    f"  - {analysis.get('name')} ({analysis.get('analysis_id')}) status={analysis.get('status')}"
                )

    unique_analyses = {
        item["analysis_id"]
        for row in dataset_scan["matched_datasets"]
        for item in row.get("analyses", [])
        if item.get("analysis_id")
    }

    payload = {
        "account_id": QS_ACCOUNT_ID,
        "region": QS_REGION,
        "filters": {
            "data_source_id": args.data_source_id,
            "data_source_name": args.data_source_name,
            "data_source_name_contains": args.data_source_name_contains,
            "dataset_name_contains": args.dataset_name_contains,
            "limit": args.limit,
            "skip_analyses": args.skip_analyses,
        },
        "matched_data_sources": [
            {
                "data_source_id": source.get("DataSourceId"),
                "name": source.get("Name"),
                "arn": source.get("Arn"),
                "type": source.get("Type"),
                "status": source.get("Status"),
            }
            for source in target_sources
        ],
        "summary": {
            "datasets_scanned": dataset_scan["datasets_scanned"],
            "datasets_matched": len(dataset_scan["matched_datasets"]),
            "analyses_scanned": analysis_scan["analyses_scanned"],
            "analyses_matched_unique": len(unique_analyses),
            "dataset_errors": len(dataset_scan["dataset_errors"]),
            "analysis_errors": len(analysis_scan["analysis_errors"]),
        },
        "datasets": dataset_scan["matched_datasets"],
        "dataset_errors": dataset_scan["dataset_errors"],
        "analysis_errors": analysis_scan["analysis_errors"],
    }

    with open(json_report_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)

    logger.log("")
    logger.log("SUMMARY")
    logger.log(f"Matched data sources: {len(target_sources)}")
    logger.log(f"Datasets scanned: {payload['summary']['datasets_scanned']}")
    logger.log(f"Datasets matched: {payload['summary']['datasets_matched']}")
    logger.log(f"Analyses scanned: {payload['summary']['analyses_scanned']}")
    logger.log(f"Unique analyses matched: {payload['summary']['analyses_matched_unique']}")
    logger.log(f"Dataset inspection errors: {payload['summary']['dataset_errors']}")
    logger.log(f"Analysis inspection errors: {payload['summary']['analysis_errors']}")
    logger.log("")
    logger.log(f"JSON report written to: {json_report_path}")
    logger.log(f"Text report written to: {text_report_path}")


if __name__ == "__main__":
    main()
