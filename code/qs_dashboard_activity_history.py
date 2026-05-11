import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    get_all_summaries,
    require_env,
)


DEFAULT_WRITE_EVENT_NAMES = {
    "CreateDashboard",
    "UpdateDashboard",
    "UpdateDashboardAccess",
    "UpdateDashboardPermissions",
    "DeleteDashboard",
}


def normalize(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def describe_auth_source(profile: Optional[str]) -> str:
    if profile:
        return f"named profile override ({profile})"
    if os.getenv("AWSUME_PROFILE"):
        return f"current awsume session ({os.getenv('AWSUME_PROFILE')})"
    if os.getenv("AWS_PROFILE"):
        return f"current shell profile ({os.getenv('AWS_PROFILE')})"
    if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SESSION_TOKEN"):
        return "current shell session credentials"
    if os.getenv("AWS_ACCESS_KEY_ID"):
        return "current shell long-lived credentials"
    return "default boto3 credential chain"


def create_boto3_session(profile: Optional[str]) -> boto3.Session:
    if profile:
        return boto3.Session(profile_name=profile)
    return boto3.Session()


def create_quicksight_client_for_session(session: boto3.Session, region: str):
    return session.client("quicksight", region_name=region)


def create_cloudtrail_client_for_session(session: boto3.Session, region: str):
    return session.client("cloudtrail", region_name=region)


def load_all_dashboards(qs_client) -> List[Dict[str, Any]]:
    return get_all_summaries(qs_client.list_dashboards, QS_ACCOUNT_ID, "DashboardSummaryList")


def parse_utc_date_start(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def parse_utc_date_end(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1) - timedelta(seconds=1)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_source_entity_arn(source_arn: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not source_arn or ":" not in source_arn or "/" not in source_arn:
        return None, None

    resource_path = source_arn.split(":", 5)[-1]
    resource_type, _, resource_id = resource_path.partition("/")
    if not resource_type or not resource_id:
        return None, None
    return resource_type, resource_id


def find_dashboard_target(
    qs_client,
    dashboard_id: Optional[str],
    dashboard_name: Optional[str],
) -> Dict[str, Any]:
    if dashboard_id:
        response = qs_client.describe_dashboard(
            AwsAccountId=QS_ACCOUNT_ID,
            DashboardId=dashboard_id,
        )
        dashboard = response.get("Dashboard", {})
        version = dashboard.get("Version", {}) or {}
        return {
            "dashboard_id": dashboard.get("DashboardId", dashboard_id),
            "name": dashboard.get("Name", dashboard_id),
            "arn": dashboard.get("Arn", ""),
            "created_time": dashboard.get("CreatedTime"),
            "last_updated_time": dashboard.get("LastUpdatedTime"),
            "last_published_time": version.get("CreatedTime"),
            "published_version": version.get("VersionNumber"),
            "source_entity_arn": version.get("SourceEntityArn"),
            "status": dashboard.get("Version", {}).get("Status") or dashboard.get("Status"),
        }

    dashboards = load_all_dashboards(qs_client)
    matches = [item for item in dashboards if item.get("Name") == dashboard_name]
    if not matches:
        raise SystemExit(f"No dashboard found with exact name: {dashboard_name}")
    if len(matches) > 1:
        names = ", ".join(f"{item['Name']} ({item['DashboardId']})" for item in matches[:10])
        raise SystemExit(f"Multiple dashboards matched '{dashboard_name}': {names}")

    dashboard_id = matches[0]["DashboardId"]
    response = qs_client.describe_dashboard(
        AwsAccountId=QS_ACCOUNT_ID,
        DashboardId=dashboard_id,
    )
    dashboard = response.get("Dashboard", {})
    version = dashboard.get("Version", {}) or {}
    return {
        "dashboard_id": dashboard.get("DashboardId", dashboard_id),
        "name": dashboard.get("Name", dashboard_name),
        "arn": dashboard.get("Arn", ""),
        "created_time": dashboard.get("CreatedTime"),
        "last_updated_time": dashboard.get("LastUpdatedTime"),
        "last_published_time": version.get("CreatedTime"),
        "published_version": version.get("VersionNumber"),
        "source_entity_arn": version.get("SourceEntityArn"),
        "status": dashboard.get("Version", {}).get("Status") or dashboard.get("Status"),
    }


def parse_cloudtrail_event(raw_event: str) -> Dict[str, Any]:
    try:
        return json.loads(raw_event)
    except json.JSONDecodeError:
        return {"raw_event": raw_event}


def summarize_user_identity(event: Dict[str, Any], fallback_username: Optional[str]) -> str:
    user_identity = event.get("userIdentity", {}) or {}
    username = fallback_username or ""
    if username and username.lower() != "unknown":
        return username

    arn = user_identity.get("arn")
    if arn:
        return arn

    session_issuer = user_identity.get("sessionContext", {}).get("sessionIssuer", {}) or {}
    issuer_arn = session_issuer.get("arn")
    if issuer_arn:
        return issuer_arn

    principal_id = user_identity.get("principalId")
    if principal_id:
        return principal_id

    return "unknown"


def event_matches_dashboard(
    event: Dict[str, Any],
    cloudtrail_event: Dict[str, Any],
    dashboard_id: str,
    dashboard_name: str,
    dashboard_arn: str,
) -> bool:
    targets = [value for value in (dashboard_id, dashboard_name, dashboard_arn) if value]
    if not targets:
        return False

    normalized_targets = [normalize(value) for value in targets]
    resources = event.get("Resources", []) or []
    resource_names = [normalize(item.get("ResourceName")) for item in resources if item.get("ResourceName")]
    resource_types = [normalize(item.get("ResourceType")) for item in resources if item.get("ResourceType")]
    cloudtrail_blob = normalize(json.dumps(cloudtrail_event, sort_keys=True))

    if any(target in resource_names for target in normalized_targets):
        return True
    if any("dashboard" in item for item in resource_types) and any(target in cloudtrail_blob for target in normalized_targets):
        return True
    return any(target in cloudtrail_blob for target in normalized_targets)


def lookup_quicksight_events(
    cloudtrail_client,
    start_time: datetime,
    end_time: datetime,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    next_token: Optional[str] = None

    while True:
        kwargs: Dict[str, Any] = {
            "LookupAttributes": [
                {
                    "AttributeKey": "EventSource",
                    "AttributeValue": "quicksight.amazonaws.com",
                }
            ],
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
            return events


def filter_dashboard_events(
    events: Sequence[Dict[str, Any]],
    dashboard_id: str,
    dashboard_name: str,
    dashboard_arn: str,
    include_read_only: bool,
    event_names: Optional[Sequence[str]],
    contains_text: Optional[Sequence[str]],
) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    normalized_event_names = {normalize(name) for name in (event_names or []) if name}
    normalized_terms = [normalize(term) for term in (contains_text or []) if term]

    for event in events:
        if not include_read_only and str(event.get("ReadOnly", "")).lower() == "true":
            continue

        if normalized_event_names and normalize(event.get("EventName")) not in normalized_event_names:
            continue

        cloudtrail_event = parse_cloudtrail_event(event.get("CloudTrailEvent", ""))
        if not event_matches_dashboard(
            event,
            cloudtrail_event,
            dashboard_id=dashboard_id,
            dashboard_name=dashboard_name,
            dashboard_arn=dashboard_arn,
        ):
            continue

        raw_blob = normalize(json.dumps(cloudtrail_event, sort_keys=True))
        if normalized_terms and not any(term in raw_blob for term in normalized_terms):
            continue

        filtered.append(
            {
                "event_time": event.get("EventTime"),
                "event_name": event.get("EventName"),
                "username": event.get("Username"),
                "actor": summarize_user_identity(cloudtrail_event, event.get("Username")),
                "read_only": str(event.get("ReadOnly", "")).lower() == "true",
                "event_id": event.get("EventId"),
                "event_source": event.get("EventSource"),
                "source_ip_address": cloudtrail_event.get("sourceIPAddress"),
                "user_agent": cloudtrail_event.get("userAgent"),
                "resources": event.get("Resources", []),
                "request_parameters": cloudtrail_event.get("requestParameters"),
                "response_elements": cloudtrail_event.get("responseElements"),
                "service_event_details": cloudtrail_event.get("serviceEventDetails"),
                "cloudtrail_event": cloudtrail_event,
            }
        )

    filtered.sort(key=lambda item: item.get("event_time") or datetime.min.replace(tzinfo=timezone.utc))
    return filtered


def serialize_for_json(value: Any) -> Any:
    if isinstance(value, datetime):
        return isoformat_utc(value)
    if isinstance(value, list):
        return [serialize_for_json(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize_for_json(item) for key, item in value.items()}
    return value


def log_event_summary(logger: Logger, event: Dict[str, Any], show_raw: bool) -> None:
    timestamp = event.get("event_time")
    if isinstance(timestamp, datetime):
        timestamp_text = isoformat_utc(timestamp)
    else:
        timestamp_text = str(timestamp)

    logger.log(f"{timestamp_text} | {event.get('event_name')} | {event.get('actor')}")
    logger.log(f"  Read only: {event.get('read_only')}")
    logger.log(f"  Source IP: {event.get('source_ip_address') or 'N/A'}")
    logger.log(f"  User agent: {event.get('user_agent') or 'N/A'}")
    if event.get("request_parameters") is not None:
        logger.log(f"  Request parameters: {json.dumps(event['request_parameters'], ensure_ascii=True, sort_keys=True)}")
    if event.get("service_event_details") is not None:
        logger.log(f"  Service event details: {json.dumps(event['service_event_details'], ensure_ascii=True, sort_keys=True)}")
    if show_raw:
        logger.log(f"  Raw event: {json.dumps(serialize_for_json(event['cloudtrail_event']), ensure_ascii=True, sort_keys=True)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show QuickSight activity history for one dashboard from CloudTrail."
    )
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--dashboard-id", help="QuickSight dashboard id.")
    target_group.add_argument("--dashboard-name", help="Exact QuickSight dashboard name.")
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="How many days of CloudTrail event history to inspect. CloudTrail LookupEvents supports at most 90 days.",
    )
    parser.add_argument(
        "--start-date",
        help="Optional UTC start date in YYYY-MM-DD format. Overrides --days when paired with --end-date.",
    )
    parser.add_argument(
        "--end-date",
        help="Optional UTC end date in YYYY-MM-DD format, inclusive through 23:59:59Z. Overrides --days when paired with --start-date.",
    )
    parser.add_argument(
        "--cloudtrail-region",
        default=QS_REGION,
        help="AWS region for CloudTrail LookupEvents. Defaults to QS_AWS_REGION.",
    )
    parser.add_argument(
        "--all-event-names",
        action="store_true",
        help="Do not restrict the search to the default dashboard write event names.",
    )
    parser.add_argument(
        "--include-read-only",
        action="store_true",
        help="Include read-only events like GetDashboard.",
    )
    parser.add_argument(
        "--event-names",
        nargs="+",
        help="Optional explicit CloudTrail event names to keep, for example UpdateDashboard GetDashboard.",
    )
    parser.add_argument(
        "--contains-text",
        nargs="+",
        help="Optional case-insensitive text terms that must appear in the raw CloudTrail event, for example export pdf csv xlsx download.",
    )
    parser.add_argument(
        "--profile",
        help="Optional AWS profile override. By default the script uses the credentials already active in your shell, including awsume.",
    )
    parser.add_argument(
        "--show-raw",
        action="store_true",
        help="Include the raw parsed CloudTrail event in the text report.",
    )
    args = parser.parse_args()

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    if bool(args.start_date) != bool(args.end_date):
        raise SystemExit("Use --start-date and --end-date together.")
    if args.start_date and args.end_date:
        start_time = parse_utc_date_start(args.start_date)
        end_time = parse_utc_date_end(args.end_date)
    else:
        if args.days < 1 or args.days > 90:
            raise SystemExit("--days must be between 1 and 90 because CloudTrail LookupEvents only supports the last 90 days.")
        start_time = datetime.now(timezone.utc) - timedelta(days=args.days)
        end_time = datetime.now(timezone.utc)

    if start_time > end_time:
        raise SystemExit("--start-date must be on or before --end-date.")

    if datetime.now(timezone.utc) - start_time > timedelta(days=90, hours=1):
        raise SystemExit("CloudTrail LookupEvents only supports roughly the last 90 days.")

    safe_target = (args.dashboard_id or args.dashboard_name or "dashboard").replace("/", "_").replace(" ", "_")
    log_path = build_log_path(f"dashboard_activity_history_{safe_target}")
    json_path = build_log_path(f"dashboard_activity_history_{safe_target}", extension="json")
    logger = Logger(log_path, "QUICKSIGHT DASHBOARD ACTIVITY HISTORY")
    session = create_boto3_session(args.profile)

    logger.log(f"Account: {QS_ACCOUNT_ID}")
    logger.log(f"QuickSight region: {QS_REGION}")
    logger.log(f"CloudTrail region: {args.cloudtrail_region}")
    logger.log(f"AWS auth source: {describe_auth_source(args.profile)}")
    logger.log(f"Time window start: {isoformat_utc(start_time)}")
    logger.log(f"Time window end: {isoformat_utc(end_time)}")
    logger.log(f"Text log: {log_path}")
    logger.log(f"JSON report: {json_path}")
    logger.log("")

    try:
        qs_client = create_quicksight_client_for_session(session, QS_REGION)
        cloudtrail_client = create_cloudtrail_client_for_session(session, args.cloudtrail_region)

        target = find_dashboard_target(
            qs_client,
            dashboard_id=args.dashboard_id,
            dashboard_name=args.dashboard_name,
        )
        source_type, source_id = parse_source_entity_arn(target.get("source_entity_arn"))

        if args.all_event_names:
            event_names = None
        else:
            event_names = args.event_names or sorted(DEFAULT_WRITE_EVENT_NAMES)

        quicksight_events = lookup_quicksight_events(
            cloudtrail_client,
            start_time=start_time,
            end_time=end_time,
        )
        matching_events = filter_dashboard_events(
            quicksight_events,
            dashboard_id=target["dashboard_id"],
            dashboard_name=target["name"],
            dashboard_arn=target["arn"],
            include_read_only=args.include_read_only,
            event_names=event_names,
            contains_text=args.contains_text,
        )

        logger.log(f"Dashboard: {target['name']} ({target['dashboard_id']})")
        logger.log(f"Dashboard ARN: {target['arn'] or 'N/A'}")
        logger.log(f"Status: {target.get('status') or 'N/A'}")
        logger.log(f"QuickSight last updated time: {target.get('last_updated_time') or 'N/A'}")
        logger.log(f"Published version: {target.get('published_version') or 'N/A'}")
        logger.log(f"Last published time: {target.get('last_published_time') or 'N/A'}")
        logger.log(f"Source entity ARN: {target.get('source_entity_arn') or 'N/A'}")
        if source_type and source_id:
            logger.log(f"Source type: {source_type}")
            logger.log(f"Source id: {source_id}")
        logger.log(f"Event name filter: {', '.join(event_names) if event_names else 'all QuickSight event names'}")
        logger.log(f"Raw text filter: {', '.join(args.contains_text) if args.contains_text else 'none'}")
        logger.log("")
        logger.log(f"QuickSight CloudTrail events scanned: {len(quicksight_events)}")
        logger.log(f"Matching dashboard events: {len(matching_events)}")
        logger.log("")

        for event in matching_events:
            log_event_summary(logger, event, args.show_raw)
            logger.log("")

        if not matching_events:
            logger.log("No matching CloudTrail events were found for this dashboard in the selected window.")
            logger.log("If the activity is older than 90 days, you would need a CloudTrail trail or Lake query instead of LookupEvents.")

        payload = {
            "account_id": QS_ACCOUNT_ID,
            "quicksight_region": QS_REGION,
            "cloudtrail_region": args.cloudtrail_region,
            "profile": args.profile,
            "time_window_start": isoformat_utc(start_time),
            "time_window_end": isoformat_utc(end_time),
            "dashboard": serialize_for_json(target),
            "source_type": source_type,
            "source_id": source_id,
            "event_names_filter": event_names,
            "contains_text_filter": args.contains_text,
            "include_read_only": args.include_read_only,
            "quicksight_events_scanned": len(quicksight_events),
            "matching_events": serialize_for_json(matching_events),
        }
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

        logger.log("")
        logger.log(f"JSON report written to: {json_path}")
        logger.log(f"Text log written to: {log_path}")
    except NoCredentialsError:
        logger.log("ERROR: Unable to locate AWS credentials for the current environment or profile.")
        raise SystemExit(1)
    except (BotoCoreError, ClientError) as exc:
        logger.log(f"ERROR: AWS request failed: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
