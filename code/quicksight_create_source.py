'''
Script to create a QuickSight data source connecting to a PostgreSQL database via an existing VPC connection.
'''
import os
import boto3
import sys
import time
from dotenv import load_dotenv

# load env
env_loaded = load_dotenv()
print(f'Loaded .env file: {env_loaded}')

# configuration
QS_ACCOUNT_ID = os.getenv('QS_AWS_ACCOUNT_ID')
REGION = os.getenv('QS_AWS_REGION')

DB_HOST = os.getenv('DB_HOST')
DB_NAME = os.getenv('DB_NAME')
DB_PORT = os.getenv('DB_PORT')
DB_USER = os.getenv('DB_USER')
DB_PASS = os.getenv('DB_PASS')

if DB_PORT:
    DB_PORT = int(DB_PORT)
else:
    DB_PORT = 5432

EXISTING_VPC_ARN = os.getenv('VPC_CONN_ARN')

def create_cross_account_source(qs_client, username, password):
    print('Creating QuickSight data source')
    print(f'Target host: {DB_HOST}')
    print('Using PostgreSQL with existing VPC connection')

    ds_name = 'DataTeam_CrossAccount_DB'
    ds_id = 'ds-datateam-cross-account'

    ds_params = {
        'PostgreSqlParameters': {
            'Host': DB_HOST,
            'Port': DB_PORT,
            'Database': DB_NAME
        }
    }

    vpc_props = {
        'VpcConnectionArn': EXISTING_VPC_ARN
    }

    creds = {
        'CredentialPair': {
            'Username': username,
            'Password': password
        }
    }

    try:
        qs_client.create_data_source(
            AwsAccountId=QS_ACCOUNT_ID,
            DataSourceId=ds_id,
            Name=ds_name,
            Type='POSTGRESQL',
            DataSourceParameters=ds_params,
            Credentials=creds,
            VpcConnectionProperties=vpc_props,
            SslProperties={'DisableSsl': False}
        )

        print(f'Data source created with ID {ds_id}')
        return ds_id

    except qs_client.exceptions.ResourceExistsException:
        print(f'Data source {ds_id} already exists')
        return ds_id

    except Exception as e:
        print(f'Failed to create data source: {e}')
        return None

def verify_connection(qs_client, ds_id):
    print('Waiting for QuickSight to validate the connection')

    for i in range(10):
        try:
            ds = qs_client.describe_data_source(
                AwsAccountId=QS_ACCOUNT_ID,
                DataSourceId=ds_id
            )

            status = ds['DataSource']['Status']
            print(f'Attempt {i + 1}/10, status: {status}')

            if status in ('CREATION_SUCCESSFUL', 'UPDATE_SUCCESSFUL'):
                print('Connection validated successfully')
                return

            if status == 'CREATION_FAILED':
                print('Connection validation failed')
                return

            time.sleep(3)

        except Exception:
            pass

    print('Timed out while waiting for validation')

if __name__ == '__main__':
    try:
        qs = boto3.client('quicksight', region_name=REGION)
        print('Connected to QuickSight')

        if not DB_USER or not DB_PASS:
            print('DB_USER or DB_PASS is missing')
            sys.exit(1)

        ds_id = create_cross_account_source(qs, DB_USER, DB_PASS)
        if ds_id:
            verify_connection(qs, ds_id)

    except Exception as e:
        print(f'Error running script: {e}')
