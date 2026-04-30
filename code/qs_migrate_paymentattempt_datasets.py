import argparse
import copy
import datetime
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv


load_dotenv()

QS_ACCOUNT_ID = os.getenv("QS_AWS_ACCOUNT_ID")
REGION = os.getenv("QS_AWS_REGION", "eu-central-1")
ROOT_DIR = sys.path[0].rsplit("\\code", 1)[0]
LOG_DIR = os.path.join(ROOT_DIR, "logs")
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

PAYMENTATTEMPT_PATTERN = re.compile(r"paymentattempt", re.IGNORECASE)
LEGACY_FIELD_PATTERNS = [
    re.compile(r"\bdonation_object_id(?:\[[^\]]+\])?\b", re.IGNORECASE),
    re.compile(r"\bdonation_content_type_id(?:\[[^\]]+\])?\b", re.IGNORECASE),
]
PRESERVE_EXPOSED_NAME_RISK_PATTERNS = [
    re.compile(r"\bdonation_content_type_id(?:\[[^\]]+\])?\b", re.IGNORECASE),
]

SQL_FILTER_CLAUSE = r"""
    \(?\s*
    (?:
        (?:[A-Za-z_][A-Za-z0-9_]*|"[^"]+")\s*\.\s*
    )?
    "?donation_content_type_id"?
    \s*=\s*
    ['"]?25['"]?
    (?:\s*::\s*[A-Za-z_][A-Za-z0-9_]*)?
    \s*\)?
"""

SQL_TRAILING_KEYWORDS = r"(GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION|QUALIFY|WINDOW)"
PAYMENTATTEMPT_SELECT_STAR_PATTERN = re.compile(
    r"(?is)^\s*SELECT\s+DISTINCT\s+ON\s*\(\s*donation_object_id\s*\)\s+\*\s+FROM\s+bureau_paymentattempt\b"
)
PAYMENTATTEMPT_SELECT_STAR_GENERIC_PATTERN = re.compile(
    r"(?is)^\s*SELECT\s*\*\s*FROM\s+bureau_paymentattempt\b"
)
RAISENOW_EPMSWEBHOOK_PATTERN = re.compile(r"\braisenow_epmswebhook\b", re.IGNORECASE)
LEADING_WHERE_PATTERN = re.compile(
    rf"(?isx)\bWHERE\s+({SQL_FILTER_CLAUSE})\s+AND\s+"
)
TRAILING_WHERE_PATTERN = re.compile(
    rf"(?isx)\bWHERE\s+({SQL_FILTER_CLAUSE})(?=\s*(?:{SQL_TRAILING_KEYWORDS})\b|\s*$)"
)
MIDDLE_AND_PATTERN = re.compile(
    rf"(?isx)\s+AND\s+({SQL_FILTER_CLAUSE})(?=\s+(?:AND\b|{SQL_TRAILING_KEYWORDS}\b)|\s*$)"
)
MIDDLE_OR_PATTERN = re.compile(
    rf"(?isx)\s+OR\s+({SQL_FILTER_CLAUSE})(?=\s+(?:OR\b|{SQL_TRAILING_KEYWORDS}\b)|\s*$)"
)

SKIP = object()


class Logger:
    def __init__(self, filename: str):
        self.filename = filename
        with open(self.filename, "w", encoding="utf-8") as handle:
            handle.write("QUICKSIGHT PAYMENTATTEMPT DATASET MIGRATION\n")
            handle.write(f"Generated on: {datetime.datetime.now().isoformat()}\n")
            handle.write("=" * 80 + "\n\n")

    def log(self, message: str) -> None:
        print(message)
        with open(self.filename, "a", encoding="utf-8") as handle:
            handle.write(message + "\n")


def require_env(name: str, value: Optional[str]) -> str:
    if value:
        return value
    raise SystemExit(f"Missing required environment variable: {name}")


def json_default(value: Any) -> str:
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    raise TypeError(f"Unsupported type: {type(value)!r}")


def get_all_summaries(func, account_id: str, key_name: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    next_token = None
    while True:
        kwargs = {"AwsAccountId": account_id}
        if next_token:
            kwargs["NextToken"] = next_token
        response = func(**kwargs)
        items.extend(response.get(key_name, []))
        next_token = response.get("NextToken")
        if not next_token:
            return items


def select_datasets(
    all_datasets: List[Dict[str, Any]],
    target_ids: Optional[List[str]],
    target_names: Optional[List[str]],
    name_contains: Optional[str],
) -> List[Dict[str, Any]]:
    selected = []
    exact_ids = set(target_ids or [])
    exact_names = set(target_names or [])
    substring = name_contains.lower() if name_contains else None

    for dataset in all_datasets:
        name = dataset["Name"]
        dataset_id = dataset["DataSetId"]
        exact_id_match = dataset_id in exact_ids if exact_ids else True
        exact_match = name in exact_names if exact_names else True
        substring_match = substring in name.lower() if substring else True
        if exact_id_match and exact_match and substring_match:
            selected.append(dataset)
    return selected


def build_backup_dir() -> str:
    backup_dir = os.path.join(LOG_DIR, f"paymentattempt_dataset_backups_{TIMESTAMP}")
    os.makedirs(backup_dir, exist_ok=True)
    return backup_dir


def build_plan_file() -> str:
    return os.path.join(LOG_DIR, f"paymentattempt_dataset_plan_{TIMESTAMP}.json")


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "dataset"


def build_placeholder_restore_file(plan_path: str, plan: Dict[str, Any]) -> str:
    base, _ = os.path.splitext(plan_path)
    placeholder_dataset_names = [
        dataset.get("name")
        for dataset in plan.get("datasets", [])
        if dataset.get("placeholder_changes") or dataset.get("preexisting_placeholder_columns")
    ]
    unique_names = [
        name for name in dict.fromkeys(placeholder_dataset_names) if isinstance(name, str) and name
    ]
    if len(unique_names) == 1:
        return f"{base}__{slugify(unique_names[0])}_placeholder_restore.txt"
    return f"{base}_placeholder_restore.txt"


def contains_legacy_field(text: str, preserve_exposed_names: bool = False) -> bool:
    patterns = (
        PRESERVE_EXPOSED_NAME_RISK_PATTERNS
        if preserve_exposed_names
        else LEGACY_FIELD_PATTERNS
    )
    return any(pattern.search(text) for pattern in patterns)


def is_legacy_donation_object_field(value: Optional[str]) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"donation_object_id(?:\[[^\]]+\])?", value, re.IGNORECASE)
    )


def is_legacy_donation_content_type_field(value: Optional[str]) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"donation_content_type_id(?:\[[^\]]+\])?", value, re.IGNORECASE)
    )


def get_target_field_name(
    value: Optional[str],
    preserve_exposed_names: bool = False,
) -> Optional[str]:
    if not isinstance(value, str):
        return value
    if is_legacy_donation_content_type_field(value):
        return None
    if is_legacy_donation_object_field(value):
        if preserve_exposed_names:
            return value
        return re.sub(r"(?i)^donation_object_id", "donation_id", value, count=1)
    return value


def rewrite_field_text(text: str, preserve_exposed_names: bool = False) -> str:
    if preserve_exposed_names:
        return text
    updated = re.sub(
        r"\{donation_object_id(\[[^\]]+\])?\}",
        lambda match: "{donation_id" + (match.group(1) or "") + "}",
        text,
        flags=re.IGNORECASE,
    )
    updated = re.sub(
        r"\bdonation_object_id(\[[^\]]+\])?\b",
        lambda match: "donation_id" + (match.group(1) or ""),
        updated,
        flags=re.IGNORECASE,
    )
    return updated


def load_dataset_targets_from_plan(plan_file: str) -> Tuple[List[str], List[str]]:
    with open(plan_file, "r", encoding="utf-8") as handle:
        plan = json.load(handle)

    names: List[str] = []
    ids: List[str] = []
    for dataset in plan.get("datasets", []):
        name = dataset.get("name")
        dataset_id = dataset.get("data_set_id")
        if name:
            names.append(name)
        if dataset_id:
            ids.append(dataset_id)
    return names, ids


def get_oversized_calculated_columns(dataset: Dict[str, Any]) -> List[Dict[str, Any]]:
    oversized: List[Dict[str, Any]] = []
    output_types = {
        column.get("Name"): column.get("Type")
        for column in dataset.get("OutputColumns", [])
        if isinstance(column, dict) and isinstance(column.get("Name"), str)
    }

    logical_map = dataset.get("LogicalTableMap")
    if not isinstance(logical_map, dict):
        return oversized

    for logical_table_id, logical_table in logical_map.items():
        transforms = logical_table.get("DataTransforms")
        if not isinstance(transforms, list):
            continue

        for transform_index, transform in enumerate(transforms):
            operation = transform.get("CreateColumnsOperation")
            if not operation:
                continue

            for column_index, column in enumerate(operation.get("Columns", [])):
                expression = column.get("Expression")
                if not isinstance(expression, str):
                    continue
                if len(expression) <= 4096:
                    continue
                oversized.append(
                    {
                        "column_name": column.get("ColumnName"),
                        "output_type": output_types.get(column.get("ColumnName")),
                        "expression_length": len(expression),
                        "original_expression": expression,
                        "path": (
                            f"LogicalTableMap.{logical_table_id}.DataTransforms[{transform_index}]"
                            f".CreateColumnsOperation.Columns[{column_index}]"
                        ),
                        "expression_path": (
                            f"LogicalTableMap.{logical_table_id}.DataTransforms[{transform_index}]"
                            f".CreateColumnsOperation.Columns[{column_index}].Expression"
                        ),
                    }
                )

    return oversized


def placeholder_expression_for_type(output_type: Optional[str]) -> Optional[str]:
    if output_type == "STRING":
        return "'TEMP_MIGRATION_PLACEHOLDER'"
    if output_type in {"DECIMAL", "INTEGER"}:
        return "0"
    return None


def get_preexisting_placeholder_columns(dataset: Dict[str, Any]) -> List[Dict[str, Any]]:
    placeholder_columns: List[Dict[str, Any]] = []
    output_types = {
        column.get("Name"): column.get("Type")
        for column in dataset.get("OutputColumns", [])
        if isinstance(column, dict) and isinstance(column.get("Name"), str)
    }

    logical_map = dataset.get("LogicalTableMap")
    if not isinstance(logical_map, dict):
        return placeholder_columns

    for logical_table_id, logical_table in logical_map.items():
        transforms = logical_table.get("DataTransforms")
        if not isinstance(transforms, list):
            continue

        for transform_index, transform in enumerate(transforms):
            operation = transform.get("CreateColumnsOperation")
            if not operation:
                continue

            for column_index, column in enumerate(operation.get("Columns", [])):
                column_name = column.get("ColumnName")
                expression = column.get("Expression")
                if not isinstance(column_name, str) or not isinstance(expression, str):
                    continue
                output_type = output_types.get(column_name)
                placeholder_expression = placeholder_expression_for_type(output_type)
                if placeholder_expression is None or expression != placeholder_expression:
                    continue
                placeholder_columns.append(
                    {
                        "column_name": column_name,
                        "output_type": output_type,
                        "expression_path": (
                            f"LogicalTableMap.{logical_table_id}.DataTransforms[{transform_index}]"
                            f".CreateColumnsOperation.Columns[{column_index}].Expression"
                        ),
                        "placeholder_expression": placeholder_expression,
                    }
                )

    return placeholder_columns


def apply_expression_at_path(dataset: Dict[str, Any], expression_path: str, expression: str) -> None:
    parts = expression_path.split(".")
    current: Any = dataset
    for part in parts[:-1]:
        if "[" in part and part.endswith("]"):
            key, index_text = part[:-1].split("[", 1)
            current = current[key][int(index_text)]
        else:
            current = current[part]
    current[parts[-1]] = expression


def apply_oversized_placeholders(
    dataset: Dict[str, Any],
    oversized_columns: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    updated_dataset = copy.deepcopy(dataset)
    placeholder_changes: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for column in oversized_columns:
        placeholder_expression = placeholder_expression_for_type(column.get("output_type"))
        if placeholder_expression is None:
            warnings.append(
                f"Unsupported placeholder type for oversized calculated column {column.get('column_name')}: {column.get('output_type')}"
            )
            continue

        apply_expression_at_path(
            updated_dataset,
            column["expression_path"],
            placeholder_expression,
        )
        placeholder_changes.append(
            {
                "column_name": column["column_name"],
                "output_type": column.get("output_type"),
                "expression_length": column["expression_length"],
                "expression_path": column["expression_path"],
                "original_expression": column["original_expression"],
                "placeholder_expression": placeholder_expression,
            }
        )

    return updated_dataset, placeholder_changes, warnings


def rewrite_sql(
    sql_query: str,
    preserve_exposed_names: bool = False,
) -> Tuple[str, List[str], bool]:
    warnings: List[str] = []
    if preserve_exposed_names:
        updated_sql = sql_query
        updated_sql = re.sub(
            r"(?is)\bDISTINCT\s+ON\s*\(\s*donation_object_id\s*\)",
            "DISTINCT ON (donation_id)",
            updated_sql,
        )
        updated_sql = re.sub(
            r"(?is)\bORDER\s+BY\s+donation_object_id\b",
            "ORDER BY donation_id",
            updated_sql,
        )
    else:
        updated_sql = rewrite_field_text(sql_query)
    updated_sql = LEADING_WHERE_PATTERN.sub("WHERE ", updated_sql)
    updated_sql = MIDDLE_AND_PATTERN.sub("", updated_sql)
    updated_sql = MIDDLE_OR_PATTERN.sub("", updated_sql)
    updated_sql = TRAILING_WHERE_PATTERN.sub("", updated_sql)
    updated_sql = re.sub(
        rf"(?is)\bWHERE\s+(?={SQL_TRAILING_KEYWORDS}\b)",
        "",
        updated_sql,
    )
    updated_sql = re.sub(r"[ \t]{2,}", " ", updated_sql)
    updated_sql = re.sub(r"\n{3,}", "\n\n", updated_sql).strip()

    if re.search(r"\bdonation_content_type_id\b", updated_sql, re.IGNORECASE):
        warnings.append(
            "The SQL still contains donation_content_type_id after the automated rewrite. Manual review required."
        )
    if (
        not preserve_exposed_names
        and re.search(r"\bdonation_object_id\b", updated_sql, re.IGNORECASE)
    ):
        warnings.append(
            "The SQL still contains donation_object_id after the automated rewrite. Manual review required."
        )

    return updated_sql, warnings, updated_sql != sql_query


def rewrite_raisenow_webhook_sql(
    sql_query: str,
    preserve_exposed_names: bool = False,
) -> Tuple[str, List[str], bool]:
    warnings: List[str] = []
    if not RAISENOW_EPMSWEBHOOK_PATTERN.search(sql_query):
        return sql_query, warnings, False

    updated_sql = sql_query
    if preserve_exposed_names:
        updated_sql = re.sub(
            r"(?i)(?<!\.)\bdonation_object_id\b(?!\s*\])",
            "donation_id AS donation_object_id",
            updated_sql,
            count=1,
        )
    else:
        updated_sql = re.sub(
            r"(?i)(?<!\.)\bdonation_object_id\b(?!\s*\])",
            "donation_id",
            updated_sql,
            count=1,
        )

    changed = updated_sql != sql_query

    if not changed and re.search(r"\bdonation_object_id\b", sql_query, re.IGNORECASE):
        warnings.append(
            "Non-paymentattempt raisenow_epmswebhook SQL still contains donation_object_id after the automated rewrite. Manual review required."
        )

    return updated_sql, warnings, changed


def build_explicit_paymentattempt_select_sql(
    sql_query: str,
    columns: List[Dict[str, Any]],
    preserve_exposed_names: bool = False,
) -> Optional[str]:
    distinct_on_legacy = bool(PAYMENTATTEMPT_SELECT_STAR_PATTERN.search(sql_query))
    plain_select_star = bool(PAYMENTATTEMPT_SELECT_STAR_GENERIC_PATTERN.search(sql_query))
    if not distinct_on_legacy and not plain_select_star:
        return None

    has_legacy_donation_object_id = any(
        isinstance(column, dict) and is_legacy_donation_object_field(column.get("Name"))
        for column in columns
    )
    selected_columns: List[str] = []
    seen = set()
    for column in columns:
        if not isinstance(column, dict):
            continue
        column_name = column.get("Name")
        if not isinstance(column_name, str):
            continue
        if column_name == "donation_content_type_id":
            continue
        if (
            preserve_exposed_names
            and column_name == "donation_id"
            and has_legacy_donation_object_id
        ):
            continue
        if preserve_exposed_names and column_name == "donation_object_id":
            rewritten_name = "donation_id AS donation_object_id"
            dedupe_name = "donation_object_id"
        else:
            rewritten_name = "donation_id" if column_name == "donation_object_id" else column_name
            dedupe_name = rewritten_name
        if dedupe_name in seen:
            continue
        seen.add(dedupe_name)
        selected_columns.append(rewritten_name)

    if not selected_columns:
        return None

    select_list = ", ".join(selected_columns)
    if distinct_on_legacy:
        return PAYMENTATTEMPT_SELECT_STAR_PATTERN.sub(
            f"SELECT DISTINCT ON (donation_id) {select_list} FROM bureau_paymentattempt",
            sql_query,
            count=1,
        )
    return PAYMENTATTEMPT_SELECT_STAR_GENERIC_PATTERN.sub(
        f"SELECT {select_list} FROM bureau_paymentattempt",
        sql_query,
        count=1,
    )


def rewrite_strings_in_structure(
    value: Any,
    path: str,
    changes: List[Dict[str, Any]],
    preserve_exposed_names: bool = False,
) -> Any:
    if isinstance(value, dict):
        updated: Dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{path}.{key}"
            transformed = rewrite_strings_in_structure(
                child,
                child_path,
                changes,
                preserve_exposed_names=preserve_exposed_names,
            )
            if transformed is SKIP:
                continue
            updated[key] = transformed
        return updated

    if isinstance(value, list):
        updated_list = []
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            transformed = rewrite_strings_in_structure(
                child,
                child_path,
                changes,
                preserve_exposed_names=preserve_exposed_names,
            )
            if transformed is SKIP:
                continue
            updated_list.append(transformed)
        return updated_list

    if isinstance(value, str):
        updated = rewrite_field_text(value, preserve_exposed_names=preserve_exposed_names)
        if updated != value:
            changes.append(
                {
                    "type": "replace_text",
                    "path": path,
                    "old_value": value,
                    "new_value": updated,
                }
            )
        return updated

    return value


def extract_expression_field_references(expression: str) -> List[str]:
    if not isinstance(expression, str):
        return []
    return re.findall(r"\{([^}]+)\}", expression)


def reorder_create_column_transforms(
    transforms: List[Dict[str, Any]],
    logical_table_id: str,
    changes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    def is_create_transform(transform: Dict[str, Any]) -> bool:
        return isinstance(transform, dict) and "CreateColumnsOperation" in transform

    updated = list(transforms)
    index = 0
    while index < len(updated):
        if not is_create_transform(updated[index]):
            index += 1
            continue

        start = index
        while index < len(updated) and is_create_transform(updated[index]):
            index += 1
        end = index
        block = updated[start:end]
        if len(block) < 2:
            continue

        created_by_transform: List[set] = []
        all_created_names = set()
        for transform in block:
            names = {
                column.get("ColumnName")
                for column in transform["CreateColumnsOperation"].get("Columns", [])
                if isinstance(column, dict) and isinstance(column.get("ColumnName"), str)
            }
            created_by_transform.append(names)
            all_created_names.update(names)

        dependencies: List[set] = []
        for transform in block:
            refs = set()
            for column in transform["CreateColumnsOperation"].get("Columns", []):
                if not isinstance(column, dict):
                    continue
                refs.update(extract_expression_field_references(column.get("Expression", "")))
            dependencies.append(refs.intersection(all_created_names))

        position_map = {
            name: position
            for position, names in enumerate(created_by_transform)
            for name in names
        }

        changed = True
        while changed:
            changed = False
            for pos in range(len(block)):
                dep_positions = sorted(
                    {
                        position_map[dep_name]
                        for dep_name in dependencies[pos]
                        if dep_name in position_map and position_map[dep_name] > pos
                    }
                )
                if not dep_positions:
                    continue
                first_dep_pos = dep_positions[0]
                transform = block.pop(first_dep_pos)
                created_names = created_by_transform.pop(first_dep_pos)
                dep_names = dependencies.pop(first_dep_pos)
                block.insert(pos, transform)
                created_by_transform.insert(pos, created_names)
                dependencies.insert(pos, dep_names)
                position_map = {
                    name: position
                    for position, names in enumerate(created_by_transform)
                    for name in names
                }
                changed = True
                changes.append(
                    {
                        "type": "reorder_create_column_transform",
                        "path": f"LogicalTableMap.{logical_table_id}.DataTransforms",
                        "details": (
                            f"Moved CreateColumnsOperation from block position {first_dep_pos} "
                            f"to {pos} so dependent calculated fields are created in dependency order."
                        ),
                    }
                )
                break

        updated[start:end] = block

    return updated


def rewrite_paymentattempt_custom_sql_columns(
    columns: List[Dict[str, Any]],
    path: str,
    changes: List[Dict[str, Any]],
    preserve_exposed_names: bool = False,
) -> List[Dict[str, Any]]:
    source_columns = copy.deepcopy(columns)
    rewritten_columns: List[Dict[str, Any]] = []
    existing_names = {
        column.get("Name")
        for column in source_columns
        if isinstance(column, dict) and isinstance(column.get("Name"), str)
    }
    has_legacy_donation_object_id = any(
        is_legacy_donation_object_field(name) for name in existing_names
    )

    for index, column in enumerate(source_columns):
        if not isinstance(column, dict):
            rewritten_columns.append(column)
            continue
        column_name = column.get("Name")
        if not isinstance(column_name, str):
            rewritten_columns.append(column)
            continue

        if is_legacy_donation_content_type_field(column_name):
            changes.append(
                {
                    "type": "remove_column",
                    "path": f"{path}[{index}]",
                    "old_value": column_name,
                }
            )
            continue

        if (
            preserve_exposed_names
            and column_name == "donation_id"
            and has_legacy_donation_object_id
        ):
            changes.append(
                {
                    "type": "remove_column",
                    "path": f"{path}[{index}]",
                    "old_value": column_name,
                    "reason": "Removed raw donation_id so paymentattempt keeps exposing donation_object_id without alias conflicts.",
                }
            )
            continue

        if preserve_exposed_names and is_legacy_donation_object_field(column_name):
            rewritten_columns.append(column)
            continue

        target_name = get_target_field_name(
            column_name,
            preserve_exposed_names=preserve_exposed_names,
        )
        if not target_name:
            rewritten_columns.append(column)
            continue

        if target_name in existing_names and column_name != target_name:
            rewritten_columns.append(column)
            continue

        if target_name != column_name:
            column["Name"] = target_name
            changes.append(
                {
                    "type": "replace_text",
                    "path": f"{path}[{index}].Name",
                    "old_value": column_name,
                    "new_value": target_name,
                }
            )

        rewritten_columns.append(column)

    return rewritten_columns


def migrate_paymentattempt_custom_sql(
    physical_map: Dict[str, Any],
    preserve_exposed_names: bool = False,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    updated_map = copy.deepcopy(physical_map)
    changes: List[Dict[str, Any]] = []

    for physical_table_id, physical_table in updated_map.items():
        custom_sql = physical_table.get("CustomSql")
        if not custom_sql:
            continue

        sql_query = custom_sql.get("SqlQuery", "")
        if not PAYMENTATTEMPT_PATTERN.search(sql_query):
            continue
        column_names = {
            column.get("Name")
            for column in custom_sql.get("Columns", [])
            if isinstance(column, dict) and isinstance(column.get("Name"), str)
        }
        has_legacy_sql_patterns = any(pattern.search(sql_query) for pattern in LEGACY_FIELD_PATTERNS)
        has_legacy_columns = any(
            is_legacy_donation_content_type_field(name) or is_legacy_donation_object_field(name)
            for name in column_names
        )
        uses_supported_select_star = bool(
            PAYMENTATTEMPT_SELECT_STAR_PATTERN.search(sql_query)
            or PAYMENTATTEMPT_SELECT_STAR_GENERIC_PATTERN.search(sql_query)
        )
        if not has_legacy_sql_patterns and not has_legacy_columns and not uses_supported_select_star:
            continue

        explicit_sql = None
        if isinstance(custom_sql.get("Columns"), list):
            explicit_sql = build_explicit_paymentattempt_select_sql(
                sql_query,
                custom_sql["Columns"],
                preserve_exposed_names=preserve_exposed_names,
            )

        sql_source = explicit_sql or sql_query
        updated_sql, warnings, changed = rewrite_sql(
            sql_source,
            preserve_exposed_names=preserve_exposed_names,
        )
        custom_sql_changes: List[Dict[str, Any]] = []
        if explicit_sql and explicit_sql != sql_query:
            custom_sql["SqlQuery"] = explicit_sql
            custom_sql_changes.append(
                {
                    "type": "expand_select_star",
                    "path": f"PhysicalTableMap.{physical_table_id}.CustomSql.SqlQuery",
                    "old_value": sql_query,
                    "new_value": explicit_sql,
                }
            )
        if changed:
            custom_sql["SqlQuery"] = updated_sql
            custom_sql_changes.append(
                {
                    "type": "rewrite_sql",
                    "path": f"PhysicalTableMap.{physical_table_id}.CustomSql.SqlQuery",
                    "old_value": sql_query,
                    "new_value": updated_sql,
                }
            )

        if isinstance(custom_sql.get("Columns"), list):
            custom_sql["Columns"] = rewrite_paymentattempt_custom_sql_columns(
                custom_sql["Columns"],
                f"PhysicalTableMap.{physical_table_id}.CustomSql.Columns",
                custom_sql_changes,
                preserve_exposed_names=preserve_exposed_names,
            )

        changes.append(
            {
                "physical_table_id": physical_table_id,
                "custom_sql_name": custom_sql.get("Name", physical_table_id),
                "changed": changed or bool(custom_sql_changes),
                "warnings": warnings,
                "old_sql": sql_query,
                "new_sql": updated_sql,
                "definition_changes": custom_sql_changes,
            }
        )

    return updated_map, changes


def migrate_supported_non_paymentattempt_custom_sql(
    physical_map: Dict[str, Any],
    preserve_exposed_names: bool = False,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    updated_map = copy.deepcopy(physical_map)
    changes: List[Dict[str, Any]] = []

    for physical_table_id, physical_table in updated_map.items():
        custom_sql = physical_table.get("CustomSql")
        if not custom_sql:
            continue

        sql_query = custom_sql.get("SqlQuery", "")
        if not isinstance(sql_query, str):
            continue
        if PAYMENTATTEMPT_PATTERN.search(sql_query):
            continue
        if not RAISENOW_EPMSWEBHOOK_PATTERN.search(sql_query):
            continue
        if not re.search(r"\bdonation_object_id\b", sql_query, re.IGNORECASE):
            continue

        updated_sql, warnings, changed = rewrite_raisenow_webhook_sql(
            sql_query,
            preserve_exposed_names=preserve_exposed_names,
        )
        custom_sql_changes: List[Dict[str, Any]] = []
        if changed:
            custom_sql["SqlQuery"] = updated_sql
            custom_sql_changes.append(
                {
                    "type": "rewrite_sql",
                    "path": f"PhysicalTableMap.{physical_table_id}.CustomSql.SqlQuery",
                    "old_value": sql_query,
                    "new_value": updated_sql,
                }
            )

        changes.append(
            {
                "physical_table_id": physical_table_id,
                "custom_sql_name": custom_sql.get("Name", physical_table_id),
                "changed": changed or bool(custom_sql_changes),
                "warnings": warnings,
                "old_sql": sql_query,
                "new_sql": updated_sql,
                "definition_changes": custom_sql_changes,
            }
        )

    return updated_map, changes


def build_paymentattempt_physical_table_info(
    physical_map: Dict[str, Any],
    sql_changes: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    changed_table_ids = {change["physical_table_id"] for change in sql_changes}
    table_info: Dict[str, Dict[str, Any]] = {}

    for physical_table_id, physical_table in physical_map.items():
        if physical_table_id not in changed_table_ids:
            continue

        custom_sql = physical_table.get("CustomSql", {})
        columns = custom_sql.get("Columns", [])
        column_names = {
            column.get("Name")
            for column in columns
            if isinstance(column, dict) and isinstance(column.get("Name"), str)
        }
        table_info[physical_table_id] = {
            "column_names": column_names,
        }

    return table_info


def migrate_logical_table_map(
    logical_map: Dict[str, Any],
    paymentattempt_table_info: Dict[str, Dict[str, Any]],
    preserve_exposed_names: bool = False,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    updated_map = copy.deepcopy(logical_map)
    changes: List[Dict[str, Any]] = []

    for logical_table_id, logical_table in updated_map.items():
        source = logical_table.get("Source", {})
        physical_table_id = source.get("PhysicalTableId")
        source_table_info = paymentattempt_table_info.get(physical_table_id, {})
        source_columns = source_table_info.get("column_names", set())

        transforms = logical_table.get("DataTransforms")
        if not isinstance(transforms, list):
            logical_table["Source"] = rewrite_strings_in_structure(
                source,
                f"LogicalTableMap.{logical_table_id}.Source",
                changes,
                preserve_exposed_names=preserve_exposed_names,
            )
            continue

        new_transforms = []
        for index, transform in enumerate(transforms):
            path = f"LogicalTableMap.{logical_table_id}.DataTransforms[{index}]"

            if "RenameColumnOperation" in transform:
                rename_op = transform["RenameColumnOperation"]
                column_name = rename_op.get("ColumnName")
                new_column_name = rename_op.get("NewColumnName")

                if is_legacy_donation_content_type_field(
                    column_name
                ) or is_legacy_donation_content_type_field(new_column_name):
                    changes.append(
                        {
                            "type": "remove_transform",
                            "path": path,
                            "reason": "Removed legacy donation_content_type field rename.",
                            "old_value": transform,
                        }
                    )
                    continue

                rewritten_column_name = (
                    column_name
                    if preserve_exposed_names and is_legacy_donation_object_field(column_name)
                    else get_target_field_name(
                        column_name,
                        preserve_exposed_names=preserve_exposed_names,
                    )
                    or column_name
                )
                rewritten_new_column_name = (
                    new_column_name
                    if preserve_exposed_names and is_legacy_donation_object_field(new_column_name)
                    else get_target_field_name(
                        new_column_name,
                        preserve_exposed_names=preserve_exposed_names,
                    )
                    or new_column_name
                )

                if (
                    isinstance(column_name, str)
                    and isinstance(rewritten_column_name, str)
                    and rewritten_column_name in source_columns
                ):
                    rename_op["ColumnName"] = rewritten_column_name
                if (
                    isinstance(new_column_name, str)
                    and isinstance(rewritten_new_column_name, str)
                ):
                    rename_op["NewColumnName"] = rewritten_new_column_name

                if (
                    rename_op.get("ColumnName") == rename_op.get("NewColumnName")
                    or (
                        isinstance(rename_op.get("NewColumnName"), str)
                        and rename_op.get("NewColumnName") in source_columns
                        and rename_op.get("ColumnName") != rename_op.get("NewColumnName")
                    )
                ):
                    changes.append(
                        {
                            "type": "remove_transform",
                            "path": path,
                            "reason": "Removed rename that would collide with an existing source column.",
                            "old_value": transform,
                        }
                    )
                    continue

            if "ProjectOperation" in transform:
                projected_columns = transform["ProjectOperation"].get("ProjectedColumns")
                if isinstance(projected_columns, list):
                    original_column_names = set(projected_columns)
                    rewritten_columns = []
                    seen_columns = set()
                    for column_index, column_name in enumerate(projected_columns):
                        column_path = f"{path}.ProjectOperation.ProjectedColumns[{column_index}]"
                        if is_legacy_donation_content_type_field(column_name):
                            changes.append(
                                {
                                    "type": "remove_projected_column",
                                    "path": column_path,
                                    "old_value": column_name,
                                }
                            )
                            continue

                        target_name = get_target_field_name(
                            column_name,
                            preserve_exposed_names=preserve_exposed_names,
                        )
                        if target_name is None and (
                            is_legacy_donation_object_field(column_name)
                            or is_legacy_donation_content_type_field(column_name)
                        ):
                            continue

                        if target_name and target_name in original_column_names and target_name != column_name:
                            changes.append(
                                {
                                    "type": "remove_duplicate_projected_column",
                                    "path": column_path,
                                    "old_value": column_name,
                                    "new_value": target_name,
                                }
                            )
                            continue

                        rewritten_name = target_name or rewrite_field_text(
                            column_name,
                            preserve_exposed_names=preserve_exposed_names,
                        )
                        if rewritten_name != column_name:
                            changes.append(
                                {
                                    "type": "rename_projected_column",
                                    "path": column_path,
                                    "old_value": column_name,
                                    "new_value": rewritten_name,
                                }
                            )

                        if rewritten_name in seen_columns:
                            changes.append(
                                {
                                    "type": "remove_duplicate_projected_column",
                                    "path": column_path,
                                    "old_value": rewritten_name,
                                }
                            )
                            continue

                        seen_columns.add(rewritten_name)
                        rewritten_columns.append(rewritten_name)
                    transform["ProjectOperation"]["ProjectedColumns"] = rewritten_columns

            rewritten_transform = rewrite_strings_in_structure(
                transform,
                path,
                changes,
                preserve_exposed_names=preserve_exposed_names,
            )
            new_transforms.append(rewritten_transform)

        new_transforms = reorder_create_column_transforms(
            new_transforms,
            logical_table_id,
            changes,
        )
        logical_table["DataTransforms"] = new_transforms
        logical_table["Source"] = rewrite_strings_in_structure(
            source,
            f"LogicalTableMap.{logical_table_id}.Source",
            changes,
            preserve_exposed_names=preserve_exposed_names,
        )

    return updated_map, changes


def migrate_dataset_definition(
    dataset: Dict[str, Any],
    preserve_exposed_names: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    updated_dataset = copy.deepcopy(dataset)

    updated_physical_map, sql_changes = migrate_paymentattempt_custom_sql(
        updated_dataset.get("PhysicalTableMap", {}),
        preserve_exposed_names=preserve_exposed_names,
    )
    updated_physical_map, supported_non_paymentattempt_sql_changes = migrate_supported_non_paymentattempt_custom_sql(
        updated_physical_map,
        preserve_exposed_names=preserve_exposed_names,
    )
    sql_changes.extend(supported_non_paymentattempt_sql_changes)
    updated_dataset["PhysicalTableMap"] = updated_physical_map

    if not sql_changes:
        return updated_dataset, {
            "sql_changes": [],
            "logical_changes": [],
            "other_definition_changes": [],
        }

    paymentattempt_table_info = build_paymentattempt_physical_table_info(
        updated_physical_map,
        sql_changes,
    )

    logical_changes: List[Dict[str, Any]] = []
    if isinstance(updated_dataset.get("LogicalTableMap"), dict):
        updated_logical_map, logical_changes = migrate_logical_table_map(
            updated_dataset["LogicalTableMap"],
            paymentattempt_table_info,
            preserve_exposed_names=preserve_exposed_names,
        )
        updated_dataset["LogicalTableMap"] = updated_logical_map

    other_definition_changes: List[Dict[str, Any]] = []
    for key in [
        "FieldFolders",
        "ColumnGroups",
        "RowLevelPermissionDataSet",
        "RowLevelPermissionTagConfiguration",
        "ColumnLevelPermissionRules",
        "DataSetUsageConfiguration",
        "DatasetParameters",
        "PerformanceConfiguration",
        "DataPrepConfiguration",
        "SemanticModelConfiguration",
    ]:
        if key in updated_dataset and updated_dataset[key]:
            updated_dataset[key] = rewrite_strings_in_structure(
                updated_dataset[key],
                key,
                other_definition_changes,
                preserve_exposed_names=preserve_exposed_names,
            )

    return updated_dataset, {
        "sql_changes": sql_changes,
        "logical_changes": logical_changes,
        "other_definition_changes": other_definition_changes,
    }


def scan_for_legacy_references(
    obj: Any,
    path: str,
    preserve_exposed_names: bool = False,
) -> List[Dict[str, str]]:
    references: List[Dict[str, str]] = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            references.extend(
                scan_for_legacy_references(
                    value,
                    f"{path}.{key}",
                    preserve_exposed_names=preserve_exposed_names,
                )
            )
        return references

    if isinstance(obj, list):
        for index, value in enumerate(obj):
            references.extend(
                scan_for_legacy_references(
                    value,
                    f"{path}[{index}]",
                    preserve_exposed_names=preserve_exposed_names,
                )
            )
        return references

    if isinstance(obj, str) and contains_legacy_field(
        obj,
        preserve_exposed_names=preserve_exposed_names,
    ):
        references.append({"path": path, "value": obj})

    return references


def scan_post_migration_risks(
    dataset: Dict[str, Any],
    preserve_exposed_names: bool = False,
) -> List[Dict[str, str]]:
    references: List[Dict[str, str]] = []
    for key in [
        "LogicalTableMap",
        "FieldFolders",
        "ColumnGroups",
        "DatasetParameters",
        "DataPrepConfiguration",
        "SemanticModelConfiguration",
    ]:
        if key in dataset and dataset[key]:
            references.extend(
                scan_for_legacy_references(
                    dataset[key],
                    key,
                    preserve_exposed_names=preserve_exposed_names,
                )
            )
    return references


def scan_non_paymentattempt_custom_sql_risks(
    dataset: Dict[str, Any],
) -> List[Dict[str, str]]:
    references: List[Dict[str, str]] = []
    physical_map = dataset.get("PhysicalTableMap")
    if not isinstance(physical_map, dict):
        return references

    for physical_table_id, physical_table in physical_map.items():
        custom_sql = physical_table.get("CustomSql")
        if not isinstance(custom_sql, dict):
            continue
        sql_query = custom_sql.get("SqlQuery")
        if not isinstance(sql_query, str):
            continue
        if PAYMENTATTEMPT_PATTERN.search(sql_query):
            continue
        if re.search(r"\bdonation_content_type_id\b", sql_query, re.IGNORECASE):
            references.append(
                {
                    "path": f"PhysicalTableMap.{physical_table_id}.CustomSql.SqlQuery",
                    "value": sql_query,
                    "name": custom_sql.get("Name", physical_table_id),
                    "reason": "non_paymentattempt_sql_contains_donation_content_type_id",
                }
            )
        normalized_sql = re.sub(
            r"(?i)\bdonation_id\s+AS\s+donation_object_id\b",
            "",
            sql_query,
        )
        if re.search(r"\bdonation_object_id\b", normalized_sql, re.IGNORECASE):
            references.append(
                {
                    "path": f"PhysicalTableMap.{physical_table_id}.CustomSql.SqlQuery",
                    "value": sql_query,
                    "name": custom_sql.get("Name", physical_table_id),
                    "reason": "non_paymentattempt_sql_contains_donation_object_id",
                }
            )
    return references


def write_backup(backup_dir: str, dataset: Dict[str, Any]) -> str:
    filename = f"{slugify(dataset['Name'])}__{slugify(dataset['DataSetId'])}.json"
    full_path = os.path.join(backup_dir, filename)
    with open(full_path, "w", encoding="utf-8") as handle:
        json.dump(dataset, handle, indent=2, default=json_default)
    return full_path


def build_update_payload(dataset: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "AwsAccountId": QS_ACCOUNT_ID,
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


def describe_dataset(qs_client, dataset_id: str) -> Dict[str, Any]:
    response = qs_client.describe_data_set(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
    )
    return response["DataSet"]


def list_all_ingestions(qs_client, dataset_id: str) -> List[Dict[str, Any]]:
    paginator = qs_client.get_paginator("list_ingestions")
    ingestions: List[Dict[str, Any]] = []
    for page in paginator.paginate(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
        PaginationConfig={"PageSize": 100},
    ):
        ingestions.extend(page.get("Ingestions", []))
    return ingestions


def ingestion_sort_key(ingestion: Dict[str, Any]) -> datetime.datetime:
    created = ingestion.get("CreatedTime")
    updated = ingestion.get("UpdatedTime")
    if isinstance(created, datetime.datetime):
        return created
    if isinstance(updated, datetime.datetime):
        return updated
    return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def build_ingestion_id(dataset_id: str, index: int) -> str:
    compact_timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    safe_dataset_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in dataset_id)
    suffix = safe_dataset_id[-48:]
    return f"migration-refresh-{compact_timestamp}-{index:02d}-{suffix}"[:128]


def wait_for_ingestion(
    qs_client,
    dataset_id: str,
    ingestion_id: str,
    poll_seconds: int,
    logger: Logger,
) -> Dict[str, Any]:
    terminal_statuses = {"COMPLETED", "FAILED", "CANCELLED"}
    while True:
        response = qs_client.describe_ingestion(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSetId=dataset_id,
            IngestionId=ingestion_id,
        )
        ingestion = response["Ingestion"]
        status = ingestion["IngestionStatus"]
        logger.log(f"    Ingestion status: {status}")
        if status in terminal_statuses:
            return ingestion
        time.sleep(poll_seconds)


def find_new_ingestion_after_update(
    qs_client,
    dataset_id: str,
    known_ingestion_ids: set,
    not_before: datetime.datetime,
) -> Optional[Dict[str, Any]]:
    ingestions = list_all_ingestions(qs_client, dataset_id)
    candidates = []
    for ingestion in ingestions:
        ingestion_id = ingestion.get("IngestionId")
        if ingestion_id in known_ingestion_ids:
            continue
        if ingestion_sort_key(ingestion) < not_before:
            continue
        candidates.append(ingestion)
    if not candidates:
        return None
    return max(candidates, key=ingestion_sort_key)


def wait_for_refresh_after_update(
    qs_client,
    dataset: Dict[str, Any],
    logger: Logger,
    index: int,
    poll_seconds: int,
    detect_timeout_seconds: int,
    create_if_missing: bool,
) -> Dict[str, Any]:
    dataset_id = dataset["DataSetId"]
    dataset_name = dataset["Name"]
    known_ingestions = list_all_ingestions(qs_client, dataset_id)
    known_ingestion_ids = {
        ingestion.get("IngestionId")
        for ingestion in known_ingestions
        if ingestion.get("IngestionId")
    }
    update_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)
    logger.log(
        f"  Waiting for dataset refresh before continuing "
        f"(auto-detect timeout: {detect_timeout_seconds}s, poll: {poll_seconds}s)."
    )

    deadline = time.time() + detect_timeout_seconds
    next_progress_log = time.time() + poll_seconds
    while time.time() < deadline:
        new_ingestion = find_new_ingestion_after_update(
            qs_client,
            dataset_id,
            known_ingestion_ids,
            update_time,
        )
        if new_ingestion:
            ingestion_id = new_ingestion.get("IngestionId")
            logger.log(f"  Detected refresh ingestion: {ingestion_id}")
            status = new_ingestion.get("IngestionStatus")
            if status in {"COMPLETED", "FAILED", "CANCELLED"}:
                return new_ingestion
            return wait_for_ingestion(qs_client, dataset_id, ingestion_id, poll_seconds, logger)
        now = time.time()
        if now >= next_progress_log:
            waited_seconds = int(detect_timeout_seconds - max(deadline - now, 0))
            logger.log(
                f"    Still waiting for automatic refresh to appear "
                f"({waited_seconds}/{detect_timeout_seconds}s elapsed)."
            )
            next_progress_log = now + poll_seconds
        time.sleep(poll_seconds)

    if not create_if_missing:
        return {
            "IngestionStatus": "NOT_DETECTED",
            "IngestionId": None,
            "DataSetId": dataset_id,
            "Message": f"No automatic refresh detected for {dataset_name} within {detect_timeout_seconds} seconds.",
        }

    ingestion_id = build_ingestion_id(dataset_id, index)
    logger.log(f"  No automatic refresh detected. Starting manual refresh: {ingestion_id}")
    create_response = qs_client.create_ingestion(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
        IngestionId=ingestion_id,
        IngestionType="FULL_REFRESH",
    )
    logger.log(f"    Initial ingestion status: {create_response.get('IngestionStatus')}")
    return wait_for_ingestion(qs_client, dataset_id, ingestion_id, poll_seconds, logger)


def is_unsupported_file_source_error(exc: Exception) -> bool:
    if not isinstance(exc, ClientError):
        return False
    error = exc.response.get("Error", {})
    return (
        error.get("Code") == "InvalidParameterValueException"
        and "File source type is not supported in Public API" in error.get("Message", "")
    )


def write_plan(plan_path: str, plan: Dict[str, Any]) -> None:
    with open(plan_path, "w", encoding="utf-8") as handle:
        json.dump(plan, handle, indent=2, default=json_default)


def write_placeholder_restore_file(plan_path: str, plan: Dict[str, Any]) -> Optional[str]:
    sections: List[str] = []

    for dataset in plan.get("datasets", []):
        placeholder_changes = dataset.get("placeholder_changes") or []
        preexisting_placeholder_columns = dataset.get("preexisting_placeholder_columns") or []
        if not placeholder_changes and not preexisting_placeholder_columns:
            continue

        sections.append(f"DATASET: {dataset.get('name')} ({dataset.get('data_set_id')})")
        sections.append("")
        for index, change in enumerate(placeholder_changes, start=1):
            sections.append(f"FIELD {index}: {change.get('column_name')}")
            sections.append(f"Output type: {change.get('output_type')}")
            sections.append(f"Expression path: {change.get('expression_path')}")
            sections.append("Original expression:")
            sections.append(change.get("original_expression", ""))
            sections.append("")
            sections.append("=" * 80)
            sections.append("")

        if preexisting_placeholder_columns:
            sections.append("Already-placeholder fields before this run:")
            sections.append(
                "These fields were already set to placeholder expressions before this migration run started."
            )
            sections.append(
                "Their original formulas are not available from this plan file. Recover them from an earlier restore file or dataset history."
            )
            sections.append("")
            for index, change in enumerate(preexisting_placeholder_columns, start=1):
                sections.append(f"FIELD {index}: {change.get('column_name')}")
                sections.append(f"Output type: {change.get('output_type')}")
                sections.append(f"Expression path: {change.get('expression_path')}")
                sections.append(f"Current placeholder expression: {change.get('placeholder_expression')}")
                sections.append("")
            sections.append("=" * 80)
            sections.append("")

    if not sections:
        return None

    output_path = build_placeholder_restore_file(plan_path, plan)
    with open(output_path, "w", encoding="utf-8-sig", newline="\n") as handle:
        handle.write("\n".join(sections).rstrip() + "\n")
    return output_path


def summarize_changes(changes: Dict[str, Any]) -> int:
    sql_change_count = sum(1 for change in changes["sql_changes"] if change.get("changed"))
    return sql_change_count + len(changes["logical_changes"]) + len(changes["other_definition_changes"])


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", REGION)
    os.makedirs(LOG_DIR, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="Preview or apply the full paymentattempt dataset migration."
    )
    parser.add_argument("--datasets", nargs="+", help="Exact dataset names to inspect.")
    parser.add_argument(
        "--dataset-ids",
        nargs="+",
        help="Exact dataset ids to inspect. Use this when dataset names are duplicated.",
    )
    parser.add_argument(
        "--plan-file",
        help="Reuse the dataset ids/names from an existing migration plan for a scoped rerun or apply.",
    )
    parser.add_argument(
        "--dataset-name-contains",
        help="Case-insensitive substring filter for dataset names.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply dataset updates. Without this flag, the script only previews changes.",
    )
    parser.add_argument(
        "--include-unchanged",
        action="store_true",
        help="Include inspected datasets that do not need changes in the plan file.",
    )
    parser.add_argument(
        "--allow-all-datasets",
        action="store_true",
        help="Allow apply mode without an explicit dataset filter or plan file.",
    )
    parser.add_argument(
        "--replace-oversized-with-placeholders",
        action="store_true",
        help="Temporarily replace oversized calculated column expressions with type-compatible placeholders so UpdateDataSet can proceed. Original expressions are written to the plan file for manual restoration.",
    )
    parser.add_argument(
        "--preserve-exposed-field-names",
        action="store_true",
        help="Keep the dataset surface on donation_object_id while switching paymentattempt internals to donation_id. This is safer for datasets that already contain another donation_id field.",
    )
    parser.add_argument(
        "--wait-for-refresh",
        action="store_true",
        help="After each successful dataset update, wait for that dataset's refresh to finish before continuing to the next dataset.",
    )
    parser.add_argument(
        "--refresh-poll-seconds",
        type=int,
        default=15,
        help="Seconds between ingestion status checks when --wait-for-refresh is enabled.",
    )
    parser.add_argument(
        "--refresh-detect-timeout-seconds",
        type=int,
        default=60,
        help="How long to wait for an automatic refresh to appear after UpdateDataSet before starting a manual refresh.",
    )
    parser.add_argument(
        "--continue-on-refresh-failure",
        action="store_true",
        help="Continue with the next dataset if the post-update refresh fails.",
    )
    args = parser.parse_args()

    plan_names: List[str] = []
    plan_ids: List[str] = []
    if args.plan_file:
        plan_names, plan_ids = load_dataset_targets_from_plan(args.plan_file)

    if (
        args.apply
        and not args.allow_all_datasets
        and not args.datasets
        and not args.dataset_ids
        and not args.dataset_name_contains
        and not args.plan_file
    ):
        raise SystemExit(
            "Apply mode now requires an explicit scope. Use --datasets, --dataset-ids, --dataset-name-contains, or --plan-file."
        )

    logger = Logger(os.path.join(LOG_DIR, f"paymentattempt_dataset_migration_{TIMESTAMP}.txt"))
    backup_dir = build_backup_dir()
    plan_path = build_plan_file()

    qs = boto3.client("quicksight", region_name=REGION)
    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {REGION})")
    logger.log(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")

    all_datasets = get_all_summaries(qs.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
    selected_datasets = select_datasets(
        all_datasets,
        target_ids=plan_ids + (args.dataset_ids or []),
        target_names=args.datasets,
        name_contains=args.dataset_name_contains,
    )

    if args.plan_file and not args.datasets:
        plan_name_set = set(plan_names)
        selected_datasets = [
            dataset for dataset in selected_datasets if dataset["Name"] in plan_name_set
        ]

    if args.datasets:
        matched_names = {dataset["Name"] for dataset in selected_datasets}
        for missing_name in sorted(set(args.datasets) - matched_names):
            logger.log(f"Dataset not found: {missing_name}")
    if args.dataset_ids:
        matched_ids = {dataset["DataSetId"] for dataset in selected_datasets}
        for missing_id in sorted(set(args.dataset_ids) - matched_ids):
            logger.log(f"Dataset id not found: {missing_id}")

    logger.log(f"Datasets discovered in account: {len(all_datasets)}")
    logger.log(f"Datasets selected for inspection: {len(selected_datasets)}")
    if args.plan_file:
        logger.log(f"Scoped by plan file: {os.path.abspath(args.plan_file)}")
    if args.datasets:
        logger.log(f"Exact dataset filter: {', '.join(args.datasets)}")
    if args.dataset_ids:
        logger.log(f"Exact dataset id filter: {', '.join(args.dataset_ids)}")
    if args.dataset_name_contains:
        logger.log(f"Name substring filter: {args.dataset_name_contains}")

    plan: Dict[str, Any] = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": REGION,
        "apply_requested": args.apply,
        "replace_oversized_with_placeholders_requested": args.replace_oversized_with_placeholders,
        "backup_directory": backup_dir,
        "preserve_exposed_field_names_requested": args.preserve_exposed_field_names,
        "wait_for_refresh_requested": args.wait_for_refresh,
        "migration_type": "schema_stable" if args.preserve_exposed_field_names else "full",
        "renamed_fields": (
            {}
            if args.preserve_exposed_field_names
            else {
                "donation_object_id": "donation_id",
                "donation_object_id[<suffix>]": "donation_id[<suffix>]",
            }
        ),
        "removed_fields": [
            "donation_content_type_id",
            "donation_content_type_id[<suffix>]",
        ],
        "datasets": [],
    }

    apply_failures = 0
    applied_count = 0
    candidate_count = 0
    skipped_unsupported_file_sources = 0
    stop_processing = False

    total_selected = len(selected_datasets)
    for dataset_index, summary in enumerate(selected_datasets, start=1):
        if stop_processing:
            break
        logger.log("")
        logger.log(
            f"Progress: dataset {dataset_index}/{total_selected} "
            f"(applied so far: {applied_count}, failures/skips so far: {apply_failures})"
        )
        try:
            original_dataset = describe_dataset(qs, summary["DataSetId"])
        except Exception as exc:
            if is_unsupported_file_source_error(exc):
                skipped_unsupported_file_sources += 1
                logger.log("")
                logger.log(
                    f"SKIPPED: {summary['Name']} ({summary['DataSetId']}) uses a file source that QuickSight does not expose through DescribeDataSet."
                )
                continue
            raise

        updated_dataset, migration_changes = migrate_dataset_definition(
            original_dataset,
            preserve_exposed_names=args.preserve_exposed_field_names,
        )
        legacy_references_before = scan_post_migration_risks(original_dataset)
        legacy_references_after = scan_post_migration_risks(
            updated_dataset,
            preserve_exposed_names=args.preserve_exposed_field_names,
        )
        non_paymentattempt_sql_risks = scan_non_paymentattempt_custom_sql_risks(updated_dataset)
        preexisting_placeholder_columns = get_preexisting_placeholder_columns(original_dataset)
        oversized_calculated_columns = get_oversized_calculated_columns(updated_dataset)
        placeholder_changes: List[Dict[str, Any]] = []
        placeholder_warnings: List[str] = []

        if args.replace_oversized_with_placeholders and oversized_calculated_columns:
            updated_dataset, placeholder_changes, placeholder_warnings = apply_oversized_placeholders(
                updated_dataset,
                oversized_calculated_columns,
            )
            oversized_calculated_columns = get_oversized_calculated_columns(updated_dataset)
            migration_changes["other_definition_changes"].extend(
                [
                    {
                        "type": "replace_oversized_calculated_column_with_placeholder",
                        "path": change["expression_path"],
                        "old_value": change["original_expression"],
                        "new_value": change["placeholder_expression"],
                        "column_name": change["column_name"],
                        "output_type": change["output_type"],
                    }
                    for change in placeholder_changes
                ]
            )
        total_change_count = summarize_changes(migration_changes)

        if total_change_count:
            candidate_count += 1

        if not total_change_count and not args.include_unchanged:
            continue

        backup_path = write_backup(backup_dir, original_dataset)
        dataset_plan = {
            "name": original_dataset["Name"],
            "data_set_id": original_dataset["DataSetId"],
            "arn": original_dataset["Arn"],
            "import_mode": original_dataset["ImportMode"],
            "backup_file": backup_path,
            "migration_type": "schema_stable" if args.preserve_exposed_field_names else "full",
            "sql_changes": migration_changes["sql_changes"],
            "logical_changes": migration_changes["logical_changes"],
            "other_definition_changes": migration_changes["other_definition_changes"],
            "legacy_references_before": legacy_references_before,
            "legacy_references_after": legacy_references_after,
            "non_paymentattempt_sql_risks": non_paymentattempt_sql_risks,
            "oversized_calculated_columns": oversized_calculated_columns,
            "placeholder_changes": placeholder_changes,
            "preexisting_placeholder_columns": preexisting_placeholder_columns,
            "applied": False,
            "update_status": None,
            "refresh_waited": False,
            "refresh_ingestion_id": None,
            "refresh_status": None,
            "warnings": [],
        }

        logger.log("")
        logger.log(f"DATASET: {original_dataset['Name']} ({original_dataset['DataSetId']})")
        logger.log(f"Import mode: {original_dataset['ImportMode']}")
        logger.log(f"Backup: {backup_path}")
        logger.log(f"  SQL changes: {len(migration_changes['sql_changes'])}")
        logger.log(f"  Logical changes: {len(migration_changes['logical_changes'])}")
        logger.log(f"  Other definition changes: {len(migration_changes['other_definition_changes'])}")

        if migration_changes["sql_changes"]:
            for sql_change in migration_changes["sql_changes"]:
                logger.log(
                    f"  Custom SQL: {sql_change['custom_sql_name']} [{sql_change['physical_table_id']}] changed={'yes' if sql_change['changed'] else 'no'}"
                )
                for warning in sql_change["warnings"]:
                    logger.log(f"    Warning: {warning}")
                    dataset_plan["warnings"].append(warning)

        for warning in placeholder_warnings:
            logger.log(f"    Warning: {warning}")
            dataset_plan["warnings"].append(warning)

        if legacy_references_before:
            preview_limit = 10
            logger.log(
                f"  Legacy references before migration: {len(legacy_references_before)}"
            )
            for reference in legacy_references_before[:preview_limit]:
                logger.log(f"    Before: {reference['path']} = {reference['value']}")
            if len(legacy_references_before) > preview_limit:
                logger.log(
                    f"    ... plus {len(legacy_references_before) - preview_limit} more references in the plan file."
                )

        if non_paymentattempt_sql_risks:
            preview_limit = 10
            logger.log(
                f"  Non-paymentattempt custom SQL references needing manual review: {len(non_paymentattempt_sql_risks)}"
            )
            for reference in non_paymentattempt_sql_risks[:preview_limit]:
                logger.log(
                    f"    SQL risk: {reference['name']} at {reference['path']} ({reference['reason']})"
                )
            if len(non_paymentattempt_sql_risks) > preview_limit:
                logger.log(
                    f"    ... plus {len(non_paymentattempt_sql_risks) - preview_limit} more SQL-risk references in the plan file."
                )
            dataset_plan["warnings"].append(
                "A non-paymentattempt custom SQL query still references donation_object_id or donation_content_type_id. Manual review is required because this script only rewrites bureau_paymentattempt queries."
            )

        unsafe_change = False

        if oversized_calculated_columns:
            unsafe_change = True
            preview_limit = 10
            logger.log(
                f"  Oversized calculated columns (>4096 chars): {len(oversized_calculated_columns)}"
            )
            for column in oversized_calculated_columns[:preview_limit]:
                logger.log(
                    f"    Oversized: {column['column_name']} ({column['expression_length']} chars) at {column['path']}"
                )
            if len(oversized_calculated_columns) > preview_limit:
                logger.log(
                    f"    ... plus {len(oversized_calculated_columns) - preview_limit} more oversized calculated columns in the plan file."
                )
            dataset_plan["warnings"].append(
                "QuickSight public UpdateDataSet rejects this dataset because at least one calculated column expression exceeds 4096 characters."
            )
        elif placeholder_changes:
            logger.log(
                f"  Oversized calculated columns replaced with placeholders: {len(placeholder_changes)}"
            )
        if preexisting_placeholder_columns:
            logger.log(
                f"  Calculated columns already in placeholder state before this run: {len(preexisting_placeholder_columns)}"
            )
            preview_limit = 10
            for column in preexisting_placeholder_columns[:preview_limit]:
                logger.log(
                    f"    Already placeholder: {column['column_name']} at {column['expression_path']}"
                )
            if len(preexisting_placeholder_columns) > preview_limit:
                logger.log(
                    f"    ... plus {len(preexisting_placeholder_columns) - preview_limit} more already-placeholder calculated columns in the plan file."
                )
        if legacy_references_after:
            unsafe_change = True
            logger.log(
                f"  Remaining legacy references after migration: {len(legacy_references_after)}"
            )
            preview_limit = 10
            for reference in legacy_references_after[:preview_limit]:
                logger.log(f"    Remaining: {reference['path']} = {reference['value']}")
            if len(legacy_references_after) > preview_limit:
                logger.log(
                    f"    ... plus {len(legacy_references_after) - preview_limit} more references in the plan file."
                )
            dataset_plan["warnings"].append(
                "Legacy references remain after automated migration. Manual review required before apply."
            )

        for sql_change in migration_changes["sql_changes"]:
            if sql_change["warnings"]:
                unsafe_change = True
        if non_paymentattempt_sql_risks:
            unsafe_change = True

        if args.apply and total_change_count and not unsafe_change:
            payload = build_update_payload(updated_dataset)
            try:
                response = qs.update_data_set(**payload)
                dataset_plan["applied"] = True
                dataset_plan["update_status"] = response.get("Status")
                applied_count += 1
                logger.log(f"  Update applied successfully. HTTP status: {response.get('Status')}")
                if args.wait_for_refresh and original_dataset["ImportMode"] == "SPICE":
                    dataset_plan["refresh_waited"] = True
                    ingestion = wait_for_refresh_after_update(
                        qs,
                        original_dataset,
                        logger,
                        dataset_index,
                        args.refresh_poll_seconds,
                        args.refresh_detect_timeout_seconds,
                        create_if_missing=True,
                    )
                    dataset_plan["refresh_ingestion_id"] = ingestion.get("IngestionId")
                    dataset_plan["refresh_status"] = ingestion.get("IngestionStatus")
                    refresh_status = ingestion.get("IngestionStatus")
                    if refresh_status == "COMPLETED":
                        logger.log("  Refresh completed successfully.")
                    elif refresh_status == "NOT_DETECTED":
                        warning = ingestion.get("Message") or "No automatic refresh was detected after the update."
                        dataset_plan["warnings"].append(warning)
                        logger.log(f"  Warning: {warning}")
                    else:
                        error_info = ingestion.get("ErrorInfo", {})
                        message = error_info.get("Message") or ingestion.get("Message") or "No error message returned."
                        warning = f"Refresh finished with status {refresh_status}: {message}"
                        dataset_plan["warnings"].append(warning)
                        logger.log(f"  {warning}")
                        apply_failures += 1
                        if not args.continue_on_refresh_failure:
                            logger.log("  Stopping after refresh failure.")
                            stop_processing = True
            except Exception as exc:
                apply_failures += 1
                dataset_plan["warnings"].append(str(exc))
                logger.log(f"  Failed during update or refresh wait: {exc}")
        elif args.apply and unsafe_change:
            apply_failures += 1
            logger.log("  Skipped apply because manual review is still required.")
        else:
            logger.log("  Preview only. No QuickSight changes were sent.")

        plan["datasets"].append(dataset_plan)

    write_plan(plan_path, plan)
    placeholder_restore_file = write_placeholder_restore_file(plan_path, plan)
    logger.log("")
    logger.log(f"Candidate datasets with migration changes: {candidate_count}")
    logger.log(f"Datasets written to plan: {len(plan['datasets'])}")
    logger.log(f"Datasets updated: {applied_count}")
    logger.log(f"Apply failures or manual-review skips: {apply_failures}")
    logger.log(f"Unsupported uploaded-file datasets skipped: {skipped_unsupported_file_sources}")
    logger.log(f"Plan file: {plan_path}")
    if placeholder_restore_file:
        logger.log(f"Placeholder restore file: {placeholder_restore_file}")
    logger.log(f"Backup directory: {backup_dir}")


if __name__ == "__main__":
    main()
