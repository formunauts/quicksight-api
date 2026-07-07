import argparse
import copy
import json
import os
import sys
import time
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


PARAMETER_BLOCKS_WITH_TARGET = [
    "PostgreSqlParameters",
    "AuroraPostgreSqlParameters",
    "AuroraParameters",
    "RdsParameters",
]

TARGET_FIELD_BY_BLOCK = {
    "PostgreSqlParameters": "Host",
    "AuroraPostgreSqlParameters": "Host",
    "AuroraParameters": "Host",
    "RdsParameters": "InstanceId",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Update one QuickSight data source in place so existing datasets keep working without ARN remapping."
        )
    )

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--data-source-id", help="Target QuickSight data source id to update.")
    target.add_argument("--data-source-name", help="Target QuickSight data source name to update.")

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--new-host", help="New database host for the target data source.")
    source.add_argument(
        "--new-instance-id",
        help="New RDS instance id for target data sources that use RdsParameters.",
    )
    source.add_argument(
        "--from-data-source-id",
        help="Copy host from another QuickSight data source identified by id.",
    )
    source.add_argument(
        "--from-data-source-name",
        help="Copy host from another QuickSight data source identified by exact name.",
    )

    parser.add_argument(
        "--new-port",
        type=int,
        help="Optional port override. If omitted, the existing port is kept.",
    )
    parser.add_argument(
        "--new-database",
        help="Optional database-name override. If omitted, the existing database is kept.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually update the data source. Without this flag the script is a dry run.",
    )
    parser.add_argument(
        "--db-user",
        default=os.getenv("DB_USER"),
        help="Optional DB username for explicit CredentialPair update. Defaults to DB_USER env var.",
    )
    parser.add_argument(
        "--db-pass",
        default=os.getenv("DB_PASS"),
        help="Optional DB password for explicit CredentialPair update. Defaults to DB_PASS env var.",
    )
    return parser.parse_args()


def find_source_summary(summaries: List[Dict[str, Any]], args: argparse.Namespace) -> Dict[str, Any]:
    for item in summaries:
        if args.data_source_id and item.get("DataSourceId") == args.data_source_id:
            return item
        if args.data_source_name and item.get("Name") == args.data_source_name:
            return item
    raise SystemExit("Target data source not found. Check --data-source-id or --data-source-name.")


def find_template_summary(summaries: List[Dict[str, Any]], args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    if not any([args.from_data_source_id, args.from_data_source_name]):
        return None

    for item in summaries:
        if args.from_data_source_id and item.get("DataSourceId") == args.from_data_source_id:
            return item
        if args.from_data_source_name and item.get("Name") == args.from_data_source_name:
            return item

    raise SystemExit("Template data source not found. Check --from-data-source-id or --from-data-source-name.")


def select_connection_block(params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    for key in PARAMETER_BLOCKS_WITH_TARGET:
        block = params.get(key)
        if isinstance(block, dict):
            return key, block
    raise SystemExit(
        "Target data source is not using a supported connection block. "
        "Supported: PostgreSqlParameters, AuroraPostgreSqlParameters, AuroraParameters, RdsParameters."
    )


def extract_connection_target(params: Dict[str, Any]) -> Tuple[str, str]:
    block_name, block = select_connection_block(params)
    field_name = TARGET_FIELD_BY_BLOCK[block_name]
    value = block.get(field_name)
    if isinstance(value, str) and value:
        return field_name, value
    raise SystemExit(
        f"Could not extract {field_name} from {block_name} in selected data source connection parameters."
    )


def wait_for_update_status(qs_client, logger: Logger, data_source_id: str, max_attempts: int = 20) -> bool:
    for attempt in range(1, max_attempts + 1):
        try:
            response = qs_client.describe_data_source(
                AwsAccountId=QS_ACCOUNT_ID,
                DataSourceId=data_source_id,
            )
            status = response.get("DataSource", {}).get("Status")
        except Exception as exc:
            logger.log(f"Status attempt {attempt}/{max_attempts} failed: {exc}")
            time.sleep(3)
            continue

        logger.log(f"Status attempt {attempt}/{max_attempts}: {status}")
        if status in ("UPDATE_SUCCESSFUL", "CREATION_SUCCESSFUL"):
            logger.log("Connection update validated successfully.")
            return True
        if status in ("UPDATE_FAILED", "CREATION_FAILED"):
            logger.log("Connection update failed according to QuickSight status.")
            error_info = response.get("DataSource", {}).get("ErrorInfo")
            if isinstance(error_info, dict):
                logger.log(f"Error type: {error_info.get('Type')}")
                logger.log(f"Error message: {error_info.get('Message')}")
            return False

        time.sleep(3)

    logger.log("Timed out waiting for final data source status.")
    return False


def build_safe_request_preview(request: Dict[str, Any]) -> Dict[str, Any]:
    preview = copy.deepcopy(request)
    credentials = preview.get("Credentials")
    if isinstance(credentials, dict):
        pair = credentials.get("CredentialPair")
        if isinstance(pair, dict) and "Password" in pair:
            pair["Password"] = "***REDACTED***"
    return preview


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)
    args = parse_args()

    text_report_path = build_log_path("datasource_connection_update", "txt")
    json_report_path = build_log_path("datasource_connection_update", "json")
    logger = Logger(text_report_path, "QUICKSIGHT DATA SOURCE CONNECTION UPDATE")

    qs_client = create_quicksight_client()
    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
    logger.log(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    logger.log("This script updates a data source in place (same DataSourceId/ARN),")
    logger.log("so dependent datasets and analyses do not need remapping.")
    logger.log(f"Command: {' '.join(sys.argv)}")
    logger.log(f"Text report: {text_report_path}")
    logger.log(f"JSON report: {json_report_path}")
    logger.log("")

    summaries = get_all_summaries(qs_client.list_data_sources, QS_ACCOUNT_ID, "DataSources")
    target_summary = find_source_summary(summaries, args)
    template_summary = find_template_summary(summaries, args)

    target_id = target_summary["DataSourceId"]
    target_name = target_summary.get("Name")
    target_desc = qs_client.describe_data_source(AwsAccountId=QS_ACCOUNT_ID, DataSourceId=target_id)
    target_data_source = target_desc.get("DataSource", {})

    target_params = target_data_source.get("DataSourceParameters", {})
    if not isinstance(target_params, dict):
        raise SystemExit("Target data source has no DataSourceParameters to update.")

    update_params = copy.deepcopy(target_params)
    block_name, update_block = select_connection_block(update_params)
    old_block = target_params.get(block_name, {}) if isinstance(target_params.get(block_name), dict) else {}

    target_field_name = TARGET_FIELD_BY_BLOCK[block_name]
    old_target_value = old_block.get(target_field_name)
    new_target_value = args.new_host or args.new_instance_id

    template_info: Optional[Dict[str, Any]] = None
    template_arn: Optional[str] = None
    if template_summary:
        template_id = template_summary["DataSourceId"]
        template_desc = qs_client.describe_data_source(AwsAccountId=QS_ACCOUNT_ID, DataSourceId=template_id)
        template_data_source = template_desc.get("DataSource", {})
        template_params = template_data_source.get("DataSourceParameters", {})
        template_arn_value = template_data_source.get("Arn")
        if isinstance(template_arn_value, str) and template_arn_value:
            template_arn = template_arn_value
        if not isinstance(template_params, dict):
            raise SystemExit("Template data source has no DataSourceParameters.")
        _, new_target_value = extract_connection_target(template_params)
        template_info = {
            "data_source_id": template_data_source.get("DataSourceId"),
            "name": template_data_source.get("Name"),
            "arn": template_arn,
            "connection_target": new_target_value,
        }

    if not new_target_value:
        raise SystemExit(
            "No target connection value resolved. Provide --new-host, --new-instance-id, or a template data source."
        )

    update_block[target_field_name] = new_target_value
    if args.new_port is not None:
        update_block["Port"] = args.new_port
    if args.new_database:
        update_block["Database"] = args.new_database

    request: Dict[str, Any] = {
        "AwsAccountId": QS_ACCOUNT_ID,
        "DataSourceId": target_id,
        "Name": target_data_source.get("Name", target_name),
        "DataSourceParameters": update_params,
    }

    target_arn = target_data_source.get("Arn")
    using_explicit_credentials = bool(args.db_user and args.db_pass)

    if using_explicit_credentials:
        request["Credentials"] = {
            "CredentialPair": {
                "Username": args.db_user,
                "Password": args.db_pass,
            }
        }
    elif template_arn:
        # QuickSight can require credentials to be explicitly present on update.
        # Copying from the template source keeps secrets server-side and avoids plaintext credentials.
        request["Credentials"] = {"CopySourceArn": template_arn}

    vpc_properties = target_data_source.get("VpcConnectionProperties")
    if isinstance(vpc_properties, dict):
        request["VpcConnectionProperties"] = vpc_properties

    ssl_properties = target_data_source.get("SslProperties")
    if isinstance(ssl_properties, dict):
        request["SslProperties"] = ssl_properties

    payload = {
        "account_id": QS_ACCOUNT_ID,
        "region": QS_REGION,
        "mode": "APPLY" if args.apply else "DRY RUN",
        "target_data_source": {
            "data_source_id": target_data_source.get("DataSourceId", target_id),
            "name": target_data_source.get("Name", target_name),
            "arn": target_arn,
            "type": target_data_source.get("Type"),
            "status": target_data_source.get("Status"),
        },
        "template_data_source": template_info,
        "connection_parameter_block": block_name,
        "connection_target_field": target_field_name,
        "before": {
            "connection_target": old_target_value,
            "port": old_block.get("Port"),
            "database": old_block.get("Database"),
        },
        "after": {
            "connection_target": update_block.get(target_field_name),
            "port": update_block.get("Port"),
            "database": update_block.get("Database"),
        },
        "request_preview": build_safe_request_preview(request),
        "update_response": None,
    }

    logger.log(f"Target data source: {target_data_source.get('Name')} ({target_id})")
    if template_info:
        logger.log(
            "Template data source: "
            f"{template_info['name']} ({template_info['data_source_id']}) "
            f"connection_target={template_info['connection_target']}"
        )
        logger.log(f"Credential source ARN: {template_info.get('arn')}")
    if using_explicit_credentials:
        logger.log("Credential mode: explicit CredentialPair from --db-user/--db-pass or environment")
    elif template_arn:
        logger.log("Credential mode: CopySourceArn from template data source")
    else:
        logger.log("Credential mode: unchanged/not specified in request")
    logger.log(f"Connection block: {block_name}")
    logger.log(f"Connection target field: {target_field_name}")
    logger.log(f"Current connection target: {old_target_value}")
    logger.log(f"New connection target: {update_block.get(target_field_name)}")
    logger.log(f"Current port: {old_block.get('Port')}")
    logger.log(f"New port: {update_block.get('Port')}")
    logger.log(f"Current database: {old_block.get('Database')}")
    logger.log(f"New database: {update_block.get('Database')}")

    if not args.apply:
        logger.log("")
        logger.log("Dry run only. Re-run with --apply to perform the update.")
    else:
        logger.log("")
        logger.log("Applying update_data_source...")
        try:
            response = qs_client.update_data_source(**request)
        except Exception as exc:
            logger.log(f"Update failed: {exc}")
            logger.log(
                "If this mentions credentials, re-run with the current script version so Credentials.CopySourceArn is included."
            )
            if "CopyDataSourceCredentials" in str(exc):
                logger.log(
                    "Role lacks quicksight:CopyDataSourceCredentials. Re-run with explicit DB credentials, "
                    "for example DB_USER/DB_PASS in environment or --db-user/--db-pass."
                )
            with open(json_report_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            raise
        payload["update_response"] = response
        logger.log(f"Update request submitted. HTTP status: {response.get('Status')}")
        update_ok = wait_for_update_status(qs_client, logger, target_id)

        try:
            latest = qs_client.describe_data_source(AwsAccountId=QS_ACCOUNT_ID, DataSourceId=target_id).get(
                "DataSource", {}
            )
            payload["final_status"] = latest.get("Status")
            payload["final_error_info"] = latest.get("ErrorInfo")
        except Exception as exc:
            payload["final_status"] = "UNKNOWN"
            payload["final_error_info"] = {"Message": f"Could not fetch final status: {exc}"}

    with open(json_report_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)

    logger.log("")
    logger.log(f"JSON report written to: {json_report_path}")
    logger.log(f"Text report written to: {text_report_path}")

    if args.apply and not update_ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
