import argparse
import copy
import datetime
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

import boto3
from dotenv import load_dotenv


load_dotenv()

QS_ACCOUNT_ID = os.getenv("QS_AWS_ACCOUNT_ID")
REGION = os.getenv("QS_AWS_REGION", "eu-central-1")
QS_PRINCIPAL_ARN = os.getenv("QS_PRINCIPAL_ARN")
ROOT_DIR = sys.path[0].rsplit("\\code", 1)[0]
LOG_DIR = os.path.join(ROOT_DIR, "logs")
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

DEFAULT_RENAMES = {
    "donation_object_id[Cancellations]": "donation_id[Cancellations]",
    "donation_object_id": "donation_id",
}

DEFAULT_TARGET_FIELDS = [
    "donation_object_id[Cancellations]",
    "donation_object_id",
    "donation_content_type_id[Cancellations]",
    "donation_content_type_id",
]


class Logger:
    def __init__(self, filename: str):
        self.filename = filename
        with open(self.filename, "w", encoding="utf-8") as handle:
            handle.write("QUICKSIGHT PAYMENTATTEMPT DOWNSTREAM MIGRATION\n")
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


def json_default(value: Any) -> str:
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    raise TypeError(f"Unsupported type: {type(value)!r}")


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "asset"


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build_backup_dir() -> str:
    path = os.path.join(LOG_DIR, f"paymentattempt_downstream_backups_{TIMESTAMP}")
    os.makedirs(path, exist_ok=True)
    return path


def build_plan_path() -> str:
    return os.path.join(LOG_DIR, f"paymentattempt_downstream_migration_{TIMESTAMP}.json")


def build_log_path() -> str:
    return os.path.join(LOG_DIR, f"paymentattempt_downstream_migration_{TIMESTAMP}.txt")


def normalize_renames(plan: Dict[str, Any]) -> Dict[str, str]:
    renames = dict(DEFAULT_RENAMES)
    for old_value, new_value in plan.get("renamed_fields", {}).items():
        renames[old_value] = new_value
    return dict(sorted(renames.items(), key=lambda item: len(item[0]), reverse=True))


def contains_target_field(text: str, target_fields: List[str]) -> bool:
    lower_text = text.lower()
    return any(field.lower() in lower_text for field in target_fields)


def rewrite_text(text: str, renames: Dict[str, str]) -> str:
    updated = text
    for old_value, new_value in renames.items():
        updated = updated.replace(old_value, new_value)
        updated = updated.replace(f"<<{old_value}>>", f"<<{new_value}>>")
        updated = updated.replace(f"{{{old_value}}}", f"{{{new_value}}}")
    return updated


def rewrite_structure(value: Any, path: str, renames: Dict[str, str], changes: List[Dict[str, Any]]) -> Any:
    if isinstance(value, dict):
        updated: Dict[str, Any] = {}
        for key, child in value.items():
            updated[key] = rewrite_structure(child, f"{path}.{key}", renames, changes)
        return updated

    if isinstance(value, list):
        updated_list = []
        for index, child in enumerate(value):
            updated_list.append(rewrite_structure(child, f"{path}[{index}]", renames, changes))
        return updated_list

    if isinstance(value, str):
        updated = rewrite_text(value, renames)
        if updated != value:
            changes.append(
                {
                    "path": path,
                    "old_value": value,
                    "new_value": updated,
                }
            )
        return updated

    return value


def extract_references(obj: Any, target_fields: List[str], path: str = "Definition") -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            matches.extend(extract_references(value, target_fields, f"{path}.{key}"))
        return matches

    if isinstance(obj, list):
        for index, value in enumerate(obj):
            matches.extend(extract_references(value, target_fields, f"{path}[{index}]"))
        return matches

    if isinstance(obj, str) and contains_target_field(obj, target_fields):
        for field in target_fields:
            if field.lower() in obj.lower():
                matches.append({"path": path, "field": field, "value": obj})
        return matches

    return matches


def write_backup(backup_dir: str, label: str, payload: Dict[str, Any]) -> str:
    filename = f"{slugify(label)}.json"
    full_path = os.path.join(backup_dir, filename)
    with open(full_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=json_default)
    return full_path


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
            logger.log(f"  Granted dataset access on {dataset_id} to {principal_arn}")
        except Exception as exc:
            logger.log(f"  Warning: could not grant dataset access on {dataset_id}: {exc}")


def extract_dataset_arns_from_definition(definition: Dict[str, Any]) -> Set[str]:
    declarations = definition.get("DataSetIdentifierDeclarations", [])
    return {item.get("DataSetArn") for item in declarations if item.get("DataSetArn")}


def describe_analysis_definition(qs_client, analysis_id: str) -> Dict[str, Any]:
    return qs_client.describe_analysis_definition(
        AwsAccountId=QS_ACCOUNT_ID,
        AnalysisId=analysis_id,
    )


def describe_dashboard_definition(qs_client, dashboard_id: str) -> Dict[str, Any]:
    return qs_client.describe_dashboard_definition(
        AwsAccountId=QS_ACCOUNT_ID,
        DashboardId=dashboard_id,
    )


def parse_dashboard_version_number(version_arn: str) -> int:
    match = re.search(r"/version/(\d+)$", version_arn)
    if not match:
        raise ValueError(f"Could not parse dashboard version from ARN: {version_arn}")
    return int(match.group(1))


def migrate_analysis(
    qs_client,
    analysis_id: str,
    renames: Dict[str, str],
    target_fields: List[str],
    backup_dir: str,
    apply: bool,
    principal_arn: Optional[str],
    logger: Logger,
) -> Dict[str, Any]:
    response = describe_analysis_definition(qs_client, analysis_id)
    original_definition = response["Definition"]
    updated_definition = copy.deepcopy(original_definition)
    definition_changes: List[Dict[str, Any]] = []
    updated_definition = rewrite_structure(updated_definition, "Definition", renames, definition_changes)
    remaining_references = extract_references(updated_definition, target_fields)
    backup_path = write_backup(
        backup_dir,
        f"analysis_{response['Name']}__{analysis_id}",
        response,
    )

    result = {
        "type": "analysis",
        "name": response["Name"],
        "analysis_id": analysis_id,
        "backup_file": backup_path,
        "change_count": len(definition_changes),
        "changes": definition_changes,
        "remaining_references": remaining_references,
        "applied": False,
        "update_status": None,
        "warnings": [],
    }

    if apply and definition_changes and not remaining_references:
        if principal_arn:
            dataset_arns = extract_dataset_arns_from_definition(original_definition)
            grant_dataset_permissions(qs_client, principal_arn, dataset_arns, logger)
        update_response = qs_client.update_analysis(
            AwsAccountId=QS_ACCOUNT_ID,
            AnalysisId=analysis_id,
            Name=response["Name"],
            Definition=updated_definition,
            ThemeArn=response.get("ThemeArn"),
        )
        result["applied"] = True
        result["update_status"] = update_response.get("UpdateStatus")
    return result


def migrate_dashboard(
    qs_client,
    dashboard_id: str,
    renames: Dict[str, str],
    target_fields: List[str],
    backup_dir: str,
    apply: bool,
    principal_arn: Optional[str],
    logger: Logger,
) -> Dict[str, Any]:
    response = describe_dashboard_definition(qs_client, dashboard_id)
    original_definition = response["Definition"]
    updated_definition = copy.deepcopy(original_definition)
    definition_changes: List[Dict[str, Any]] = []
    updated_definition = rewrite_structure(updated_definition, "Definition", renames, definition_changes)
    remaining_references = extract_references(updated_definition, target_fields)
    backup_path = write_backup(
        backup_dir,
        f"dashboard_{response['Name']}__{dashboard_id}",
        response,
    )

    result = {
        "type": "dashboard",
        "name": response["Name"],
        "dashboard_id": dashboard_id,
        "backup_file": backup_path,
        "change_count": len(definition_changes),
        "changes": definition_changes,
        "remaining_references": remaining_references,
        "applied": False,
        "update_status": None,
        "published_version": None,
        "warnings": [],
    }

    if apply and definition_changes and not remaining_references:
        if principal_arn:
            dataset_arns = extract_dataset_arns_from_definition(original_definition)
            grant_dataset_permissions(qs_client, principal_arn, dataset_arns, logger)

        update_kwargs = {
            "AwsAccountId": QS_ACCOUNT_ID,
            "DashboardId": dashboard_id,
            "Name": response["Name"],
            "Definition": updated_definition,
            "VersionDescription": f"paymentattempt migration {TIMESTAMP}",
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
    require_env("QS_AWS_REGION", REGION)
    os.makedirs(LOG_DIR, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="Preview or apply the downstream paymentattempt migration for analyses and dashboards."
    )
    parser.add_argument(
        "--audit-file",
        required=True,
        help="JSON report produced by qs_audit_paymentattempt_downstream.py.",
    )
    parser.add_argument(
        "--plan-file",
        required=True,
        help="Dataset plan file produced by qs_migrate_paymentattempt_datasets.py.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply analysis/dashboard updates. Without this flag, the script only previews changes.",
    )
    parser.add_argument(
        "--principal-arn",
        default=QS_PRINCIPAL_ARN,
        help="Optional QuickSight principal ARN to grant temporary dataset pass permissions before updates.",
    )
    parser.add_argument(
        "--skip-dashboards",
        action="store_true",
        help="Only process analyses and skip dashboards.",
    )
    parser.add_argument(
        "--skip-analyses",
        action="store_true",
        help="Only process dashboards and skip analyses.",
    )
    args = parser.parse_args()

    logger = Logger(build_log_path())
    backup_dir = build_backup_dir()
    output_plan_path = build_plan_path()

    audit_report = load_json(args.audit_file)
    dataset_plan = load_json(args.plan_file)
    renames = normalize_renames(dataset_plan)
    target_fields = audit_report.get("target_fields", DEFAULT_TARGET_FIELDS)

    logger.log(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    logger.log(f"Audit file: {args.audit_file}")
    logger.log(f"Dataset plan file: {args.plan_file}")
    logger.log(f"Backup directory: {backup_dir}")

    qs = boto3.client("quicksight", region_name=REGION)
    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {REGION})")

    output_plan: Dict[str, Any] = {
        "generated_at": datetime.datetime.now().isoformat(),
        "account_id": QS_ACCOUNT_ID,
        "region": REGION,
        "apply_requested": args.apply,
        "audit_file": args.audit_file,
        "dataset_plan_file": args.plan_file,
        "backup_directory": backup_dir,
        "renames": renames,
        "analyses": [],
        "dashboards": [],
    }

    applied_count = 0
    blocked_count = 0

    if not args.skip_analyses:
        analyses = audit_report.get("analyses", [])
        logger.log(f"Analyses to process: {len(analyses)}")
        for index, item in enumerate(analyses, start=1):
            logger.log(f"  Analysis {index}/{len(analyses)}: {item['name']} ({item['analysis_id']})")
            result = migrate_analysis(
                qs_client=qs,
                analysis_id=item["analysis_id"],
                renames=renames,
                target_fields=target_fields,
                backup_dir=backup_dir,
                apply=args.apply,
                principal_arn=args.principal_arn,
                logger=logger,
            )
            if result["remaining_references"]:
                blocked_count += 1
                result["warnings"].append("Remaining target-field references after rewrite.")
            if result["applied"]:
                applied_count += 1
            logger.log(
                f"    Changes: {result['change_count']}, remaining references: {len(result['remaining_references'])}, applied: {result['applied']}"
            )
            output_plan["analyses"].append(result)

    if not args.skip_dashboards:
        dashboards = audit_report.get("dashboards", [])
        logger.log(f"Dashboards to process: {len(dashboards)}")
        for index, item in enumerate(dashboards, start=1):
            logger.log(f"  Dashboard {index}/{len(dashboards)}: {item['name']} ({item['dashboard_id']})")
            result = migrate_dashboard(
                qs_client=qs,
                dashboard_id=item["dashboard_id"],
                renames=renames,
                target_fields=target_fields,
                backup_dir=backup_dir,
                apply=args.apply,
                principal_arn=args.principal_arn,
                logger=logger,
            )
            if result["remaining_references"]:
                blocked_count += 1
                result["warnings"].append("Remaining target-field references after rewrite.")
            if result["applied"]:
                applied_count += 1
            logger.log(
                f"    Changes: {result['change_count']}, remaining references: {len(result['remaining_references'])}, applied: {result['applied']}"
            )
            output_plan["dashboards"].append(result)

    with open(output_plan_path, "w", encoding="utf-8") as handle:
        json.dump(output_plan, handle, indent=2, default=json_default)

    logger.log("")
    logger.log(f"Analyses written to plan: {len(output_plan['analyses'])}")
    logger.log(f"Dashboards written to plan: {len(output_plan['dashboards'])}")
    logger.log(f"Assets applied: {applied_count}")
    logger.log(f"Assets blocked by remaining references: {blocked_count}")
    logger.log(f"Output plan: {output_plan_path}")


if __name__ == "__main__":
    main()
