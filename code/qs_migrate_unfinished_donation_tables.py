import argparse
import copy
import datetime
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Pattern, Set, Tuple

from qs_common import (
    LOG_DIR,
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    require_env,
)


OLD_UNFINISHED_TABLE = "bureau_unfinisheddonation"
NEW_DONATION_TABLE = "bureau_donation"
UNFINISHED_CONDITION = "kind != 'finished'"
SELF_CHECKOUT_CONDITION = "kind = 'unfinished_sco'"
TERMINAL_INGESTION_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
SQL_KEYWORDS = {
    "WHERE",
    "ON",
    "JOIN",
    "LEFT",
    "RIGHT",
    "FULL",
    "INNER",
    "OUTER",
    "CROSS",
    "GROUP",
    "ORDER",
    "HAVING",
    "LIMIT",
    "UNION",
    "QUALIFY",
    "WINDOW",
    "FETCH",
    "OFFSET",
}


def json_default(value: Any) -> str:
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    raise TypeError(f"Unsupported type: {type(value)!r}")


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "dataset"


def build_backup_dir() -> str:
    backup_dir = os.path.join(LOG_DIR, f"unfinished_donation_dataset_backups_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(backup_dir, exist_ok=True)
    return backup_dir


def write_json(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=json_default)


def write_backup(backup_dir: str, dataset: Dict[str, Any]) -> str:
    path = os.path.join(
        backup_dir,
        f"{slugify(dataset['Name'])}__{slugify(dataset['DataSetId'])}.json",
    )
    write_json(path, dataset)
    return path


def load_audit_dataset_ids(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    dataset_ids: List[str] = []
    for row in payload.get("datasets", []):
        dataset_id = row.get("data_set_id")
        if dataset_id:
            dataset_ids.append(dataset_id)
    return list(dict.fromkeys(dataset_ids))


def describe_dataset(qs_client, dataset_id: str) -> Dict[str, Any]:
    response = qs_client.describe_data_set(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
    )
    return response["DataSet"]


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


def old_table_pattern() -> Pattern[str]:
    identifier = r'(?:"bureau_unfinisheddonation"|`bureau_unfinisheddonation`|\[bureau_unfinisheddonation\]|bureau_unfinisheddonation)'
    keyword_guard = "|".join(sorted(SQL_KEYWORDS))
    return re.compile(
        rf"(?is)\b(?P<keyword>FROM|JOIN)\s+"
        rf"(?P<table>{identifier})"
        rf"(?P<alias>\s+(?:AS\s+)?(?!(?:{keyword_guard})\b)(?P<alias_name>\"?[A-Za-z_][A-Za-z0-9_]*\"?))?"
    )


def contains_old_table(sql: str) -> bool:
    return bool(re.search(r"(?i)(?<![A-Za-z0-9_])bureau_unfinisheddonation(?![A-Za-z0-9_])", sql))


def is_self_checkout_sql(sql: str, source_name: str) -> bool:
    combined = f"{source_name}\n{sql}"
    return bool(
        re.search(r"(?i)self[-_ ]?checkout", combined)
        or re.search(r"(?i)original_payment_method\s*=\s*['\"]self-checkout['\"]", sql)
    )


def build_replacement(alias: str, condition: str) -> str:
    alias_sql = alias.strip() if alias else f"AS {OLD_UNFINISHED_TABLE}"
    if alias_sql.upper().startswith("AS "):
        alias_sql = alias_sql
    else:
        alias_sql = f"AS {alias_sql}"
    return f"(SELECT * FROM {NEW_DONATION_TABLE} WHERE {condition}) {alias_sql}"


def rewrite_unfinished_table_sql(sql: str, source_name: str) -> Tuple[str, List[Dict[str, str]], List[str]]:
    pattern = old_table_pattern()
    warnings: List[str] = []
    replacements: List[Dict[str, str]] = []
    condition = SELF_CHECKOUT_CONDITION if is_self_checkout_sql(sql, source_name) else UNFINISHED_CONDITION

    def replace(match: re.Match[str]) -> str:
        alias = match.group("alias") or ""
        alias_name = (match.group("alias_name") or "").strip('"').upper()
        if alias_name in SQL_KEYWORDS:
            alias = ""
        replacement = f"{match.group('keyword')} {build_replacement(alias, condition)}"
        replacements.append(
            {
                "old": match.group(0),
                "new": replacement,
                "condition": condition,
            }
        )
        return replacement

    rewritten = pattern.sub(replace, sql)

    if not replacements and contains_old_table(sql):
        warnings.append("Old unfinished donation table was found, but no FROM/JOIN pattern could be rewritten.")
    if pattern.search(rewritten):
        warnings.append("Old unfinished donation table still appears after rewriting.")
    if re.search(r"(?is)\bSELECT\s+\*", sql):
        warnings.append("SQL uses SELECT *; review the proposed schema before applying.")

    return rewritten, replacements, warnings


def find_custom_sql_changes(dataset: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    updated_dataset = copy.deepcopy(dataset)
    changes: List[Dict[str, Any]] = []
    warnings: List[str] = []
    physical_map = updated_dataset.get("PhysicalTableMap", {})

    if not isinstance(physical_map, dict):
        return updated_dataset, changes, ["Dataset has no PhysicalTableMap dictionary."]

    for physical_table_id, physical_table in physical_map.items():
        if not isinstance(physical_table, dict):
            continue
        custom_sql = physical_table.get("CustomSql")
        if not isinstance(custom_sql, dict):
            continue
        original_sql = custom_sql.get("SqlQuery")
        if not isinstance(original_sql, str) or not contains_old_table(original_sql):
            continue

        source_name = custom_sql.get("Name") or physical_table_id
        proposed_sql, replacements, sql_warnings = rewrite_unfinished_table_sql(original_sql, str(source_name))
        if proposed_sql == original_sql:
            warnings.extend(sql_warnings)
            continue

        custom_sql["SqlQuery"] = proposed_sql
        change = {
            "physical_table_id": physical_table_id,
            "source_name": source_name,
            "path": f"PhysicalTableMap.{physical_table_id}.CustomSql.SqlQuery",
            "original_sql": original_sql,
            "proposed_sql": proposed_sql,
            "replacements": replacements,
            "warnings": sql_warnings,
        }
        changes.append(change)
        warnings.extend(sql_warnings)

    return updated_dataset, changes, warnings


def build_ingestion_id(dataset_id: str, index: int) -> str:
    compact_timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    suffix = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in dataset_id)[-48:]
    return f"unfinished-donation-refresh-{compact_timestamp}-{index:02d}-{suffix}"[:128]


def wait_for_ingestion(qs_client, dataset_id: str, ingestion_id: str, poll_seconds: int, logger: Logger) -> Dict[str, Any]:
    logger.log("    Waiting for this ingestion to finish before continuing to the next dataset.")
    while True:
        response = qs_client.describe_ingestion(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSetId=dataset_id,
            IngestionId=ingestion_id,
        )
        ingestion = response["Ingestion"]
        status = ingestion["IngestionStatus"]
        logger.log(f"    Ingestion status: {status}")
        if status in TERMINAL_INGESTION_STATUSES:
            logger.log(f"    Ingestion reached terminal status: {status}")
            return ingestion
        time.sleep(poll_seconds)


def refresh_dataset(qs_client, dataset: Dict[str, Any], index: int, poll_seconds: int, logger: Logger) -> Dict[str, Any]:
    ingestion_id = build_ingestion_id(dataset["DataSetId"], index)
    logger.log(f"  Starting SPICE refresh: {ingestion_id}")
    qs_client.create_ingestion(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset["DataSetId"],
        IngestionId=ingestion_id,
        IngestionType="FULL_REFRESH",
    )
    return wait_for_ingestion(qs_client, dataset["DataSetId"], ingestion_id, poll_seconds, logger)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate QuickSight dataset SQL from bureau_unfinisheddonation to filtered bureau_donation subqueries."
    )
    parser.add_argument("--audit-file", help="Table reference audit JSON from qs_find_table_references.py.")
    parser.add_argument("--dataset-ids", nargs="+", help="Dataset IDs to migrate without an audit file.")
    parser.add_argument("--apply", action="store_true", help="Update QuickSight datasets. Without this, only writes a migration plan.")
    parser.add_argument("--allow-warnings", action="store_true", help="Allow apply even when generated SQL has warnings such as SELECT *.")
    parser.add_argument(
        "--refresh-after-apply",
        "--wait-for-refresh",
        dest="refresh_after_apply",
        action="store_true",
        help="Run a full SPICE refresh after each applied dataset update and wait for it to finish before processing the next dataset.",
    )
    parser.add_argument("--refresh-poll-seconds", type=int, default=15, help="Seconds between refresh status checks.")
    parser.add_argument(
        "--between-refresh-delay-seconds",
        type=int,
        default=0,
        help="Optional pause after a completed refresh before updating the next dataset.",
    )
    parser.add_argument("--continue-on-failure", action="store_true", help="Continue with remaining datasets after update or refresh failure.")
    return parser.parse_args()


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    args = parse_args()
    if not args.audit_file and not args.dataset_ids:
        raise SystemExit("Provide --audit-file or --dataset-ids.")

    dataset_ids = []
    if args.audit_file:
        dataset_ids.extend(load_audit_dataset_ids(args.audit_file))
    dataset_ids.extend(args.dataset_ids or [])
    dataset_ids = list(dict.fromkeys(dataset_ids))

    backup_dir = build_backup_dir()
    text_log_path = build_log_path("unfinished_donation_dataset_migration", "txt")
    plan_path = build_log_path("unfinished_donation_dataset_migration_plan", "json")
    logger = Logger(text_log_path, "QUICKSIGHT UNFINISHED DONATION DATASET MIGRATION")
    qs_client = create_quicksight_client()

    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
    logger.log(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    logger.log(f"Dataset targets: {len(dataset_ids)}")
    logger.log(f"Backup directory: {backup_dir}")
    if args.refresh_after_apply:
        logger.log("Refresh mode: one SPICE refresh at a time; the script waits for a terminal ingestion status before continuing.")
        if args.between_refresh_delay_seconds:
            logger.log(f"Refresh spacing: {args.between_refresh_delay_seconds}s pause after each refresh.")
    else:
        logger.log("Refresh mode: disabled. Use --wait-for-refresh to refresh each SPICE dataset sequentially after update.")
    logger.log("")

    plan: Dict[str, Any] = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": QS_REGION,
        "mode": "APPLY" if args.apply else "DRY_RUN",
        "source_audit_file": args.audit_file,
        "backup_directory": backup_dir,
        "datasets": [],
        "summary": {},
    }

    changed = 0
    applied = 0
    skipped_apply = 0
    failed = 0

    for index, dataset_id in enumerate(dataset_ids, start=1):
        logger.log(f"{index}. Inspecting dataset {dataset_id}")
        try:
            original_dataset = describe_dataset(qs_client, dataset_id)
        except Exception as exc:
            failed += 1
            logger.log(f"  Failed to describe dataset: {exc}")
            plan["datasets"].append({"data_set_id": dataset_id, "error": str(exc)})
            if not args.continue_on_failure:
                break
            continue

        backup_path = write_backup(backup_dir, original_dataset)
        updated_dataset, changes, warnings = find_custom_sql_changes(original_dataset)
        has_warnings = bool(warnings)
        can_apply = bool(changes) and (args.allow_warnings or not has_warnings)

        row: Dict[str, Any] = {
            "name": original_dataset["Name"],
            "data_set_id": original_dataset["DataSetId"],
            "arn": original_dataset.get("Arn"),
            "import_mode": original_dataset.get("ImportMode"),
            "backup_file": backup_path,
            "changes": changes,
            "warnings": sorted(set(warnings)),
            "applied": False,
            "refresh": None,
        }
        plan["datasets"].append(row)

        logger.log(f"  Dataset: {original_dataset['Name']}")
        logger.log(f"  Backup: {backup_path}")
        logger.log(f"  SQL changes: {len(changes)}")
        for change in changes:
            logger.log(f"    Change: {change['source_name']} [{change['physical_table_id']}]")
            for replacement in change["replacements"]:
                logger.log(f"      {replacement['old'].strip()} -> {replacement['new'].strip()}")
        for warning in row["warnings"]:
            logger.log(f"  Warning: {warning}")

        if changes:
            changed += 1

        if not args.apply:
            logger.log("  Dry run only. No QuickSight update was sent.")
            logger.log("")
            continue

        if not can_apply:
            skipped_apply += 1
            logger.log("  Skipped apply because warnings require review. Use --allow-warnings after reviewing the plan.")
            logger.log("")
            continue

        try:
            response = qs_client.update_data_set(**build_update_payload(updated_dataset))
            row["applied"] = True
            row["update_status"] = response.get("Status")
            applied += 1
            logger.log(f"  Update applied. HTTP status: {response.get('Status')}")

            if args.refresh_after_apply and original_dataset.get("ImportMode") == "SPICE":
                ingestion = refresh_dataset(qs_client, original_dataset, index, args.refresh_poll_seconds, logger)
                row["refresh"] = {
                    "ingestion_id": ingestion.get("IngestionId"),
                    "status": ingestion.get("IngestionStatus"),
                    "error_info": ingestion.get("ErrorInfo"),
                }
                if ingestion.get("IngestionStatus") != "COMPLETED":
                    failed += 1
                    logger.log(f"  Refresh failed or stopped: {ingestion.get('IngestionStatus')}")
                    if not args.continue_on_failure:
                        break
                elif args.between_refresh_delay_seconds:
                    logger.log(
                        f"  Waiting {args.between_refresh_delay_seconds}s before processing the next dataset."
                    )
                    time.sleep(args.between_refresh_delay_seconds)
            elif args.refresh_after_apply:
                logger.log("  Refresh skipped because the dataset is not SPICE-backed.")
        except Exception as exc:
            failed += 1
            row["error"] = str(exc)
            logger.log(f"  Update failed: {exc}")
            if not args.continue_on_failure:
                break

        logger.log("")

    plan["summary"] = {
        "datasets_targeted": len(dataset_ids),
        "datasets_with_changes": changed,
        "datasets_applied": applied,
        "datasets_skipped_apply": skipped_apply,
        "failures": failed,
    }
    write_json(plan_path, plan)

    logger.log("SUMMARY")
    logger.log(f"Datasets targeted: {len(dataset_ids)}")
    logger.log(f"Datasets with changes: {changed}")
    logger.log(f"Datasets applied: {applied}")
    logger.log(f"Datasets skipped apply: {skipped_apply}")
    logger.log(f"Failures: {failed}")
    logger.log(f"Plan file: {plan_path}")
    logger.log(f"Text log: {text_log_path}")


if __name__ == "__main__":
    main()
