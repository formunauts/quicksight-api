import argparse
from typing import Any, Dict, List, Optional, Set

from tqdm import tqdm

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


def extract_fields_from_json(obj: Any, target_fields: Optional[Set[str]] = None) -> Set[str]:
    if target_fields is None:
        target_fields = set()

    if isinstance(obj, dict):
        for key in ("FieldId", "ColumnName", "Name"):
            if key in obj and isinstance(obj[key], str):
                target_fields.add(obj[key])
        for value in obj.values():
            extract_fields_from_json(value, target_fields)
    elif isinstance(obj, list):
        for item in obj:
            extract_fields_from_json(item, target_fields)

    return target_fields


def load_all_analyses(qs_client) -> List[Dict[str, Any]]:
    return get_all_summaries(qs_client.list_analyses, QS_ACCOUNT_ID, "AnalysisSummaryList")


def audit_field_usage(qs_client, logger: Logger, dataset_id: str) -> None:
    analyses = load_all_analyses(qs_client)
    global_usage_stats: Dict[str, int] = {}
    matched_analyses = 0

    logger.log(f"Dataset ID: {dataset_id}")
    logger.log(f"Total analyses to scan: {len(analyses)}")
    logger.log("")

    for summary in tqdm(analyses, desc="Scanning analyses"):
        analysis_id = summary["AnalysisId"]
        try:
            details = qs_client.describe_analysis_definition(
                AwsAccountId=QS_ACCOUNT_ID,
                AnalysisId=analysis_id,
            )
        except Exception:
            continue

        declarations = details.get("Definition", {}).get("DataSetIdentifierDeclarations", [])
        if not any(dataset_id in str(declaration) for declaration in declarations):
            continue

        fields_found = sorted(extract_fields_from_json(details["Definition"]))
        if not fields_found:
            continue

        matched_analyses += 1
        logger.log(f"ANALYSIS: {summary['Name']} ({analysis_id})")
        logger.log(f"Fields used: {', '.join(fields_found)}")
        logger.log("-" * 40)
        for field in fields_found:
            global_usage_stats[field] = global_usage_stats.get(field, 0) + 1

    logger.log("")
    logger.log("=" * 60)
    logger.log("GLOBAL FIELD USAGE SUMMARY")
    logger.log("=" * 60)
    logger.log(f"Matched analyses: {matched_analyses}")
    if not global_usage_stats:
        logger.log("No field usage found for this dataset.")
        return

    for field, count in sorted(global_usage_stats.items(), key=lambda item: item[1], reverse=True):
        logger.log(f"{field}: used in {count} analyses")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit which analyses reference fields from one dataset.")
    parser.add_argument("dataset_id", help="QuickSight dataset id to inspect.")
    args = parser.parse_args()

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)
    safe_dataset_id = args.dataset_id.replace("/", "_")
    log_path = build_log_path(f"usage_{safe_dataset_id}")
    logger = Logger(log_path, "QUICKSIGHT FIELD USAGE AUDIT")

    try:
        qs_client = create_quicksight_client()
        logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
        logger.log(f"Log file: {log_path}")
        logger.log("")
        audit_field_usage(qs_client, logger, args.dataset_id)
        logger.log("")
        logger.log(f"Audit complete. Results saved to: {log_path}")
    except Exception as exc:
        logger.log(f"FATAL ERROR: {exc}")


if __name__ == "__main__":
    main()
