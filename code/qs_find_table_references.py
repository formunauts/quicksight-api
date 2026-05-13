import argparse
import datetime
import json
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Pattern, Set

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


IDENTIFIER_CHARS = r"A-Za-z0-9_"


def read_table_file(path: str) -> List[str]:
    tables: List[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            tables.append(value)
    return tables


def normalize_identifier(value: str) -> str:
    parts = [part.strip().strip('"').strip("'").strip("`").strip("[]") for part in value.split(".")]
    return ".".join(part for part in parts if part).lower()


def dedupe_preserving_order(values: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for value in values:
        normalized = normalize_identifier(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def build_identifier_pattern(identifier: str) -> Pattern[str]:
    parts = [re.escape(part) for part in normalize_identifier(identifier).split(".") if part]
    if not parts:
        raise ValueError("Empty table identifier")

    quoted_parts = [rf'(?:"{part}"|`{part}`|\[{part}\]|{part})' for part in parts]
    separator = r"\s*\.\s*"
    expression = separator.join(quoted_parts)
    return re.compile(rf"(?<![{IDENTIFIER_CHARS}]){expression}(?![{IDENTIFIER_CHARS}])", re.IGNORECASE)


def sql_snippet(sql: str, match: re.Match[str], radius: int = 90) -> str:
    start = max(match.start() - radius, 0)
    end = min(match.end() + radius, len(sql))
    snippet = sql[start:end]
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(sql):
        snippet = snippet + "..."
    return snippet


def find_sql_table_matches(sql: str, table_patterns: Dict[str, Pattern[str]]) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []
    for table, pattern in table_patterns.items():
        found = pattern.search(sql)
        if not found:
            continue
        matches.append(
            {
                "table": table,
                "match_type": "custom_sql",
                "snippet": sql_snippet(sql, found),
            }
        )
    return matches


def relational_table_name(physical_table: Dict[str, Any]) -> Optional[str]:
    relational = physical_table.get("RelationalTable")
    if not isinstance(relational, dict):
        return None
    table_name = relational.get("Name")
    if not isinstance(table_name, str) or not table_name:
        return None
    schema = relational.get("Schema")
    catalog = relational.get("Catalog")
    parts = [part for part in [catalog, schema, table_name] if isinstance(part, str) and part]
    return ".".join(parts)


def relational_table_matches(table_name: str, target_tables: List[str]) -> List[Dict[str, str]]:
    normalized_name = normalize_identifier(table_name)
    name_parts = normalized_name.split(".")
    matches: List[Dict[str, str]] = []

    for table in target_tables:
        table_parts = table.split(".")
        exact_match = normalized_name == table
        suffix_match = len(table_parts) < len(name_parts) and name_parts[-len(table_parts) :] == table_parts
        if not exact_match and not suffix_match:
            continue
        matches.append(
            {
                "table": table,
                "match_type": "relational_table",
                "snippet": table_name,
            }
        )
    return matches


def find_dataset_table_references(
    dataset: Dict[str, Any],
    target_tables: List[str],
    table_patterns: Dict[str, Pattern[str]],
) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []
    physical_map = dataset.get("PhysicalTableMap", {})
    if not isinstance(physical_map, dict):
        return matches

    for physical_table_id, physical_table in physical_map.items():
        if not isinstance(physical_table, dict):
            continue

        custom_sql = physical_table.get("CustomSql")
        if isinstance(custom_sql, dict):
            sql_query = custom_sql.get("SqlQuery", "")
            if isinstance(sql_query, str):
                for match in find_sql_table_matches(sql_query, table_patterns):
                    match.update(
                        {
                            "physical_table_id": physical_table_id,
                            "source_name": custom_sql.get("Name", physical_table_id),
                        }
                    )
                    matches.append(match)

        table_name = relational_table_name(physical_table)
        if table_name:
            for match in relational_table_matches(table_name, target_tables):
                match.update(
                    {
                        "physical_table_id": physical_table_id,
                        "source_name": table_name,
                    }
                )
                matches.append(match)

    return matches


def extract_dataset_arns_from_definition(definition: Dict[str, Any]) -> Set[str]:
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


def scan_dataset_consumers(qs_client, matched_arns: Set[str]) -> Dict[str, Any]:
    consumers: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        arn: {"analyses": [], "dashboards": []} for arn in matched_arns
    }
    skipped: Dict[str, List[Dict[str, str]]] = {"analyses": [], "dashboards": []}

    analyses = get_all_summaries(qs_client.list_analyses, QS_ACCOUNT_ID, "AnalysisSummaryList")
    for index, summary in enumerate(analyses, start=1):
        try:
            response = qs_client.describe_analysis_definition(
                AwsAccountId=QS_ACCOUNT_ID,
                AnalysisId=summary["AnalysisId"],
            )
        except Exception as exc:
            skipped["analyses"].append(
                {
                    "analysis_id": summary.get("AnalysisId", ""),
                    "name": summary.get("Name", ""),
                    "error": str(exc),
                }
            )
            continue

        used_arns = extract_dataset_arns_from_definition(response.get("Definition", {}))
        for arn in sorted(matched_arns.intersection(used_arns)):
            consumers[arn]["analyses"].append(
                {
                    "analysis_id": summary.get("AnalysisId"),
                    "name": summary.get("Name"),
                    "status": summary.get("Status"),
                    "index": index,
                }
            )

    dashboards = get_all_summaries(qs_client.list_dashboards, QS_ACCOUNT_ID, "DashboardSummaryList")
    for index, summary in enumerate(dashboards, start=1):
        try:
            response = qs_client.describe_dashboard_definition(
                AwsAccountId=QS_ACCOUNT_ID,
                DashboardId=summary["DashboardId"],
            )
        except Exception as exc:
            skipped["dashboards"].append(
                {
                    "dashboard_id": summary.get("DashboardId", ""),
                    "name": summary.get("Name", ""),
                    "error": str(exc),
                }
            )
            continue

        used_arns = extract_dataset_arns_from_definition(response.get("Definition", {}))
        for arn in sorted(matched_arns.intersection(used_arns)):
            consumers[arn]["dashboards"].append(
                {
                    "dashboard_id": summary.get("DashboardId"),
                    "name": summary.get("Name"),
                    "published_version_number": summary.get("PublishedVersionNumber"),
                    "index": index,
                }
            )

    return {
        "consumers": consumers,
        "skipped": skipped,
        "analyses_scanned": len(analyses),
        "dashboards_scanned": len(dashboards),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find QuickSight datasets whose physical tables or custom SQL reference target database tables."
    )
    parser.add_argument("--tables", nargs="+", help="Table names to search for. Supports unqualified or schema-qualified names.")
    parser.add_argument("--table-file", help="Text file with one table name per line. Blank lines and # comments are ignored.")
    parser.add_argument("--dataset-name-contains", help="Optional case-insensitive dataset name filter before scanning.")
    parser.add_argument("--skip-consumers", action="store_true", help="Skip analysis/dashboard consumer lookup for matched datasets.")
    parser.add_argument("--limit", type=int, help="Optional limit on datasets to inspect after filtering.")
    return parser


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    parser = build_parser()
    args = parser.parse_args()

    requested_tables = list(args.tables or [])
    if args.table_file:
        requested_tables.extend(read_table_file(args.table_file))
    target_tables = dedupe_preserving_order(requested_tables)
    if not target_tables:
        raise SystemExit("Provide at least one table via --tables or --table-file.")

    table_patterns = {table: build_identifier_pattern(table) for table in target_tables}
    text_report_path = build_log_path("table_reference_audit", "txt")
    json_report_path = build_log_path("table_reference_audit", "json")
    logger = Logger(text_report_path, "QUICKSIGHT TABLE REFERENCE AUDIT")

    qs_client = create_quicksight_client()
    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
    logger.log("Mode: READ ONLY")
    logger.log(f"Command: {' '.join(sys.argv)}")
    logger.log(f"Tables: {', '.join(target_tables)}")
    logger.log("")

    datasets = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
    if args.dataset_name_contains:
        needle = args.dataset_name_contains.lower()
        datasets = [dataset for dataset in datasets if needle in dataset.get("Name", "").lower()]
    if args.limit:
        datasets = datasets[: args.limit]

    logger.log(f"Datasets selected for scan: {len(datasets)}")

    matched_datasets: List[Dict[str, Any]] = []
    inspection_errors: List[Dict[str, str]] = []

    for index, summary in enumerate(datasets, start=1):
        if index == 1 or index % 25 == 0:
            logger.log(f"Progress: inspected {index}/{len(datasets)} datasets")

        dataset_id = summary["DataSetId"]
        dataset_name = summary["Name"]
        try:
            response = qs_client.describe_data_set(
                AwsAccountId=QS_ACCOUNT_ID,
                DataSetId=dataset_id,
            )
        except Exception as exc:
            inspection_errors.append(
                {
                    "name": dataset_name,
                    "data_set_id": dataset_id,
                    "error": str(exc),
                }
            )
            continue

        dataset = response.get("DataSet", {})
        matches = find_dataset_table_references(dataset, target_tables, table_patterns)
        if not matches:
            continue

        row = {
            "name": dataset.get("Name", dataset_name),
            "data_set_id": dataset.get("DataSetId", dataset_id),
            "arn": dataset.get("Arn"),
            "import_mode": dataset.get("ImportMode"),
            "matches": matches,
            "consumers": {"analyses": [], "dashboards": []},
        }
        matched_datasets.append(row)

        logger.log("")
        logger.log(f"DATASET: {row['name']} ({row['data_set_id']})")
        logger.log(f"  ARN: {row.get('arn')}")
        logger.log(f"  Import mode: {row.get('import_mode')}")
        for match in matches:
            logger.log(
                f"  {match['table']} in {match['match_type']}: {match['source_name']} [{match['physical_table_id']}]"
            )
            logger.log(f"    {match['snippet']}")

    consumer_scan: Optional[Dict[str, Any]] = None
    matched_arns = {row["arn"] for row in matched_datasets if row.get("arn")}
    if matched_arns and not args.skip_consumers:
        logger.log("")
        logger.log("Scanning analyses and dashboards that consume matched datasets...")
        consumer_scan = scan_dataset_consumers(qs_client, matched_arns)
        consumer_map = consumer_scan["consumers"]
        for row in matched_datasets:
            row["consumers"] = consumer_map.get(row.get("arn"), {"analyses": [], "dashboards": []})

        for row in matched_datasets:
            analyses = row["consumers"]["analyses"]
            dashboards = row["consumers"]["dashboards"]
            logger.log("")
            logger.log(f"CONSUMERS FOR: {row['name']} ({row['data_set_id']})")
            logger.log(f"  Analyses: {len(analyses)}")
            for analysis in analyses:
                logger.log(f"    - {analysis.get('name')} ({analysis.get('analysis_id')})")
            logger.log(f"  Dashboards: {len(dashboards)}")
            for dashboard in dashboards:
                logger.log(f"    - {dashboard.get('name')} ({dashboard.get('dashboard_id')})")

    summary = {
        "datasets_selected": len(datasets),
        "datasets_with_table_references": len(matched_datasets),
        "inspection_errors": len(inspection_errors),
        "consumer_lookup_skipped": args.skip_consumers,
        "analyses_scanned": consumer_scan["analyses_scanned"] if consumer_scan else 0,
        "dashboards_scanned": consumer_scan["dashboards_scanned"] if consumer_scan else 0,
        "consumer_scan_errors": (
            len(consumer_scan["skipped"]["analyses"]) + len(consumer_scan["skipped"]["dashboards"])
            if consumer_scan
            else 0
        ),
    }

    payload = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": QS_REGION,
        "filters": {
            "tables": target_tables,
            "dataset_name_contains": args.dataset_name_contains,
            "limit": args.limit,
            "skip_consumers": args.skip_consumers,
        },
        "summary": summary,
        "datasets": matched_datasets,
        "inspection_errors": inspection_errors,
        "consumer_scan_errors": consumer_scan["skipped"] if consumer_scan else {"analyses": [], "dashboards": []},
    }

    with open(json_report_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)

    logger.log("")
    logger.log(f"Datasets selected: {summary['datasets_selected']}")
    logger.log(f"Datasets with table references: {summary['datasets_with_table_references']}")
    logger.log(f"Inspection errors: {summary['inspection_errors']}")
    if not args.skip_consumers:
        logger.log(f"Analyses scanned: {summary['analyses_scanned']}")
        logger.log(f"Dashboards scanned: {summary['dashboards_scanned']}")
        logger.log(f"Consumer scan errors: {summary['consumer_scan_errors']}")
    logger.log(f"Text report: {text_report_path}")
    logger.log(f"JSON report: {json_report_path}")


if __name__ == "__main__":
    main()
