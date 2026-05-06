import argparse
import sys
from typing import Any, Dict, List, Optional, Tuple

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


def parse_source_entity_arn(source_arn: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not source_arn or ":" not in source_arn or "/" not in source_arn:
        return None, None

    resource_path = source_arn.split(":", 5)[-1]
    resource_type, _, resource_id = resource_path.partition("/")
    if not resource_type or not resource_id:
        return None, None
    return resource_type, resource_id


def collect_dashboard_targets(
    qs_client,
    dashboard_id: Optional[str],
    dashboard_name: Optional[str],
    dashboard_name_contains: Optional[str],
) -> List[Dict[str, str]]:
    if dashboard_id:
        response = qs_client.describe_dashboard(
            AwsAccountId=QS_ACCOUNT_ID,
            DashboardId=dashboard_id,
        )
        dashboard = response.get("Dashboard", {})
        return [
            {
                "DashboardId": dashboard.get("DashboardId", dashboard_id),
                "Name": dashboard.get("Name", dashboard_id),
            }
        ]

    dashboards = get_all_summaries(qs_client.list_dashboards, QS_ACCOUNT_ID, "DashboardSummaryList")
    if dashboard_name:
        return [item for item in dashboards if item.get("Name") == dashboard_name]

    needle = (dashboard_name_contains or "").lower()
    return [item for item in dashboards if needle in item.get("Name", "").lower()]


def log_dashboard_source(qs_client, logger: Logger, target: Dict[str, str]) -> None:
    dashboard_id = target["DashboardId"]
    response = qs_client.describe_dashboard(
        AwsAccountId=QS_ACCOUNT_ID,
        DashboardId=dashboard_id,
    )
    dashboard = response.get("Dashboard", {})
    version = dashboard.get("Version", {}) or {}
    source_arn = version.get("SourceEntityArn")
    source_type, source_id = parse_source_entity_arn(source_arn)

    logger.log("-" * 40)
    logger.log(f"DASHBOARD: {dashboard.get('Name', target.get('Name', dashboard_id))}")
    logger.log(f"Dashboard ID: {dashboard_id}")
    logger.log(f"Dashboard ARN: {dashboard.get('Arn', 'N/A')}")
    logger.log(f"Published version: {version.get('VersionNumber', 'N/A')}")
    logger.log(f"SourceEntityArn: {source_arn or 'N/A'}")

    if source_type == "analysis" and source_id:
        logger.log(f"Source type: analysis")
        logger.log(f"Analysis ID: {source_id}")
        try:
            analysis_response = qs_client.describe_analysis(
                AwsAccountId=QS_ACCOUNT_ID,
                AnalysisId=source_id,
            )
            analysis = analysis_response.get("Analysis", {})
            logger.log(f"Analysis name: {analysis.get('Name', 'N/A')}")
            logger.log(f"Analysis ARN: {analysis.get('Arn', 'N/A')}")
            logger.log(f"Analysis status: {analysis.get('Status', 'N/A')}")
        except Exception as exc:
            logger.log(f"Analysis lookup failed: {exc}")
        return

    if source_type and source_id:
        logger.log(f"Source type: {source_type}")
        logger.log(f"Source ID: {source_id}")
        logger.log("This dashboard source is not an analysis.")
        return

    logger.log("No source analysis information was exposed for this dashboard.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find the source entity behind one or more QuickSight dashboards."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dashboard-id", help="Inspect one dashboard by QuickSight dashboard id.")
    group.add_argument("--dashboard-name", help="Inspect dashboards whose name exactly matches this value.")
    group.add_argument(
        "--dashboard-name-contains",
        help="Inspect dashboards whose name contains this substring (case-insensitive).",
    )
    args = parser.parse_args()

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    log_path = build_log_path("quicksight_dashboard_source")
    logger = Logger(log_path, "QUICKSIGHT DASHBOARD SOURCE REPORT")

    try:
        qs_client = create_quicksight_client()
        logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
        logger.log(f"Command: {' '.join(sys.argv)}")
        logger.log(f"Log file: {log_path}")
        logger.log("")

        targets = collect_dashboard_targets(
            qs_client,
            dashboard_id=args.dashboard_id,
            dashboard_name=args.dashboard_name,
            dashboard_name_contains=args.dashboard_name_contains,
        )

        if args.dashboard_name:
            logger.log(f"Exact dashboard-name matches: {len(targets)}")
        elif args.dashboard_name_contains:
            logger.log(f"Substring dashboard-name matches: {len(targets)}")
        else:
            logger.log("Dashboard lookup mode: direct dashboard id")

        if not targets:
            logger.log("No matching dashboards found.")
            logger.log("")
            logger.log(f"DONE. Output saved to {log_path}")
            return

        for target in targets:
            log_dashboard_source(qs_client, logger, target)

        logger.log("")
        logger.log(f"DONE. Output saved to {log_path}")
    except Exception as exc:
        logger.log(f"FATAL ERROR: {exc}")


if __name__ == "__main__":
    main()
