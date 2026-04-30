import argparse
import os
from typing import List, Optional

import boto3
from dotenv import load_dotenv


load_dotenv()

QS_ACCOUNT_ID = os.getenv("QS_AWS_ACCOUNT_ID")
REGION = os.getenv("QS_AWS_REGION", "eu-central-1")
DEFAULT_DATA_SOURCE_ID = os.getenv("DATASOURCE_ID") or os.getenv("QS_DATASOURCE_ID")

DEFAULT_ACTIONS = [
    "quicksight:DescribeDataSource",
    "quicksight:DescribeDataSourcePermissions",
    "quicksight:PassDataSource",
    "quicksight:UpdateDataSource",
    "quicksight:DeleteDataSource",
    "quicksight:UpdateDataSourcePermissions",
]


def require_value(label: str, value: Optional[str]) -> str:
    if value:
        return value
    raise SystemExit(f"Missing required value for {label}.")


def list_users(qs_client) -> List[dict]:
    users = qs_client.list_users(
        AwsAccountId=require_value("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID),
        Namespace="default",
    )["UserList"]
    for user in users:
        print(f"user: {user['UserName']}")
        print(f"arn:  {user['Arn']}")
        print()
    return users


def check_status(qs_client, data_source_id: str) -> None:
    try:
        response = qs_client.describe_data_source(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSourceId=data_source_id,
        )
    except Exception as exc:
        print(f"Could not fetch data source status: {exc}")
        return

    data_source = response["DataSource"]
    print(f"Data source status: {data_source.get('Status')}")
    if "ErrorInfo" in data_source:
        print(f"Error info: {data_source['ErrorInfo']}")


def share_datasource(qs_client, data_source_id: str, user_arn: str) -> None:
    qs_client.update_data_source_permissions(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSourceId=data_source_id,
        GrantPermissions=[
            {
                "Principal": user_arn,
                "Actions": DEFAULT_ACTIONS,
            }
        ],
    )
    print("Data source shared successfully")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Grant one QuickSight user access to one QuickSight data source."
    )
    parser.add_argument(
        "--data-source-id",
        default=DEFAULT_DATA_SOURCE_ID,
        help="QuickSight data source id. Defaults to DATASOURCE_ID or QS_DATASOURCE_ID.",
    )
    parser.add_argument(
        "--user-arn",
        help="QuickSight user ARN to grant access to. If omitted, the script lists users and exits.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually update permissions. Without this flag the script only previews.",
    )
    args = parser.parse_args()

    data_source_id = require_value("data source id", args.data_source_id)
    qs_client = boto3.client("quicksight", region_name=REGION)
    print("Connected to QuickSight")
    check_status(qs_client, data_source_id)

    if not args.user_arn:
        print("No --user-arn provided. Listing users for lookup:")
        list_users(qs_client)
        return

    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Data source id: {data_source_id}")
    print(f"User ARN: {args.user_arn}")

    if not args.apply:
        return

    try:
        share_datasource(qs_client, data_source_id, args.user_arn)
    except Exception as exc:
        print(f"Error sharing data source: {exc}")


if __name__ == "__main__":
    main()
