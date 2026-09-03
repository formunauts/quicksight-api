"""List datasets used by published dashboards in selected QuickSight folder trees.

The script is read-only.  It follows folder membership recursively, then traces
each published dashboard to its source analysis and reads that analysis's
current definition to find the dataset declarations.
"""

import argparse
import json
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from botocore.exceptions import ClientError

from qs_common import (
    QS_ACCOUNT_ID,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


DEFAULT_ROOT_FOLDERS = [
    "SPAIN",
    "Quicksight-Kunden",
    "Marketplace DACH",
    "FormunautsOneUK",
]


def parse_resource_id(arn: str, resource_type: str) -> Optional[str]:
    marker = f"{resource_type}/"
    if marker not in arn:
        return None
    value = arn.split(marker, 1)[1]
    return value or None


def list_folder_members(qs_client, folder_id: str) -> List[Dict[str, Any]]:
    members: List[Dict[str, Any]] = []
    next_token = None
    while True:
        request: Dict[str, str] = {"AwsAccountId": QS_ACCOUNT_ID, "FolderId": folder_id}
        if next_token:
            request["NextToken"] = next_token
        response = qs_client.list_folder_members(**request)
        members.extend(response.get("FolderMemberList", []))
        next_token = response.get("NextToken")
        if not next_token:
            return members


def search_subfolders(qs_client, parent_folder_arn: str) -> List[Dict[str, Any]]:
    """Return the direct child folders of a folder."""
    folders: List[Dict[str, Any]] = []
    next_token = None
    while True:
        request: Dict[str, Any] = {
            "AwsAccountId": QS_ACCOUNT_ID,
            "Filters": [
                {
                    "Name": "PARENT_FOLDER_ARN",
                    "Operator": "StringEquals",
                    "Value": parent_folder_arn,
                }
            ],
        }
        if next_token:
            request["NextToken"] = next_token
        response = qs_client.search_folders(**request)
        folders.extend(response.get("FolderSummaryList", []))
        next_token = response.get("NextToken")
        if not next_token:
            return folders


def collect_folder_tree(
    qs_client,
    root_folder_id: str,
    folder_names: Dict[str, str],
    folder_arns: Dict[str, str],
    errors: List[Dict[str, str]],
) -> Tuple[Set[str], Set[str]]:
    """Return folder and dashboard IDs found below a root folder (including it)."""
    folder_ids: Set[str] = set()
    dashboard_ids: Set[str] = set()
    pending = [root_folder_id]
    while pending:
        folder_id = pending.pop()
        if folder_id in folder_ids:
            continue
        folder_ids.add(folder_id)
        try:
            members = list_folder_members(qs_client, folder_id)
        except ClientError as exc:
            errors.append(
                {
                    "stage": "list_folder_members",
                    "folder_id": folder_id,
                    "folder_name": folder_names.get(folder_id, ""),
                    "error": str(exc),
                }
            )
            continue
        for member in members:
            # ListFolderMembers returns MemberArn and MemberId, but not MemberType.
            dashboard_id = parse_resource_id(member.get("MemberArn", ""), "dashboard")
            if dashboard_id:
                dashboard_ids.add(dashboard_id)

        try:
            children = search_subfolders(qs_client, folder_arns[folder_id])
        except ClientError as exc:
            errors.append(
                {
                    "stage": "search_subfolders",
                    "folder_id": folder_id,
                    "folder_name": folder_names.get(folder_id, ""),
                    "error": str(exc),
                }
            )
            continue
        for child in children:
            child_id = child.get("FolderId")
            child_arn = child.get("Arn")
            if isinstance(child_id, str) and child_id:
                if isinstance(child_arn, str) and child_arn:
                    folder_arns[child_id] = child_arn
                    folder_names[child_id] = child.get("Name", "")
                else:
                    errors.append(
                        {
                            "stage": "search_subfolders",
                            "folder_id": child_id,
                            "error": "Child folder result had no ARN.",
                        }
                    )
                    continue
                if child_id not in folder_ids:
                    pending.append(child_id)
    return folder_ids, dashboard_ids


def extract_dataset_arns(definition: Dict[str, Any]) -> Set[str]:
    declarations = definition.get("DataSetIdentifierDeclarations", [])
    return {
        item.get("DataSetArn")
        for item in declarations
        if isinstance(item, dict) and isinstance(item.get("DataSetArn"), str)
    }


def write_text_report(
    logger: Logger,
    report: Dict[str, Any],
) -> None:
    summary = report["summary"]
    logger.log("QuickSight folder dashboard dataset audit")
    logger.log(f"Root folder matches: {summary['root_folder_matches']}")
    logger.log(f"Folders scanned (unique): {summary['folders_scanned']}")
    logger.log(f"Dashboards found (unique): {summary['dashboards_found']}")
    logger.log(f"Source analyses resolved: {summary['analyses_resolved']}")
    logger.log(f"Unique datasets: {summary['unique_datasets']}")
    logger.log(f"Errors: {summary['errors']}")
    logger.log()
    logger.log("Datasets:")
    for dataset in report["datasets"]:
        logger.log(f"- {dataset['dataset_name']} ({dataset['dataset_id']})")
        logger.log(f"  Analyses: {len(dataset['analyses'])}; dashboards: {len(dataset['dashboards'])}")
        for dashboard in dataset["dashboards"]:
            logger.log(
                f"  Dashboard: {dashboard['dashboard_name']} ({dashboard['dashboard_id']}) "
                f"<- {dashboard['analysis_name']} ({dashboard['analysis_id']})"
            )
    if report["errors"]:
        logger.log()
        logger.log("Errors:")
        for error in report["errors"]:
            logger.log(f"- {error}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List unique datasets used by dashboards in selected QuickSight folder trees."
    )
    parser.add_argument(
        "--folder-name",
        action="append",
        dest="folder_names",
        help="Exact root-folder name. Repeat to override the default four folders.",
    )
    parser.add_argument(
        "--exclude-folder-name",
        action="append",
        default=[],
        help="Exact root-folder name to exclude. Repeat as needed.",
    )
    args = parser.parse_args()

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    root_names = args.folder_names or DEFAULT_ROOT_FOLDERS
    excluded_root_names = set(args.exclude_folder_name)
    root_names = [name for name in root_names if name not in excluded_root_names]
    if not root_names:
        raise SystemExit("No root folders remain after applying --exclude-folder-name.")
    qs_client = create_quicksight_client()
    txt_path = build_log_path("folder_dashboard_datasets")
    json_path = build_log_path("folder_dashboard_datasets", extension="json")
    logger = Logger(txt_path, "QuickSight folder dashboard dataset audit")

    folders = get_all_summaries(qs_client.list_folders, QS_ACCOUNT_ID, "FolderSummaryList")
    folder_name_by_id = {item["FolderId"]: item.get("Name", "") for item in folders if item.get("FolderId")}
    folder_arn_by_id = {item["FolderId"]: item.get("Arn", "") for item in folders if item.get("FolderId")}
    roots_by_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for folder in folders:
        if folder.get("Name") in root_names:
            roots_by_name[folder["Name"]].append(folder)

    errors: List[Dict[str, str]] = []
    root_folders: List[Dict[str, str]] = []
    folder_to_roots: Dict[str, Set[str]] = defaultdict(set)
    dashboard_to_roots: Dict[str, Set[str]] = defaultdict(set)
    all_folder_ids: Set[str] = set()
    all_dashboard_ids: Set[str] = set()

    for root_name in root_names:
        matches = roots_by_name.get(root_name, [])
        if not matches:
            errors.append({"stage": "find_root_folder", "folder_name": root_name, "error": "No exact folder match."})
            continue
        for root in matches:
            root_id = root["FolderId"]
            root_folders.append({"folder_id": root_id, "folder_name": root_name})
            found_folders, found_dashboards = collect_folder_tree(
                qs_client, root_id, folder_name_by_id, folder_arn_by_id, errors
            )
            all_folder_ids.update(found_folders)
            all_dashboard_ids.update(found_dashboards)
            for folder_id in found_folders:
                folder_to_roots[folder_id].add(root_name)
            for dashboard_id in found_dashboards:
                dashboard_to_roots[dashboard_id].add(root_name)

    datasets = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
    dataset_details = {
        item.get("Arn", ""): {"dataset_id": item.get("DataSetId", ""), "dataset_name": item.get("Name", "")}
        for item in datasets
        if item.get("Arn")
    }
    analysis_cache: Dict[str, Dict[str, Any]] = {}
    dataset_usage: Dict[str, Dict[str, Any]] = {}
    dashboard_rows: List[Dict[str, Any]] = []

    for dashboard_id in sorted(all_dashboard_ids):
        try:
            dashboard = qs_client.describe_dashboard(
                AwsAccountId=QS_ACCOUNT_ID, DashboardId=dashboard_id
            ).get("Dashboard", {})
            version = dashboard.get("Version", {})
            analysis_id = parse_resource_id(version.get("SourceEntityArn", ""), "analysis")
            if not analysis_id:
                errors.append({"stage": "resolve_dashboard_analysis", "dashboard_id": dashboard_id, "error": "No source analysis ARN."})
                continue
        except ClientError as exc:
            errors.append({"stage": "describe_dashboard", "dashboard_id": dashboard_id, "error": str(exc)})
            continue

        if analysis_id not in analysis_cache:
            try:
                analysis = qs_client.describe_analysis(
                    AwsAccountId=QS_ACCOUNT_ID, AnalysisId=analysis_id
                ).get("Analysis", {})
                definition = qs_client.describe_analysis_definition(
                    AwsAccountId=QS_ACCOUNT_ID, AnalysisId=analysis_id
                ).get("Definition", {})
                analysis_cache[analysis_id] = {
                    "analysis_id": analysis_id,
                    "analysis_name": analysis.get("Name", analysis_id),
                    "dataset_arns": extract_dataset_arns(definition),
                }
            except ClientError as exc:
                errors.append({"stage": "describe_analysis", "analysis_id": analysis_id, "error": str(exc)})
                continue

        analysis_row = analysis_cache[analysis_id]
        dashboard_row = {
            "dashboard_id": dashboard_id,
            "dashboard_name": dashboard.get("Name", dashboard_id),
            "analysis_id": analysis_id,
            "analysis_name": analysis_row["analysis_name"],
            "root_folders": sorted(dashboard_to_roots[dashboard_id]),
            "dataset_arns": sorted(analysis_row["dataset_arns"]),
        }
        dashboard_rows.append(dashboard_row)
        for dataset_arn in analysis_row["dataset_arns"]:
            dataset = dataset_details.get(dataset_arn, {})
            usage = dataset_usage.setdefault(
                dataset_arn,
                {
                    "dataset_arn": dataset_arn,
                    "dataset_id": dataset.get("dataset_id", parse_resource_id(dataset_arn, "dataset") or ""),
                    "dataset_name": dataset.get("dataset_name", "<not returned by ListDataSets>"),
                    "analyses": {},
                    "dashboards": [],
                },
            )
            usage["analyses"][analysis_id] = analysis_row["analysis_name"]
            usage["dashboards"].append(dashboard_row)

    dataset_rows = []
    for usage in dataset_usage.values():
        usage["analyses"] = [
            {"analysis_id": analysis_id, "analysis_name": name}
            for analysis_id, name in sorted(usage["analyses"].items(), key=lambda item: item[1].lower())
        ]
        usage["dashboards"] = sorted(usage["dashboards"], key=lambda item: item["dashboard_name"].lower())
        dataset_rows.append(usage)
    dataset_rows.sort(
        key=lambda item: (
            -len(item["dashboards"]),
            item["dataset_name"].lower(),
            item["dataset_id"],
        )
    )

    report = {
        "excluded_root_folder_names": sorted(excluded_root_names),
        "root_folders": root_folders,
        "summary": {
            "root_folder_matches": len(root_folders),
            "folders_scanned": len(all_folder_ids),
            "dashboards_found": len(all_dashboard_ids),
            "analyses_resolved": len(analysis_cache),
            "unique_datasets": len(dataset_rows),
            "errors": len(errors),
        },
        "datasets": dataset_rows,
        "dashboards": sorted(dashboard_rows, key=lambda item: item["dashboard_name"].lower()),
        "errors": errors,
    }
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, default=str)
    write_text_report(logger, report)
    logger.log()
    logger.log(f"Text report: {txt_path}")
    logger.log(f"JSON report: {json_path}")


if __name__ == "__main__":
    main()
