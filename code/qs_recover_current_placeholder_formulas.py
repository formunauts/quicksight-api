import argparse
import datetime
import json
import os
import re
import sys
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


class Logger:
    def __init__(self, filename: str):
        self.filename = filename
        with open(self.filename, "w", encoding="utf-8") as handle:
            handle.write("QUICKSIGHT PLACEHOLDER FORMULA RECOVERY\n")
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


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "dataset"


def build_json_report_path() -> str:
    return os.path.join(LOG_DIR, f"placeholder_formula_recovery_{TIMESTAMP}.json")


def build_text_report_path() -> str:
    return os.path.join(LOG_DIR, f"placeholder_formula_recovery_{TIMESTAMP}.txt")


def build_restore_output_path(report_path: str, datasets: List[Dict[str, Any]]) -> str:
    base, _ = os.path.splitext(report_path)
    names = [d.get("name") for d in datasets if d.get("name")]
    unique_names = [name for name in dict.fromkeys(names) if isinstance(name, str) and name]
    if len(unique_names) == 1:
        return f"{base}__{slugify(unique_names[0])}_restore.txt"
    return f"{base}_restore.txt"


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
) -> List[Dict[str, Any]]:
    selected = []
    exact_ids = set(target_ids or [])
    exact_names = set(target_names or [])
    for dataset in all_datasets:
        if exact_ids and dataset["DataSetId"] not in exact_ids:
            continue
        if exact_names and dataset["Name"] not in exact_names:
            continue
        selected.append(dataset)
    return selected


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


def describe_dataset(qs_client: Any, dataset_id: str) -> Dict[str, Any]:
    response = qs_client.describe_data_set(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
    )
    return response["DataSet"]


def build_output_type_map(dataset: Dict[str, Any]) -> Dict[str, Optional[str]]:
    return {
        column.get("Name"): column.get("Type")
        for column in dataset.get("OutputColumns", [])
        if isinstance(column, dict) and isinstance(column.get("Name"), str)
    }


def placeholder_expression_for_type(output_type: Optional[str]) -> Optional[str]:
    if output_type == "STRING":
        return "'TEMP_MIGRATION_PLACEHOLDER'"
    if output_type in {"DECIMAL", "INTEGER"}:
        return "0"
    return None


def find_placeholder_columns(dataset: Dict[str, Any]) -> List[Dict[str, Any]]:
    placeholder_columns: List[Dict[str, Any]] = []
    output_types = build_output_type_map(dataset)
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


def iter_backup_files() -> List[Tuple[str, datetime.datetime]]:
    backups: List[Tuple[str, datetime.datetime]] = []
    for entry in os.scandir(LOG_DIR):
        if not entry.is_dir() or not entry.name.startswith("paymentattempt_dataset_backups_"):
            continue
        timestamp_text = entry.name.rsplit("_", 2)[-2] + "_" + entry.name.rsplit("_", 1)[-1]
        try:
            timestamp = datetime.datetime.strptime(timestamp_text, "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        for child in os.scandir(entry.path):
            if child.is_file() and child.name.lower().endswith(".json"):
                backups.append((child.path, timestamp))
    backups.sort(key=lambda item: item[1], reverse=True)
    return backups


def load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def find_original_expression_in_backup(
    backup_dataset: Dict[str, Any],
    column_name: str,
    placeholder_expression: str,
) -> Optional[Dict[str, Any]]:
    logical_map = backup_dataset.get("LogicalTableMap")
    if not isinstance(logical_map, dict):
        return None

    for logical_table_id, logical_table in logical_map.items():
        transforms = logical_table.get("DataTransforms")
        if not isinstance(transforms, list):
            continue
        for transform_index, transform in enumerate(transforms):
            operation = transform.get("CreateColumnsOperation")
            if not operation:
                continue
            for column_index, column in enumerate(operation.get("Columns", [])):
                if column.get("ColumnName") != column_name:
                    continue
                expression = column.get("Expression")
                if not isinstance(expression, str) or expression == placeholder_expression:
                    continue
                return {
                    "expression": expression,
                    "expression_path": (
                        f"LogicalTableMap.{logical_table_id}.DataTransforms[{transform_index}]"
                        f".CreateColumnsOperation.Columns[{column_index}].Expression"
                    ),
                }
    return None


def recover_formulas_for_dataset(
    dataset: Dict[str, Any],
    current_placeholders: List[Dict[str, Any]],
    backups: List[Tuple[str, datetime.datetime]],
) -> List[Dict[str, Any]]:
    recovered: List[Dict[str, Any]] = []
    dataset_id = dataset["DataSetId"]

    for placeholder in current_placeholders:
        found = None
        checked_backups = 0
        for backup_path, backup_timestamp in backups:
            backup_dataset = load_json(backup_path)
            if not backup_dataset or backup_dataset.get("DataSetId") != dataset_id:
                continue
            checked_backups += 1
            found_expression = find_original_expression_in_backup(
                backup_dataset,
                placeholder["column_name"],
                placeholder["placeholder_expression"],
            )
            if found_expression:
                found = {
                    "column_name": placeholder["column_name"],
                    "output_type": placeholder.get("output_type"),
                    "current_expression_path": placeholder["expression_path"],
                    "current_placeholder_expression": placeholder["placeholder_expression"],
                    "recovered_expression": found_expression["expression"],
                    "recovered_from_backup": backup_path,
                    "recovered_backup_timestamp": backup_timestamp.isoformat(),
                    "recovered_expression_path": found_expression["expression_path"],
                    "checked_matching_backups": checked_backups,
                }
                break

        if not found:
            found = {
                "column_name": placeholder["column_name"],
                "output_type": placeholder.get("output_type"),
                "current_expression_path": placeholder["expression_path"],
                "current_placeholder_expression": placeholder["placeholder_expression"],
                "recovered_expression": None,
                "recovered_from_backup": None,
                "recovered_backup_timestamp": None,
                "recovered_expression_path": None,
                "checked_matching_backups": checked_backups,
            }
        recovered.append(found)

    return recovered


def write_restore_file(report: Dict[str, Any], output_path: str) -> Optional[str]:
    sections: List[str] = []
    for dataset in report.get("datasets", []):
        placeholder_columns = dataset.get("current_placeholder_columns") or []
        recovered_formulas = dataset.get("recovered_formulas") or []
        if not placeholder_columns:
            continue

        sections.append(f"DATASET: {dataset.get('name')} ({dataset.get('data_set_id')})")
        sections.append("")
        if recovered_formulas:
            for index, formula in enumerate(recovered_formulas, start=1):
                sections.append(f"FIELD {index}: {formula.get('column_name')}")
                sections.append(f"Output type: {formula.get('output_type')}")
                sections.append(f"Current expression path: {formula.get('current_expression_path')}")
                if formula.get("recovered_expression"):
                    sections.append(f"Recovered from backup: {formula.get('recovered_from_backup')}")
                    sections.append("Recovered original expression:")
                    sections.append(formula.get("recovered_expression") or "")
                else:
                    sections.append("Recovered original expression: NOT FOUND")
                sections.append("")
                sections.append("=" * 80)
                sections.append("")
        else:
            sections.append("No placeholder columns found.")
            sections.append("")
            sections.append("=" * 80)
            sections.append("")

    if not sections:
        return None

    with open(output_path, "w", encoding="utf-8-sig", newline="\n") as handle:
        handle.write("\n".join(sections).rstrip() + "\n")
    return output_path


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", REGION)
    os.makedirs(LOG_DIR, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="Find currently placeholder-valued calculated fields and recover their original formulas from older backup files."
    )
    parser.add_argument("--datasets", nargs="+", help="Exact dataset names to inspect.")
    parser.add_argument(
        "--plan-file",
        help="Use the dataset ids/names from an existing plan file.",
    )
    args = parser.parse_args()

    target_names = args.datasets or []
    target_ids: List[str] = []

    if args.plan_file:
        plan_names, plan_ids = load_dataset_targets_from_plan(args.plan_file)
        if not target_names:
            target_names = plan_names
        target_ids = plan_ids

    if not target_names and not target_ids:
        raise SystemExit("Provide --datasets or --plan-file.")

    text_report_path = build_text_report_path()
    json_report_path = build_json_report_path()
    logger = Logger(text_report_path)

    session = boto3.Session(region_name=REGION)
    qs = session.client("quicksight")
    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {REGION})")

    all_datasets = get_all_summaries(qs.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
    selected = select_datasets(all_datasets, target_ids or None, target_names or None)
    logger.log(f"Datasets discovered in account: {len(all_datasets)}")
    logger.log(f"Datasets selected for inspection: {len(selected)}")

    backups = iter_backup_files()
    logger.log(f"Backup files available for recovery search: {len(backups)}")

    report: Dict[str, Any] = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": REGION,
        "datasets": [],
    }

    for index, summary in enumerate(selected, start=1):
        logger.log("")
        logger.log(f"Progress: dataset {index}/{len(selected)}")
        logger.log(f"DATASET: {summary['Name']} ({summary['DataSetId']})")
        try:
            dataset = describe_dataset(qs, summary["DataSetId"])
        except ClientError as exc:
            logger.log(f"  Failed to describe dataset: {exc}")
            report["datasets"].append(
                {
                    "name": summary["Name"],
                    "data_set_id": summary["DataSetId"],
                    "error": str(exc),
                    "current_placeholder_columns": [],
                    "recovered_formulas": [],
                }
            )
            continue

        placeholder_columns = find_placeholder_columns(dataset)
        logger.log(f"  Current placeholder-valued calculated fields: {len(placeholder_columns)}")
        for column in placeholder_columns[:10]:
            logger.log(f"    Placeholder: {column['column_name']} at {column['expression_path']}")

        recovered = recover_formulas_for_dataset(dataset, placeholder_columns, backups)
        recovered_count = sum(1 for item in recovered if item.get("recovered_expression"))
        if placeholder_columns:
            logger.log(f"  Recovered original formulas from backups: {recovered_count}/{len(placeholder_columns)}")
            missing = [item["column_name"] for item in recovered if not item.get("recovered_expression")]
            for column_name in missing[:10]:
                logger.log(f"    Missing recovery: {column_name}")

        report["datasets"].append(
            {
                "name": dataset["Name"],
                "data_set_id": dataset["DataSetId"],
                "arn": dataset["Arn"],
                "import_mode": dataset["ImportMode"],
                "current_placeholder_columns": placeholder_columns,
                "recovered_formulas": recovered,
            }
        )

    with open(json_report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)

    restore_path = write_restore_file(
        report,
        build_restore_output_path(json_report_path, report.get("datasets", [])),
    )

    logger.log("")
    logger.log(f"JSON report: {json_report_path}")
    if restore_path:
        logger.log(f"Restore file: {restore_path}")
    else:
        logger.log("Restore file: no current placeholder-valued calculated fields were found.")
    logger.log(f"Text report: {text_report_path}")


if __name__ == "__main__":
    main()
