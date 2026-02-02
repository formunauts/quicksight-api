import boto3
import sys
import argparse
import os
from dotenv import load_dotenv
load_dotenv()

# configuration
QS_ACCOUNT_ID = os.getenv('QS_AWS_ACCOUNT_ID')
REGION = os.getenv('QS_AWS_REGION')
DATASOURCE_ID = os.getenv('DATASOURCE_ID')

def list_users(qs_client):
    users = qs_client.list_users(
        AwsAccountId=QS_ACCOUNT_ID,
        Namespace='default'
    )['UserList']

    for u in users:
        print(f"user: {u['UserName']}")
        print(f"arn:  {u['Arn']}")
        print()

    return users

def share_datasource(qs_client, user_arn):
    try:
        qs_client.update_data_source_permissions(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSourceId=DATASOURCE_ID,
            GrantPermissions=[
                {
                    'Principal': user_arn,
                    'Actions': [
                        'quicksight:DescribeDataSource',
                        'quicksight:DescribeDataSourcePermissions',
                        'quicksight:PassDataSource',
                        'quicksight:UpdateDataSource',
                        'quicksight:DeleteDataSource',
                        'quicksight:UpdateDataSourcePermissions'
                    ]
                }
            ]
        )
        print('data source shared successfully')
    except Exception as e:
        print(f'error sharing data source: {e}')

def check_status(qs_client):
    try:
        ds = qs_client.describe_data_source(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSourceId=DATASOURCE_ID
        )
        status = ds['DataSource']['Status']
        print(f'data source status: {status}')

        if 'ErrorInfo' in ds['DataSource']:
            print(f"error info: {ds['DataSource']['ErrorInfo']}")
    except Exception as e:
        print(f'could not fetch data source status: {e}')

if __name__ == '__main__':
    qs = boto3.client('quicksight', region_name=REGION)
    print('connected to quicksight')

    check_status(qs)
    list_users(qs)

    target_arn = input('paste user arn to grant access: ').strip()
    if target_arn:
        share_datasource(qs, target_arn)
