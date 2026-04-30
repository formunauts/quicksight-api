import argparse
import os
from typing import List, Optional

import boto3
from dotenv import load_dotenv


load_dotenv()

QS_ACCOUNT_ID = os.getenv("QS_AWS_ACCOUNT_ID")
REGION = os.getenv("QS_AWS_REGION", "eu-central-1")
DEFAULT_DATA_SOURCE_ID = os.getenv("QS_DATASOURCE_ID") or os.getenv("DATASOURCE_ID")
DEFAULT_TARGET_USERS = [value.strip() for value in os.getenv("QS_TARGET_USERS", "").split(",") if value.strip()]

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


def check_status(qs_client, data_source_id: str) -> None:
    try:
        response = qs_client.describe_data_source(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSourceId=data_source_id,
        )
        print(f"Current data source status: {response['DataSource']['Status']}")
    except Exception as exc:
        print(f"Could not fetch data source status: {exc}")


def share_with_team(qs_client, data_source_id: str, user_arns: List[str]) -> None:
    grant_permissions = [
        {
            "Principal": user_arn,
            "Actions": DEFAULT_ACTIONS,
        }
        for user_arn in user_arns
    ]
    qs_client.update_data_source_permissions(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSourceId=data_source_id,
        GrantPermissions=grant_permissions,
    )
    print("Access granted to all listed users")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Grant one QuickSight data source to multiple QuickSight users."
    )
    parser.add_argument(
        "--data-source-id",
        default=DEFAULT_DATA_SOURCE_ID,
        help="QuickSight data source id. Defaults to QS_DATASOURCE_ID or DATASOURCE_ID.",
    )
    parser.add_argument(
        "--user-arns",
        nargs="+",
        default=DEFAULT_TARGET_USERS,
        help="QuickSight user ARNs. Defaults to QS_TARGET_USERS from the environment.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually update permissions. Without this flag the script only previews.",
    )
    args = parser.parse_args()

    data_source_id = require_value("data source id", args.data_source_id)
    user_arns = [value.strip() for value in args.user_arns if value.strip()]
    if not user_arns:
        raise SystemExit("No target user ARNs were provided.")

    qs_client = boto3.client("quicksight", region_name=REGION)
    print("Connected to QuickSight")
    check_status(qs_client, data_source_id)
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Data source id: {data_source_id}")
    print(f"Target users: {len(user_arns)}")
    for user_arn in user_arns:
        print(f"  {user_arn}")

    if not args.apply:
        return

    try:
        share_with_team(qs_client, data_source_id, user_arns)
    except qs_client.exceptions.ResourceNotFoundException:
        print("Data source was not found")
    except Exception as exc:
        print(f"Failed to update permissions: {exc}")


if __name__ == "__main__":
    main()
