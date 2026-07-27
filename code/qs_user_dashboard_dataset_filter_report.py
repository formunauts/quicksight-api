import argparse
import csv
import json
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

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


RETRYABLE_ERROR_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "TooManyRequests",
    "LimitExceededException",
}

TARGET_FILTER_FIELD_ALIASES: Dict[str, List[str]] = {
    "organization_id": ["organization_id", "organisation_id"],
    "organization_name": ["organization_name", "organisation_name"],
    "customer_id": ["customer_id"],
    "customer_name": ["customer_name"],
    "fundraiser_coder": ["fundraiser_coder", "fundraiser_code"],
    "fundraiser_name": ["fundraiser_name"],
    "campaign_id": ["campaign_id"],
    "campaign_name": ["campaign_name"],
}
OUTPUT_FILTER_KEYS = list(TARGET_FILTER_FIELD_ALIASES.keys())

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


def parse_dataset_id_from_arn(dataset_arn: str) -> Optional[str]:
    if "/" not in dataset_arn:
        return None
    resource = dataset_arn.split(":", 5)[-1]
    prefix = "dataset/"
    if not resource.startswith(prefix):
        return None
    dataset_id = resource[len(prefix) :]
    return dataset_id or None


def is_retryable_error(exc: Exception) -> bool:
    if not isinstance(exc, ClientError):
        return False
    return exc.response.get("Error", {}).get("Code") in RETRYABLE_ERROR_CODES


def call_with_retries(
    logger: Logger,
    label: str,
    func,
    retry_attempts: int,
    retry_base_seconds: float,
    *args,
    **kwargs,
):
    attempt = 1
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if attempt >= retry_attempts or not is_retryable_error(exc):
                raise
            delay = retry_base_seconds * (2 ** (attempt - 1))
            logger.log(
                f"Retryable error during {label}: {exc}. Retrying in {delay:.1f}s "
                f"({attempt}/{retry_attempts - 1})."
            )
            time.sleep(delay)
            attempt += 1


def list_all_users(qs_client, namespace: str, logger: Logger, retry_attempts: int, retry_base_seconds: float) -> List[Dict[str, Any]]:
    users: List[Dict[str, Any]] = []
    next_token = None
    while True:
        kwargs: Dict[str, Any] = {
            "AwsAccountId": QS_ACCOUNT_ID,
            "Namespace": namespace,
        }
        if next_token:
            kwargs["NextToken"] = next_token
        response = call_with_retries(
            logger,
            "list_users",
            qs_client.list_users,
            retry_attempts,
            retry_base_seconds,
            **kwargs,
        )
        users.extend(response.get("UserList", []))
        next_token = response.get("NextToken")
        if not next_token:
            return users


def list_user_groups(
    qs_client,
    namespace: str,
    user_name: str,
    logger: Logger,
    retry_attempts: int,
    retry_base_seconds: float,
) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    next_token = None
    while True:
        kwargs: Dict[str, Any] = {
            "AwsAccountId": QS_ACCOUNT_ID,
            "Namespace": namespace,
            "UserName": user_name,
        }
        if next_token:
            kwargs["NextToken"] = next_token
        response = call_with_retries(
            logger,
            f"list_user_groups for {user_name}",
            qs_client.list_user_groups,
            retry_attempts,
            retry_base_seconds,
            **kwargs,
        )
        groups.extend(response.get("GroupList", []))
        next_token = response.get("NextToken")
        if not next_token:
            return groups


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


def field_name_matches(column_names: Set[str], field_name: str) -> bool:
    needle = normalize(field_name)
    return any(normalize(name) == needle for name in column_names)


def field_name_matches_any(column_names: Set[str], aliases: List[str]) -> bool:
    normalized_columns = {normalize(name) for name in column_names}
    normalized_aliases = {normalize(alias) for alias in aliases}
    return bool(normalized_columns.intersection(normalized_aliases))


def text_mentions_any_alias(text: str, aliases: List[str]) -> bool:
    text_lc = normalize(text)
    for alias in aliases:
        pattern = rf"(?<![a-z0-9_]){re.escape(normalize(alias))}(?![a-z0-9_])"
        if re.search(pattern, text_lc):
            return True
    return False


def find_field_filter_literals(
    obj: Any,
    field_aliases: Dict[str, List[str]],
    path: str = "Definition",
) -> Dict[str, Set[str]]:
    found: Dict[str, Set[str]] = {name: set() for name in field_aliases}

    if isinstance(obj, dict):
        if is_filter_like(obj):
            column_names = collect_strings_for_keys(obj.get("Column"), {"ColumnName"})
            operators = collect_strings_for_keys(obj, OPERATOR_KEYS)
            values = collect_strings_for_keys(obj, FILTER_VALUE_KEYS)

            for field_name, aliases in field_aliases.items():
                if not field_name_matches_any(column_names, aliases):
                    continue

                literal_values = [
                    value
                    for value in values
                    if normalize(value) not in {normalize(alias) for alias in aliases}
                ]
                if literal_values:
                    found[field_name].update(literal_values)
                else:
                    operator_display = ",".join(sorted(operators)) if operators else "unknown_operator"
                    found[field_name].add(f"<filter_defined:{operator_display}:{path}>")

        for key, child in obj.items():
            child_found = find_field_filter_literals(child, field_aliases, path=f"{path}.{key}")
            for field_name, values in child_found.items():
                found[field_name].update(values)
        return found

    if isinstance(obj, list):
        for index, child in enumerate(obj):
            child_found = find_field_filter_literals(child, field_aliases, path=f"{path}[{index}]")
            for field_name, values in child_found.items():
                found[field_name].update(values)

    return found


def collect_condition_expressions(obj: Any, results: Optional[List[str]] = None) -> List[str]:
    if results is None:
        results = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "ConditionExpression" and isinstance(value, str):
                results.append(value)
            else:
                collect_condition_expressions(value, results)
    elif isinstance(obj, list):
        for value in obj:
            collect_condition_expressions(value, results)

    return results


def collect_column_names(obj: Any, results: Optional[Set[str]] = None) -> Set[str]:
    if results is None:
        results = set()

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in {"ColumnName", "Name"} and isinstance(value, str):
                results.add(value)
            collect_column_names(value, results)
    elif isinstance(obj, list):
        for item in obj:
            collect_column_names(item, results)

    return results


def truncate_text(value: str, limit: int = 280) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def extract_custom_sql_where_filters(
    dataset: Dict[str, Any],
    target_field_aliases: Dict[str, List[str]],
) -> Dict[str, Set[str]]:
    filters: Dict[str, Set[str]] = {field: set() for field in target_field_aliases}
    physical_map = dataset.get("PhysicalTableMap", {})
    if not isinstance(physical_map, dict):
        return filters

    for physical_table_id, physical_table in physical_map.items():
        if not isinstance(physical_table, dict):
            continue

        custom_sql = physical_table.get("CustomSql")
        if not isinstance(custom_sql, dict):
            continue

        sql_query = custom_sql.get("SqlQuery")
        if not isinstance(sql_query, str) or not sql_query.strip():
            continue

        # Pull the WHERE segment, then only keep entries for requested fields.
        where_match = re.search(
            r"(?is)\\bwhere\\b(.*?)(?:\\bgroup\\s+by\\b|\\border\\s+by\\b|\\bhaving\\b|\\blimit\\b|$)",
            sql_query,
        )
        if not where_match:
            continue

        where_clause = where_match.group(1).strip()
        if not where_clause:
            continue

        where_clause_lc = normalize(where_clause)
        source_name = custom_sql.get("Name") or str(physical_table_id)

        for field, aliases in target_field_aliases.items():
            if not text_mentions_any_alias(where_clause_lc, aliases):
                continue
            filters[field].add(
                f"CUSTOM_SQL_WHERE[{source_name}]: {truncate_text(where_clause)}"
            )

    return filters


def describe_data_set_safe(
    qs_client,
    dataset_id: str,
    logger: Logger,
    retry_attempts: int,
    retry_base_seconds: float,
) -> Dict[str, Any]:
    response = call_with_retries(
        logger,
        f"describe_data_set {dataset_id}",
        qs_client.describe_data_set,
        retry_attempts,
        retry_base_seconds,
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
    )
    return response.get("DataSet", {})


def extract_dataset_filters(
    dataset: Dict[str, Any],
    target_field_aliases: Dict[str, List[str]],
) -> Dict[str, Set[str]]:
    filters: Dict[str, Set[str]] = {field: set() for field in target_field_aliases}

    # Filter expressions from logical transforms are the closest thing to dataset-level hard filters.
    for expression in collect_condition_expressions(dataset.get("LogicalTableMap", {})):
        expression_lc = normalize(expression)
        for field, aliases in target_field_aliases.items():
            if text_mentions_any_alias(expression_lc, aliases):
                filters[field].add(expression)

    tag_config = dataset.get("RowLevelPermissionTagConfiguration")
    if isinstance(tag_config, dict):
        rules = tag_config.get("TagRules", [])
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                column_name = rule.get("ColumnName")
                if not isinstance(column_name, str):
                    continue
                for field, aliases in target_field_aliases.items():
                    if normalize(column_name) not in {normalize(alias) for alias in aliases}:
                        continue
                    tag_key = rule.get("TagKey", "")
                    match_all = rule.get("MatchAllValue")
                    delimiter = rule.get("TagMultiValueDelimiter")
                    filters[field].add(
                        "RLS_TAG_RULE: "
                        f"TagKey={tag_key or 'N/A'}, "
                        f"MatchAllValue={match_all if isinstance(match_all, str) else 'N/A'}, "
                        f"Delimiter={delimiter if isinstance(delimiter, str) else 'N/A'}"
                    )

    custom_sql_filters = extract_custom_sql_where_filters(dataset, target_field_aliases)
    for field, values in custom_sql_filters.items():
        filters[field].update(values)

    return filters


def extract_rls_dataset_hint(
    dataset: Dict[str, Any],
    target_field_aliases: Dict[str, List[str]],
    described_dataset_cache: Dict[str, Dict[str, Any]],
) -> Dict[str, Set[str]]:
    hints: Dict[str, Set[str]] = {field: set() for field in target_field_aliases}

    rls_data_set = dataset.get("RowLevelPermissionDataSet")
    if not isinstance(rls_data_set, dict):
        return hints

    rls_arn = rls_data_set.get("Arn")
    if not isinstance(rls_arn, str):
        return hints

    rls_dataset_id = parse_dataset_id_from_arn(rls_arn)
    if not rls_dataset_id:
        return hints

    rls_dataset = described_dataset_cache.get(rls_dataset_id, {})
    rls_columns = collect_column_names(rls_dataset)
    rls_columns_normalized = {normalize(name) for name in rls_columns}

    for field, aliases in target_field_aliases.items():
        if any(normalize(alias) in rls_columns_normalized for alias in aliases):
            hints[field].add(
                f"RLS_DATASET:{rls_dataset_id} (field present, row values not exposed by QuickSight API)"
            )

    return hints


def merge_filter_maps(*maps: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
    merged: Dict[str, Set[str]] = {}
    for mapping in maps:
        for field, values in mapping.items():
            merged.setdefault(field, set()).update(values)
    return merged


def format_values(values: Set[str]) -> str:
    if not values:
        return ""
    return " | ".join(sorted(values))


def build_filter_columns(prefix: str, filter_map: Dict[str, Set[str]]) -> Dict[str, str]:
    return {
        f"{prefix}_{field}_filters": format_values(set(filter_map.get(field, set())))
        for field in OUTPUT_FILTER_KEYS
    }


def any_filter_used(dashboard_filters: Dict[str, Set[str]], dataset_filters: Dict[str, Set[str]]) -> bool:
    for field in OUTPUT_FILTER_KEYS:
        if dashboard_filters.get(field) or dataset_filters.get(field):
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a user -> dashboard -> dataset access table and extract filter hints for "
            "organization/customer/fundraiser/campaign identifiers and names."
        )
    )
    parser.add_argument(
        "--namespace",
        default="default",
        help="QuickSight namespace. Defaults to 'default'.",
    )
    parser.add_argument(
        "--dashboard-name-contains",
        help="Optional case-insensitive dashboard name filter.",
    )
    parser.add_argument(
        "--user-email-contains",
        help="Optional case-insensitive user email filter.",
    )
    parser.add_argument(
        "--max-users",
        type=int,
        help="Optional cap on users after filtering.",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=8,
        help="Attempts for retryable QuickSight throttling errors.",
    )
    parser.add_argument(
        "--retry-base-seconds",
        type=float,
        default=1.0,
        help="Initial backoff delay for retryable QuickSight throttling errors.",
    )
    args = parser.parse_args()

    if args.retry_attempts < 1:
        raise SystemExit("--retry-attempts must be at least 1.")
    if args.retry_base_seconds < 0:
        raise SystemExit("--retry-base-seconds must be >= 0.")
    if args.max_users is not None and args.max_users < 1:
        raise SystemExit("--max-users must be >= 1.")

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    text_path = build_log_path("user_dashboard_dataset_filter_report", "txt")
    json_path = build_log_path("user_dashboard_dataset_filter_report", "json")
    csv_path = build_log_path("user_dashboard_dataset_filter_report", "csv")
    logger = Logger(text_path, "QUICKSIGHT USER DASHBOARD DATASET FILTER REPORT")

    try:
        qs_client = create_quicksight_client()

        logger.log(f"Account: {QS_ACCOUNT_ID}")
        logger.log(f"Region: {QS_REGION}")
        logger.log(f"Namespace: {args.namespace}")
        logger.log(f"Dashboard name filter: {args.dashboard_name_contains or '(none)'}")
        logger.log(f"User email filter: {args.user_email_contains or '(none)'}")
        logger.log("")

        users = list_all_users(
            qs_client,
            namespace=args.namespace,
            logger=logger,
            retry_attempts=args.retry_attempts,
            retry_base_seconds=args.retry_base_seconds,
        )
        if args.user_email_contains:
            needle = normalize(args.user_email_contains)
            users = [
                user
                for user in users
                if needle in normalize(str(user.get("Email", "")))
            ]
        if args.max_users is not None:
            users = users[: args.max_users]

        logger.log(f"Users in scope: {len(users)}")

        user_group_arns: Dict[str, Set[str]] = {}
        for index, user in enumerate(users, start=1):
            if index == 1 or index % 25 == 0 or index == len(users):
                logger.log(f"Collecting group memberships: {index}/{len(users)}")

            user_name = user.get("UserName")
            user_arn = user.get("Arn")
            if not user_name or not user_arn:
                continue

            groups = list_user_groups(
                qs_client,
                namespace=args.namespace,
                user_name=user_name,
                logger=logger,
                retry_attempts=args.retry_attempts,
                retry_base_seconds=args.retry_base_seconds,
            )
            user_group_arns[user_arn] = {
                group.get("Arn")
                for group in groups
                if isinstance(group.get("Arn"), str)
            }

        dashboard_summaries = get_all_summaries(
            qs_client.list_dashboards,
            QS_ACCOUNT_ID,
            "DashboardSummaryList",
        )
        if args.dashboard_name_contains:
            needle = normalize(args.dashboard_name_contains)
            dashboard_summaries = [
                item for item in dashboard_summaries if needle in normalize(item.get("Name", ""))
            ]

        logger.log(f"Dashboards in scope: {len(dashboard_summaries)}")

        principal_to_dashboard_ids: Dict[str, Set[str]] = {}
        dashboard_info: Dict[str, Dict[str, Any]] = {}
        dataset_arns: Set[str] = set()
        dashboard_errors: List[Dict[str, str]] = []

        for index, summary in enumerate(dashboard_summaries, start=1):
            if index == 1 or index % 25 == 0 or index == len(dashboard_summaries):
                logger.log(f"Inspecting dashboards: {index}/{len(dashboard_summaries)}")

            dashboard_id = summary.get("DashboardId")
            if not dashboard_id:
                continue

            try:
                permissions_response = call_with_retries(
                    logger,
                    f"describe_dashboard_permissions {dashboard_id}",
                    qs_client.describe_dashboard_permissions,
                    args.retry_attempts,
                    args.retry_base_seconds,
                    AwsAccountId=QS_ACCOUNT_ID,
                    DashboardId=dashboard_id,
                )
                definition_response = call_with_retries(
                    logger,
                    f"describe_dashboard_definition {dashboard_id}",
                    qs_client.describe_dashboard_definition,
                    args.retry_attempts,
                    args.retry_base_seconds,
                    AwsAccountId=QS_ACCOUNT_ID,
                    DashboardId=dashboard_id,
                )
            except Exception as exc:
                dashboard_errors.append(
                    {
                        "dashboard_id": dashboard_id,
                        "name": summary.get("Name", dashboard_id),
                        "error": str(exc),
                    }
                )
                continue

            permissions = permissions_response.get("Permissions", [])
            for permission in permissions:
                principal = permission.get("Principal")
                if not isinstance(principal, str):
                    continue
                principal_to_dashboard_ids.setdefault(principal, set()).add(dashboard_id)

            definition = definition_response.get("Definition", {})
            declarations = definition.get("DataSetIdentifierDeclarations", [])
            dashboard_dataset_arns: Set[str] = set()
            if isinstance(declarations, list):
                for declaration in declarations:
                    if not isinstance(declaration, dict):
                        continue
                    dataset_arn = declaration.get("DataSetArn")
                    if isinstance(dataset_arn, str):
                        dashboard_dataset_arns.add(dataset_arn)
                        dataset_arns.add(dataset_arn)

            dashboard_filters = find_field_filter_literals(definition, TARGET_FILTER_FIELD_ALIASES)
            dashboard_info[dashboard_id] = {
                "dashboard_id": dashboard_id,
                "dashboard_name": summary.get("Name", dashboard_id),
                "dataset_arns": sorted(dashboard_dataset_arns),
                "dashboard_filters": dashboard_filters,
            }

        described_dataset_cache: Dict[str, Dict[str, Any]] = {}
        dataset_rows: Dict[str, Dict[str, Any]] = {}
        dataset_errors: List[Dict[str, str]] = []

        for dataset_arn in sorted(dataset_arns):
            dataset_id = parse_dataset_id_from_arn(dataset_arn)
            if not dataset_id:
                dataset_errors.append(
                    {
                        "dataset_arn": dataset_arn,
                        "error": "Could not parse DataSetId from ARN.",
                    }
                )
                continue

            try:
                dataset = describe_data_set_safe(
                    qs_client,
                    dataset_id,
                    logger,
                    args.retry_attempts,
                    args.retry_base_seconds,
                )
                described_dataset_cache[dataset_id] = dataset
            except Exception as exc:
                dataset_errors.append(
                    {
                        "dataset_id": dataset_id,
                        "dataset_arn": dataset_arn,
                        "error": str(exc),
                    }
                )
                continue

        # Second pass so RLS dataset field hints can reuse the cache.
        for dataset_arn in sorted(dataset_arns):
            dataset_id = parse_dataset_id_from_arn(dataset_arn)
            if not dataset_id or dataset_id not in described_dataset_cache:
                continue
            dataset = described_dataset_cache[dataset_id]

            dataset_filters = extract_dataset_filters(dataset, TARGET_FILTER_FIELD_ALIASES)
            dataset_rls_hints = extract_rls_dataset_hint(
                dataset,
                TARGET_FILTER_FIELD_ALIASES,
                described_dataset_cache,
            )
            merged_dataset_filters = merge_filter_maps(dataset_filters, dataset_rls_hints)

            dataset_rows[dataset_arn] = {
                "dataset_id": dataset.get("DataSetId", dataset_id),
                "dataset_name": dataset.get("Name", dataset_id),
                "dataset_arn": dataset_arn,
                "dataset_filters": merged_dataset_filters,
            }

        output_rows: List[Dict[str, Any]] = []

        for user in users:
            user_arn = user.get("Arn")
            if not isinstance(user_arn, str):
                continue

            email = user.get("Email") or ""
            user_name = user.get("UserName") or ""
            user_display = email if email else user_name

            accessible_dashboard_ids = set(principal_to_dashboard_ids.get(user_arn, set()))
            for group_arn in user_group_arns.get(user_arn, set()):
                accessible_dashboard_ids.update(principal_to_dashboard_ids.get(group_arn, set()))

            for dashboard_id in sorted(accessible_dashboard_ids):
                dashboard = dashboard_info.get(dashboard_id)
                if not dashboard:
                    continue

                dashboard_name = dashboard.get("dashboard_name", dashboard_id)
                dashboard_filters = dashboard.get("dashboard_filters", {})
                dashboard_dataset_arns = dashboard.get("dataset_arns", [])

                if not dashboard_dataset_arns:
                    dashboard_filter_columns = build_filter_columns("dashboard", dashboard_filters)
                    empty_dataset_filters = {field: set() for field in OUTPUT_FILTER_KEYS}
                    dataset_filter_columns = build_filter_columns("dataset", empty_dataset_filters)
                    output_rows.append(
                        {
                            "username_email": user_display,
                            "dashboard_name": dashboard_name,
                            "dataset_name": "",
                            **dashboard_filter_columns,
                            **dataset_filter_columns,
                            "any_org_customer_fundraiser_campaign_filter_used": any_filter_used(
                                dashboard_filters,
                                empty_dataset_filters,
                            ),
                        }
                    )
                    continue

                for dataset_arn in dashboard_dataset_arns:
                    dataset_row = dataset_rows.get(dataset_arn, {})
                    dataset_filters = dataset_row.get("dataset_filters", {})
                    dashboard_filter_columns = build_filter_columns("dashboard", dashboard_filters)
                    dataset_filter_columns = build_filter_columns("dataset", dataset_filters)

                    output_rows.append(
                        {
                            "username_email": user_display,
                            "dashboard_name": dashboard_name,
                            "dataset_name": dataset_row.get("dataset_name", parse_dataset_id_from_arn(dataset_arn) or dataset_arn),
                            **dashboard_filter_columns,
                            **dataset_filter_columns,
                            "any_org_customer_fundraiser_campaign_filter_used": any_filter_used(
                                dashboard_filters,
                                dataset_filters,
                            ),
                        }
                    )

        output_rows.sort(
            key=lambda row: (
                normalize(str(row.get("username_email", ""))),
                normalize(str(row.get("dashboard_name", ""))),
                normalize(str(row.get("dataset_name", ""))),
            )
        )

        fieldnames = ["username_email", "dashboard_name", "dataset_name"]
        for field in OUTPUT_FILTER_KEYS:
            fieldnames.append(f"dashboard_{field}_filters")
        for field in OUTPUT_FILTER_KEYS:
            fieldnames.append(f"dataset_{field}_filters")
        fieldnames.append("any_org_customer_fundraiser_campaign_filter_used")

        with open(csv_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(output_rows)

        report_payload = {
            "account_id": QS_ACCOUNT_ID,
            "region": QS_REGION,
            "namespace": args.namespace,
            "dashboard_name_contains": args.dashboard_name_contains,
            "user_email_contains": args.user_email_contains,
            "users_in_scope": len(users),
            "dashboards_in_scope": len(dashboard_summaries),
            "rows": output_rows,
            "dashboard_errors": dashboard_errors,
            "dataset_errors": dataset_errors,
            "notes": [
                "Dashboard filter values are extracted from dashboard definition literals/parameters, not from each viewer's runtime interaction state.",
                "Dataset row values from RLS permission datasets are not exposed via QuickSight API. The report includes RLS dataset hints where fields are detectable.",
            ],
        }
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(report_payload, handle, indent=2)

        logger.log("")
        logger.log(f"Rows written: {len(output_rows)}")
        logger.log(f"Dashboard errors: {len(dashboard_errors)}")
        logger.log(f"Dataset errors: {len(dataset_errors)}")
        logger.log(f"CSV report: {csv_path}")
        logger.log(f"JSON report: {json_path}")
        logger.log(f"Text log: {text_path}")
    except (NoCredentialsError, PartialCredentialsError):
        raise SystemExit(
            "AWS credentials are missing/expired. Run awsume in this same terminal and retry."
        )


if __name__ == "__main__":
    main()
