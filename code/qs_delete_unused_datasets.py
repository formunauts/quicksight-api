import argparse
import datetime
import json
import os
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv


load_dotenv()

QS_ACCOUNT_ID = os.getenv("QS_AWS_ACCOUNT_ID")
REGION = os.getenv("QS_AWS_REGION", "eu-central-1")
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT_DIR, "logs")
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


class Logger:
    def __init__(self, filename: str):
        self.filename = filename
        with open(self.filename, "w", encoding="utf-8") as handle:
            handle.write("QUICKSIGHT UNUSED DATASET DELETE\n")
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


def build_log_path() -> str:
    return os.path.join(LOG_DIR, f"quicksight_unused_dataset_delete_{TIMESTAMP}.txt")


def build_json_path() -> str:
    return os.path.join(LOG_DIR, f"quicksight_unused_dataset_delete_{TIMESTAMP}.json")


def load_consumer_audit(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def collect_unused_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(payload.get("unused_datasets"), list):
        return payload["unused_datasets"]
    rows = payload.get("datasets", [])
    return [row for row in rows if not row.get("is_used_anywhere")]


def describe_dataset(qs_client: Any, dataset_id: str) -> Dict[str, Any]:
    response = qs_client.describe_data_set(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
    )
    return response["DataSet"]


def delete_dataset(qs_client: Any, dataset_id: str) -> Dict[str, Any]:
    return qs_client.delete_data_set(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
    )


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", REGION)
    os.makedirs(LOG_DIR, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="Delete QuickSight datasets that are marked unused in a dataset consumer audit."
    )
    parser.add_argument(
        "--consumer-audit-file",
        required=True,
        help="JSON output from qs_audit_dataset_consumers.py or qs_filter_used_consumer_targets.py.",
    )
    parser.add_argument(
        "--dataset-ids",
        nargs="+",
        help="Optional exact dataset ids to limit deletion to a subset of the unused datasets.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the datasets. Without this flag, the script only previews the targets.",
    )
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Continue with the next dataset if one deletion fails.",
    )
    args = parser.parse_args()

    logger = Logger(build_log_path())
    report_path = build_json_path()
    qs = boto3.client("quicksight", region_name=REGION)

    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {REGION})")
    logger.log(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    logger.log(f"Consumer audit file: {os.path.abspath(args.consumer_audit_file)}")

    payload = load_consumer_audit(args.consumer_audit_file)
    unused_rows = collect_unused_rows(payload)
    if args.dataset_ids:
        requested_ids = set(args.dataset_ids)
        unused_rows = [row for row in unused_rows if row.get("data_set_id") in requested_ids]

    logger.log(f"Unused datasets selected: {len(unused_rows)}")

    deleted = 0
    skipped_missing = 0
    failed = 0
    report_rows: List[Dict[str, Any]] = []

    for index, row in enumerate(unused_rows, start=1):
        dataset_id = row.get("data_set_id")
        dataset_name = row.get("name") or dataset_id

        logger.log("")
        logger.log(f"Progress: dataset {index}/{len(unused_rows)}")
        logger.log(f"DATASET: {dataset_name} ({dataset_id})")

        result: Dict[str, Any] = {
            "name": dataset_name,
            "data_set_id": dataset_id,
            "arn": row.get("arn"),
            "analysis_count": row.get("analysis_count"),
            "dashboard_count": row.get("dashboard_count"),
            "is_used_anywhere": row.get("is_used_anywhere"),
            "exists": None,
            "import_mode": None,
            "applied": False,
            "status": None,
            "error": None,
        }

        try:
            current = describe_dataset(qs, dataset_id)
            result["exists"] = True
            result["import_mode"] = current.get("ImportMode")
            logger.log(f"  Import mode: {current.get('ImportMode')}")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"ResourceNotFoundException", "InvalidParameterValueException"}:
                skipped_missing += 1
                result["exists"] = False
                result["error"] = str(exc)
                logger.log("  Dataset no longer exists. Skipping.")
                report_rows.append(result)
                continue
            raise

        if not args.apply:
            logger.log("  Preview only. No delete request was sent.")
            report_rows.append(result)
            continue

        try:
            response = delete_dataset(qs, dataset_id)
            result["applied"] = True
            result["status"] = response.get("Status")
            deleted += 1
            logger.log(f"  Delete applied successfully. HTTP status: {response.get('Status')}")
        except Exception as exc:
            failed += 1
            result["error"] = str(exc)
            logger.log(f"  Failed to delete dataset: {exc}")
            if not args.continue_on_failure:
                report_rows.append(result)
                report = {
                    "generated_at": datetime.datetime.now().isoformat(),
                    "account_id": QS_ACCOUNT_ID,
                    "region": REGION,
                    "apply_requested": args.apply,
                    "source_consumer_audit_file": os.path.abspath(args.consumer_audit_file),
                    "datasets": report_rows,
                }
                with open(report_path, "w", encoding="utf-8") as handle:
                    json.dump(report, handle, indent=2)
                logger.log("  Stopping after failure.")
                logger.log(f"JSON report: {report_path}")
                logger.log(f"Text report: {logger.filename}")
                return

        report_rows.append(result)

    report = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": REGION,
        "apply_requested": args.apply,
        "source_consumer_audit_file": os.path.abspath(args.consumer_audit_file),
        "selected_unused_datasets": len(unused_rows),
        "deleted": deleted,
        "skipped_missing": skipped_missing,
        "failed": failed,
        "datasets": report_rows,
    }

    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)

    logger.log("")
    logger.log(f"Deleted datasets: {deleted}")
    logger.log(f"Skipped missing datasets: {skipped_missing}")
    logger.log(f"Failed deletions: {failed}")
    logger.log(f"JSON report: {report_path}")
    logger.log(f"Text report: {logger.filename}")


if __name__ == "__main__":
    main()
