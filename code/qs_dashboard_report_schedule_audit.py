import argparse
import datetime
import json
from typing import Any, Dict, List, Optional, Set

from boto3.session import Session

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


SNAPSHOT_EVENT_NAMES = [
    "StartDashboardSnapshotJob",
    "StartDashboardSnapshotJobSchedule",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit QuickSight dashboard report schedules and snapshot executions. "
            "Uses CloudTrail events and snapshot job APIs."
        )
    )
    parser.add_argument(
        "--mystery-ids",
        nargs="+",
        help="Optional IDs to match (dashboard, schedule, snapshot job, request/event IDs, or IDs appearing in raw CloudTrail event JSON).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Look back this many days in CloudTrail when start/end dates are not provided (default: 30).",
    )
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD).")
    parser.add_argument("--end-date", help="End date (YYYY-MM-DD).")
    parser.add_argument(
        "--dashboard-id-contains",
        help="Optional case-insensitive dashboard ID/name substring filter before event enrichment.",
    )
    parser.add_argument(
        "--limit-dashboards",
        type=int,
        help="Optional dashboard limit after filters.",
    )
    parser.add_argument(
        "--all-regions",
        action="store_true",
        help="Run in every account-active AWS region that supports QuickSight.",
    )
    parser.add_argument(
        "--cloudtrail-region",
        help="Optional CloudTrail region override. Defaults to the current QuickSight region per scan.",
    )
    return parser.parse_args()


def get_target_regions(all_regions: bool) -> List[str]:
    if not all_regions:
        return [QS_REGION]

    session = Session()
    qs_supported = set(session.get_available_regions("quicksight"))
    if not qs_supported:
        return [QS_REGION]

    try:
        ec2 = session.client("ec2", region_name="us-east-1")
        response = ec2.describe_regions(AllRegions=True)
        active = {
            row.get("RegionName")
            for row in response.get("Regions", [])
            if row.get("OptInStatus") in {"opt-in-not-required", "opted-in"}
        }
        selected = sorted(region for region in qs_supported if region in active)
        selected = selected or sorted(qs_supported)
    except Exception:
        selected = sorted(qs_supported)

    if QS_REGION in selected:
        selected = [QS_REGION] + [region for region in selected if region != QS_REGION]
    return selected


def parse_time_range(args: argparse.Namespace) -> tuple[datetime.datetime, datetime.datetime]:
    if args.start_date or args.end_date:
        if not args.start_date or not args.end_date:
            raise SystemExit("Use both --start-date and --end-date together.")
        start = datetime.datetime.strptime(args.start_date, "%Y-%m-%d")
        end = datetime.datetime.strptime(args.end_date, "%Y-%m-%d") + datetime.timedelta(days=1)
        return start, end

    end = datetime.datetime.utcnow()
    start = end - datetime.timedelta(days=args.days)
    return start, end


def lookup_cloudtrail_events(
    cloudtrail_client,
    start_time: datetime.datetime,
    end_time: datetime.datetime,
    event_name: str,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    next_token: Optional[str] = None

    while True:
        kwargs: Dict[str, Any] = {
            "LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": event_name}],
            "StartTime": start_time,
            "EndTime": end_time,
            "MaxResults": 50,
        }
        if next_token:
            kwargs["NextToken"] = next_token

        response = cloudtrail_client.lookup_events(**kwargs)
        events.extend(response.get("Events", []))
        next_token = response.get("NextToken")
        if not next_token:
            break

    return events


def extract_event_fields(raw_event: Dict[str, Any]) -> Dict[str, Any]:
    payload_text = raw_event.get("CloudTrailEvent", "")
    payload = {}
    if isinstance(payload_text, str) and payload_text:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            payload = {}

    request = payload.get("requestParameters", {}) if isinstance(payload, dict) else {}
    dashboard_id = request.get("dashboardId")
    snapshot_job_id = request.get("snapshotJobId")
    schedule_id = request.get("scheduleId")

    return {
        "event_id": raw_event.get("EventId"),
        "event_name": raw_event.get("EventName"),
        "event_time": raw_event.get("EventTime"),
        "username": raw_event.get("Username"),
        "dashboard_id": dashboard_id,
        "snapshot_job_id": snapshot_job_id,
        "schedule_id": schedule_id,
        "raw_event": payload,
        "raw_event_text": payload_text,
    }


def matches_ids(row: Dict[str, Any], mystery_ids: Set[str]) -> bool:
    if not mystery_ids:
        return True

    candidates = {
        str(row.get("event_id") or ""),
        str(row.get("dashboard_id") or ""),
        str(row.get("snapshot_job_id") or ""),
        str(row.get("schedule_id") or ""),
        str(row.get("username") or ""),
    }
    if any(candidate in mystery_ids for candidate in candidates if candidate):
        return True

    raw_text = str(row.get("raw_event_text") or "")
    return any(mystery_id in raw_text for mystery_id in mystery_ids)


def enrich_snapshot_job(qs_client, row: Dict[str, Any]) -> None:
    dashboard_id = row.get("dashboard_id")
    snapshot_job_id = row.get("snapshot_job_id")
    if not dashboard_id or not snapshot_job_id:
        return

    try:
        details = qs_client.describe_dashboard_snapshot_job(
            AwsAccountId=QS_ACCOUNT_ID,
            DashboardId=dashboard_id,
            SnapshotJobId=snapshot_job_id,
        )
        row["job_status"] = details.get("JobStatus")
        row["job_created_time"] = details.get("CreatedTime")
        row["job_last_updated_time"] = details.get("LastUpdatedTime")
    except Exception as exc:
        row["job_details_error"] = str(exc)

    try:
        result = qs_client.describe_dashboard_snapshot_job_result(
            AwsAccountId=QS_ACCOUNT_ID,
            DashboardId=dashboard_id,
            SnapshotJobId=snapshot_job_id,
        )
        row["result_status"] = result.get("JobStatus")
        row["result_created_time"] = result.get("CreatedTime")
        row["result_last_updated_time"] = result.get("LastUpdatedTime")
        if isinstance(result.get("ErrorInfo"), dict):
            row["result_error_info"] = result.get("ErrorInfo")
    except Exception as exc:
        row["job_result_error"] = str(exc)


def main() -> None:
    args = parse_args()

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    start_time, end_time = parse_time_range(args)
    mystery_ids = set(args.mystery_ids or [])
    target_regions = get_target_regions(args.all_regions)

    text_log_path = build_log_path("dashboard_report_schedule_audit", "txt")
    json_log_path = build_log_path("dashboard_report_schedule_audit", "json")
    logger = Logger(text_log_path, "QUICKSIGHT DASHBOARD REPORT/SCHEDULE AUDIT")

    rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    region_statuses: List[Dict[str, str]] = []

    logger.log(f"Connected to QuickSight account: {QS_ACCOUNT_ID}")
    logger.log(f"Regions selected: {target_regions}")
    logger.log(f"CloudTrail range: {start_time.isoformat()} -> {end_time.isoformat()}")
    logger.log(
        f"CloudTrail region mode: {'fixed ' + args.cloudtrail_region if args.cloudtrail_region else 'per QuickSight region'}"
    )
    if mystery_ids:
        logger.log(f"Mystery IDs provided: {sorted(mystery_ids)}")

    for region in target_regions:
        logger.log("")
        logger.log("=" * 80)
        logger.log(f"REGION: {region}")
        logger.log("=" * 80)

        try:
            qs_client = create_quicksight_client(region=region)
            cloudtrail_region = args.cloudtrail_region or region
            cloudtrail_client = Session().client("cloudtrail", region_name=cloudtrail_region)
            logger.log(f"CloudTrail region in use: {cloudtrail_region}")

            dashboards = get_all_summaries(qs_client.list_dashboards, QS_ACCOUNT_ID, "DashboardSummaryList")
            if args.dashboard_id_contains:
                needle = args.dashboard_id_contains.lower()
                dashboards = [
                    d
                    for d in dashboards
                    if needle in str(d.get("DashboardId", "")).lower()
                    or needle in str(d.get("Name", "")).lower()
                ]
            if args.limit_dashboards:
                dashboards = dashboards[: args.limit_dashboards]

            dashboard_ids = {d.get("DashboardId") for d in dashboards if d.get("DashboardId")}
            logger.log(f"Dashboards selected for scope: {len(dashboard_ids)}")

            region_events: List[Dict[str, Any]] = []
            for event_name in SNAPSHOT_EVENT_NAMES:
                region_events.extend(lookup_cloudtrail_events(cloudtrail_client, start_time, end_time, event_name))

            logger.log(f"CloudTrail events found: {len(region_events)}")

            matched_in_region = 0
            for event in region_events:
                row = extract_event_fields(event)
                row["region"] = region
                row["cloudtrail_region"] = cloudtrail_region

                dashboard_id = row.get("dashboard_id")
                if dashboard_ids and dashboard_id and dashboard_id not in dashboard_ids:
                    continue
                if dashboard_ids and not dashboard_id:
                    continue

                if not matches_ids(row, mystery_ids):
                    continue

                enrich_snapshot_job(qs_client, row)
                rows.append(row)
                matched_in_region += 1

            logger.log(f"Matched rows in region: {matched_in_region}")
            region_statuses.append({"region": region, "status": "ok"})
        except Exception as exc:
            logger.log(f"REGION ERROR: {exc}")
            cloudtrail_region = args.cloudtrail_region or region
            errors.append(
                {
                    "region": region,
                    "cloudtrail_region": cloudtrail_region,
                    "error": str(exc),
                    "stage": "region_scan",
                }
            )
            region_statuses.append(
                {
                    "region": region,
                    "cloudtrail_region": cloudtrail_region,
                    "status": "error",
                    "error": str(exc),
                }
            )

    payload = {
        "account_id": QS_ACCOUNT_ID,
        "regions_selected": target_regions,
        "cloudtrail_region_mode": args.cloudtrail_region or "per_quicksight_region",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "mystery_ids": sorted(mystery_ids),
        "rows": rows,
        "errors": errors,
        "region_statuses": region_statuses,
        "summary": {
            "rows": len(rows),
            "errors": len(errors),
        },
    }

    with open(json_log_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)

    logger.log("")
    logger.log("SUMMARY")
    logger.log(f"Rows: {len(rows)}")
    logger.log(f"Errors: {len(errors)}")
    logger.log(f"Text report: {text_log_path}")
    logger.log(f"JSON report: {json_log_path}")


if __name__ == "__main__":
    main()