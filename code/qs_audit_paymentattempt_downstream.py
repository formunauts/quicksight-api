import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional, Set

import boto3
from dotenv import load_dotenv


load_dotenv()

QS_ACCOUNT_ID = os.getenv("QS_AWS_ACCOUNT_ID")
REGION = os.getenv("QS_AWS_REGION", "eu-central-1")
ROOT_DIR = sys.path[0].rsplit("\\code", 1)[0]
LOG_DIR = os.path.join(ROOT_DIR, "logs")
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

DEFAULT_TARGET_FIELDS = [
    "donation_object_id",
    "donation_object_id[Cancellations]",
    "donation_content_type_id",
    "donation_content_type_id[Cancellations]",
]


class Logger:
    def __init__(self, filename: str):
        self.filename = filename
        with open(self.filename, "w", encoding="utf-8") as handle:
            handle.write("QUICKSIGHT PAYMENTATTEMPT DOWNSTREAM AUDIT\n")
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


def load_plan(plan_file: str) -> Dict[str, Any]:
    with open(plan_file, "r", encoding="utf-8") as handle:
        return json.load(handle)


def extract_references(
    obj: Any,
    target_fields: List[str],
    path: str = "Definition",
) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            matches.extend(extract_references(value, target_fields, f"{path}.{key}"))
        return matches

    if isinstance(obj, list):
        for index, value in enumerate(obj):
            matches.extend(extract_references(value, target_fields, f"{path}[{index}]"))
        return matches

    if isinstance(obj, str):
        lower_value = obj.lower()
        for field in target_fields:
            if field.lower() in lower_value:
                matches.append(
                    {
                        "path": path,
                        "field": field,
                        "value": obj,
                    }
                )
        return matches

    return matches


def definition_uses_dataset(definition: Dict[str, Any], dataset_arns: Set[str], dataset_ids: Set[str]) -> bool:
    declarations = definition.get("DataSetIdentifierDeclarations", [])
    for declaration in declarations:
        dataset_arn = declaration.get("DataSetArn", "")
        if dataset_arn in dataset_arns:
            return True
        for dataset_id in dataset_ids:
            if dataset_id and dataset_id in dataset_arn:
                return True
    return False


def audit_analyses(
    qs_client,
    dataset_arns: Set[str],
    dataset_ids: Set[str],
    target_fields: List[str],
    logger: Logger,
) -> Tuple[List[Dict[str, Any]], int]:
    results: List[Dict[str, Any]] = []
    skipped = 0
    analyses = get_all_summaries(qs_client.list_analyses, QS_ACCOUNT_ID, "AnalysisSummaryList")
    logger.log(f"Scanning analyses: {len(analyses)} total")

    for index, summary in enumerate(analyses, start=1):
        if index == 1 or index % 25 == 0 or index == len(analyses):
            logger.log(f"  Analyses progress: {index}/{len(analyses)}")
        analysis_id = summary["AnalysisId"]
        try:
            response = qs_client.describe_analysis_definition(
                AwsAccountId=QS_ACCOUNT_ID,
                AnalysisId=analysis_id,
            )
        except Exception:
            skipped += 1
            continue

        definition = response.get("Definition", {})
        if not definition_uses_dataset(definition, dataset_arns, dataset_ids):
            continue

        references = extract_references(definition, target_fields)
        if not references:
            continue

        results.append(
            {
                "name": summary["Name"],
                "analysis_id": analysis_id,
                "references": references,
            }
        )

    return results, skipped


def audit_dashboards(
    qs_client,
    dataset_arns: Set[str],
    dataset_ids: Set[str],
    target_fields: List[str],
    logger: Logger,
) -> Tuple[List[Dict[str, Any]], int]:
    results: List[Dict[str, Any]] = []
    skipped = 0
    dashboards = get_all_summaries(qs_client.list_dashboards, QS_ACCOUNT_ID, "DashboardSummaryList")
    logger.log(f"Scanning dashboards: {len(dashboards)} total")

    for index, summary in enumerate(dashboards, start=1):
        if index == 1 or index % 25 == 0 or index == len(dashboards):
            logger.log(f"  Dashboards progress: {index}/{len(dashboards)}")
        dashboard_id = summary["DashboardId"]
        try:
            response = qs_client.describe_dashboard_definition(
                AwsAccountId=QS_ACCOUNT_ID,
                DashboardId=dashboard_id,
            )
        except Exception:
            skipped += 1
            continue

        definition = response.get("Definition", {})
        if not definition_uses_dataset(definition, dataset_arns, dataset_ids):
            continue

        references = extract_references(definition, target_fields)
        if not references:
            continue

        results.append(
            {
                "name": summary["Name"],
                "dashboard_id": dashboard_id,
                "references": references,
            }
        )

    return results, skipped


def write_json_report(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", REGION)
    os.makedirs(LOG_DIR, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="Audit analyses and dashboards impacted by the paymentattempt dataset migration."
    )
    parser.add_argument(
        "--plan-file",
        required=True,
        help="Plan file produced by qs_migrate_paymentattempt_datasets.py.",
    )
    parser.add_argument(
        "--fields",
        nargs="+",
        default=DEFAULT_TARGET_FIELDS,
        help="Field names to search for in downstream definitions.",
    )
    parser.add_argument(
        "--skip-dashboards",
        action="store_true",
        help="Only scan analyses and skip dashboard definitions.",
    )
    parser.add_argument(
        "--skip-analyses",
        action="store_true",
        help="Only scan dashboards and skip analysis definitions.",
    )
    args = parser.parse_args()

    log_path = os.path.join(LOG_DIR, f"paymentattempt_downstream_audit_{TIMESTAMP}.txt")
    json_path = os.path.join(LOG_DIR, f"paymentattempt_downstream_audit_{TIMESTAMP}.json")
    logger = Logger(log_path)

    plan = load_plan(args.plan_file)
    datasets = plan.get("datasets", [])
    dataset_arns = {dataset["arn"] for dataset in datasets if dataset.get("arn")}
    dataset_ids = {dataset["data_set_id"] for dataset in datasets if dataset.get("data_set_id")}

    qs = boto3.client("quicksight", region_name=REGION)
    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {REGION})")
    logger.log(f"Datasets in scope: {len(datasets)}")
    logger.log(f"Target fields: {', '.join(args.fields)}")
    logger.log(f"Text log: {log_path}")
    logger.log(f"JSON report: {json_path}")

    if args.skip_analyses:
        analysis_results, skipped_analyses = [], 0
    else:
        analysis_results, skipped_analyses = audit_analyses(
            qs,
            dataset_arns,
            dataset_ids,
            args.fields,
            logger,
        )

    if args.skip_dashboards:
        dashboard_results, skipped_dashboards = [], 0
    else:
        dashboard_results, skipped_dashboards = audit_dashboards(
            qs,
            dataset_arns,
            dataset_ids,
            args.fields,
            logger,
        )

    report = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": REGION,
        "plan_file": args.plan_file,
        "datasets_in_scope": [
            {
                "name": dataset["name"],
                "data_set_id": dataset["data_set_id"],
                "arn": dataset["arn"],
            }
            for dataset in datasets
        ],
        "target_fields": args.fields,
        "analyses": analysis_results,
        "dashboards": dashboard_results,
        "skipped_analyses": skipped_analyses,
        "skipped_dashboards": skipped_dashboards,
    }

    logger.log("")
    logger.log(f"Analyses with matching references: {len(analysis_results)}")
    for result in analysis_results:
        logger.log(f"  Analysis: {result['name']} ({result['analysis_id']})")
        for reference in result["references"][:10]:
            logger.log(
                f"    {reference['field']} at {reference['path']} = {reference['value']}"
            )
        if len(result["references"]) > 10:
            logger.log(
                f"    ... plus {len(result['references']) - 10} more references in the JSON report."
            )

    logger.log("")
    logger.log(f"Dashboards with matching references: {len(dashboard_results)}")
    for result in dashboard_results:
        logger.log(f"  Dashboard: {result['name']} ({result['dashboard_id']})")
        for reference in result["references"][:10]:
            logger.log(
                f"    {reference['field']} at {reference['path']} = {reference['value']}"
            )
        if len(result["references"]) > 10:
            logger.log(
                f"    ... plus {len(result['references']) - 10} more references in the JSON report."
            )

    logger.log("")
    logger.log(f"Analyses skipped because they could not be described: {skipped_analyses}")
    logger.log(f"Dashboards skipped because they could not be described: {skipped_dashboards}")
    logger.log(f"JSON report: {json_path}")
    logger.log(f"Text report: {log_path}")

    write_json_report(json_path, report)


if __name__ == "__main__":
    main()
