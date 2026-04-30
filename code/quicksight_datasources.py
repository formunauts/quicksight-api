import argparse
from typing import Dict, List, Optional

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


def extract_vpc_arn(params: Dict[str, object]) -> Optional[str]:
    for value in params.values():
        if isinstance(value, dict) and "VpcConnectionArn" in value:
            return value["VpcConnectionArn"]
    return None


def matches_type_filter(source_type: str, target_types: Optional[List[str]]) -> bool:
    if not target_types:
        return True
    normalized = source_type.upper()
    normalized_targets = [value.upper() for value in target_types]
    if "AURORA" in normalized_targets and "AURORA" in normalized:
        return True
    return normalized in normalized_targets


def describe_parameters(logger: Logger, params: Dict[str, object]) -> None:
    vpc_arn = extract_vpc_arn(params)
    if vpc_arn:
        logger.log(f"- Uses VPC Connection: {vpc_arn}")

    if "RdsParameters" in params:
        rds = params["RdsParameters"]
        logger.log(f"- DB ID: {rds.get('InstanceId')}")
        logger.log(f"- Database: {rds.get('Database')}")
    elif "AuroraParameters" in params:
        aurora = params["AuroraParameters"]
        logger.log(f"- Host: {aurora.get('Host')}")
        logger.log(f"- Database: {aurora.get('Database')}")
    elif "AuroraPostgreSqlParameters" in params:
        aurora_pg = params["AuroraPostgreSqlParameters"]
        logger.log(f"- Host: {aurora_pg.get('Host')}")
        logger.log(f"- Database: {aurora_pg.get('Database')}")
    elif "PostgreSqlParameters" in params:
        postgres = params["PostgreSqlParameters"]
        logger.log(f"- Host: {postgres.get('Host')}")
        logger.log(f"- Database: {postgres.get('Database')}")
    elif "AthenaParameters" in params:
        athena = params["AthenaParameters"]
        logger.log(f"- Workgroup: {athena.get('WorkGroup')}")
    elif "S3Parameters" in params:
        s3 = params["S3Parameters"]
        location = s3.get("ManifestFileLocation", {})
        logger.log(f"- Manifest: s3://{location.get('Bucket', 'Unknown Bucket')}/{location.get('Key', 'Unknown Key')}")


def list_data_sources(qs_client, logger: Logger, target_types: Optional[List[str]]) -> None:
    logger.log("--- SECTION 1: DATA SOURCES ---")
    if target_types:
        logger.log(f"Filter active: Showing only {[value.upper() for value in target_types]}")

    try:
        sources = get_all_summaries(qs_client.list_data_sources, QS_ACCOUNT_ID, "DataSources")
    except Exception as exc:
        logger.log(f"Error scanning data sources: {exc}")
        return

    count = 0
    for source in sources:
        source_type = source.get("Type", "Unknown").upper()
        if not matches_type_filter(source_type, target_types):
            continue

        count += 1
        logger.log(f"[{source_type}] {source.get('Name', 'Unnamed')}")
        logger.log(f"ID: {source.get('DataSourceId')}")
        logger.log(f"Status: {source.get('Status', 'Unknown')}")

        try:
            details = qs_client.describe_data_source(
                AwsAccountId=QS_ACCOUNT_ID,
                DataSourceId=source["DataSourceId"],
            )
            params = details["DataSource"].get("DataSourceParameters", {})
            describe_parameters(logger, params)
        except Exception as exc:
            logger.log(f"Warning: Could not fetch deep details: {exc}")

        logger.log("-" * 30)

    logger.log(f"Total data sources found: {count}")


def list_vpc_connections(qs_client, logger: Logger) -> None:
    logger.log("")
    logger.log("--- SECTION 2: AVAILABLE VPC CONNECTIONS ---")
    try:
        connections = qs_client.list_vpc_connections(AwsAccountId=QS_ACCOUNT_ID).get("VPCConnectionSummaries", [])
    except Exception as exc:
        logger.log(f"Error scanning VPC connections: {exc}")
        return

    if not connections:
        logger.log("No VPC Connections configured.")
        return

    for connection in connections:
        logger.log(f"NAME: {connection.get('Name')}")
        logger.log(f"ID: {connection.get('VPCConnectionId')}")
        logger.log(f"Status: {connection.get('Status')}")
        logger.log(f"ARN: {connection.get('Arn')}")
        logger.log("-" * 30)


def main() -> None:
    parser = argparse.ArgumentParser(description="List QuickSight data sources and VPC connections.")
    parser.add_argument("--type", nargs="+", help="Filter by type, for example AURORA, S3, ATHENA, POSTGRESQL.")
    args = parser.parse_args()

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)
    log_path = build_log_path("quicksight_datasource_report")
    logger = Logger(log_path, "QUICKSIGHT DATA SOURCE REPORT")

    try:
        qs_client = create_quicksight_client()
        logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
        logger.log(f"Log file: {log_path}")
        logger.log("")
        list_data_sources(qs_client, logger, target_types=args.type)
        list_vpc_connections(qs_client, logger)
        logger.log("")
        logger.log(f"DONE. Output saved to {log_path}")
    except Exception as exc:
        logger.log(f"Connection failed: {exc}")


if __name__ == "__main__":
    main()
