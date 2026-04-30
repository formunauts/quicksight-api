import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional

import boto3
from dotenv import load_dotenv

from qs_migrate_paymentattempt_datasets import (
    LOG_DIR,
    QS_ACCOUNT_ID,
    REGION,
    Logger,
    build_update_payload,
    require_env,
)


load_dotenv()

TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def load_plan(plan_file: str) -> Dict[str, Any]:
    with open(plan_file, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_backup(backup_file: str) -> Dict[str, Any]:
    with open(backup_file, "r", encoding="utf-8") as handle:
        return json.load(handle)


def dataset_matches_name(dataset_name: str, selected_names: Optional[List[str]]) -> bool:
    if not selected_names:
        return True
    return dataset_name in set(selected_names)


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", REGION)
    os.makedirs(LOG_DIR, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="Restore QuickSight datasets from backup files referenced by a migration plan."
    )
    parser.add_argument(
        "--plan-file",
        required=True,
        help="Migration plan JSON containing backup_file references.",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        help="Optional exact dataset names to restore.",
    )
    parser.add_argument(
        "--include-unapplied",
        action="store_true",
        help="Restore all datasets in the plan, not only those marked as applied.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the restore. Without this flag, the script only previews the restore set.",
    )
    args = parser.parse_args()

    logger = Logger(os.path.join(LOG_DIR, f"paymentattempt_dataset_restore_{TIMESTAMP}.txt"))
    qs = boto3.client("quicksight", region_name=REGION)

    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {REGION})")
    logger.log(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    logger.log(f"Plan file: {args.plan_file}")

    plan = load_plan(args.plan_file)
    candidates = []
    for dataset in plan.get("datasets", []):
        if not dataset_matches_name(dataset.get("name", ""), args.datasets):
            continue
        if not args.include_unapplied and not dataset.get("applied"):
            continue
        candidates.append(dataset)

    logger.log(f"Datasets selected for restore: {len(candidates)}")

    restored_count = 0
    failed_count = 0

    for dataset in candidates:
        logger.log("")
        logger.log(f"DATASET: {dataset['name']} ({dataset['data_set_id']})")
        logger.log(f"Backup: {dataset['backup_file']}")
        if dataset.get("warnings"):
            logger.log(f"Previous warnings: {len(dataset['warnings'])}")

        if not args.apply:
            logger.log("  Preview only. No QuickSight changes were sent.")
            continue

        backup_definition = load_backup(dataset["backup_file"])
        payload = build_update_payload(backup_definition)
        try:
            response = qs.update_data_set(**payload)
            restored_count += 1
            logger.log(f"  Restore applied successfully. HTTP status: {response.get('Status')}")
        except Exception as exc:  # pragma: no cover - depends on AWS state
            failed_count += 1
            logger.log(f"  Failed to restore dataset: {exc}")

    logger.log("")
    logger.log(f"Datasets restored: {restored_count}")
    logger.log(f"Restore failures: {failed_count}")


if __name__ == "__main__":
    main()
