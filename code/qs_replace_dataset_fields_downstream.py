import argparse
import copy
import datetime
import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
    timestamp_now,
)


QS_PRINCIPAL_ARN = os.getenv("QS_PRINCIPAL_ARN")
IDENTIFIER_CHARS = r"A-Za-z0-9_"
TIMESTAMP = timestamp_now()


def json_default(value: Any) -> str:
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    raise TypeError(f"Unsupported type: {type(value)!r}")


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "asset"


def parse_rename_pairs(rename_pairs: List[str]) -> Dict[str, str]:
    renames: Dict[str, str] = {}
    for pair in rename_pairs:
        if "=" not in pair:
            raise SystemExit(f"Invalid --rename value '{pair}'. Expected format old_field=new_field")
        old_field, new_field = pair.split("=", 1)
        old_field = old_field.strip()
        new_field = new_field.strip()
        if not old_field or not new_field:
            raise SystemExit(f"Invalid --rename value '{pair}'. Both old and new field names are required.")
        renames[old_field] = new_field
    if not renames:
        raise SystemExit("Provide at least one --rename old_field=new_field pair.")
    return dict(sorted(renames.items(), key=lambda item: len(item[0]), reverse=True))


def build_token_pattern(token: str) -> re.Pattern[str]:
    escaped = re.escape(token)
    return re.compile(rf"(?<![{IDENTIFIER_CHARS}]){escaped}(?![{IDENTIFIER_CHARS}])")


def extract_dataset_arns_from_definition(definition: Dict[str, Any]) -> Set[str]:
    declarations = definition.get("DataSetIdentifierDeclarations", [])
    arns: Set[str] = set()
    if not isinstance(declarations, list):
        return arns
    for declaration in declarations:
        if not isinstance(declaration, dict):
            continue
        arn = declaration.get("DataSetArn")
        if isinstance(arn, str) and arn:
            arns.add(arn)
    return arns


def build_backup_dir() -> str:
    path = os.path.join(os.path.dirname(build_log_path("_tmp", "txt", TIMESTAMP)), f"dataset_field_replacements_backups_{TIMESTAMP}")
    os.makedirs(path, exist_ok=True)
    return path


def write_backup(backup_dir: str, label: str, payload: Dict[str, Any]) -> str:
    filename = f"{slugify(label)}.json"
    full_path = os.path.join(backup_dir, filename)
    with open(full_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=json_default)
    return full_path


def find_target_datasets(qs_client, dataset_name: Optional[str], dataset_id: Optional[str]) -> List[Dict[str, str]]:
    summaries = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
    matched: List[Dict[str, str]] = []
    for summary in summaries:
        name = summary.get("Name", "")
        current_id = summary.get("DataSetId", "")
        arn = summary.get("Arn", "")
        if dataset_id and current_id == dataset_id:
            matched.append({"name": name, "data_set_id": current_id, "arn": arn})
            continue
        if dataset_name and name == dataset_name:
            matched.append({"name": name, "data_set_id": current_id, "arn": arn})
    return matched


def rewrite_text(text: str, replacements: List[Tuple[str, str, re.Pattern[str]]]) -> Tuple[str, List[Dict[str, str]]]:
    updated = text
    details: List[Dict[str, str]] = []
    for old_value, new_value, pattern in replacements:
        replaced_text, count = pattern.subn(new_value, updated)
        if count:
            details.append({"old": old_value, "new": new_value, "count": str(count)})
        updated = replaced_text
    return updated, details


def rewrite_structure(
    value: Any,
    path: str,
    replacements: List[Tuple[str, str, re.Pattern[str]]],
    changes: List[Dict[str, Any]],
) -> Any:
    if isinstance(value, dict):
        updated: Dict[str, Any] = {}
        for key, child in value.items():
            updated[key] = rewrite_structure(child, f"{path}.{key}", replacements, changes)
        return updated

    if isinstance(value, list):
        updated_list = []
        for index, child in enumerate(value):
            updated_list.append(rewrite_structure(child, f"{path}[{index}]", replacements, changes))
        return updated_list

    if isinstance(value, str):
        updated, details = rewrite_text(value, replacements)
        if updated != value:
            changes.append(
                {
                    "path": path,
                    "old_value": value,
                    "new_value": updated,
                    "replacement_details": details,
                }
            )
        return updated

    return value


def find_remaining_references(definition: Dict[str, Any], old_fields: List[str]) -> List[Dict[str, str]]:
    patterns = {field: build_token_pattern(field) for field in old_fields}
    rows: List[Dict[str, str]] = []

    def walk(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for key, child in obj.items():
                walk(child, f"{path}.{key}")
            return
        if isinstance(obj, list):
            for index, child in enumerate(obj):
                walk(child, f"{path}[{index}]")
            return
        if not isinstance(obj, str):
            return
        for field, pattern in patterns.items():
            if pattern.search(obj):
                rows.append({"path": path, "field": field, "value": obj})

    walk(definition, "Definition")
    return rows


def parse_dashboard_version_number(version_arn: str) -> int:
    match = re.search(r"/version/(\d+)$", version_arn)
    if not match:
        raise ValueError(f"Could not parse dashboard version from ARN: {version_arn}")
    return int(match.group(1))


def grant_dataset_permissions(qs_client, principal_arn: str, dataset_arns: Set[str], logger: Logger) -> None:
    for dataset_arn in sorted(dataset_arns):
        dataset_id = dataset_arn.split("/")[-1]
        try:
            qs_client.update_data_set_permissions(
                AwsAccountId=QS_ACCOUNT_ID,
                DataSetId=dataset_id,
                GrantPermissions=[
                    {
                        "Principal": principal_arn,
                        "Actions": [
                            "quicksight:DescribeDataSet",
                            "quicksight:DescribeDataSetPermissions",
                            "quicksight:PassDataSet",
                        ],
                    }
                ],
            )
            logger.log(f"  Granted dataset pass permission on {dataset_id} to {principal_arn}")
        except Exception as exc:
            logger.log(f"  Warning: could not grant dataset access on {dataset_id}: {exc}")


def replacement_field_presence(
    qs_client,
    target_datasets: List[Dict[str, str]],
    replacement_fields: Set[str],
) -> Dict[str, Dict[str, bool]]:
    output: Dict[str, Dict[str, bool]] = {}
    for dataset in target_datasets:
        response = qs_client.describe_data_set(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSetId=dataset["data_set_id"],
        )
        output_columns = response.get("DataSet", {}).get("OutputColumns", [])
        names = {column.get("Name") for column in output_columns if isinstance(column, dict) and isinstance(column.get("Name"), str)}
        output[dataset["data_set_id"]] = {field: field in names for field in replacement_fields}
    return output


def migrate_analysis(
    qs_client,
    summary: Dict[str, Any],
    target_dataset_arns: Set[str],
    replacements: List[Tuple[str, str, re.Pattern[str]]],
    old_fields: List[str],
    backup_dir: str,
    apply: bool,
    principal_arn: Optional[str],
    logger: Logger,
) -> Optional[Dict[str, Any]]:
    analysis_id = summary.get("AnalysisId")
    if not analysis_id:
        return None

    response = qs_client.describe_analysis_definition(
        AwsAccountId=QS_ACCOUNT_ID,
        AnalysisId=analysis_id,
    )
    definition = response.get("Definition", {})
    used_dataset_arns = extract_dataset_arns_from_definition(definition)
    if not used_dataset_arns.intersection(target_dataset_arns):
        return None

    updated_definition = copy.deepcopy(definition)
    changes: List[Dict[str, Any]] = []
    updated_definition = rewrite_structure(updated_definition, "Definition", replacements, changes)
    if not changes:
        return None

    remaining = find_remaining_references(updated_definition, old_fields)
    backup_path = write_backup(backup_dir, f"analysis_{response.get('Name', analysis_id)}__{analysis_id}", response)

    result = {
        "type": "analysis",
        "name": response.get("Name", summary.get("Name", "")),
        "analysis_id": analysis_id,
        "backup_file": backup_path,
        "change_count": len(changes),
        "changes": changes,
        "remaining_references": remaining,
        "applied": False,
        "update_status": None,
    }

    if apply and not remaining:
        if principal_arn:
            grant_dataset_permissions(qs_client, principal_arn, used_dataset_arns, logger)
        update_response = qs_client.update_analysis(
            AwsAccountId=QS_ACCOUNT_ID,
            AnalysisId=analysis_id,
            Name=response.get("Name", summary.get("Name", analysis_id)),
            Definition=updated_definition,
            ThemeArn=response.get("ThemeArn"),
        )
        result["applied"] = True
        result["update_status"] = update_response.get("UpdateStatus")

    return result


def migrate_dashboard(
    qs_client,
    summary: Dict[str, Any],
    target_dataset_arns: Set[str],
    replacements: List[Tuple[str, str, re.Pattern[str]]],
    old_fields: List[str],
    backup_dir: str,
    apply: bool,
    principal_arn: Optional[str],
    logger: Logger,
) -> Optional[Dict[str, Any]]:
    dashboard_id = summary.get("DashboardId")
    if not dashboard_id:
        return None

    response = qs_client.describe_dashboard_definition(
        AwsAccountId=QS_ACCOUNT_ID,
        DashboardId=dashboard_id,
    )
    definition = response.get("Definition", {})
    used_dataset_arns = extract_dataset_arns_from_definition(definition)
    if not used_dataset_arns.intersection(target_dataset_arns):
        return None

    updated_definition = copy.deepcopy(definition)
    changes: List[Dict[str, Any]] = []
    updated_definition = rewrite_structure(updated_definition, "Definition", replacements, changes)
    if not changes:
        return None

    remaining = find_remaining_references(updated_definition, old_fields)
    backup_path = write_backup(backup_dir, f"dashboard_{response.get('Name', dashboard_id)}__{dashboard_id}", response)

    result = {
        "type": "dashboard",
        "name": response.get("Name", summary.get("Name", "")),
        "dashboard_id": dashboard_id,
        "backup_file": backup_path,
        "change_count": len(changes),
        "changes": changes,
        "remaining_references": remaining,
        "applied": False,
        "update_status": None,
        "published_version": None,
    }

    if apply and not remaining:
        if principal_arn:
            grant_dataset_permissions(qs_client, principal_arn, used_dataset_arns, logger)

        update_kwargs = {
            "AwsAccountId": QS_ACCOUNT_ID,
            "DashboardId": dashboard_id,
            "Name": response.get("Name", summary.get("Name", dashboard_id)),
            "Definition": updated_definition,
            "VersionDescription": f"dataset field replacement {TIMESTAMP}",
        }
        if response.get("ThemeArn"):
            update_kwargs["ThemeArn"] = response["ThemeArn"]
        if response.get("DashboardPublishOptions"):
            update_kwargs["DashboardPublishOptions"] = response["DashboardPublishOptions"]

        update_response = qs_client.update_dashboard(**update_kwargs)
        result["applied"] = True
        result["update_status"] = update_response.get("CreationStatus")

        version_arn = update_response.get("VersionArn")
        if version_arn:
            version_number = parse_dashboard_version_number(version_arn)
            qs_client.update_dashboard_published_version(
                AwsAccountId=QS_ACCOUNT_ID,
                DashboardId=dashboard_id,
                VersionNumber=version_number,
            )
            result["published_version"] = version_number

    return result


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    parser = argparse.ArgumentParser(
        description="Replace field references in analyses/dashboards that consume a target QuickSight dataset."
    )
    parser.add_argument("--dataset-name", help="Exact dataset name to scope the migration.")
    parser.add_argument("--dataset-id", help="Exact dataset id to scope the migration.")
    parser.add_argument(
        "--rename",
        action="append",
        required=True,
        help="Field mapping in old_field=new_field format. Repeat for multiple mappings.",
    )
    parser.add_argument("--apply", action="store_true", help="Apply the updates. Without this flag, runs in dry-run mode.")
    parser.add_argument("--skip-analyses", action="store_true", help="Skip analyses.")
    parser.add_argument("--skip-dashboards", action="store_true", help="Skip dashboards.")
    parser.add_argument(
        "--principal-arn",
        default=QS_PRINCIPAL_ARN,
        help="Optional QuickSight principal ARN to grant temporary dataset pass permissions before updates.",
    )
    args = parser.parse_args()

    if not args.dataset_name and not args.dataset_id:
        raise SystemExit("Provide --dataset-name or --dataset-id.")

    renames = parse_rename_pairs(args.rename)
    old_fields = list(renames.keys())
    replacement_fields = set(renames.values())
    replacements = [(old, new, build_token_pattern(old)) for old, new in renames.items()]

    text_report_path = build_log_path("dataset_field_replacement", "txt", TIMESTAMP)
    json_report_path = build_log_path("dataset_field_replacement", "json", TIMESTAMP)
    logger = Logger(text_report_path, "QUICKSIGHT DATASET FIELD REPLACEMENT")

    qs_client = create_quicksight_client()
    target_datasets = find_target_datasets(qs_client, args.dataset_name, args.dataset_id)
    if not target_datasets:
        raise SystemExit("No datasets matched the provided --dataset-name/--dataset-id.")

    target_dataset_arns = {row["arn"] for row in target_datasets if row.get("arn")}
    if not target_dataset_arns:
        raise SystemExit("Matched datasets do not have ARN values. Cannot continue.")

    field_presence = replacement_field_presence(qs_client, target_datasets, replacement_fields)
    missing_replacements = {
        dataset_id: [field for field, present in fields.items() if not present]
        for dataset_id, fields in field_presence.items()
        if any(not present for present in fields.values())
    }

    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
    logger.log(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    logger.log(f"Matched datasets: {len(target_datasets)}")
    for dataset in target_datasets:
        logger.log(f"  - {dataset['name']} ({dataset['data_set_id']})")
    logger.log(f"Renames: {renames}")
    logger.log(f"Text report: {text_report_path}")
    logger.log(f"JSON report: {json_report_path}")

    if missing_replacements:
        logger.log("")
        logger.log("WARNING: Some replacement fields are missing from dataset output columns.")
        for dataset_id, missing_fields in missing_replacements.items():
            logger.log(f"  Dataset {dataset_id} missing: {', '.join(missing_fields)}")

    backup_dir = build_backup_dir()
    logger.log(f"Backup directory: {backup_dir}")

    analyses_results: List[Dict[str, Any]] = []
    dashboard_results: List[Dict[str, Any]] = []
    analysis_errors: List[Dict[str, str]] = []
    dashboard_errors: List[Dict[str, str]] = []

    if not args.skip_analyses:
        analyses = get_all_summaries(qs_client.list_analyses, QS_ACCOUNT_ID, "AnalysisSummaryList")
        logger.log(f"Scanning analyses: {len(analyses)}")
        for index, summary in enumerate(analyses, start=1):
            if index == 1 or index % 25 == 0 or index == len(analyses):
                logger.log(f"  Analyses progress: {index}/{len(analyses)}")
            try:
                result = migrate_analysis(
                    qs_client,
                    summary,
                    target_dataset_arns,
                    replacements,
                    old_fields,
                    backup_dir,
                    args.apply,
                    args.principal_arn,
                    logger,
                )
            except Exception as exc:
                analysis_errors.append(
                    {
                        "analysis_id": summary.get("AnalysisId", ""),
                        "name": summary.get("Name", ""),
                        "error": str(exc),
                    }
                )
                continue
            if result:
                analyses_results.append(result)

    if not args.skip_dashboards:
        dashboards = get_all_summaries(qs_client.list_dashboards, QS_ACCOUNT_ID, "DashboardSummaryList")
        logger.log(f"Scanning dashboards: {len(dashboards)}")
        for index, summary in enumerate(dashboards, start=1):
            if index == 1 or index % 25 == 0 or index == len(dashboards):
                logger.log(f"  Dashboards progress: {index}/{len(dashboards)}")
            try:
                result = migrate_dashboard(
                    qs_client,
                    summary,
                    target_dataset_arns,
                    replacements,
                    old_fields,
                    backup_dir,
                    args.apply,
                    args.principal_arn,
                    logger,
                )
            except Exception as exc:
                dashboard_errors.append(
                    {
                        "dashboard_id": summary.get("DashboardId", ""),
                        "name": summary.get("Name", ""),
                        "error": str(exc),
                    }
                )
                continue
            if result:
                dashboard_results.append(result)

    blocked_analyses = sum(1 for row in analyses_results if row.get("remaining_references"))
    blocked_dashboards = sum(1 for row in dashboard_results if row.get("remaining_references"))
    applied_analyses = sum(1 for row in analyses_results if row.get("applied"))
    applied_dashboards = sum(1 for row in dashboard_results if row.get("applied"))

    logger.log("")
    logger.log(f"Analyses changed: {len(analyses_results)}")
    logger.log(f"Dashboards changed: {len(dashboard_results)}")
    logger.log(f"Analyses applied: {applied_analyses}")
    logger.log(f"Dashboards applied: {applied_dashboards}")
    logger.log(f"Analyses blocked by remaining references: {blocked_analyses}")
    logger.log(f"Dashboards blocked by remaining references: {blocked_dashboards}")
    logger.log(f"Analysis errors: {len(analysis_errors)}")
    logger.log(f"Dashboard errors: {len(dashboard_errors)}")

    payload = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": QS_REGION,
        "apply_requested": args.apply,
        "dataset_scope": target_datasets,
        "renames": renames,
        "replacement_field_presence": field_presence,
        "missing_replacements": missing_replacements,
        "backup_directory": backup_dir,
        "summary": {
            "analyses_changed": len(analyses_results),
            "dashboards_changed": len(dashboard_results),
            "analyses_applied": applied_analyses,
            "dashboards_applied": applied_dashboards,
            "analyses_blocked": blocked_analyses,
            "dashboards_blocked": blocked_dashboards,
            "analysis_errors": len(analysis_errors),
            "dashboard_errors": len(dashboard_errors),
        },
        "analyses": analyses_results,
        "dashboards": dashboard_results,
        "analysis_errors": analysis_errors,
        "dashboard_errors": dashboard_errors,
    }

    with open(json_report_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=json_default)


if __name__ == "__main__":
    main()
