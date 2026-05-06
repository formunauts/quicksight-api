import argparse
import json
import os
from typing import Any, Dict, List, Optional, Set

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


OPERATOR_KEYS = {"MatchOperator", "Operator"}
FILTER_VALUE_KEYS = {
    "Value",
    "CategoryValues",
    "RangeMinimumValue",
    "RangeMaximumValue",
    "ParameterName",
}
FILTER_LIKE_KEYS = {
    "Column",
    "Configuration",
    "CustomFilterConfiguration",
    "CustomFilterListConfiguration",
    "Exact",
    "FilterId",
    "IncludeInnerSet",
    "InnerFilter",
    "MatchOperator",
    "NullOption",
    "Operator",
    "ParameterName",
    "RangeMaximumValue",
    "RangeMinimumValue",
    "RelativeDateType",
    "RollingDate",
    "SelectAllOptions",
    "TimeGranularity",
    "Value",
}


def normalize(value: str) -> str:
    return value.strip().lower()


def collect_strings_for_keys(obj: Any, keys: Set[str], results: Optional[Set[str]] = None) -> Set[str]:
    if results is None:
        results = set()

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys:
                if isinstance(value, str):
                    results.add(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            results.add(item)
            collect_strings_for_keys(value, keys, results)
    elif isinstance(obj, list):
        for item in obj:
            collect_strings_for_keys(item, keys, results)

    return results


def is_filter_like(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if "Column" not in obj:
        return False
    return any(key in obj for key in FILTER_LIKE_KEYS)


def field_matches(columns: Set[str], requested_field: str, match_mode: str) -> bool:
    requested = normalize(requested_field)
    normalized_columns = {normalize(column) for column in columns}
    if match_mode == "contains":
        return any(requested in column for column in normalized_columns)
    return requested in normalized_columns


def value_matches(values: Set[str], requested_value: Optional[str]) -> bool:
    if requested_value is None:
        return True
    requested = normalize(requested_value)
    return any(normalize(value) == requested for value in values)


def find_matching_filters(
    obj: Any,
    field: str,
    operator: Optional[str],
    value: Optional[str],
    field_match_mode: str,
    path: str = "Definition",
) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []

    if isinstance(obj, dict):
        if is_filter_like(obj):
            column_names = collect_strings_for_keys(obj.get("Column"), {"ColumnName"})
            operators = collect_strings_for_keys(obj, OPERATOR_KEYS)
            filter_values = collect_strings_for_keys(obj, FILTER_VALUE_KEYS)

            if field_matches(column_names, field, field_match_mode):
                operator_matches = True
                if operator:
                    operator_matches = normalize(operator) in {
                        normalize(found_operator) for found_operator in operators
                    }

                if operator_matches and value_matches(filter_values, value):
                    matches.append(
                        {
                            "path": path,
                            "column_names": sorted(column_names),
                            "operators": sorted(operators),
                            "values": sorted(filter_values),
                            "definition": obj,
                        }
                    )

        for key, child in obj.items():
            matches.extend(
                find_matching_filters(
                    child,
                    field=field,
                    operator=operator,
                    value=value,
                    field_match_mode=field_match_mode,
                    path=f"{path}.{key}",
                )
            )
        return matches

    if isinstance(obj, list):
        for index, child in enumerate(obj):
            matches.extend(
                find_matching_filters(
                    child,
                    field=field,
                    operator=operator,
                    value=value,
                    field_match_mode=field_match_mode,
                    path=f"{path}[{index}]",
                )
            )

    return matches


def load_all_analyses(qs_client) -> List[Dict[str, Any]]:
    return get_all_summaries(qs_client.list_analyses, QS_ACCOUNT_ID, "AnalysisSummaryList")


def truncate_json(obj: Any, limit: int = 240) -> str:
    preview = json.dumps(obj, ensure_ascii=True, sort_keys=True)
    if len(preview) <= limit:
        return preview
    return f"{preview[: limit - 3]}..."


def describe_auth_source(profile: Optional[str]) -> str:
    if profile:
        return f"named profile override ({profile})"
    if os.getenv("AWSUME_PROFILE"):
        return f"current awsume session ({os.getenv('AWSUME_PROFILE')})"
    if os.getenv("AWS_PROFILE"):
        return f"current shell profile ({os.getenv('AWS_PROFILE')})"
    if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SESSION_TOKEN"):
        return "current shell session credentials"
    if os.getenv("AWS_ACCESS_KEY_ID"):
        return "current shell long-lived credentials"
    return "default boto3 credential chain"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find QuickSight analyses whose definitions contain matching filters."
    )
    parser.add_argument("--field", required=True, help="Filter column name to match, for example customer_id.")
    parser.add_argument(
        "--operator",
        help="Optional filter operator to match, for example EQUALS.",
    )
    parser.add_argument(
        "--value",
        help="Optional exact filter value to match when the definition stores a literal value.",
    )
    parser.add_argument(
        "--field-match",
        choices=("exact", "contains"),
        default="exact",
        help="How to compare the requested field against QuickSight column names.",
    )
    parser.add_argument(
        "--profile",
        help="Optional AWS profile override. By default the script uses the credentials from your current shell, including an awsume session.",
    )
    args = parser.parse_args()

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    safe_field = args.field.replace("/", "_")
    log_path = build_log_path(f"analysis_filter_search_{safe_field}")
    json_path = build_log_path(f"analysis_filter_search_{safe_field}", extension="json")
    logger = Logger(log_path, "QUICKSIGHT ANALYSIS FILTER SEARCH")

    logger.log(f"Account: {QS_ACCOUNT_ID}")
    logger.log(f"Region: {QS_REGION}")
    logger.log(f"AWS auth source: {describe_auth_source(args.profile)}")
    logger.log(f"Field: {args.field}")
    logger.log(f"Operator: {args.operator or 'any'}")
    logger.log(f"Value: {args.value or 'any'}")
    logger.log(f"Field match mode: {args.field_match}")
    logger.log(f"Text log: {log_path}")
    logger.log(f"JSON report: {json_path}")
    logger.log("")

    try:
        if args.profile:
            session = boto3.Session(profile_name=args.profile)
            qs_client = session.client("quicksight", region_name=QS_REGION)
        else:
            qs_client = create_quicksight_client()

        analyses = load_all_analyses(qs_client)
        results: List[Dict[str, Any]] = []
        skipped: List[Dict[str, str]] = []

        logger.log(f"Total analyses to scan: {len(analyses)}")
        logger.log("")

        for index, summary in enumerate(analyses, start=1):
            if index == 1 or index % 25 == 0 or index == len(analyses):
                logger.log(f"Scanning analyses: {index}/{len(analyses)}")

            analysis_id = summary["AnalysisId"]
            try:
                response = qs_client.describe_analysis_definition(
                    AwsAccountId=QS_ACCOUNT_ID,
                    AnalysisId=analysis_id,
                )
            except Exception as exc:
                skipped.append(
                    {
                        "analysis_id": analysis_id,
                        "name": summary.get("Name", analysis_id),
                        "error": str(exc),
                    }
                )
                continue

            definition = response.get("Definition", {})
            matches = find_matching_filters(
                definition,
                field=args.field,
                operator=args.operator,
                value=args.value,
                field_match_mode=args.field_match,
            )
            if not matches:
                continue

            results.append(
                {
                    "analysis_id": analysis_id,
                    "name": summary.get("Name", analysis_id),
                    "matches": matches,
                }
            )

        logger.log(f"Matching analyses: {len(results)}")
        for result in results:
            logger.log(f"Analysis: {result['name']} ({result['analysis_id']})")
            for match in result["matches"]:
                operators = ", ".join(match["operators"]) if match["operators"] else "none recorded"
                values = ", ".join(match["values"]) if match["values"] else "none recorded"
                logger.log(f"  Path: {match['path']}")
                logger.log(f"  Operators: {operators}")
                logger.log(f"  Values: {values}")
                logger.log(f"  Preview: {truncate_json(match['definition'])}")
            logger.log("")

        logger.log(f"Skipped analyses: {len(skipped)}")

        payload = {
            "account_id": QS_ACCOUNT_ID,
            "region": QS_REGION,
            "profile": args.profile,
            "field": args.field,
            "operator": args.operator,
            "value": args.value,
            "field_match_mode": args.field_match,
            "analysis_count": len(analyses),
            "matching_analysis_count": len(results),
            "results": results,
            "skipped": skipped,
        }
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

        logger.log(f"JSON report written to: {json_path}")
        logger.log(f"Text log written to: {log_path}")
    except NoCredentialsError:
        logger.log("ERROR: Unable to locate AWS credentials for the current environment or profile.")
        raise SystemExit(1)
    except (BotoCoreError, ClientError) as exc:
        logger.log(f"ERROR: AWS request failed: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
