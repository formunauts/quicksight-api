import argparse
import datetime
import json
import os
from typing import Any, Dict, List, Optional, Set

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
            handle.write("QUICKSIGHT DATASET CONSUMER AUDIT\n")
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


def get_all_summaries(func, account_id: str, key_name: str, list_key_override: Optional[str] = None) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    next_token = None
    while True:
        kwargs = {"AwsAccountId": account_id}
        if next_token:
            kwargs["NextToken"] = next_token
        response = func(**kwargs)
        response_key = list_key_override or key_name
        items.extend(response.get(response_key, []))
        next_token = response.get("NextToken")
        if not next_token:
            return items


def load_targets(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload.get("datasets", [])


def collect_dataset_arn_map(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    arn_map: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        arn = row.get("arn")
        if arn:
            arn_map[arn] = row
    return arn_map


def extract_dataset_arns_from_definition(definition: Dict[str, Any]) -> Set[str]:
    declarations = definition.get("DataSetIdentifierDeclarations", [])
    arns: Set[str] = set()
    for item in declarations:
        arn = item.get("DataSetArn")
        if isinstance(arn, str):
            arns.add(arn)
    return arns


def build_json_path() -> str:
    return os.path.join(LOG_DIR, f"dataset_consumer_audit_{TIMESTAMP}.json")


def build_log_path() -> str:
    return os.path.join(LOG_DIR, f"dataset_consumer_audit_{TIMESTAMP}.txt")


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", REGION)
    os.makedirs(LOG_DIR, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="Audit which analyses and dashboards consume the datasets in a target plan."
    )
    parser.add_argument("--plan-file", required=True, help="Target plan or migration plan JSON file.")
    parser.add_argument("--skip-dashboards", action="store_true", help="Skip dashboard scanning.")
    parser.add_argument("--skip-analyses", action="store_true", help="Skip analysis scanning.")
    args = parser.parse_args()

    targets = load_targets(args.plan_file)
    if not targets:
        raise SystemExit("No datasets found in the provided plan file.")

    logger = Logger(build_log_path())
    json_path = build_json_path()

    qs = boto3.client("quicksight", region_name=REGION)
    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {REGION})")
    logger.log(f"Datasets in scope: {len(targets)}")
    logger.log(f"JSON report: {json_path}")

    dataset_arn_map = collect_dataset_arn_map(targets)
    usage: Dict[str, Dict[str, Any]] = {
        row["arn"]: {
            "name": row.get("name"),
            "data_set_id": row.get("data_set_id"),
            "arn": row.get("arn"),
            "analyses": [],
            "dashboards": [],
        }
        for row in targets
        if row.get("arn")
    }

    skipped_analyses: List[Dict[str, str]] = []
    skipped_dashboards: List[Dict[str, str]] = []

    if not args.skip_analyses:
        analyses = get_all_summaries(qs.list_analyses, QS_ACCOUNT_ID, "AnalysisSummaryList")
        logger.log(f"Scanning analyses: {len(analyses)}")
        for index, summary in enumerate(analyses, start=1):
            if index == 1 or index % 25 == 0:
                logger.log(f"  Analyses progress: {index}/{len(analyses)}")
            try:
                response = qs.describe_analysis_definition(
                    AwsAccountId=QS_ACCOUNT_ID,
                    AnalysisId=summary["AnalysisId"],
                )
            except Exception as exc:
                skipped_analyses.append(
                    {
                        "name": summary.get("Name"),
                        "analysis_id": summary.get("AnalysisId"),
                        "error": str(exc),
                    }
                )
                continue
            used_arns = extract_dataset_arns_from_definition(response.get("Definition", {}))
            for arn in used_arns:
                if arn in dataset_arn_map:
                    usage[arn]["analyses"].append(
                        {
                            "name": summary.get("Name"),
                            "analysis_id": summary.get("AnalysisId"),
                        }
                    )

    if not args.skip_dashboards:
        dashboards = get_all_summaries(
            qs.list_dashboards,
            QS_ACCOUNT_ID,
            "DashboardSummaryList",
        )
        logger.log(f"Scanning dashboards: {len(dashboards)}")
        for index, summary in enumerate(dashboards, start=1):
            if index == 1 or index % 25 == 0:
                logger.log(f"  Dashboards progress: {index}/{len(dashboards)}")
            try:
                response = qs.describe_dashboard_definition(
                    AwsAccountId=QS_ACCOUNT_ID,
                    DashboardId=summary["DashboardId"],
                )
            except Exception as exc:
                skipped_dashboards.append(
                    {
                        "name": summary.get("Name"),
                        "dashboard_id": summary.get("DashboardId"),
                        "error": str(exc),
                    }
                )
                continue
            used_arns = extract_dataset_arns_from_definition(response.get("Definition", {}))
            for arn in used_arns:
                if arn in dataset_arn_map:
                    usage[arn]["dashboards"].append(
                        {
                            "name": summary.get("Name"),
                            "dashboard_id": summary.get("DashboardId"),
                        }
                    )

    rows = []
    for arn, row in usage.items():
        rows.append(
            {
                **row,
                "analysis_count": len(row["analyses"]),
                "dashboard_count": len(row["dashboards"]),
                "is_used_anywhere": bool(row["analyses"] or row["dashboards"]),
            }
        )

    rows.sort(
        key=lambda item: (
            item["analysis_count"] + item["dashboard_count"],
            item["name"] or "",
        ),
        reverse=True,
    )

    report = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": REGION,
        "source_plan_file": os.path.abspath(args.plan_file),
        "summary": {
            "datasets_in_scope": len(rows),
            "datasets_used_anywhere": sum(1 for row in rows if row["is_used_anywhere"]),
            "datasets_with_no_known_consumers": sum(1 for row in rows if not row["is_used_anywhere"]),
            "skipped_analyses": len(skipped_analyses),
            "skipped_dashboards": len(skipped_dashboards),
        },
        "datasets": rows,
        "skipped_analyses": skipped_analyses,
        "skipped_dashboards": skipped_dashboards,
    }

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)

    logger.log("")
    logger.log(f"Datasets used anywhere: {report['summary']['datasets_used_anywhere']}")
    logger.log(f"Datasets with no known consumers: {report['summary']['datasets_with_no_known_consumers']}")
    logger.log(f"Skipped analyses: {report['summary']['skipped_analyses']}")
    logger.log(f"Skipped dashboards: {report['summary']['skipped_dashboards']}")


if __name__ == "__main__":
    main()
