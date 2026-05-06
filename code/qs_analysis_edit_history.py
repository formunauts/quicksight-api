import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

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
    "CreateAnalysis",
    "UpdateAnalysis",
    "RenameAnalysis",
    "DeleteAnalysis",
    "RestoreAnalysis",
    "UpdateAnalysisAccess",
    "UpdateAnalysisPermissions",
    "CreateVisual",
    "RenameVisual",
    "DeleteVisual",
}
LAYOUT_KEYS = ("FreeFormLayout", "GridLayout", "SectionBasedLayout")


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


def load_all_analyses(qs_client) -> List[Dict[str, Any]]:
    return get_all_summaries(qs_client.list_analyses, QS_ACCOUNT_ID, "AnalysisSummaryList")


def find_analysis_target(
    qs_client,
    analysis_id: Optional[str],
    analysis_name: Optional[str],
) -> Dict[str, Any]:
    if analysis_id:
        response = qs_client.describe_analysis(
            AwsAccountId=QS_ACCOUNT_ID,
            AnalysisId=analysis_id,
        )
        analysis = response.get("Analysis", {})
        return {
            "analysis_id": analysis.get("AnalysisId", analysis_id),
            "name": analysis.get("Name", analysis_id),
            "arn": analysis.get("Arn", ""),
            "created_time": analysis.get("CreatedTime"),
            "last_updated_time": analysis.get("LastUpdatedTime"),
            "status": analysis.get("Status"),
        }

    analyses = load_all_analyses(qs_client)
    matches = [item for item in analyses if item.get("Name") == analysis_name]
    if not matches:
        raise SystemExit(f"No analysis found with exact name: {analysis_name}")
    if len(matches) > 1:
        names = ", ".join(f"{item['Name']} ({item['AnalysisId']})" for item in matches[:10])
        raise SystemExit(f"Multiple analyses matched '{analysis_name}': {names}")

    item = matches[0]
    return {
        "analysis_id": item.get("AnalysisId"),
        "name": item.get("Name"),
        "arn": item.get("Arn", ""),
        "created_time": item.get("CreatedTime"),
        "last_updated_time": item.get("LastUpdatedTime"),
        "status": item.get("Status"),
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


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def collect_layouts(obj: Any, path: str = "Definition") -> List[Dict[str, str]]:
    layouts: List[Dict[str, str]] = []

    if isinstance(obj, dict):
        found_keys = [key for key in LAYOUT_KEYS if key in obj and obj.get(key) is not None]
        for key in found_keys:
            layouts.append(
                {
                    "path": f"{path}.{key}",
                    "layout_type": key,
                }
            )
        for key, value in obj.items():
            layouts.extend(collect_layouts(value, f"{path}.{key}"))
        return layouts

    if isinstance(obj, list):
        for index, item in enumerate(obj):
            layouts.extend(collect_layouts(item, f"{path}[{index}]"))

    return layouts


def get_current_layout_summary(qs_client, analysis_id: str) -> Dict[str, Any]:
    response = qs_client.describe_analysis_definition(
        AwsAccountId=QS_ACCOUNT_ID,
        AnalysisId=analysis_id,
    )
    definition = response.get("Definition", {})
    occurrences = collect_layouts(definition)
    layout_types = sorted({item["layout_type"] for item in occurrences})
    return {
        "layout_types": layout_types,
        "occurrences": occurrences,
    }


def event_matches_analysis(
    event: Dict[str, Any],
    cloudtrail_event: Dict[str, Any],
    analysis_id: str,
    analysis_name: str,
    analysis_arn: str,
) -> bool:
    targets = [value for value in (analysis_id, analysis_name, analysis_arn) if value]
    if not targets:
        return False

    normalized_targets = [normalize(value) for value in targets]
    resources = event.get("Resources", []) or []
    resource_names = [normalize(item.get("ResourceName")) for item in resources if item.get("ResourceName")]
    resource_types = [normalize(item.get("ResourceType")) for item in resources if item.get("ResourceType")]
    cloudtrail_blob = normalize(json.dumps(cloudtrail_event, sort_keys=True))

    if any(target in resource_names for target in normalized_targets):
        return True
    if any("analysis" in item for item in resource_types) and any(target in cloudtrail_blob for target in normalized_targets):
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


def filter_analysis_events(
    events: Sequence[Dict[str, Any]],
    analysis_id: str,
    analysis_name: str,
    analysis_arn: str,
    include_read_only: bool,
    event_names: Optional[Sequence[str]],
) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    normalized_event_names = {normalize(name) for name in (event_names or []) if name}

    for event in events:
        if not include_read_only and str(event.get("ReadOnly", "")).lower() == "true":
            continue

        if normalized_event_names and normalize(event.get("EventName")) not in normalized_event_names:
            continue

        cloudtrail_event = parse_cloudtrail_event(event.get("CloudTrailEvent", ""))
        if not event_matches_analysis(
            event,
            cloudtrail_event,
            analysis_id=analysis_id,
            analysis_name=analysis_name,
            analysis_arn=analysis_arn,
        ):
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
        description="Show QuickSight edit history for one analysis from CloudTrail."
    )
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--analysis-id", help="QuickSight analysis id.")
    target_group.add_argument("--analysis-name", help="Exact QuickSight analysis name.")
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="How many days of CloudTrail event history to inspect. CloudTrail LookupEvents supports at most 90 days.",
    )
    parser.add_argument(
        "--cloudtrail-region",
        default=QS_REGION,
        help="AWS region for CloudTrail LookupEvents. Defaults to QS_AWS_REGION.",
    )
    parser.add_argument(
        "--include-read-only",
        action="store_true",
        help="Include read-only events like GetAnalysis. By default the report focuses on write/edit activity.",
    )
    parser.add_argument(
        "--event-names",
        nargs="+",
        help="Optional explicit CloudTrail event names to keep, for example UpdateAnalysis RenameAnalysis.",
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

    if args.days < 1 or args.days > 90:
        raise SystemExit("--days must be between 1 and 90 because CloudTrail LookupEvents only supports the last 90 days.")

    safe_target = (args.analysis_id or args.analysis_name or "analysis").replace("/", "_").replace(" ", "_")
    log_path = build_log_path(f"analysis_edit_history_{safe_target}")
    json_path = build_log_path(f"analysis_edit_history_{safe_target}", extension="json")
    logger = Logger(log_path, "QUICKSIGHT ANALYSIS EDIT HISTORY")

    start_time = datetime.now(timezone.utc) - timedelta(days=args.days)
    end_time = datetime.now(timezone.utc)
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

        target = find_analysis_target(
            qs_client,
            analysis_id=args.analysis_id,
            analysis_name=args.analysis_name,
        )
        current_layout = get_current_layout_summary(qs_client, target["analysis_id"])

        event_names = args.event_names or sorted(DEFAULT_WRITE_EVENT_NAMES)
        quicksight_events = lookup_quicksight_events(
            cloudtrail_client,
            start_time=start_time,
            end_time=end_time,
        )
        matching_events = filter_analysis_events(
            quicksight_events,
            analysis_id=target["analysis_id"],
            analysis_name=target["name"],
            analysis_arn=target["arn"],
            include_read_only=args.include_read_only,
            event_names=event_names,
        )

        logger.log(f"Analysis: {target['name']} ({target['analysis_id']})")
        logger.log(f"Analysis ARN: {target['arn'] or 'N/A'}")
        logger.log(f"Status: {target.get('status') or 'N/A'}")
        logger.log(f"QuickSight last updated time: {target.get('last_updated_time') or 'N/A'}")
        logger.log(f"Current layout types: {', '.join(current_layout['layout_types']) or 'none detected'}")
        for layout in current_layout["occurrences"]:
            logger.log(f"  Layout at {layout['path']}: {layout['layout_type']}")
        logger.log("")
        logger.log(f"QuickSight CloudTrail events scanned: {len(quicksight_events)}")
        logger.log(f"Matching analysis events: {len(matching_events)}")
        logger.log("")

        for event in matching_events:
            log_event_summary(logger, event, args.show_raw)
            logger.log("")

        if not matching_events:
            logger.log("No matching CloudTrail edit events were found for this analysis in the selected window.")
            logger.log("If the change is older than 90 days, you would need a CloudTrail trail or Lake query instead of LookupEvents.")

        payload = {
            "account_id": QS_ACCOUNT_ID,
            "quicksight_region": QS_REGION,
            "cloudtrail_region": args.cloudtrail_region,
            "profile": args.profile,
            "time_window_start": isoformat_utc(start_time),
            "time_window_end": isoformat_utc(end_time),
            "analysis": serialize_for_json(target),
            "current_layout": current_layout,
            "event_names_filter": event_names,
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
