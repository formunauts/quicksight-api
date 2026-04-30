import datetime
import os
from typing import Any, Dict, List, Optional

import boto3
from dotenv import load_dotenv


load_dotenv()

QS_ACCOUNT_ID = os.getenv("QS_AWS_ACCOUNT_ID")
QS_REGION = os.getenv("QS_AWS_REGION", "eu-central-1")
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT_DIR, "logs")


def timestamp_now() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_log_dir() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)


def build_log_path(prefix: str, extension: str = "txt", timestamp: Optional[str] = None) -> str:
    ensure_log_dir()
    stamp = timestamp or timestamp_now()
    return os.path.join(LOG_DIR, f"{prefix}_{stamp}.{extension}")


def require_env(name: str, value: Optional[str]) -> str:
    if value:
        return value
    raise SystemExit(f"Missing required environment variable: {name}")


def create_quicksight_client(region: Optional[str] = None):
    return boto3.client("quicksight", region_name=region or QS_REGION)


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


class Logger:
    def __init__(self, filename: str, title: str) -> None:
        self.filename = filename
        with open(self.filename, "w", encoding="utf-8") as handle:
            handle.write(f"{title}\n")
            handle.write(f"Generated on: {datetime.datetime.now().isoformat()}\n")
            handle.write("=" * 80 + "\n\n")

    def log(self, message: str = "") -> None:
        print(message)
        with open(self.filename, "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
