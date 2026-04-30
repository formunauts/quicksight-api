import argparse
import os
import time
from typing import Any, Dict, Optional

import boto3
from dotenv import load_dotenv


load_dotenv()

QS_ACCOUNT_ID = os.getenv("QS_AWS_ACCOUNT_ID")
REGION = os.getenv("QS_AWS_REGION", "eu-central-1")

DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
EXISTING_VPC_ARN = os.getenv("VPC_CONN_ARN")
DEFAULT_DATA_SOURCE_ID = os.getenv("QS_DATASOURCE_ID", "ds-datateam-cross-account")
DEFAULT_DATA_SOURCE_NAME = os.getenv("QS_DATASOURCE_NAME", "DataTeam_CrossAccount_DB")


def require_value(label: str, value: Optional[str]) -> str:
    if value:
        return value
    raise SystemExit(f"Missing required value for {label}.")


def build_request(
    data_source_id: str,
    data_source_name: str,
    username: str,
    password: str,
) -> Dict[str, Any]:
    return {
        "AwsAccountId": require_value("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID),
        "DataSourceId": data_source_id,
        "Name": data_source_name,
        "Type": "POSTGRESQL",
        "DataSourceParameters": {
            "PostgreSqlParameters": {
                "Host": require_value("DB_HOST", DB_HOST),
                "Port": DB_PORT,
                "Database": require_value("DB_NAME", DB_NAME),
            }
        },
        "Credentials": {
            "CredentialPair": {
                "Username": username,
                "Password": password,
            }
        },
        "VpcConnectionProperties": {
            "VpcConnectionArn": require_value("VPC_CONN_ARN", EXISTING_VPC_ARN),
        },
        "SslProperties": {"DisableSsl": False},
    }


def create_data_source(qs_client, request: Dict[str, Any]) -> Optional[str]:
    try:
        qs_client.create_data_source(**request)
        print(f"Data source created with ID {request['DataSourceId']}")
        return request["DataSourceId"]
    except qs_client.exceptions.ResourceExistsException:
        print(f"Data source {request['DataSourceId']} already exists")
        return request["DataSourceId"]
    except Exception as exc:
        print(f"Failed to create data source: {exc}")
        return None


def verify_connection(qs_client, data_source_id: str) -> None:
    print("Waiting for QuickSight to validate the connection")
    for attempt in range(10):
        try:
            response = qs_client.describe_data_source(
                AwsAccountId=QS_ACCOUNT_ID,
                DataSourceId=data_source_id,
            )
        except Exception as exc:
            print(f"Attempt {attempt + 1}/10 could not fetch status: {exc}")
            time.sleep(3)
            continue

        status = response["DataSource"]["Status"]
        print(f"Attempt {attempt + 1}/10, status: {status}")
        if status in ("CREATION_SUCCESSFUL", "UPDATE_SUCCESSFUL"):
            print("Connection validated successfully")
            return
        if status == "CREATION_FAILED":
            print("Connection validation failed")
            return
        time.sleep(3)

    print("Timed out while waiting for validation")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a PostgreSQL QuickSight data source through an existing VPC connection."
    )
    parser.add_argument(
        "--data-source-id",
        default=DEFAULT_DATA_SOURCE_ID,
        help="QuickSight data source id. Defaults to QS_DATASOURCE_ID or ds-datateam-cross-account.",
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_DATA_SOURCE_NAME,
        help="QuickSight data source name. Defaults to QS_DATASOURCE_NAME or DataTeam_CrossAccount_DB.",
    )
    parser.add_argument(
        "--db-user",
        default=DB_USER,
        help="Database username. Defaults to DB_USER from the environment.",
    )
    parser.add_argument(
        "--db-pass",
        default=DB_PASS,
        help="Database password. Defaults to DB_PASS from the environment.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually create the data source. Without this flag the script only previews the request.",
    )
    args = parser.parse_args()

    request = build_request(
        data_source_id=args.data_source_id,
        data_source_name=args.name,
        username=require_value("db user", args.db_user),
        password=require_value("db password", args.db_pass),
    )

    print(f"Connected region: {REGION}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Data source id: {request['DataSourceId']}")
    print(f"Data source name: {request['Name']}")
    print(f"Database host: {request['DataSourceParameters']['PostgreSqlParameters']['Host']}")
    print(f"Database name: {request['DataSourceParameters']['PostgreSqlParameters']['Database']}")
    print(f"VPC connection: {request['VpcConnectionProperties']['VpcConnectionArn']}")

    if not args.apply:
        return

    qs_client = boto3.client("quicksight", region_name=REGION)
    data_source_id = create_data_source(qs_client, request)
    if data_source_id:
        verify_connection(qs_client, data_source_id)


if __name__ == "__main__":
    main()
