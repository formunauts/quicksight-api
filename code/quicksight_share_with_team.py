import boto3
import sys
import os
from dotenv import load_dotenv

# load env
env_loaded = load_dotenv()
print(f'Loaded .env file: {env_loaded}')

# configuration
QS_ACCOUNT_ID = os.getenv('QS_AWS_ACCOUNT_ID')
REGION = os.getenv('QS_AWS_REGION')
DATASOURCE_ID = os.getenv('QS_DATASOURCE_ID')

# target users
TARGET_USERS = os.getenv('QS_TARGET_USERS', '').split(',')

def share_with_team(qs_client):
    print('Sharing data source permissions')
    print(f'Data source ID: {DATASOURCE_ID}')

    grant_list = []

    for user_arn in TARGET_USERS:
        user_arn = user_arn.strip()
        if not user_arn:
            continue

        user_name = user_arn.split('/')[-1]
        print(f'Granting access to {user_name}')

        grant_list.append({
            'Principal': user_arn,
            'Actions': [
                'quicksight:DescribeDataSource',
                'quicksight:DescribeDataSourcePermissions',
                'quicksight:PassDataSource',
                'quicksight:UpdateDataSource',
                'quicksight:DeleteDataSource',
                'quicksight:UpdateDataSourcePermissions'
            ]
        })

    if not grant_list:
        print('No target users provided')
        return

    try:
        qs_client.update_data_source_permissions(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSourceId=DATASOURCE_ID,
            GrantPermissions=grant_list
        )
        print('Access granted to all listed users')

    except qs_client.exceptions.ResourceNotFoundException:
        print('Data source was not found')

    except Exception as e:
        print(f'Failed to update permissions: {e}')

def check_status(qs_client):
    try:
        ds = qs_client.describe_data_source(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSourceId=DATASOURCE_ID
        )

        status = ds['DataSource']['Status']
        print(f'Current data source status: {status}')

    except Exception as e:
        print(f'Could not fetch data source status: {e}')

if __name__ == '__main__':
    try:
        qs = boto3.client('quicksight', region_name=REGION)
        print('Connected to QuickSight')

        check_status(qs)
        share_with_team(qs)

    except Exception as e:
        print(f'Failed to connect to QuickSight: {e}')
