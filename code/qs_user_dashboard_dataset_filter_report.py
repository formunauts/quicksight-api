import argparse
import csv
import json
import random
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


def _init_timing_bucket() -> Dict[str, float]:
    return {
        "calls": 0.0,
        "total_seconds": 0.0,
        "max_seconds": 0.0,
        "retry_count": 0.0,
        "retry_sleep_seconds": 0.0,
    }


def add_timing_duration(store: Dict[str, Dict[str, float]], operation: str, elapsed_seconds: float) -> None:
    bucket = store.setdefault(operation, _init_timing_bucket())
    bucket["calls"] += 1.0
    bucket["total_seconds"] += max(0.0, elapsed_seconds)
    bucket["max_seconds"] = max(bucket["max_seconds"], max(0.0, elapsed_seconds))


def add_timing_retry(store: Dict[str, Dict[str, float]], operation: str, sleep_seconds: float) -> None:
    bucket = store.setdefault(operation, _init_timing_bucket())
    bucket["retry_count"] += 1.0
    bucket["retry_sleep_seconds"] += max(0.0, sleep_seconds)

TARGET_FILTER_FIELD_ALIASES: Dict[str, List[str]] = {
    "organization_id": ["organization_id", "organisation_id", "organizationid", "organisationid"],
    "organization_name": ["organization_name", "organisation_name"],
    "customer_id": ["customer_id", "customerid"],
    "customer_name": ["customer_name"],
    "fundraiser_coder": ["fundraiser_coder", "fundraiser_code", "fundraisercoder", "fundraisercode"],
    "fundraiser_name": ["fundraiser_name"],
    "campaign_id": ["campaign_id"],
    "campaign_name": [
        "campaign_name",
        "campaignname",
        "Campaign name",
        "name[Campaign]",
        "Kampagne",
        "Kampagnen",
        "Campaigns",
        "Kampagnenname",
    ],
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
LITERAL_FILTER_VALUE_KEYS = {
    "Value",
    "CategoryValues",
    "RangeMinimumValue",
    "RangeMaximumValue",
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
SELECT_ALL_VALUE_MARKERS = {"all_values", "filter_all_values", "all"}


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


def parse_analysis_id_from_source_entity_arn(source_arn: str) -> Optional[str]:
    if not source_arn or "/" not in source_arn:
        return None
    resource = source_arn.split(":", 5)[-1]
    if not resource.startswith("analysis/"):
        return None
    analysis_id = resource.split("/", 1)[1]
    return analysis_id or None


def format_timestamp(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return ""


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
    operation = label.split(" ", 1)[0] if label else "unknown"
    attempt = 1
    while True:
        attempt_start = time.perf_counter()
        try:
            result = func(*args, **kwargs)
            add_timing_duration(API_OPERATION_TIMINGS, operation, time.perf_counter() - attempt_start)
            return result
        except Exception as exc:
            add_timing_duration(API_OPERATION_TIMINGS, operation, time.perf_counter() - attempt_start)
            if attempt >= retry_attempts or not is_retryable_error(exc):
                raise
            delay = retry_base_seconds * (2 ** (attempt - 1))
            jitter = random.uniform(0, max(0.0, delay * 0.25))
            wait_seconds = delay + jitter
            add_timing_retry(API_OPERATION_TIMINGS, operation, wait_seconds)
            logger.log(
                f"Retryable error during {label}: {exc}. Retrying in {wait_seconds:.1f}s "
                f"({attempt}/{retry_attempts - 1})."
            )
            time.sleep(wait_seconds)
            attempt += 1


API_OPERATION_TIMINGS: Dict[str, Dict[str, float]] = {}


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


def list_all_folders(
    qs_client,
    logger: Logger,
    retry_attempts: int,
    retry_base_seconds: float,
    between_page_seconds: float,
) -> List[Dict[str, Any]]:
    folders: List[Dict[str, Any]] = []
    next_token = None
    while True:
        kwargs: Dict[str, Any] = {"AwsAccountId": QS_ACCOUNT_ID}
        if next_token:
            kwargs["NextToken"] = next_token
        response = call_with_retries(
            logger,
            "list_folders",
            qs_client.list_folders,
            retry_attempts,
            retry_base_seconds,
            **kwargs,
        )
        folders.extend(response.get("FolderSummaryList", []))
        next_token = response.get("NextToken")
        if next_token and between_page_seconds > 0:
            time.sleep(between_page_seconds)
        if not next_token:
            return folders


def list_all_folder_members(
    qs_client,
    folder_id: str,
    logger: Logger,
    retry_attempts: int,
    retry_base_seconds: float,
    between_page_seconds: float,
) -> List[Dict[str, Any]]:
    members: List[Dict[str, Any]] = []
    next_token = None
    while True:
        kwargs: Dict[str, Any] = {
            "AwsAccountId": QS_ACCOUNT_ID,
            "FolderId": folder_id,
        }
        if next_token:
            kwargs["NextToken"] = next_token
        response = call_with_retries(
            logger,
            f"list_folder_members {folder_id}",
            qs_client.list_folder_members,
            retry_attempts,
            retry_base_seconds,
            **kwargs,
        )
        members.extend(response.get("FolderMemberList", []))
        next_token = response.get("NextToken")
        if next_token and between_page_seconds > 0:
            time.sleep(between_page_seconds)
        if not next_token:
            return members


def parse_dashboard_id_from_member_id(member_id: str) -> str:
    marker = "dashboard/"
    if marker in member_id:
        return member_id.split(marker, 1)[1]
    return member_id


def parse_folder_id_from_member_id(member_id: str) -> str:
    marker = "folder/"
    if marker in member_id:
        return member_id.split(marker, 1)[1]
    return member_id


def parse_member_arn_type_and_id(member_arn: str) -> Tuple[Optional[str], Optional[str]]:
    if ":" not in member_arn or "/" not in member_arn:
        return None, None
    resource = member_arn.split(":", 5)[-1]
    resource_type, _, resource_id = resource.partition("/")
    if not resource_type or not resource_id:
        return None, None
    return resource_type.upper(), resource_id


def infer_member_type_and_id(member: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    member_id = member.get("MemberId")
    if not isinstance(member_id, str):
        return None, None

    member_type = member.get("MemberType")
    if isinstance(member_type, str) and member_type:
        return member_type.upper(), member_id

    member_arn = member.get("MemberArn")
    if isinstance(member_arn, str) and member_arn:
        arn_type, arn_id = parse_member_arn_type_and_id(member_arn)
        if arn_type and arn_id:
            return arn_type, arn_id

    return None, member_id


def collect_folder_dashboard_members_recursive(
    qs_client,
    folder_id: str,
    logger: Logger,
    retry_attempts: int,
    retry_base_seconds: float,
    folder_dashboard_cache: Dict[str, Set[str]],
    folder_member_errors: List[Dict[str, str]],
    between_folder_member_seconds: float,
    active_path: Optional[Set[str]] = None,
) -> Set[str]:
    if folder_id in folder_dashboard_cache:
        return folder_dashboard_cache[folder_id]

    if active_path is None:
        active_path = set()
    if folder_id in active_path:
        return set()
    active_path.add(folder_id)

    dashboard_ids: Set[str] = set()
    try:
        members = list_all_folder_members(
            qs_client,
            folder_id,
            logger,
            retry_attempts,
            retry_base_seconds,
            between_folder_member_seconds,
        )
    except Exception as exc:
        folder_member_errors.append(
            {
                "folder_id": folder_id,
                "stage": "list_folder_members",
                "error": str(exc),
            }
        )
        folder_dashboard_cache[folder_id] = set()
        active_path.discard(folder_id)
        return set()

    for member in members:
        member_type, member_id = infer_member_type_and_id(member)
        if not member_id:
            continue

        if member_type == "DASHBOARD":
            dashboard_ids.add(parse_dashboard_id_from_member_id(member_id))
            continue

        if member_type == "FOLDER":
            child_folder_id = parse_folder_id_from_member_id(member_id)
            if not child_folder_id:
                continue
            dashboard_ids.update(
                collect_folder_dashboard_members_recursive(
                    qs_client,
                    child_folder_id,
                    logger,
                    retry_attempts,
                    retry_base_seconds,
                    folder_dashboard_cache,
                    folder_member_errors,
                    between_folder_member_seconds,
                    active_path,
                )
            )

    folder_dashboard_cache[folder_id] = dashboard_ids
    active_path.discard(folder_id)
    return dashboard_ids


def add_principal_dashboard_access(
    principal_access_map: Dict[str, Dict[str, Set[str]]],
    principal: str,
    dashboard_id: str,
    source: str,
) -> Tuple[bool, bool]:
    dashboard_map = principal_access_map.setdefault(principal, {})
    is_new_pair = dashboard_id not in dashboard_map
    source_set = dashboard_map.setdefault(dashboard_id, set())
    before_size = len(source_set)
    source_set.add(source)
    source_added = len(source_set) > before_size
    return is_new_pair, source_added


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
    active_only: bool = False,
    path: str = "Definition",
) -> Dict[str, Set[str]]:
    found: Dict[str, Set[str]] = {name: set() for name in field_aliases}

    if isinstance(obj, dict):
        if active_only and "FilterGroupId" in obj and isinstance(obj.get("Filters"), list):
            status = normalize(str(obj.get("Status", "ENABLED")))
            if status == "disabled":
                return found

        if is_filter_like(obj):
            column_names = collect_strings_for_keys(obj.get("Column"), {"ColumnName"})
            values = collect_strings_for_keys(obj, FILTER_VALUE_KEYS)
            literal_values_raw = collect_strings_for_keys(obj, LITERAL_FILTER_VALUE_KEYS)
            parameter_names = collect_strings_for_keys(obj, {"ParameterName"})
            select_all_options = {
                normalize(value)
                for value in collect_strings_for_keys(obj, {"SelectAllOptions"})
                if isinstance(value, str)
            }

            for field_name, aliases in field_aliases.items():
                if not field_name_matches_any(column_names, aliases):
                    continue

                alias_norms = {normalize(alias) for alias in aliases}
                literal_values = [
                    value
                    for value in literal_values_raw
                    if normalize(value) not in alias_norms
                    and normalize(value) not in SELECT_ALL_VALUE_MARKERS
                    and not re.fullmatch(r"\$\{[^}]+\}", value.strip())
                ]

                if active_only:
                    # "Active" here means a concrete literal constraint is saved in the dashboard definition.
                    # Runtime viewer interaction state is not exposed by QuickSight APIs.
                    if select_all_options.intersection(SELECT_ALL_VALUE_MARKERS):
                        continue
                    if literal_values:
                        found[field_name].update(literal_values)
                    continue

                if literal_values:
                    found[field_name].update(literal_values)
                    continue

                parameter_values = [
                    value
                    for value in values
                    if normalize(value) not in alias_norms
                    and normalize(value) not in SELECT_ALL_VALUE_MARKERS
                ]
                if parameter_values:
                    found[field_name].update(parameter_values)
                elif parameter_names:
                    found[field_name].update(
                        {f"PARAMETER:{name}" for name in sorted(parameter_names)}
                    )

        for key, child in obj.items():
            child_found = find_field_filter_literals(
                child,
                field_aliases,
                active_only=active_only,
                path=f"{path}.{key}",
            )
            for field_name, values in child_found.items():
                found[field_name].update(values)
        return found

    if isinstance(obj, list):
        for index, child in enumerate(obj):
            child_found = find_field_filter_literals(
                child,
                field_aliases,
                active_only=active_only,
                path=f"{path}[{index}]",
            )
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
            r"(?is)\bwhere\b(.*?)(?:\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|$)",
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


def empty_filter_map() -> Dict[str, Set[str]]:
    return {field: set() for field in OUTPUT_FILTER_KEYS}


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
        "--user-contains",
        nargs="+",
        help=(
            "Optional case-insensitive username/email substring filters. "
            "A user is kept when any provided pattern matches either UserName or Email."
        ),
    )
    parser.add_argument(
        "--exclude-user-contains",
        nargs="+",
        help=(
            "Optional case-insensitive username/email substring exclusion filters. "
            "A user is removed when any provided pattern matches either UserName or Email."
        ),
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
    parser.add_argument(
        "--between-folder-member-seconds",
        type=float,
        default=0.1,
        help="Delay between paginated folder-member calls and nested folder scans to reduce throttling.",
    )
    parser.add_argument(
        "--between-folder-permission-seconds",
        type=float,
        default=0.1,
        help="Delay before each folder permission lookup to reduce throttling.",
    )
    parser.add_argument(
        "--access-check-only",
        action="store_true",
        help=(
            "Fast mode: resolve user-to-dashboard access only. "
            "Skips dashboard definition and dataset/filter extraction work."
        ),
    )
    parser.add_argument(
        "--skip-direct-dashboard-permissions",
        action="store_true",
        help=(
            "Skip direct dashboard permission lookups and only evaluate folder-shared access. "
            "Useful for quickly validating folder inheritance behavior."
        ),
    )
    parser.add_argument(
        "--dashboard-active-filters-only",
        action="store_true",
        help=(
            "Only include dashboard filters that have concrete literal values saved in the dashboard definition. "
            "This suppresses parameter-only and select-all style filter hints."
        ),
    )
    args = parser.parse_args()

    if args.retry_attempts < 1:
        raise SystemExit("--retry-attempts must be at least 1.")
    if args.retry_base_seconds < 0:
        raise SystemExit("--retry-base-seconds must be >= 0.")
    if args.between_folder_member_seconds < 0:
        raise SystemExit("--between-folder-member-seconds must be >= 0.")
    if args.between_folder_permission_seconds < 0:
        raise SystemExit("--between-folder-permission-seconds must be >= 0.")
    if args.max_users is not None and args.max_users < 1:
        raise SystemExit("--max-users must be >= 1.")

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    text_path = build_log_path("user_dashboard_dataset_filter_report", "txt")
    json_path = build_log_path("user_dashboard_dataset_filter_report", "json")
    csv_path = build_log_path("user_dashboard_dataset_filter_report", "csv")
    logger = Logger(text_path, "QUICKSIGHT USER DASHBOARD DATASET FILTER REPORT")

    try:
        total_start = time.perf_counter()
        stage_timings: Dict[str, float] = {}
        API_OPERATION_TIMINGS.clear()

        qs_client = create_quicksight_client()

        logger.log(f"Account: {QS_ACCOUNT_ID}")
        logger.log(f"Region: {QS_REGION}")
        logger.log(f"Namespace: {args.namespace}")
        logger.log(f"Dashboard name filter: {args.dashboard_name_contains or '(none)'}")
        logger.log(f"User email filter: {args.user_email_contains or '(none)'}")
        logger.log(f"User pattern filter: {', '.join(args.user_contains) if args.user_contains else '(none)'}")
        logger.log(
            "Exclude user pattern filter: "
            f"{', '.join(args.exclude_user_contains) if args.exclude_user_contains else '(none)'}"
        )
        logger.log(f"Access-check-only mode: {args.access_check_only}")
        logger.log(f"Skip direct dashboard permissions: {args.skip_direct_dashboard_permissions}")
        logger.log(f"Dashboard active-filters-only mode: {args.dashboard_active_filters_only}")
        logger.log("")

        stage_start = time.perf_counter()
        users = list_all_users(
            qs_client,
            namespace=args.namespace,
            logger=logger,
            retry_attempts=args.retry_attempts,
            retry_base_seconds=args.retry_base_seconds,
        )
        stage_timings["list_all_users"] = time.perf_counter() - stage_start
        user_patterns: List[str] = []
        if args.user_email_contains:
            user_patterns.append(args.user_email_contains)
        if args.user_contains:
            user_patterns.extend(args.user_contains)

        if user_patterns:
            normalized_patterns = [normalize(pattern) for pattern in user_patterns if pattern.strip()]

            def _matches_user(user: Dict[str, Any]) -> bool:
                email = normalize(str(user.get("Email", "")))
                user_name = normalize(str(user.get("UserName", "")))
                return any(pattern in email or pattern in user_name for pattern in normalized_patterns)

            users = [user for user in users if _matches_user(user)]

        if args.exclude_user_contains:
            excluded_patterns = [
                normalize(pattern)
                for pattern in args.exclude_user_contains
                if pattern.strip()
            ]

            def _matches_excluded_user(user: Dict[str, Any]) -> bool:
                email = normalize(str(user.get("Email", "")))
                user_name = normalize(str(user.get("UserName", "")))
                return any(pattern in email or pattern in user_name for pattern in excluded_patterns)

            users = [user for user in users if not _matches_excluded_user(user)]
        if args.max_users is not None:
            users = users[: args.max_users]

        logger.log(f"Users in scope: {len(users)}")

        stage_start = time.perf_counter()
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
        stage_timings["collect_user_group_memberships"] = time.perf_counter() - stage_start

        stage_start = time.perf_counter()
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
        stage_timings["list_dashboards"] = time.perf_counter() - stage_start

        logger.log(f"Dashboards in scope: {len(dashboard_summaries)}")

        principal_to_dashboard_access: Dict[str, Dict[str, Set[str]]] = {}
        direct_access_pairs_added = 0
        folder_access_pairs_added = 0
        folder_access_sources_added = 0
        dashboard_info: Dict[str, Dict[str, Any]] = {}
        analysis_name_cache: Dict[str, str] = {}
        dataset_arns: Set[str] = set()
        dashboard_errors: List[Dict[str, str]] = []

        stage_start = time.perf_counter()
        for index, summary in enumerate(dashboard_summaries, start=1):
            if index == 1 or index % 25 == 0 or index == len(dashboard_summaries):
                logger.log(f"Inspecting dashboards: {index}/{len(dashboard_summaries)}")

            dashboard_id = summary.get("DashboardId")
            if not dashboard_id:
                continue

            definition: Dict[str, Any] = {}
            dashboard_dataset_arns: Set[str] = set()
            dashboard_filters = empty_filter_map()
            source_analysis_name = ""
            dashboard_last_updated_time_best_effort = format_timestamp(
                summary.get("LastPublishedTime") or summary.get("LastUpdatedTime")
            )

            try:
                dashboard_response = call_with_retries(
                    logger,
                    f"describe_dashboard {dashboard_id}",
                    qs_client.describe_dashboard,
                    args.retry_attempts,
                    args.retry_base_seconds,
                    AwsAccountId=QS_ACCOUNT_ID,
                    DashboardId=dashboard_id,
                )
                dashboard_details = dashboard_response.get("Dashboard", {})
                version = dashboard_details.get("Version", {}) or {}
                source_entity_arn = version.get("SourceEntityArn")
                analysis_id = parse_analysis_id_from_source_entity_arn(str(source_entity_arn or ""))
                if analysis_id:
                    if analysis_id not in analysis_name_cache:
                        try:
                            analysis_response = call_with_retries(
                                logger,
                                f"describe_analysis {analysis_id}",
                                qs_client.describe_analysis,
                                args.retry_attempts,
                                args.retry_base_seconds,
                                AwsAccountId=QS_ACCOUNT_ID,
                                AnalysisId=analysis_id,
                            )
                            analysis_name_cache[analysis_id] = analysis_response.get("Analysis", {}).get("Name", "")
                        except Exception as exc:
                            analysis_name_cache[analysis_id] = ""
                            dashboard_errors.append(
                                {
                                    "dashboard_id": dashboard_id,
                                    "name": summary.get("Name", dashboard_id),
                                    "error": f"Could not resolve source analysis {analysis_id}: {exc}",
                                }
                            )
                    source_analysis_name = analysis_name_cache.get(analysis_id, "")

                if not dashboard_last_updated_time_best_effort:
                    dashboard_last_updated_time_best_effort = format_timestamp(
                        version.get("CreatedTime")
                        or dashboard_details.get("LastPublishedTime")
                        or dashboard_details.get("LastUpdatedTime")
                    )
            except Exception as exc:
                dashboard_errors.append(
                    {
                        "dashboard_id": dashboard_id,
                        "name": summary.get("Name", dashboard_id),
                        "error": f"describe_dashboard failed: {exc}",
                    }
                )

            if not args.skip_direct_dashboard_permissions:
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
                    is_new_pair, _ = add_principal_dashboard_access(
                        principal_to_dashboard_access,
                        principal,
                        dashboard_id,
                        "direct",
                    )
                    if is_new_pair:
                        direct_access_pairs_added += 1

            if not args.access_check_only:
                try:
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

                definition = definition_response.get("Definition", {})
                declarations = definition.get("DataSetIdentifierDeclarations", [])
                if isinstance(declarations, list):
                    for declaration in declarations:
                        if not isinstance(declaration, dict):
                            continue
                        dataset_arn = declaration.get("DataSetArn")
                        if isinstance(dataset_arn, str):
                            dashboard_dataset_arns.add(dataset_arn)
                            dataset_arns.add(dataset_arn)

                dashboard_filters = find_field_filter_literals(
                    definition.get("FilterGroups", []) if args.dashboard_active_filters_only else definition,
                    TARGET_FILTER_FIELD_ALIASES,
                    active_only=bool(args.dashboard_active_filters_only),
                )

            dashboard_info[dashboard_id] = {
                "dashboard_id": dashboard_id,
                "dashboard_name": summary.get("Name", dashboard_id),
                "analysis_name": source_analysis_name,
                "dashboard_last_updated_time": dashboard_last_updated_time_best_effort,
                "dataset_arns": sorted(dashboard_dataset_arns),
                "dashboard_filters": dashboard_filters,
            }
            stage_timings["inspect_dashboards"] = time.perf_counter() - stage_start

        dashboard_ids_in_scope = {summary.get("DashboardId") for summary in dashboard_summaries if summary.get("DashboardId")}
        folder_errors: List[Dict[str, str]] = []
        folder_member_errors: List[Dict[str, str]] = []
        folder_dashboard_cache: Dict[str, Set[str]] = {}
        folders_with_dashboard_members = 0
        folder_dashboard_links_in_scope = 0

        stage_start = time.perf_counter()
        try:
            folders = list_all_folders(
                qs_client,
                logger,
                args.retry_attempts,
                args.retry_base_seconds,
                args.between_folder_member_seconds,
            )
            logger.log(f"Folders in scope scan: {len(folders)}")
        except Exception as exc:
            folders = []
            folder_errors.append(
                {
                    "folder_id": "",
                    "stage": "list_folders",
                    "error": str(exc),
                }
            )
        stage_timings["list_folders"] = time.perf_counter() - stage_start

        stage_start = time.perf_counter()
        for index, folder_summary in enumerate(folders, start=1):
            if index == 1 or index % 25 == 0 or index == len(folders):
                logger.log(f"Inspecting folders: {index}/{len(folders)}")

            folder_id = folder_summary.get("FolderId")
            if not isinstance(folder_id, str) or not folder_id:
                continue

            folder_dashboard_ids = collect_folder_dashboard_members_recursive(
                qs_client,
                folder_id,
                logger,
                args.retry_attempts,
                args.retry_base_seconds,
                folder_dashboard_cache,
                folder_member_errors,
                args.between_folder_member_seconds,
            )
            scoped_dashboard_ids = folder_dashboard_ids.intersection(dashboard_ids_in_scope)
            if not scoped_dashboard_ids:
                continue
            folders_with_dashboard_members += 1
            folder_dashboard_links_in_scope += len(scoped_dashboard_ids)

            try:
                if args.between_folder_permission_seconds > 0:
                    time.sleep(args.between_folder_permission_seconds)
                folder_permissions_response = call_with_retries(
                    logger,
                    f"describe_folder_permissions {folder_id}",
                    qs_client.describe_folder_permissions,
                    args.retry_attempts,
                    args.retry_base_seconds,
                    AwsAccountId=QS_ACCOUNT_ID,
                    FolderId=folder_id,
                )
            except Exception as exc:
                folder_errors.append(
                    {
                        "folder_id": folder_id,
                        "stage": "describe_folder_permissions",
                        "error": str(exc),
                    }
                )
                continue

            for permission in folder_permissions_response.get("Permissions", []):
                principal = permission.get("Principal")
                if not isinstance(principal, str):
                    continue
                for dashboard_id in scoped_dashboard_ids:
                    is_new_pair, source_added = add_principal_dashboard_access(
                        principal_to_dashboard_access,
                        principal,
                        dashboard_id,
                        "folder",
                    )
                    if is_new_pair:
                        folder_access_pairs_added += 1
                    if source_added:
                        folder_access_sources_added += 1
        stage_timings["inspect_folders"] = time.perf_counter() - stage_start

        described_dataset_cache: Dict[str, Dict[str, Any]] = {}
        dataset_rows: Dict[str, Dict[str, Any]] = {}
        dataset_errors: List[Dict[str, str]] = []

        stage_start = time.perf_counter()
        if not args.access_check_only:
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
            stage_timings["inspect_datasets"] = time.perf_counter() - stage_start

            stage_start = time.perf_counter()
        output_rows: List[Dict[str, Any]] = []

        for user in users:
            user_arn = user.get("Arn")
            if not isinstance(user_arn, str):
                continue

            email = user.get("Email") or ""
            user_name = user.get("UserName") or ""
            user_display = email if email else user_name

            dashboard_access_by_id: Dict[str, Set[str]] = {}

            for dashboard_id, sources in principal_to_dashboard_access.get(user_arn, {}).items():
                dashboard_access_by_id.setdefault(dashboard_id, set()).update(sources)
            for group_arn in user_group_arns.get(user_arn, set()):
                for dashboard_id, sources in principal_to_dashboard_access.get(group_arn, {}).items():
                    dashboard_access_by_id.setdefault(dashboard_id, set()).update(sources)

            accessible_dashboard_ids = set(dashboard_access_by_id.keys())

            for dashboard_id in sorted(accessible_dashboard_ids):
                dashboard = dashboard_info.get(dashboard_id)
                if not dashboard:
                    continue

                dashboard_name = dashboard.get("dashboard_name", dashboard_id)
                analysis_name = dashboard.get("analysis_name", "")
                dashboard_last_updated_time = dashboard.get("dashboard_last_updated_time", "")
                dashboard_filters = dashboard.get("dashboard_filters", {})
                dashboard_dataset_arns = dashboard.get("dataset_arns", [])
                access_sources = dashboard_access_by_id.get(dashboard_id, set())
                if access_sources == {"direct"}:
                    access_via = "direct"
                elif access_sources == {"folder"}:
                    access_via = "folder"
                elif "direct" in access_sources and "folder" in access_sources:
                    access_via = "direct+folder"
                else:
                    access_via = "unknown"

                if args.access_check_only or not dashboard_dataset_arns:
                    dashboard_filter_columns = build_filter_columns("dashboard", dashboard_filters)
                    empty_dataset_filters = empty_filter_map()
                    dataset_filter_columns = build_filter_columns("dataset", empty_dataset_filters)
                    output_rows.append(
                        {
                            "username_email": user_display,
                            "dashboard_name": dashboard_name,
                            "analysis_name": analysis_name,
                            "dashboard_last_updated_time": dashboard_last_updated_time,
                            "dataset_name": "",
                            "access_via": access_via,
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
                            "analysis_name": analysis_name,
                            "dashboard_last_updated_time": dashboard_last_updated_time,
                            "dataset_name": dataset_row.get("dataset_name", parse_dataset_id_from_arn(dataset_arn) or dataset_arn),
                            "access_via": access_via,
                            **dashboard_filter_columns,
                            **dataset_filter_columns,
                            "any_org_customer_fundraiser_campaign_filter_used": any_filter_used(
                                dashboard_filters,
                                dataset_filters,
                            ),
                        }
                    )
        stage_timings["build_output_rows"] = time.perf_counter() - stage_start

        stage_start = time.perf_counter()
        output_rows.sort(
            key=lambda row: (
                normalize(str(row.get("username_email", ""))),
                normalize(str(row.get("dashboard_name", ""))),
                normalize(str(row.get("dataset_name", ""))),
            )
        )
        stage_timings["sort_output_rows"] = time.perf_counter() - stage_start

        fieldnames = [
            "username_email",
            "dashboard_name",
            "analysis_name",
            "dashboard_last_updated_time",
            "dataset_name",
            "access_via",
        ]
        for field in OUTPUT_FILTER_KEYS:
            fieldnames.append(f"dashboard_{field}_filters")
        for field in OUTPUT_FILTER_KEYS:
            fieldnames.append(f"dataset_{field}_filters")
        fieldnames.append("any_org_customer_fundraiser_campaign_filter_used")

        stage_start = time.perf_counter()
        with open(csv_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(output_rows)
        stage_timings["write_csv"] = time.perf_counter() - stage_start

        total_runtime_seconds = time.perf_counter() - total_start
        api_timing_rows = []
        for operation, values in API_OPERATION_TIMINGS.items():
            calls = int(values.get("calls", 0.0))
            total_seconds = values.get("total_seconds", 0.0)
            max_seconds = values.get("max_seconds", 0.0)
            retry_count = int(values.get("retry_count", 0.0))
            retry_sleep_seconds = values.get("retry_sleep_seconds", 0.0)
            avg_seconds = (total_seconds / calls) if calls else 0.0
            api_timing_rows.append(
                {
                    "operation": operation,
                    "calls": calls,
                    "total_seconds": round(total_seconds, 3),
                    "avg_seconds": round(avg_seconds, 3),
                    "max_seconds": round(max_seconds, 3),
                    "retry_count": retry_count,
                    "retry_sleep_seconds": round(retry_sleep_seconds, 3),
                }
            )
        api_timing_rows.sort(key=lambda row: row["total_seconds"], reverse=True)

        report_payload = {
            "account_id": QS_ACCOUNT_ID,
            "region": QS_REGION,
            "namespace": args.namespace,
            "dashboard_name_contains": args.dashboard_name_contains,
            "user_email_contains": args.user_email_contains,
            "user_contains": args.user_contains,
            "exclude_user_contains": args.exclude_user_contains,
            "dashboard_active_filters_only": bool(args.dashboard_active_filters_only),
            "users_in_scope": len(users),
            "dashboards_in_scope": len(dashboard_summaries),
            "rows": output_rows,
            "dashboard_errors": dashboard_errors,
            "folder_errors": folder_errors,
            "folder_member_errors": folder_member_errors,
            "dataset_errors": dataset_errors,
            "access_resolution_stats": {
                "direct_access_pairs_added": direct_access_pairs_added,
                "folder_access_pairs_added": folder_access_pairs_added,
                "folder_access_sources_added": folder_access_sources_added,
                "folders_scanned": len(folders),
                "folders_with_dashboard_members": folders_with_dashboard_members,
                "folder_dashboard_links_in_scope": folder_dashboard_links_in_scope,
            },
            "timing_seconds": {
                "total_runtime": round(total_runtime_seconds, 3),
                "stages": {name: round(value, 3) for name, value in stage_timings.items()},
                "api_operations": api_timing_rows,
            },
            "notes": [
                "Dashboard filter values are extracted from dashboard definition literals/parameters, not from each viewer's runtime interaction state.",
                "When --dashboard-active-filters-only is enabled, dashboard filters are only reported when concrete literal values are saved in definition objects. Parameter-driven and select-all states are intentionally ignored.",
                "Dashboard access includes both direct dashboard permissions and inherited access from shared-folder permissions when the dashboard is a member of those folders.",
                "Dataset row values from RLS permission datasets are not exposed via QuickSight API. The report includes RLS dataset hints where fields are detectable.",
                "dashboard_last_updated_time is a best-effort dashboard metadata timestamp (LastPublishedTime/LastUpdatedTime/version time), not a per-user view/access timestamp.",
                "When --access-check-only is enabled, dashboard definitions and dataset filter extraction are intentionally skipped for speed.",
                "When --skip-direct-dashboard-permissions is enabled, direct dashboard grants are intentionally omitted and only folder-derived access is evaluated.",
            ],
        }
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(report_payload, handle, indent=2)

        logger.log("")
        logger.log(f"Total runtime: {total_runtime_seconds:.2f}s")
        logger.log("Stage timings (seconds):")
        for stage_name, seconds in sorted(stage_timings.items(), key=lambda item: item[1], reverse=True):
            logger.log(f"  - {stage_name}: {seconds:.2f}")
        logger.log("Top API operation timings (seconds):")
        for row in api_timing_rows[:10]:
            logger.log(
                "  - "
                f"{row['operation']}: calls={row['calls']}, "
                f"total={row['total_seconds']:.2f}, avg={row['avg_seconds']:.2f}, "
                f"max={row['max_seconds']:.2f}, retries={row['retry_count']}, "
                f"retry_sleep={row['retry_sleep_seconds']:.2f}"
            )
        logger.log(f"Rows written: {len(output_rows)}")
        logger.log(f"Dashboard errors: {len(dashboard_errors)}")
        logger.log(f"Folder errors: {len(folder_errors)}")
        logger.log(f"Folder member errors: {len(folder_member_errors)}")
        logger.log(
            "Access-resolution stats: "
            f"direct_pairs={direct_access_pairs_added}, "
            f"folder_pairs={folder_access_pairs_added}, "
            f"folder_sources={folder_access_sources_added}, "
            f"folders_with_dashboards={folders_with_dashboard_members}, "
            f"folder_dashboard_links={folder_dashboard_links_in_scope}"
        )
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
