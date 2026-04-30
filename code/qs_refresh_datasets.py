import argparse
import datetime
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import boto3
from dotenv import load_dotenv


load_dotenv()

QS_ACCOUNT_ID = os.getenv("QS_AWS_ACCOUNT_ID")
REGION = os.getenv("QS_AWS_REGION", "eu-central-1")
ROOT_DIR = sys.path[0].rsplit("\\code", 1)[0]
LOG_DIR = os.path.join(ROOT_DIR, "logs")
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}


class Logger:
    def __init__(self, filename: str):
        self.filename = filename
        with open(self.filename, "w", encoding="utf-8") as handle:
            handle.write("QUICKSIGHT DATASET REFRESH\n")
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


def load_plan(plan_file: str) -> List[Dict[str, Any]]:
    with open(plan_file, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload.get("datasets", [])


def build_ingestion_id(dataset_id: str, index: int) -> str:
    compact_timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    safe_dataset_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in dataset_id)
    suffix = safe_dataset_id[-48:]
    return f"manual-refresh-{compact_timestamp}-{index:02d}-{suffix}"[:128]


def describe_dataset(qs_client, dataset_id: str) -> Dict[str, Any]:
    response = qs_client.describe_data_set(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
    )
    return response["DataSet"]


def wait_for_ingestion(qs_client, dataset_id: str, ingestion_id: str, poll_seconds: int, logger: Logger) -> Dict[str, Any]:
    while True:
        response = qs_client.describe_ingestion(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSetId=dataset_id,
            IngestionId=ingestion_id,
        )
        ingestion = response["Ingestion"]
        status = ingestion["IngestionStatus"]
        logger.log(f"    Ingestion status: {status}")
        if status in TERMINAL_STATUSES:
            return ingestion
        time.sleep(poll_seconds)


def collect_targets(
    qs_client,
    plan_rows: List[Dict[str, Any]],
    dataset_ids: Optional[List[str]],
) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []

    if plan_rows:
        for row in plan_rows:
            dataset_id = row["data_set_id"]
            targets.append(
                {
                    "data_set_id": dataset_id,
                    "name": row.get("name"),
                    "import_mode": row.get("import_mode"),
                }
            )

    for dataset_id in dataset_ids or []:
        targets.append(
            {
                "data_set_id": dataset_id,
                "name": None,
                "import_mode": None,
            }
        )

    deduped: Dict[str, Dict[str, Any]] = {}
    for target in targets:
        deduped[target["data_set_id"]] = target

    enriched_targets = []
    for target in deduped.values():
        if target["name"] and target["import_mode"]:
            enriched_targets.append(target)
            continue
        dataset = describe_dataset(qs_client, target["data_set_id"])
        enriched_targets.append(
            {
                "data_set_id": dataset["DataSetId"],
                "name": dataset["Name"],
                "import_mode": dataset["ImportMode"],
            }
        )
    return enriched_targets


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", REGION)
    os.makedirs(LOG_DIR, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="Refresh QuickSight datasets one at a time."
    )
    parser.add_argument(
        "--plan-file",
        help="Plan file produced by qs_migrate_paymentattempt_datasets.py.",
    )
    parser.add_argument(
        "--dataset-ids",
        nargs="+",
        help="Dataset IDs to refresh if you do not want to use a plan file.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually create ingestions. Without this flag, the script only previews the refresh order.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=15,
        help="Seconds to wait between ingestion status checks.",
    )
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Continue with the next dataset if one refresh fails.",
    )
    args = parser.parse_args()

    if not args.plan_file and not args.dataset_ids:
        raise SystemExit("Provide either --plan-file or --dataset-ids.")

    logger = Logger(os.path.join(LOG_DIR, f"quicksight_dataset_refresh_{TIMESTAMP}.txt"))
    qs = boto3.client("quicksight", region_name=REGION)
    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {REGION})")
    logger.log(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    logger.log("Refreshes are processed strictly one at a time.")

    plan_rows = load_plan(args.plan_file) if args.plan_file else []
    targets = collect_targets(qs, plan_rows, args.dataset_ids)

    logger.log(f"Targets discovered: {len(targets)}")

    refreshed = 0
    skipped = 0
    failed = 0

    for index, target in enumerate(targets, start=1):
        dataset_id = target["data_set_id"]
        dataset_name = target.get("name") or dataset_id
        import_mode = target.get("import_mode")

        logger.log("")
        logger.log(f"{index}. {dataset_name} ({dataset_id})")
        logger.log(f"   Import mode: {import_mode}")

        if import_mode != "SPICE":
            skipped += 1
            logger.log("   Skipped because the dataset is not SPICE-backed.")
            continue

        if not args.apply:
            logger.log("   Preview only. No ingestion was created.")
            continue

        ingestion_id = build_ingestion_id(dataset_id, index)
        logger.log(f"   Starting ingestion: {ingestion_id}")
        create_response = qs.create_ingestion(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSetId=dataset_id,
            IngestionId=ingestion_id,
            IngestionType="FULL_REFRESH",
        )
        logger.log(f"   Initial ingestion status: {create_response.get('IngestionStatus')}")

        try:
            ingestion = wait_for_ingestion(qs, dataset_id, ingestion_id, args.poll_seconds, logger)
        except Exception as exc:
            failed += 1
            logger.log(f"   Failed while polling ingestion: {exc}")
            if not args.continue_on_failure:
                logger.log("   Stopping after failure.")
                break
            continue

        status = ingestion["IngestionStatus"]
        if status == "COMPLETED":
            refreshed += 1
            logger.log("   Refresh completed successfully.")
        else:
            failed += 1
            error_info = ingestion.get("ErrorInfo", {})
            message = error_info.get("Message") or "No error message returned."
            logger.log(f"   Refresh finished with status {status}: {message}")
            if not args.continue_on_failure:
                logger.log("   Stopping after failure.")
                break

    logger.log("")
    logger.log(f"Completed refreshes: {refreshed}")
    logger.log(f"Skipped datasets: {skipped}")
    logger.log(f"Failed refreshes: {failed}")


if __name__ == "__main__":
    main()
