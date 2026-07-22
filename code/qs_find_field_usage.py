import argparse
import csv
import datetime
import json
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Pattern, Set, Tuple

from botocore.exceptions import ClientError
from botocore.exceptions import NoCredentialsError, PartialCredentialsError

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
HARD_DEPENDENCY_MATCH_TYPES = {
    "dataset_custom_sql_table",
    "dataset_custom_sql_column",
    "dataset_custom_sql_columns_list",
    "dataset_relational_table",
    "dataset_relational_input_columns",
}


def is_not_found_or_unsupported_error(exc: Exception) -> bool:
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code", "")
    return code in {"ResourceNotFoundException", "UnsupportedUserEditionException"}


def read_values_file(path: str) -> List[str]:
    values: List[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            values.append(value)
    return values


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
        raise ValueError("Empty identifier")

    quoted_parts = [rf'(?:"{part}"|`{part}`|\[{part}\]|{part})' for part in parts]
    expression = r"\s*\.\s*".join(quoted_parts)
    return re.compile(rf"(?<![{IDENTIFIER_CHARS}]){expression}(?![{IDENTIFIER_CHARS}])", re.IGNORECASE)


def sql_snippet(text: str, match: re.Match[str], radius: int = 90) -> str:
    start = max(match.start() - radius, 0)
    end = min(match.end() + radius, len(text))
    snippet = text[start:end]
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def collect_text_token_matches(
    text: str,
    token_patterns: Dict[str, Pattern[str]],
    match_type: str,
) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []
    for token, pattern in token_patterns.items():
        found = pattern.search(text)
        if not found:
            continue
        matches.append(
            {
                "token": token,
                "match_type": match_type,
                "snippet": sql_snippet(text, found),
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
                "token": table,
                "match_type": "dataset_relational_table",
                "snippet": table_name,
            }
        )
    return matches


def input_column_matches(
    columns: Any,
    target_columns: List[str],
    column_patterns: Dict[str, Pattern[str]],
    match_type: str,
) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []
    if not isinstance(columns, list):
        return matches

    for column in columns:
        if not isinstance(column, dict):
            continue
        column_name = column.get("Name")
        if not isinstance(column_name, str) or not column_name:
            continue

        normalized_column_name = normalize_identifier(column_name)
        for target in target_columns:
            target_normalized = normalize_identifier(target)
            target_last = target_normalized.split(".")[-1]
            if normalized_column_name == target_normalized or normalized_column_name == target_last:
                matches.append(
                    {
                        "token": target,
                        "match_type": match_type,
                        "snippet": column_name,
                    }
                )
                continue

            pattern = column_patterns.get(target)
            if not pattern:
                continue
            found = pattern.search(column_name)
            if found:
                matches.append(
                    {
                        "token": target,
                        "match_type": match_type,
                        "snippet": column_name,
                    }
                )

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


def list_refresh_schedules_safe(qs_client, dataset_id: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    try:
        response = qs_client.list_refresh_schedules(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSetId=dataset_id,
        )
        return response.get("RefreshSchedules", []), None
    except Exception as exc:
        if is_not_found_or_unsupported_error(exc):
            return [], None
        return [], str(exc)


def recursive_string_matches(
    obj: Any,
    table_patterns: Dict[str, Pattern[str]],
    column_patterns: Dict[str, Pattern[str]],
    path: str,
    include_tables: bool,
    include_columns: bool,
) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            matches.extend(
                recursive_string_matches(
                    value,
                    table_patterns,
                    column_patterns,
                    path=f"{path}.{key}",
                    include_tables=include_tables,
                    include_columns=include_columns,
                )
            )
        return matches

    if isinstance(obj, list):
        for index, value in enumerate(obj):
            matches.extend(
                recursive_string_matches(
                    value,
                    table_patterns,
                    column_patterns,
                    path=f"{path}[{index}]",
                    include_tables=include_tables,
                    include_columns=include_columns,
                )
            )
        return matches

    if not isinstance(obj, str):
        return matches

    if include_tables:
        for match in collect_text_token_matches(obj, table_patterns, "string_table_match"):
            match["path"] = path
            matches.append(match)

    if include_columns:
        for match in collect_text_token_matches(obj, column_patterns, "string_column_match"):
            match["path"] = path
            matches.append(match)

    return matches


def find_dataset_references(
    dataset: Dict[str, Any],
    target_tables: List[str],
    target_columns: List[str],
    table_patterns: Dict[str, Pattern[str]],
    column_patterns: Dict[str, Pattern[str]],
) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []
    physical_map = dataset.get("PhysicalTableMap", {})
    if isinstance(physical_map, dict):
        for physical_table_id, physical_table in physical_map.items():
            if not isinstance(physical_table, dict):
                continue

            custom_sql = physical_table.get("CustomSql")
            if isinstance(custom_sql, dict):
                sql_query = custom_sql.get("SqlQuery", "")
                if isinstance(sql_query, str):
                    for match in collect_text_token_matches(sql_query, table_patterns, "dataset_custom_sql_table"):
                        match.update(
                            {
                                "physical_table_id": physical_table_id,
                                "source_name": custom_sql.get("Name", physical_table_id),
                            }
                        )
                        matches.append(match)

                    for match in collect_text_token_matches(sql_query, column_patterns, "dataset_custom_sql_column"):
                        match.update(
                            {
                                "physical_table_id": physical_table_id,
                                "source_name": custom_sql.get("Name", physical_table_id),
                            }
                        )
                        matches.append(match)

                for match in input_column_matches(
                    custom_sql.get("Columns"),
                    target_columns,
                    column_patterns,
                    "dataset_custom_sql_columns_list",
                ):
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

            relational = physical_table.get("RelationalTable")
            if isinstance(relational, dict):
                for match in input_column_matches(
                    relational.get("InputColumns"),
                    target_columns,
                    column_patterns,
                    "dataset_relational_input_columns",
                ):
                    match.update(
                        {
                            "physical_table_id": physical_table_id,
                            "source_name": relational_table_name(physical_table) or physical_table_id,
                        }
                    )
                    matches.append(match)

    matches.extend(
        recursive_string_matches(
            dataset,
            table_patterns,
            column_patterns,
            path="DataSet",
            include_tables=bool(target_tables),
            include_columns=bool(target_columns),
        )
    )
    return matches


def find_dataset_calculated_field_matches(
    dataset: Dict[str, Any],
    column_patterns: Dict[str, Pattern[str]],
) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []
    logical_map = dataset.get("LogicalTableMap", {})
    if not isinstance(logical_map, dict):
        return matches

    for logical_table_id, logical_table in logical_map.items():
        if not isinstance(logical_table, dict):
            continue

        transforms = logical_table.get("DataTransforms", [])
        if not isinstance(transforms, list):
            continue

        for transform_index, transform in enumerate(transforms):
            if not isinstance(transform, dict):
                continue
            operation = transform.get("CreateColumnsOperation")
            if not isinstance(operation, dict):
                continue
            columns = operation.get("Columns", [])
            if not isinstance(columns, list):
                continue

            for column_index, column in enumerate(columns):
                if not isinstance(column, dict):
                    continue
                calc_name = column.get("ColumnName")
                expression = column.get("Expression")
                if not isinstance(calc_name, str) or not isinstance(expression, str):
                    continue

                token_matches = collect_text_token_matches(
                    expression,
                    column_patterns,
                    "dataset_calculated_field_expression",
                )
                for token_match in token_matches:
                    token_match.update(
                        {
                            "calculated_field_name": calc_name,
                            "path": (
                                "DataSet.LogicalTableMap."
                                f"{logical_table_id}.DataTransforms[{transform_index}]"
                                f".CreateColumnsOperation.Columns[{column_index}]"
                            ),
                        }
                    )
                    matches.append(token_match)

    return matches


def find_analysis_calculated_field_matches(
    definition: Dict[str, Any],
    column_patterns: Dict[str, Pattern[str]],
) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []
    calculated_fields = definition.get("CalculatedFields", [])
    if not isinstance(calculated_fields, list):
        return matches

    for index, field in enumerate(calculated_fields):
        if not isinstance(field, dict):
            continue

        calc_name = field.get("Name")
        expression = field.get("Expression")
        if not isinstance(calc_name, str) or not isinstance(expression, str):
            continue

        token_matches = collect_text_token_matches(
            expression,
            column_patterns,
            "analysis_calculated_field_expression",
        )
        for token_match in token_matches:
            token_match.update(
                {
                    "calculated_field_name": calc_name,
                    "path": f"Definition.CalculatedFields[{index}]",
                }
            )
            matches.append(token_match)

    return matches


def is_select_star_query(sql: str) -> bool:
    return bool(re.search(r"(?is)\bselect\s+\*\b", sql))


def definition_matches(
    definition: Dict[str, Any],
    table_patterns: Dict[str, Pattern[str]],
    column_patterns: Dict[str, Pattern[str]],
    scan_tables: bool,
    scan_columns: bool,
) -> List[Dict[str, str]]:
    return recursive_string_matches(
        definition,
        table_patterns,
        column_patterns,
        path="Definition",
        include_tables=scan_tables,
        include_columns=scan_columns,
    )


def classify_dataset_matches(matches: List[Dict[str, str]]) -> Dict[str, Any]:
    hard_matches = [match for match in matches if match.get("match_type") in HARD_DEPENDENCY_MATCH_TYPES]
    string_only_matches = [match for match in matches if match.get("match_type") == "string_column_match"]
    return {
        "hard_dependency": bool(hard_matches),
        "hard_match_count": len(hard_matches),
        "string_only_match_count": len(string_only_matches),
    }


def dataset_priority_label(row: Dict[str, Any]) -> str:
    hard_dependency = bool(row.get("hard_dependency"))
    has_consumer_match = bool(row.get("has_consumer_match"))
    has_refresh_schedule = bool(row.get("has_refresh_schedule"))
    has_dataset_calc_field_match = bool(row.get("dataset_calculated_field_match_count", 0))
    has_direct_sql_column_mentions = bool(row.get("custom_sql_direct_column_reference_count", 0))

    if has_dataset_calc_field_match or has_direct_sql_column_mentions:
        return "CRITICAL"

    if hard_dependency and (has_consumer_match or has_refresh_schedule):
        return "HIGH"
    if hard_dependency:
        return "MEDIUM"
    if has_consumer_match or has_refresh_schedule:
        return "MEDIUM"
    return "LOW"


def downstream_priority_label(
    used_dataset_arns: Set[str],
    high_priority_dataset_arns: Set[str],
    critical_priority_dataset_arns: Set[str],
    has_calc_field_match: bool,
) -> str:
    if has_calc_field_match:
        return "CRITICAL"
    if used_dataset_arns.intersection(critical_priority_dataset_arns):
        return "CRITICAL"
    if used_dataset_arns.intersection(high_priority_dataset_arns):
        return "HIGH"
    return "MEDIUM"


def write_dataset_triage_csv(path: str, dataset_rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "name",
                "data_set_id",
                "arn",
                "import_mode",
                "hard_dependency",
                "hard_match_count",
                "string_only_match_count",
                "total_match_count",
                "has_consumer_match",
                "has_refresh_schedule",
                "refresh_schedule_count",
                "custom_sql_select_star_detected",
                "custom_sql_direct_column_reference_count",
                "dataset_calculated_field_match_count",
                "priority",
            ],
        )
        writer.writeheader()
        for row in dataset_rows:
            writer.writerow(
                {
                    "name": row.get("name", ""),
                    "data_set_id": row.get("data_set_id", ""),
                    "arn": row.get("arn", ""),
                    "import_mode": row.get("import_mode", ""),
                    "hard_dependency": row.get("hard_dependency", False),
                    "hard_match_count": row.get("hard_match_count", 0),
                    "string_only_match_count": row.get("string_only_match_count", 0),
                    "total_match_count": len(row.get("matches", [])),
                    "has_consumer_match": row.get("has_consumer_match", False),
                    "has_refresh_schedule": row.get("has_refresh_schedule", False),
                    "refresh_schedule_count": row.get("refresh_schedule_count", 0),
                    "custom_sql_select_star_detected": row.get("custom_sql_select_star_detected", False),
                    "custom_sql_direct_column_reference_count": row.get("custom_sql_direct_column_reference_count", 0),
                    "dataset_calculated_field_match_count": row.get("dataset_calculated_field_match_count", 0),
                    "priority": row.get("priority", "LOW"),
                }
            )


def scan_analyses(
    qs_client,
    table_patterns: Dict[str, Pattern[str]],
    column_patterns: Dict[str, Pattern[str]],
    matched_dataset_arns: Set[str],
    only_consumers_of_matched_datasets: bool,
    logger: Logger,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], int]:
    rows: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    analyses = get_all_summaries(qs_client.list_analyses, QS_ACCOUNT_ID, "AnalysisSummaryList")
    logger.log(f"Scanning analyses: {len(analyses)} total")

    for index, summary in enumerate(analyses, start=1):
        if index == 1 or index % 25 == 0 or index == len(analyses):
            logger.log(f"  Analyses progress: {index}/{len(analyses)}")

        analysis_id = summary.get("AnalysisId", "")
        try:
            response = qs_client.describe_analysis_definition(
                AwsAccountId=QS_ACCOUNT_ID,
                AnalysisId=analysis_id,
            )
        except Exception as exc:
            skipped.append(
                {
                    "analysis_id": analysis_id,
                    "name": summary.get("Name", ""),
                    "error": str(exc),
                }
            )
            continue

        definition = response.get("Definition", {})
        used_dataset_arns = extract_dataset_arns_from_definition(definition)
        consumes_matched_dataset = bool(matched_dataset_arns.intersection(used_dataset_arns))
        if only_consumers_of_matched_datasets and matched_dataset_arns and not consumes_matched_dataset:
            continue

        matches = definition_matches(
            definition,
            table_patterns,
            column_patterns,
            scan_tables=bool(table_patterns),
            scan_columns=bool(column_patterns),
        )
        if not matches:
            continue

        rows.append(
            {
                "name": summary.get("Name", ""),
                "analysis_id": analysis_id,
                "consumes_matched_dataset": consumes_matched_dataset,
                "used_dataset_arns": sorted(used_dataset_arns),
                "matches": matches,
            }
        )

    return rows, skipped, len(analyses)


def scan_dashboards(
    qs_client,
    table_patterns: Dict[str, Pattern[str]],
    column_patterns: Dict[str, Pattern[str]],
    matched_dataset_arns: Set[str],
    only_consumers_of_matched_datasets: bool,
    logger: Logger,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], int]:
    rows: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    dashboards = get_all_summaries(qs_client.list_dashboards, QS_ACCOUNT_ID, "DashboardSummaryList")
    logger.log(f"Scanning dashboards: {len(dashboards)} total")

    for index, summary in enumerate(dashboards, start=1):
        if index == 1 or index % 25 == 0 or index == len(dashboards):
            logger.log(f"  Dashboards progress: {index}/{len(dashboards)}")

        dashboard_id = summary.get("DashboardId", "")
        try:
            response = qs_client.describe_dashboard_definition(
                AwsAccountId=QS_ACCOUNT_ID,
                DashboardId=dashboard_id,
            )
        except Exception as exc:
            skipped.append(
                {
                    "dashboard_id": dashboard_id,
                    "name": summary.get("Name", ""),
                    "error": str(exc),
                }
            )
            continue

        definition = response.get("Definition", {})
        used_dataset_arns = extract_dataset_arns_from_definition(definition)
        consumes_matched_dataset = bool(matched_dataset_arns.intersection(used_dataset_arns))
        if only_consumers_of_matched_datasets and matched_dataset_arns and not consumes_matched_dataset:
            continue

        matches = definition_matches(
            definition,
            table_patterns,
            column_patterns,
            scan_tables=bool(table_patterns),
            scan_columns=bool(column_patterns),
        )
        if not matches:
            continue

        rows.append(
            {
                "name": summary.get("Name", ""),
                "dashboard_id": dashboard_id,
                "consumes_matched_dataset": consumes_matched_dataset,
                "used_dataset_arns": sorted(used_dataset_arns),
                "matches": matches,
            }
        )

    return rows, skipped, len(dashboards)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find usage of one or more database tables and/or column names across QuickSight datasets, "
            "analyses, and dashboards."
        )
    )
    parser.add_argument("--tables", nargs="+", help="Table identifiers to search for.")
    parser.add_argument("--table-file", help="Text file with one table identifier per line.")
    parser.add_argument("--columns", nargs="+", help="Column identifiers to search for.")
    parser.add_argument("--column-file", help="Text file with one column identifier per line.")
    parser.add_argument("--dataset-name-contains", help="Optional case-insensitive dataset name filter before deep scan.")
    parser.add_argument("--limit", type=int, help="Optional dataset limit after filtering.")
    parser.add_argument("--skip-analyses", action="store_true", help="Skip analysis definition scan.")
    parser.add_argument("--skip-dashboards", action="store_true", help="Skip dashboard definition scan.")
    parser.add_argument(
        "--only-consumers-of-matched-datasets",
        action="store_true",
        help=(
            "Only include analyses/dashboards that consume datasets already matched in the dataset scan. "
            "If no datasets matched, this filter is ignored."
        ),
    )
    parser.add_argument(
        "--write-triage-csv",
        action="store_true",
        help="Write an additional dataset triage CSV for spreadsheet filtering/sorting.",
    )
    parser.add_argument(
        "--check-refresh-schedules",
        action="store_true",
        help="Check whether each matched dataset has at least one scheduled refresh and include this in prioritization.",
    )
    parser.add_argument(
        "--check-dataset-calculated-fields",
        action="store_true",
        help="Check dataset calculated field expressions for direct references to the requested columns.",
    )
    parser.add_argument(
        "--check-analysis-calculated-fields",
        action="store_true",
        help="Check analysis calculated field expressions and log the calculated field names that reference requested columns.",
    )
    parser.add_argument(
        "--check-custom-sql-column-mentions",
        action="store_true",
        help="Distinguish direct column mentions in Custom SQL from SELECT * style dependencies.",
    )
    return parser


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    args = build_parser().parse_args()

    requested_tables = list(args.tables or [])
    if args.table_file:
        requested_tables.extend(read_values_file(args.table_file))

    requested_columns = list(args.columns or [])
    if args.column_file:
        requested_columns.extend(read_values_file(args.column_file))

    target_tables = dedupe_preserving_order(requested_tables)
    target_columns = dedupe_preserving_order(requested_columns)
    if not target_tables and not target_columns:
        raise SystemExit("Provide at least one value via --tables/--table-file and/or --columns/--column-file.")

    table_patterns = {table: build_identifier_pattern(table) for table in target_tables}
    column_patterns = {column: build_identifier_pattern(column) for column in target_columns}

    text_report_path = build_log_path("field_usage_audit", "txt")
    json_report_path = build_log_path("field_usage_audit", "json")
    csv_report_path = build_log_path("field_usage_audit_triage", "csv") if args.write_triage_csv else None
    logger = Logger(text_report_path, "QUICKSIGHT TABLE/COLUMN USAGE AUDIT")

    qs_client = create_quicksight_client()

    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
    logger.log("Mode: READ ONLY")
    logger.log(f"Command: {' '.join(sys.argv)}")
    logger.log(f"Tables: {', '.join(target_tables) if target_tables else '(none)'}")
    logger.log(f"Columns: {', '.join(target_columns) if target_columns else '(none)'}")
    logger.log("")

    try:
        datasets = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
    except (NoCredentialsError, PartialCredentialsError):
        raise SystemExit(
            "AWS credentials not found or incomplete. Activate your awsume/profile session in this terminal and retry."
        )
    if args.dataset_name_contains:
        needle = args.dataset_name_contains.lower()
        datasets = [dataset for dataset in datasets if needle in dataset.get("Name", "").lower()]
    if args.limit:
        datasets = datasets[: args.limit]

    logger.log(f"Datasets selected for scan: {len(datasets)}")

    dataset_rows: List[Dict[str, Any]] = []
    dataset_errors: List[Dict[str, str]] = []

    for index, summary in enumerate(datasets, start=1):
        if index == 1 or index % 25 == 0 or index == len(datasets):
            logger.log(f"  Dataset progress: {index}/{len(datasets)}")

        dataset_id = summary.get("DataSetId", "")
        dataset_name = summary.get("Name", "")

        try:
            response = qs_client.describe_data_set(
                AwsAccountId=QS_ACCOUNT_ID,
                DataSetId=dataset_id,
            )
        except Exception as exc:
            dataset_errors.append(
                {
                    "data_set_id": dataset_id,
                    "name": dataset_name,
                    "error": str(exc),
                }
            )
            continue

        dataset = response.get("DataSet", {})
        matches = find_dataset_references(
            dataset,
            target_tables,
            target_columns,
            table_patterns,
            column_patterns,
        )
        if not matches:
            continue

        row = {
            "name": dataset.get("Name", dataset_name),
            "data_set_id": dataset.get("DataSetId", dataset_id),
            "arn": dataset.get("Arn"),
            "import_mode": dataset.get("ImportMode"),
            "matches": matches,
            "has_consumer_match": False,
            "has_refresh_schedule": False,
            "refresh_schedule_count": 0,
            "refresh_schedule_errors": [],
            "refresh_schedules": [],
            "custom_sql_select_star_detected": False,
            "custom_sql_direct_column_reference_count": 0,
            "dataset_calculated_field_matches": [],
            "dataset_calculated_field_match_count": 0,
            "priority": "LOW",
        }
        row.update(classify_dataset_matches(matches))

        if args.check_custom_sql_column_mentions:
            row["custom_sql_select_star_detected"] = False
            for physical_table in dataset.get("PhysicalTableMap", {}).values() if isinstance(dataset.get("PhysicalTableMap"), dict) else []:
                if not isinstance(physical_table, dict):
                    continue
                custom_sql = physical_table.get("CustomSql")
                if not isinstance(custom_sql, dict):
                    continue
                sql_query = custom_sql.get("SqlQuery")
                if isinstance(sql_query, str) and is_select_star_query(sql_query):
                    row["custom_sql_select_star_detected"] = True
                    break

            row["custom_sql_direct_column_reference_count"] = sum(
                1 for match in matches if match.get("match_type") == "dataset_custom_sql_column"
            )

        if args.check_dataset_calculated_fields:
            dataset_calc_matches = find_dataset_calculated_field_matches(dataset, column_patterns)
            row["dataset_calculated_field_matches"] = dataset_calc_matches
            row["dataset_calculated_field_match_count"] = len(dataset_calc_matches)
            for calc_match in dataset_calc_matches:
                matches.append(calc_match)

        dataset_rows.append(row)

        logger.log("")
        logger.log(f"DATASET: {row['name']} ({row['data_set_id']})")
        logger.log(f"  ARN: {row.get('arn')}")
        logger.log(f"  Import mode: {row.get('import_mode')}")
        logger.log(f"  Matches: {len(matches)}")
        if args.check_custom_sql_column_mentions:
            logger.log(f"  Custom SQL uses SELECT *: {row['custom_sql_select_star_detected']}")
            logger.log(f"  Custom SQL direct column mentions: {row['custom_sql_direct_column_reference_count']}")
        if args.check_dataset_calculated_fields:
            logger.log(f"  Dataset calculated-field matches: {row['dataset_calculated_field_match_count']}")
            for calc_match in row["dataset_calculated_field_matches"][:5]:
                logger.log(
                    f"    * {calc_match.get('calculated_field_name')} references {calc_match.get('token')}"
                )
            if row["dataset_calculated_field_match_count"] > 5:
                logger.log(
                    f"    ... plus {row['dataset_calculated_field_match_count'] - 5} more dataset calculated-field matches."
                )
        for match in matches[:10]:
            path_part = f" at {match['path']}" if match.get("path") else ""
            logger.log(f"    - {match['token']} [{match['match_type']}] {match.get('snippet', '')}{path_part}")
        if len(matches) > 10:
            logger.log(f"    ... plus {len(matches) - 10} more matches in the JSON report.")

    if args.check_refresh_schedules and dataset_rows:
        logger.log("")
        logger.log("Checking refresh schedules for matched datasets...")
        for index, row in enumerate(dataset_rows, start=1):
            if index == 1 or index % 25 == 0 or index == len(dataset_rows):
                logger.log(f"  Refresh schedule progress: {index}/{len(dataset_rows)}")

            schedules, error = list_refresh_schedules_safe(qs_client, row["data_set_id"])
            if error:
                row["refresh_schedule_errors"].append(error)
                continue

            row["has_refresh_schedule"] = bool(schedules)
            row["refresh_schedule_count"] = len(schedules)
            row["refresh_schedules"] = [
                {
                    "schedule_id": schedule.get("ScheduleId"),
                    "refresh_type": schedule.get("RefreshType"),
                    "schedule_frequency": schedule.get("ScheduleFrequency"),
                }
                for schedule in schedules
            ]

    matched_dataset_arns = {row["arn"] for row in dataset_rows if row.get("arn")}

    analysis_rows: List[Dict[str, Any]] = []
    dashboard_rows: List[Dict[str, Any]] = []
    analysis_errors: List[Dict[str, str]] = []
    dashboard_errors: List[Dict[str, str]] = []
    analyses_scanned = 0
    dashboards_scanned = 0

    if not args.skip_analyses:
        logger.log("")
        logger.log("Scanning analysis definitions...")
        try:
            analysis_rows, analysis_errors, analyses_scanned = scan_analyses(
                qs_client,
                table_patterns,
                column_patterns,
                matched_dataset_arns,
                args.only_consumers_of_matched_datasets,
                logger,
            )
        except (NoCredentialsError, PartialCredentialsError):
            raise SystemExit(
                "AWS credentials expired during analysis scan. Refresh your session and retry."
            )

    if not args.skip_dashboards:
        logger.log("")
        logger.log("Scanning dashboard definitions...")
        try:
            dashboard_rows, dashboard_errors, dashboards_scanned = scan_dashboards(
                qs_client,
                table_patterns,
                column_patterns,
                matched_dataset_arns,
                args.only_consumers_of_matched_datasets,
                logger,
            )
        except (NoCredentialsError, PartialCredentialsError):
            raise SystemExit(
                "AWS credentials expired during dashboard scan. Refresh your session and retry."
            )

    logger.log("")
    logger.log(f"Analyses with matches: {len(analysis_rows)}")
    for row in analysis_rows:
        logger.log(f"  Analysis: {row['name']} ({row['analysis_id']}) | matches: {len(row['matches'])}")

    logger.log("")
    logger.log(f"Dashboards with matches: {len(dashboard_rows)}")
    for row in dashboard_rows:
        logger.log(f"  Dashboard: {row['name']} ({row['dashboard_id']}) | matches: {len(row['matches'])}")

    consumer_dataset_arns: Set[str] = set()
    for row in analysis_rows:
        consumer_dataset_arns.update(row.get("used_dataset_arns", []))
    for row in dashboard_rows:
        consumer_dataset_arns.update(row.get("used_dataset_arns", []))
    for row in dataset_rows:
        row["has_consumer_match"] = bool(row.get("arn") in consumer_dataset_arns)

    for row in dataset_rows:
        row["priority"] = dataset_priority_label(row)

    if args.check_analysis_calculated_fields and analysis_rows:
        analysis_definitions: Dict[str, Dict[str, Any]] = {}
        # Re-read only the matched analyses to get definition objects for calculated-field checks.
        for row in analysis_rows:
            try:
                response = qs_client.describe_analysis_definition(
                    AwsAccountId=QS_ACCOUNT_ID,
                    AnalysisId=row["analysis_id"],
                )
                analysis_definitions[row["analysis_id"]] = response.get("Definition", {})
            except Exception as exc:
                row.setdefault("analysis_calculated_field_errors", []).append(str(exc))
                analysis_definitions[row["analysis_id"]] = {}

        for row in analysis_rows:
            definition = analysis_definitions.get(row["analysis_id"], {})
            calc_matches = find_analysis_calculated_field_matches(definition, column_patterns)
            row["analysis_calculated_field_matches"] = calc_matches
            row["analysis_calculated_field_match_count"] = len(calc_matches)
            row["matches"].extend(calc_matches)

    high_priority_dataset_arns = {
        row.get("arn")
        for row in dataset_rows
        if row.get("priority") in {"CRITICAL", "HIGH"} and row.get("arn")
    }
    critical_priority_dataset_arns = {
        row.get("arn")
        for row in dataset_rows
        if row.get("priority") == "CRITICAL" and row.get("arn")
    }

    for row in analysis_rows:
        used = set(row.get("used_dataset_arns", []))
        row["priority"] = downstream_priority_label(
            used,
            high_priority_dataset_arns,
            critical_priority_dataset_arns,
            bool(row.get("analysis_calculated_field_match_count", 0)),
        )

    for row in dashboard_rows:
        used = set(row.get("used_dataset_arns", []))
        row["priority"] = downstream_priority_label(
            used,
            high_priority_dataset_arns,
            critical_priority_dataset_arns,
            False,
        )

    hard_dependency_datasets = sum(1 for row in dataset_rows if row.get("hard_dependency"))
    string_only_datasets = len(dataset_rows) - hard_dependency_datasets
    datasets_with_consumer_matches = sum(1 for row in dataset_rows if row.get("has_consumer_match"))
    datasets_with_refresh_schedules = sum(1 for row in dataset_rows if row.get("has_refresh_schedule"))
    critical_priority_datasets = sum(1 for row in dataset_rows if row.get("priority") == "CRITICAL")
    high_priority_datasets = sum(1 for row in dataset_rows if row.get("priority") == "HIGH")
    medium_priority_datasets = sum(1 for row in dataset_rows if row.get("priority") == "MEDIUM")
    critical_priority_analyses = sum(1 for row in analysis_rows if row.get("priority") == "CRITICAL")
    high_priority_analyses = sum(1 for row in analysis_rows if row.get("priority") == "HIGH")
    critical_priority_dashboards = sum(1 for row in dashboard_rows if row.get("priority") == "CRITICAL")
    high_priority_dashboards = sum(1 for row in dashboard_rows if row.get("priority") == "HIGH")

    summary = {
        "datasets_selected": len(datasets),
        "datasets_with_matches": len(dataset_rows),
        "hard_dependency_datasets": hard_dependency_datasets,
        "string_only_datasets": string_only_datasets,
        "datasets_with_consumer_matches": datasets_with_consumer_matches,
        "datasets_with_refresh_schedules": datasets_with_refresh_schedules,
        "critical_priority_datasets": critical_priority_datasets,
        "high_priority_datasets": high_priority_datasets,
        "medium_priority_datasets": medium_priority_datasets,
        "dataset_scan_errors": len(dataset_errors),
        "analyses_scanned": analyses_scanned,
        "analyses_with_matches": len(analysis_rows),
        "critical_priority_analyses": critical_priority_analyses,
        "high_priority_analyses": high_priority_analyses,
        "analysis_scan_errors": len(analysis_errors),
        "dashboards_scanned": dashboards_scanned,
        "dashboards_with_matches": len(dashboard_rows),
        "critical_priority_dashboards": critical_priority_dashboards,
        "high_priority_dashboards": high_priority_dashboards,
        "dashboard_scan_errors": len(dashboard_errors),
    }

    payload = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": QS_REGION,
        "filters": {
            "tables": target_tables,
            "columns": target_columns,
            "dataset_name_contains": args.dataset_name_contains,
            "limit": args.limit,
            "skip_analyses": args.skip_analyses,
            "skip_dashboards": args.skip_dashboards,
            "only_consumers_of_matched_datasets": args.only_consumers_of_matched_datasets,
            "write_triage_csv": args.write_triage_csv,
            "check_refresh_schedules": args.check_refresh_schedules,
            "check_dataset_calculated_fields": args.check_dataset_calculated_fields,
            "check_analysis_calculated_fields": args.check_analysis_calculated_fields,
            "check_custom_sql_column_mentions": args.check_custom_sql_column_mentions,
        },
        "summary": summary,
        "datasets": dataset_rows,
        "analyses": analysis_rows,
        "dashboards": dashboard_rows,
        "dataset_scan_errors": dataset_errors,
        "analysis_scan_errors": analysis_errors,
        "dashboard_scan_errors": dashboard_errors,
    }

    with open(json_report_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)

    if csv_report_path:
        write_dataset_triage_csv(csv_report_path, dataset_rows)

    logger.log("")
    logger.log(f"Datasets with matches: {summary['datasets_with_matches']}")
    logger.log(f"Hard dependency datasets: {summary['hard_dependency_datasets']}")
    logger.log(f"String-only datasets: {summary['string_only_datasets']}")
    logger.log(f"Datasets with consumer matches: {summary['datasets_with_consumer_matches']}")
    if args.check_refresh_schedules:
        logger.log(f"Datasets with refresh schedules: {summary['datasets_with_refresh_schedules']}")
    logger.log(f"Critical priority datasets: {summary['critical_priority_datasets']}")
    logger.log(f"High priority datasets: {summary['high_priority_datasets']}")
    logger.log(f"Medium priority datasets: {summary['medium_priority_datasets']}")
    logger.log(f"Analyses with matches: {summary['analyses_with_matches']}")
    logger.log(f"Critical priority analyses: {summary['critical_priority_analyses']}")
    logger.log(f"High priority analyses: {summary['high_priority_analyses']}")
    logger.log(f"Dashboards with matches: {summary['dashboards_with_matches']}")
    logger.log(f"Critical priority dashboards: {summary['critical_priority_dashboards']}")
    logger.log(f"High priority dashboards: {summary['high_priority_dashboards']}")
    logger.log(f"Dataset scan errors: {summary['dataset_scan_errors']}")
    logger.log(f"Analysis scan errors: {summary['analysis_scan_errors']}")
    logger.log(f"Dashboard scan errors: {summary['dashboard_scan_errors']}")

    prioritized_datasets = [row for row in dataset_rows if row.get("priority") in {"CRITICAL", "HIGH"}]
    prioritized_analyses = [row for row in analysis_rows if row.get("priority") in {"CRITICAL", "HIGH"}]
    prioritized_dashboards = [row for row in dashboard_rows if row.get("priority") in {"CRITICAL", "HIGH"}]

    logger.log("")
    logger.log("PRIORITY DATASETS (CRITICAL/HIGH):")
    if prioritized_datasets:
        for row in prioritized_datasets:
            logger.log(
                f"  - {row['name']} ({row['data_set_id']}) | priority={row['priority']} | hard_dependency={row['hard_dependency']} "
                f"| has_refresh_schedule={row['has_refresh_schedule']} | has_consumer_match={row['has_consumer_match']}"
            )
            if args.check_dataset_calculated_fields and row.get("dataset_calculated_field_match_count", 0):
                for calc_match in row.get("dataset_calculated_field_matches", [])[:5]:
                    logger.log(
                        f"      calc_field={calc_match.get('calculated_field_name')} token={calc_match.get('token')}"
                    )
            if args.check_custom_sql_column_mentions:
                logger.log(
                    f"      custom_sql_select_star={row.get('custom_sql_select_star_detected')} "
                    f"custom_sql_direct_column_mentions={row.get('custom_sql_direct_column_reference_count')}"
                )
    else:
        logger.log("  (none)")

    logger.log("")
    logger.log("PRIORITY ANALYSES (CRITICAL/HIGH):")
    if prioritized_analyses:
        for row in prioritized_analyses:
            logger.log(f"  - {row['name']} ({row['analysis_id']}) | priority={row['priority']}")
            if args.check_analysis_calculated_fields and row.get("analysis_calculated_field_match_count", 0):
                for calc_match in row.get("analysis_calculated_field_matches", [])[:5]:
                    logger.log(
                        f"      calc_field={calc_match.get('calculated_field_name')} token={calc_match.get('token')}"
                    )
    else:
        logger.log("  (none)")

    logger.log("")
    logger.log("PRIORITY DASHBOARDS (CRITICAL/HIGH):")
    if prioritized_dashboards:
        for row in prioritized_dashboards:
            logger.log(f"  - {row['name']} ({row['dashboard_id']}) | priority={row['priority']}")
    else:
        logger.log("  (none)")

    logger.log(f"Text report: {text_report_path}")
    logger.log(f"JSON report: {json_report_path}")
    if csv_report_path:
        logger.log(f"CSV triage report: {csv_report_path}")


if __name__ == "__main__":
    main()
