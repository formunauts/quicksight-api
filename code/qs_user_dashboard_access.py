import argparse
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import ClientError, NoCredentialsError

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


RETRYABLE_ERROR_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "TooManyRequests",
    "LimitExceededException",
}


def parse_source_entity_arn(source_arn: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not source_arn or ":" not in source_arn or "/" not in source_arn:
        return None, None

    resource_path = source_arn.split(":", 5)[-1]
    resource_type, _, resource_id = resource_path.partition("/")
    if not resource_type or not resource_id:
        return None, None
    return resource_type, resource_id


def list_all_users(qs_client) -> List[Dict[str, Any]]:
    users: List[Dict[str, Any]] = []
    next_token = None
    while True:
        kwargs: Dict[str, Any] = {
            "AwsAccountId": QS_ACCOUNT_ID,
            "Namespace": "default",
        }
        if next_token:
            kwargs["NextToken"] = next_token
        response = qs_client.list_users(**kwargs)
        users.extend(response.get("UserList", []))
        next_token = response.get("NextToken")
        if not next_token:
            return users


def list_user_groups(qs_client, user_name: str) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    next_token = None
    while True:
        kwargs: Dict[str, Any] = {
            "AwsAccountId": QS_ACCOUNT_ID,
            "Namespace": "default",
            "UserName": user_name,
        }
        if next_token:
            kwargs["NextToken"] = next_token
        response = qs_client.list_user_groups(**kwargs)
        groups.extend(response.get("GroupList", []))
        next_token = response.get("NextToken")
        if not next_token:
            return groups


def resolve_user(
    users: List[Dict[str, Any]],
    user_arn: Optional[str],
    user_email: Optional[str],
    user_name: Optional[str],
) -> Dict[str, Any]:
    if user_arn:
        matches = [item for item in users if item.get("Arn") == user_arn]
        if not matches:
            raise SystemExit(f"No QuickSight user found with ARN: {user_arn}")
        return matches[0]

    if user_email:
        matches = [item for item in users if (item.get("Email") or "").lower() == user_email.lower()]
        if not matches:
            raise SystemExit(f"No QuickSight user found with email: {user_email}")
        if len(matches) > 1:
            preview = ", ".join(item.get("Arn", "N/A") for item in matches[:10])
            raise SystemExit(f"Multiple users matched email '{user_email}': {preview}")
        return matches[0]

    if user_name:
        matches = [item for item in users if item.get("UserName") == user_name]
        if not matches:
            raise SystemExit(f"No QuickSight user found with UserName: {user_name}")
        if len(matches) > 1:
            preview = ", ".join(item.get("Arn", "N/A") for item in matches[:10])
            raise SystemExit(f"Multiple users matched UserName '{user_name}': {preview}")
        return matches[0]

    raise SystemExit("Provide one of --user-arn, --user-email, or --user-name.")


def describe_dashboard_permissions(qs_client, dashboard_id: str) -> List[Dict[str, Any]]:
    response = qs_client.describe_dashboard_permissions(
        AwsAccountId=QS_ACCOUNT_ID,
        DashboardId=dashboard_id,
    )
    return response.get("Permissions", [])


def is_retryable_error(exc: Exception) -> bool:
    if not isinstance(exc, ClientError):
        return False
    error_code = exc.response.get("Error", {}).get("Code")
    return error_code in RETRYABLE_ERROR_CODES


def call_with_retries(
    logger: Logger,
    label: str,
    func,
    retry_attempts: int,
    retry_base_seconds: float,
    *args,
    **kwargs,
):
    attempt = 1
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            if attempt >= retry_attempts or not is_retryable_error(exc):
                raise
            wait_seconds = retry_base_seconds * (2 ** (attempt - 1))
            logger.log(
                f"Retryable QuickSight error during {label}: {exc}. "
                f"Retrying in {wait_seconds:.1f}s ({attempt}/{retry_attempts - 1})."
            )
            time.sleep(wait_seconds)
            attempt += 1


def collect_dashboard_access(
    qs_client,
    logger: Logger,
    target_principals: Dict[str, Dict[str, str]],
    dashboard_name_contains: Optional[str],
    retry_attempts: int,
    retry_base_seconds: float,
    between_dashboard_seconds: float,
) -> List[Dict[str, Any]]:
    dashboards = get_all_summaries(qs_client.list_dashboards, QS_ACCOUNT_ID, "DashboardSummaryList")
    name_filter = (dashboard_name_contains or "").lower()

    results: List[Dict[str, Any]] = []
    for summary in dashboards:
        dashboard_id = summary.get("DashboardId")
        dashboard_name = summary.get("Name", "")
        if not dashboard_id:
            continue
        if name_filter and name_filter not in dashboard_name.lower():
            continue

        try:
            permissions = call_with_retries(
                logger,
                f"describe dashboard permissions for {dashboard_id}",
                describe_dashboard_permissions,
                retry_attempts,
                retry_base_seconds,
                qs_client,
                dashboard_id,
            )
        except Exception as exc:
            logger.log(f"Skipping dashboard {dashboard_id} because permissions lookup failed: {exc}")
            continue

        if between_dashboard_seconds > 0:
            time.sleep(between_dashboard_seconds)

        via_principals = []
        for permission in permissions:
            principal = permission.get("Principal", "")
            if principal in target_principals:
                via_principals.append(
                    {
                        "principal": principal,
                        "principal_type": target_principals[principal]["principal_type"],
                        "name": target_principals[principal]["name"],
                        "actions": sorted(permission.get("Actions", [])),
                    }
                )

        if not via_principals:
            continue

        try:
            dashboard_response = call_with_retries(
                logger,
                f"describe dashboard {dashboard_id}",
                qs_client.describe_dashboard,
                retry_attempts,
                retry_base_seconds,
                AwsAccountId=QS_ACCOUNT_ID,
                DashboardId=dashboard_id,
            )
        except Exception as exc:
            logger.log(f"Skipping dashboard {dashboard_id} because dashboard details lookup failed: {exc}")
            continue

        dashboard = dashboard_response.get("Dashboard", {})
        version = dashboard.get("Version", {}) or {}
        source_arn = version.get("SourceEntityArn")
        source_type, source_id = parse_source_entity_arn(source_arn)

        analysis_name = None
        analysis_arn = None
        if source_type == "analysis" and source_id:
            try:
                analysis_response = call_with_retries(
                    logger,
                    f"describe analysis {source_id}",
                    qs_client.describe_analysis,
                    retry_attempts,
                    retry_base_seconds,
                    AwsAccountId=QS_ACCOUNT_ID,
                    AnalysisId=source_id,
                )
                analysis = analysis_response.get("Analysis", {})
                analysis_name = analysis.get("Name")
                analysis_arn = analysis.get("Arn")
            except Exception as exc:
                logger.log(f"Could not resolve source analysis {source_id} for dashboard {dashboard_id}: {exc}")
                analysis_name = None
                analysis_arn = None

        results.append(
            {
                "dashboard_id": dashboard_id,
                "dashboard_name": dashboard.get("Name", dashboard_name),
                "dashboard_arn": dashboard.get("Arn"),
                "published_version": version.get("VersionNumber"),
                "source_entity_arn": source_arn,
                "source_type": source_type,
                "source_id": source_id,
                "analysis_name": analysis_name,
                "analysis_arn": analysis_arn,
                "access_via": via_principals,
            }
        )

    results.sort(key=lambda item: (item.get("dashboard_name") or "").lower())
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List dashboards a QuickSight user can access and resolve source analysis names."
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--user-arn", help="QuickSight user ARN.")
    selector.add_argument("--user-email", help="QuickSight user email (exact match).")
    selector.add_argument("--user-name", help="QuickSight UserName (exact match).")
    parser.add_argument(
        "--dashboard-name-contains",
        help="Optional case-insensitive dashboard name filter.",
    )
    parser.add_argument(
        "--retry-attempts",
        type=int,
        default=8,
        help="Attempts for retryable QuickSight throttling errors.",
    )
    parser.add_argument(
        "--retry-base-seconds",
        type=float,
        default=1.0,
        help="Initial backoff delay for retryable QuickSight throttling errors.",
    )
    parser.add_argument(
        "--between-dashboard-seconds",
        type=float,
        default=0.15,
        help="Optional delay after each permissions lookup to reduce API burst pressure.",
    )
    args = parser.parse_args()

    if args.retry_attempts < 1:
        raise SystemExit("--retry-attempts must be at least 1.")
    if args.retry_base_seconds < 0:
        raise SystemExit("--retry-base-seconds must be >= 0.")
    if args.between_dashboard_seconds < 0:
        raise SystemExit("--between-dashboard-seconds must be >= 0.")

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    safe_label = (
        args.user_email
        or args.user_name
        or args.user_arn
        or "user"
    ).replace("/", "_").replace(" ", "_").replace("@", "_at_")
    log_path = build_log_path(f"user_dashboard_access_{safe_label}")
    json_path = build_log_path(f"user_dashboard_access_{safe_label}", extension="json")
    logger = Logger(log_path, "QUICKSIGHT USER DASHBOARD ACCESS")

    qs_client = create_quicksight_client()

    try:
        users = list_all_users(qs_client)
        target_user = resolve_user(
            users,
            user_arn=args.user_arn,
            user_email=args.user_email,
            user_name=args.user_name,
        )
        user_groups = list_user_groups(qs_client, target_user["UserName"])
    except NoCredentialsError as exc:
        raise SystemExit(
            "AWS credentials were not found. Run awsume (or otherwise set AWS credentials) in the same shell before running this script."
        ) from exc

    target_principals: Dict[str, Dict[str, str]] = {}
    target_principals[target_user["Arn"]] = {
        "principal_type": "user",
        "name": target_user.get("UserName", target_user["Arn"]),
    }
    for group in user_groups:
        group_arn = group.get("Arn")
        if not group_arn:
            continue
        target_principals[group_arn] = {
            "principal_type": "group",
            "name": group.get("GroupName", group_arn),
        }

    access_rows = collect_dashboard_access(
        qs_client,
        logger=logger,
        target_principals=target_principals,
        dashboard_name_contains=args.dashboard_name_contains,
        retry_attempts=args.retry_attempts,
        retry_base_seconds=args.retry_base_seconds,
        between_dashboard_seconds=args.between_dashboard_seconds,
    )

    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
    logger.log(f"Target user: {target_user.get('UserName')} ({target_user.get('Email', 'N/A')})")
    logger.log(f"Target user ARN: {target_user.get('Arn')}")
    logger.log(f"User groups: {len(user_groups)}")
    logger.log(
        f"Retry attempts: {args.retry_attempts}, retry base seconds: {args.retry_base_seconds}, "
        f"between dashboard seconds: {args.between_dashboard_seconds}"
    )
    for group in user_groups:
        logger.log(f"  - {group.get('GroupName', 'N/A')} ({group.get('Arn', 'N/A')})")
    logger.log(f"Matching dashboards: {len(access_rows)}")
    logger.log(f"Text log: {log_path}")
    logger.log(f"JSON report: {json_path}")
    logger.log("")

    if not access_rows:
        logger.log("No dashboards were found with explicit permissions for this user or its groups.")
    else:
        for row in access_rows:
            logger.log("-" * 60)
            logger.log(f"Dashboard: {row['dashboard_name']} ({row['dashboard_id']})")
            logger.log(f"Dashboard ARN: {row.get('dashboard_arn') or 'N/A'}")
            logger.log(f"SourceEntityArn: {row.get('source_entity_arn') or 'N/A'}")
            if row.get("source_type"):
                logger.log(f"Source type: {row['source_type']}")
            if row.get("source_id"):
                logger.log(f"Source id: {row['source_id']}")
            logger.log(f"Analysis name: {row.get('analysis_name') or 'N/A'}")
            logger.log(f"Analysis ARN: {row.get('analysis_arn') or 'N/A'}")
            logger.log("Access via:")
            for via in row["access_via"]:
                logger.log(
                    f"  - {via['principal_type']}: {via['name']} ({via['principal']})"
                )
                logger.log(f"    Actions: {', '.join(via['actions'])}")

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "account_id": QS_ACCOUNT_ID,
                "region": QS_REGION,
                "target_user": {
                    "user_name": target_user.get("UserName"),
                    "email": target_user.get("Email"),
                    "arn": target_user.get("Arn"),
                    "groups": [
                        {
                            "group_name": group.get("GroupName"),
                            "arn": group.get("Arn"),
                        }
                        for group in user_groups
                    ],
                },
                "dashboards": access_rows,
            },
            handle,
            indent=2,
            default=str,
        )

    logger.log("")
    logger.log(f"DONE. Output saved to {log_path}")


if __name__ == "__main__":
    main()