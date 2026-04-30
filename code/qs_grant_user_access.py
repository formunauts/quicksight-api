import argparse
import datetime
import json
import os
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv


load_dotenv()

QS_ACCOUNT_ID = os.getenv("QS_AWS_ACCOUNT_ID")
REGION = os.getenv("QS_AWS_REGION", "eu-central-1")
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT_DIR, "logs")
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

RESOURCE_TYPES = ("datasets", "analyses", "dashboards")


class Logger:
    def __init__(self, filename: str):
        self.filename = filename
        with open(self.filename, "w", encoding="utf-8") as handle:
            handle.write("QUICKSIGHT USER ACCESS GRANT\n")
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


def load_plan_datasets(plan_file: str) -> List[Dict[str, Any]]:
    with open(plan_file, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload.get("datasets", [])


def build_log_path() -> str:
    return os.path.join(LOG_DIR, f"quicksight_user_access_grant_{TIMESTAMP}.txt")


def build_json_path() -> str:
    return os.path.join(LOG_DIR, f"quicksight_user_access_grant_{TIMESTAMP}.json")


def list_all_users(qs_client) -> List[Dict[str, Any]]:
    users: List[Dict[str, Any]] = []
    next_token = None
    while True:
        kwargs = {
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


def resolve_user_arn(qs_client, identity: str) -> Tuple[str, Dict[str, Any]]:
    if identity.startswith("arn:aws:quicksight:"):
        users = list_all_users(qs_client)
        for user in users:
            if user.get("Arn") == identity:
                return identity, user
        raise SystemExit(f"QuickSight user ARN not found: {identity}")

    users = list_all_users(qs_client)
    normalized = identity.strip().lower()
    for user in users:
        if str(user.get("Email", "")).strip().lower() == normalized:
            return user["Arn"], user
        if str(user.get("UserName", "")).strip().lower() == normalized:
            return user["Arn"], user

    sample_users = sorted(
        {
            user.get("Email") or user.get("UserName")
            for user in users
            if user.get("Email") or user.get("UserName")
        }
    )[:10]
    raise SystemExit(
        f"QuickSight user not found for '{identity}'. Sample known users: {', '.join(sample_users)}"
    )


def normalize_actions(actions: Sequence[str]) -> List[str]:
    return sorted({action for action in actions if isinstance(action, str)})


def choose_actions_from_permissions(
    permissions: List[Dict[str, Any]],
    desired_mode: str,
) -> List[str]:
    action_lists = [
        normalize_actions(permission.get("Actions", []))
        for permission in permissions
        if isinstance(permission, dict)
    ]
    action_lists = [actions for actions in action_lists if actions]
    if not action_lists:
        return []

    if desired_mode == "max":
        return max(action_lists, key=len)

    merged: Set[str] = set()
    for actions in action_lists:
        merged.update(actions)
    return sorted(merged)


def extract_existing_actions(
    permissions: List[Dict[str, Any]],
    principal_arn: str,
) -> List[str]:
    for permission in permissions:
        if permission.get("Principal") == principal_arn:
            return normalize_actions(permission.get("Actions", []))
    return []


def diff_actions(desired_actions: List[str], existing_actions: List[str]) -> List[str]:
    existing = set(existing_actions)
    return [action for action in desired_actions if action not in existing]


def describe_dataset_permissions(qs_client, dataset_id: str) -> List[Dict[str, Any]]:
    response = qs_client.describe_data_set_permissions(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
    )
    return response.get("Permissions", [])


def describe_analysis_permissions(qs_client, analysis_id: str) -> List[Dict[str, Any]]:
    response = qs_client.describe_analysis_permissions(
        AwsAccountId=QS_ACCOUNT_ID,
        AnalysisId=analysis_id,
    )
    return response.get("Permissions", [])


def describe_dashboard_permissions(qs_client, dashboard_id: str) -> List[Dict[str, Any]]:
    response = qs_client.describe_dashboard_permissions(
        AwsAccountId=QS_ACCOUNT_ID,
        DashboardId=dashboard_id,
    )
    return response.get("Permissions", [])


def grant_dataset_permissions(qs_client, dataset_id: str, principal_arn: str, actions: List[str]) -> Dict[str, Any]:
    return qs_client.update_data_set_permissions(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
        GrantPermissions=[{"Principal": principal_arn, "Actions": actions}],
    )


def grant_analysis_permissions(qs_client, analysis_id: str, principal_arn: str, actions: List[str]) -> Dict[str, Any]:
    return qs_client.update_analysis_permissions(
        AwsAccountId=QS_ACCOUNT_ID,
        AnalysisId=analysis_id,
        GrantPermissions=[{"Principal": principal_arn, "Actions": actions}],
    )


def grant_dashboard_permissions(qs_client, dashboard_id: str, principal_arn: str, actions: List[str]) -> Dict[str, Any]:
    return qs_client.update_dashboard_permissions(
        AwsAccountId=QS_ACCOUNT_ID,
        DashboardId=dashboard_id,
        GrantPermissions=[{"Principal": principal_arn, "Actions": actions}],
    )


def collect_dataset_targets(qs_client, args) -> List[Dict[str, Any]]:
    if args.all_datasets:
        return [
            {
                "name": row["Name"],
                "data_set_id": row["DataSetId"],
                "arn": row.get("Arn"),
            }
            for row in get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
        ]

    if args.plan_file:
        rows = load_plan_datasets(args.plan_file)
        return [
            {
                "name": row.get("name"),
                "data_set_id": row.get("data_set_id"),
                "arn": row.get("arn"),
            }
            for row in rows
            if row.get("data_set_id")
        ]

    return []


def collect_analysis_targets(qs_client, args) -> List[Dict[str, Any]]:
    if not args.all_analyses:
        return []
    return [
        {
            "name": row["Name"],
            "analysis_id": row["AnalysisId"],
            "arn": row.get("Arn"),
        }
        for row in get_all_summaries(qs_client.list_analyses, QS_ACCOUNT_ID, "AnalysisSummaryList")
    ]


def collect_dashboard_targets(qs_client, args) -> List[Dict[str, Any]]:
    if not args.all_dashboards:
        return []
    return [
        {
            "name": row["Name"],
            "dashboard_id": row["DashboardId"],
            "arn": row.get("Arn"),
        }
        for row in get_all_summaries(qs_client.list_dashboards, QS_ACCOUNT_ID, "DashboardSummaryList")
    ]


def log_resource_progress(
    logger: Logger,
    resource_label: str,
    index: int,
    total: int,
    name: str,
    applied: int,
    skipped: int,
    errors: int,
) -> None:
    logger.log("")
    logger.log(
        f"{resource_label} progress: {index}/{total} | "
        f"applied={applied}, skipped={skipped}, errors={errors}"
    )
    logger.log(f"  Target: {name}")


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", REGION)
    os.makedirs(LOG_DIR, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="Grant QuickSight datasets, analyses, and dashboards to a user by copying existing permission action sets."
    )
    parser.add_argument("--user", required=True, help="QuickSight email, username, or full QuickSight user ARN.")
    parser.add_argument("--plan-file", help="Optional dataset plan file to scope dataset grants.")
    parser.add_argument("--all-datasets", action="store_true", help="Grant access to all datasets.")
    parser.add_argument("--all-analyses", action="store_true", help="Grant access to all analyses.")
    parser.add_argument("--all-dashboards", action="store_true", help="Grant access to all dashboards.")
    parser.add_argument(
        "--action-template",
        choices=["max", "union"],
        default="max",
        help="How to derive the permission action list from existing permissions on each resource.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply the permission grants. Without this flag, the script is dry-run only.",
    )
    args = parser.parse_args()

    if not args.all_datasets and not args.plan_file and not args.all_analyses and not args.all_dashboards:
        raise SystemExit("Choose at least one scope: --all-datasets, --plan-file, --all-analyses, or --all-dashboards.")

    logger = Logger(build_log_path())
    json_path = build_json_path()
    qs = boto3.client("quicksight", region_name=REGION)

    target_arn, user = resolve_user_arn(qs, args.user)
    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {REGION})")
    logger.log(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    logger.log(f"Target user: {user.get('Email') or user.get('UserName')} ({target_arn})")
    if args.plan_file:
        logger.log(f"Dataset scope from plan file: {os.path.abspath(args.plan_file)}")
    logger.log(f"Action template mode: {args.action_template}")

    dataset_targets = collect_dataset_targets(qs, args)
    analysis_targets = collect_analysis_targets(qs, args)
    dashboard_targets = collect_dashboard_targets(qs, args)

    logger.log(f"Dataset targets: {len(dataset_targets)}")
    logger.log(f"Analysis targets: {len(analysis_targets)}")
    logger.log(f"Dashboard targets: {len(dashboard_targets)}")

    report: Dict[str, Any] = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": REGION,
        "apply_requested": args.apply,
        "target_user": {
            "input": args.user,
            "arn": target_arn,
            "email": user.get("Email"),
            "user_name": user.get("UserName"),
            "role": user.get("Role"),
        },
        "scopes": {
            "plan_file": os.path.abspath(args.plan_file) if args.plan_file else None,
            "all_datasets": args.all_datasets,
            "all_analyses": args.all_analyses,
            "all_dashboards": args.all_dashboards,
            "action_template": args.action_template,
        },
        "datasets": [],
        "analyses": [],
        "dashboards": [],
    }

    applied = 0
    skipped = 0
    errors = 0

    total_dataset_targets = len(dataset_targets)
    for index, target in enumerate(dataset_targets, start=1):
        dataset_id = target["data_set_id"]
        name = target.get("name") or dataset_id
        log_resource_progress(
            logger,
            "Dataset",
            index,
            total_dataset_targets,
            f"{name} ({dataset_id})",
            applied,
            skipped,
            errors,
        )
        permissions = describe_dataset_permissions(qs, dataset_id)
        desired_actions = choose_actions_from_permissions(permissions, args.action_template)
        existing_actions = extract_existing_actions(permissions, target_arn)
        missing_actions = diff_actions(desired_actions, existing_actions)
        row = {
            "name": name,
            "data_set_id": dataset_id,
            "desired_actions": desired_actions,
            "existing_actions": existing_actions,
            "missing_actions": missing_actions,
            "applied": False,
            "status": None,
            "error": None,
        }
        if not desired_actions or not missing_actions:
            skipped += 1
            if not desired_actions:
                logger.log("  Skipped: no existing permission action template was found on this dataset.")
            else:
                logger.log("  Skipped: target user already has all desired dataset actions.")
        elif args.apply:
            try:
                response = grant_dataset_permissions(qs, dataset_id, target_arn, missing_actions)
                row["applied"] = True
                row["status"] = response.get("Status")
                applied += 1
                logger.log(
                    f"  Applied dataset grant with {len(missing_actions)} missing actions. "
                    f"HTTP status: {response.get('Status')}"
                )
            except Exception as exc:
                row["error"] = str(exc)
                errors += 1
                logger.log(f"  Error while granting dataset access: {exc}")
        else:
            logger.log(
                f"  Preview: would grant {len(missing_actions)} dataset actions "
                f"(already had {len(existing_actions)})."
            )
        report["datasets"].append(row)

    total_analysis_targets = len(analysis_targets)
    for index, target in enumerate(analysis_targets, start=1):
        analysis_id = target["analysis_id"]
        name = target.get("name") or analysis_id
        log_resource_progress(
            logger,
            "Analysis",
            index,
            total_analysis_targets,
            f"{name} ({analysis_id})",
            applied,
            skipped,
            errors,
        )
        permissions = describe_analysis_permissions(qs, analysis_id)
        desired_actions = choose_actions_from_permissions(permissions, args.action_template)
        existing_actions = extract_existing_actions(permissions, target_arn)
        missing_actions = diff_actions(desired_actions, existing_actions)
        row = {
            "name": name,
            "analysis_id": analysis_id,
            "desired_actions": desired_actions,
            "existing_actions": existing_actions,
            "missing_actions": missing_actions,
            "applied": False,
            "status": None,
            "error": None,
        }
        if not desired_actions or not missing_actions:
            skipped += 1
            if not desired_actions:
                logger.log("  Skipped: no existing permission action template was found on this analysis.")
            else:
                logger.log("  Skipped: target user already has all desired analysis actions.")
        elif args.apply:
            try:
                response = grant_analysis_permissions(qs, analysis_id, target_arn, missing_actions)
                row["applied"] = True
                row["status"] = response.get("Status")
                applied += 1
                logger.log(
                    f"  Applied analysis grant with {len(missing_actions)} missing actions. "
                    f"HTTP status: {response.get('Status')}"
                )
            except Exception as exc:
                row["error"] = str(exc)
                errors += 1
                logger.log(f"  Error while granting analysis access: {exc}")
        else:
            logger.log(
                f"  Preview: would grant {len(missing_actions)} analysis actions "
                f"(already had {len(existing_actions)})."
            )
        report["analyses"].append(row)

    total_dashboard_targets = len(dashboard_targets)
    for index, target in enumerate(dashboard_targets, start=1):
        dashboard_id = target["dashboard_id"]
        name = target.get("name") or dashboard_id
        log_resource_progress(
            logger,
            "Dashboard",
            index,
            total_dashboard_targets,
            f"{name} ({dashboard_id})",
            applied,
            skipped,
            errors,
        )
        permissions = describe_dashboard_permissions(qs, dashboard_id)
        desired_actions = choose_actions_from_permissions(permissions, args.action_template)
        existing_actions = extract_existing_actions(permissions, target_arn)
        missing_actions = diff_actions(desired_actions, existing_actions)
        row = {
            "name": name,
            "dashboard_id": dashboard_id,
            "desired_actions": desired_actions,
            "existing_actions": existing_actions,
            "missing_actions": missing_actions,
            "applied": False,
            "status": None,
            "error": None,
        }
        if not desired_actions or not missing_actions:
            skipped += 1
            if not desired_actions:
                logger.log("  Skipped: no existing permission action template was found on this dashboard.")
            else:
                logger.log("  Skipped: target user already has all desired dashboard actions.")
        elif args.apply:
            try:
                response = grant_dashboard_permissions(qs, dashboard_id, target_arn, missing_actions)
                row["applied"] = True
                row["status"] = response.get("Status")
                applied += 1
                logger.log(
                    f"  Applied dashboard grant with {len(missing_actions)} missing actions. "
                    f"HTTP status: {response.get('Status')}"
                )
            except Exception as exc:
                row["error"] = str(exc)
                errors += 1
                logger.log(f"  Error while granting dashboard access: {exc}")
        else:
            logger.log(
                f"  Preview: would grant {len(missing_actions)} dashboard actions "
                f"(already had {len(existing_actions)})."
            )
        report["dashboards"].append(row)

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)

    logger.log("")
    logger.log(f"Permission grants applied: {applied}")
    logger.log(f"Resources skipped (already covered or no template actions): {skipped}")
    logger.log(f"Errors: {errors}")
    logger.log(f"JSON report: {json_path}")


if __name__ == "__main__":
    main()
