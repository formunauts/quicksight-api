import argparse
import json
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


def list_all_groups(qs_client) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    next_token = None
    while True:
        kwargs = {
            "AwsAccountId": QS_ACCOUNT_ID,
            "Namespace": "default",
        }
        if next_token:
            kwargs["NextToken"] = next_token
        response = qs_client.list_groups(**kwargs)
        groups.extend(response.get("GroupList", []))
        next_token = response.get("NextToken")
        if not next_token:
            return groups


def build_user_index(users: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for user in users:
        arn = user.get("Arn")
        if arn:
            index[arn] = user
    return index


def build_group_index(groups: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        arn = group.get("Arn")
        if arn:
            index[arn] = group
    return index


def resolve_analysis_target(
    qs_client,
    analysis_id: Optional[str],
    analysis_name: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not analysis_id and not analysis_name:
        return None

    if analysis_id:
        response = qs_client.describe_analysis(
            AwsAccountId=QS_ACCOUNT_ID,
            AnalysisId=analysis_id,
        )
        return response.get("Analysis", {})

    analyses = get_all_summaries(qs_client.list_analyses, QS_ACCOUNT_ID, "AnalysisSummaryList")
    matches = [item for item in analyses if item.get("Name") == analysis_name]
    if not matches:
        raise SystemExit(f"No analysis found with exact name: {analysis_name}")
    if len(matches) > 1:
        preview = ", ".join(f"{item['Name']} ({item['AnalysisId']})" for item in matches[:10])
        raise SystemExit(f"Multiple analyses matched '{analysis_name}': {preview}")

    analysis_id = matches[0]["AnalysisId"]
    response = qs_client.describe_analysis(
        AwsAccountId=QS_ACCOUNT_ID,
        AnalysisId=analysis_id,
    )
    return response.get("Analysis", {})


def resolve_dashboard_target(qs_client, dashboard_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not dashboard_id:
        return None
    response = qs_client.describe_dashboard(
        AwsAccountId=QS_ACCOUNT_ID,
        DashboardId=dashboard_id,
    )
    return response.get("Dashboard", {})


def resolve_folder_target(
    qs_client,
    folder_id: Optional[str],
    folder_name: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not folder_id and not folder_name:
        return None

    if folder_id:
        response = qs_client.describe_folder(
            AwsAccountId=QS_ACCOUNT_ID,
            FolderId=folder_id,
        )
        return response.get("Folder", {})

    folders = get_all_summaries(qs_client.list_folders, QS_ACCOUNT_ID, "FolderSummaryList")
    matches = [item for item in folders if item.get("Name") == folder_name]
    if not matches:
        raise SystemExit(f"No folder found with exact name: {folder_name}")
    if len(matches) > 1:
        preview = ", ".join(f"{item['Name']} ({item['FolderId']})" for item in matches[:10])
        raise SystemExit(f"Multiple folders matched '{folder_name}': {preview}")

    folder_id = matches[0]["FolderId"]
    response = qs_client.describe_folder(
        AwsAccountId=QS_ACCOUNT_ID,
        FolderId=folder_id,
    )
    return response.get("Folder", {})


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


def describe_folder_permissions(qs_client, folder_id: str) -> List[Dict[str, Any]]:
    response = qs_client.describe_folder_permissions(
        AwsAccountId=QS_ACCOUNT_ID,
        FolderId=folder_id,
        Namespace="default",
    )
    return response.get("Permissions", [])


def classify_principal(principal_arn: str) -> str:
    if ":user/" in principal_arn:
        return "user"
    if ":group/" in principal_arn:
        return "group"
    if principal_arn.endswith(":root"):
        return "account-root"
    return "other"


def enrich_permissions(
    permissions: List[Dict[str, Any]],
    user_index: Dict[str, Dict[str, Any]],
    group_index: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for permission in permissions:
        principal = permission.get("Principal", "")
        principal_type = classify_principal(principal)
        row: Dict[str, Any] = {
            "principal": principal,
            "principal_type": principal_type,
            "actions": sorted(permission.get("Actions", [])),
            "display_name": None,
            "email": None,
        }

        if principal_type == "user" and principal in user_index:
            user = user_index[principal]
            row["display_name"] = user.get("UserName")
            row["email"] = user.get("Email")
        elif principal_type == "group" and principal in group_index:
            group = group_index[principal]
            row["display_name"] = group.get("GroupName")

        enriched.append(row)

    return enriched


def log_permissions(logger: Logger, title: str, permissions: List[Dict[str, Any]]) -> None:
    logger.log(title)
    if not permissions:
        logger.log("  No explicit permissions found.")
        return

    for permission in permissions:
        logger.log(f"  Principal: {permission['principal']}")
        logger.log(f"  Type: {permission['principal_type']}")
        if permission.get("display_name"):
            logger.log(f"  Name: {permission['display_name']}")
        if permission.get("email"):
            logger.log(f"  Email: {permission['email']}")
        logger.log(f"  Actions: {', '.join(permission['actions'])}")
        logger.log("")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report current QuickSight access for one analysis, dashboard, and/or shared folder."
    )
    parser.add_argument("--analysis-id", help="QuickSight analysis id.")
    parser.add_argument("--analysis-name", help="Exact QuickSight analysis name.")
    parser.add_argument("--dashboard-id", help="QuickSight dashboard id.")
    parser.add_argument("--folder-id", help="QuickSight folder id.")
    parser.add_argument("--folder-name", help="Exact QuickSight folder name.")
    args = parser.parse_args()

    if not any([args.analysis_id, args.analysis_name, args.dashboard_id, args.folder_id, args.folder_name]):
        raise SystemExit("Provide at least one of --analysis-id, --analysis-name, --dashboard-id, --folder-id, or --folder-name.")
    if args.analysis_id and args.analysis_name:
        raise SystemExit("Use either --analysis-id or --analysis-name, not both.")
    if args.folder_id and args.folder_name:
        raise SystemExit("Use either --folder-id or --folder-name, not both.")

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    label = (
        args.analysis_id
        or args.analysis_name
        or args.dashboard_id
        or args.folder_id
        or args.folder_name
        or "asset"
    ).replace("/", "_").replace(" ", "_")
    log_path = build_log_path(f"asset_access_report_{label}")
    json_path = build_log_path(f"asset_access_report_{label}", extension="json")
    logger = Logger(log_path, "QUICKSIGHT ASSET ACCESS REPORT")

    qs_client = create_quicksight_client()
    users = list_all_users(qs_client)
    groups = list_all_groups(qs_client)
    user_index = build_user_index(users)
    group_index = build_group_index(groups)

    payload: Dict[str, Any] = {
        "account_id": QS_ACCOUNT_ID,
        "region": QS_REGION,
        "analysis": None,
        "dashboard": None,
        "folder": None,
    }

    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
    logger.log(f"Known users loaded: {len(users)}")
    logger.log(f"Known groups loaded: {len(groups)}")
    logger.log(f"Text log: {log_path}")
    logger.log(f"JSON report: {json_path}")
    logger.log("")

    analysis = resolve_analysis_target(qs_client, args.analysis_id, args.analysis_name)
    if analysis:
        permissions = enrich_permissions(
            describe_analysis_permissions(qs_client, analysis["AnalysisId"]),
            user_index,
            group_index,
        )
        logger.log(f"ANALYSIS: {analysis.get('Name')} ({analysis.get('AnalysisId')})")
        logger.log(f"Analysis ARN: {analysis.get('Arn')}")
        logger.log(f"Status: {analysis.get('Status')}")
        logger.log(f"CreatedTime: {analysis.get('CreatedTime')}")
        logger.log(f"LastUpdatedTime: {analysis.get('LastUpdatedTime')}")
        log_permissions(logger, "Current analysis access:", permissions)
        logger.log("")
        payload["analysis"] = {
            "analysis_id": analysis.get("AnalysisId"),
            "name": analysis.get("Name"),
            "arn": analysis.get("Arn"),
            "status": analysis.get("Status"),
            "created_time": str(analysis.get("CreatedTime")),
            "last_updated_time": str(analysis.get("LastUpdatedTime")),
            "permissions": permissions,
        }

    dashboard = resolve_dashboard_target(qs_client, args.dashboard_id)
    if dashboard:
        version = dashboard.get("Version", {}) or {}
        source_arn = version.get("SourceEntityArn")
        source_type, source_id = parse_source_entity_arn(source_arn)
        permissions = enrich_permissions(
            describe_dashboard_permissions(qs_client, dashboard["DashboardId"]),
            user_index,
            group_index,
        )

        logger.log(f"DASHBOARD: {dashboard.get('Name')} ({dashboard.get('DashboardId')})")
        logger.log(f"Dashboard ARN: {dashboard.get('Arn')}")
        logger.log(f"Published version: {version.get('VersionNumber', 'N/A')}")
        logger.log(f"LastPublishedTime: {version.get('CreatedTime', 'N/A')}")
        logger.log(f"SourceEntityArn: {source_arn or 'N/A'}")
        if source_type and source_id:
            logger.log(f"Source type: {source_type}")
            logger.log(f"Source id: {source_id}")
        log_permissions(logger, "Current dashboard access:", permissions)
        logger.log("")

        payload["dashboard"] = {
            "dashboard_id": dashboard.get("DashboardId"),
            "name": dashboard.get("Name"),
            "arn": dashboard.get("Arn"),
            "published_version": version.get("VersionNumber"),
            "last_published_time": str(version.get("CreatedTime")),
            "source_entity_arn": source_arn,
            "source_type": source_type,
            "source_id": source_id,
            "permissions": permissions,
        }

    folder = resolve_folder_target(qs_client, args.folder_id, args.folder_name)
    if folder:
        permissions = enrich_permissions(
            describe_folder_permissions(qs_client, folder["FolderId"]),
            user_index,
            group_index,
        )
        logger.log(f"FOLDER: {folder.get('Name')} ({folder.get('FolderId')})")
        logger.log(f"Folder ARN: {folder.get('Arn')}")
        logger.log(f"Folder type: {folder.get('FolderType', 'N/A')}")
        logger.log(f"Sharing model: {folder.get('SharingModel', 'N/A')}")
        logger.log(f"CreatedTime: {folder.get('CreatedTime', 'N/A')}")
        logger.log(f"LastUpdatedTime: {folder.get('LastUpdatedTime', 'N/A')}")
        log_permissions(logger, "Current folder access:", permissions)
        logger.log("")

        payload["folder"] = {
            "folder_id": folder.get("FolderId"),
            "name": folder.get("Name"),
            "arn": folder.get("Arn"),
            "folder_type": folder.get("FolderType"),
            "sharing_model": folder.get("SharingModel"),
            "created_time": str(folder.get("CreatedTime")),
            "last_updated_time": str(folder.get("LastUpdatedTime")),
            "permissions": permissions,
        }

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)

    logger.log(f"JSON report written to: {json_path}")
    logger.log(f"Text log written to: {log_path}")


if __name__ == "__main__":
    main()
