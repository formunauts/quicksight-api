'''
QuickSight Data Source Lister
Generates a report of all QuickSight data sources in the specified account,
including details about VPC connections and database parameters.

Requires boto3 and appropriate AWS credentials with QuickSight access.

How to use:
    python quicksight_datasources.py --type AURORA S3 ATHENA

Output is saved to a timestamped log file in the logs/ directory.
'''

import boto3
import datetime
import argparse
import sys
import os
from dotenv import load_dotenv
# load environment variables
load_dotenv()

# configuration
QS_ACCOUNT_ID = os.getenv('QS_AWS_ACCOUNT_ID')
REGION = os.getenv('QS_AWS_REGION')
ROOT_DIR = sys.path[0].rsplit('\\code', 1)[0]
timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
OUTPUT_FILE = f'{ROOT_DIR}/logs/quicksight_datasource_report_{timestamp}.txt'

class Logger:
    def __init__(self, filename):
        self.filename = filename
        with open(self.filename, 'w', encoding='utf-8') as f:
            f.write(f"QUICKSIGHT DATA SOURCE REPORT\n")
            f.write(f"Generated on: {datetime.datetime.now()}\n")
            f.write("="*60 + "\n\n")

    def log(self, message):
        print(message)
        with open(self.filename, 'a', encoding='utf-8') as f:
            f.write(message + "\n")

logger = Logger(OUTPUT_FILE)

def get_all_summaries(func, account_id, key_name):
    # generic pagination helper
    items = []
    next_token = None
    
    while True:
        kwargs = {'AwsAccountId': account_id}
        if next_token:
            kwargs['NextToken'] = next_token
        
        response = func(**kwargs)
        batch = response.get(key_name, [])
        items.extend(batch)
        
        next_token = response.get('NextToken')
        if not next_token:
            break
            
    return items

def extract_vpc_arn(params):
    # helper to find vpc connection arn in nested dicts
    # quicksight stores this in different places for different db types
    for key, val in params.items():
        if isinstance(val, dict) and 'VpcConnectionArn' in val:
            return val['VpcConnectionArn']
    return None

def list_data_sources(qs_client, target_types=None):
    logger.log("--- SECTION 1: DATA SOURCES ---")
    
    if target_types:
        target_types = [t.upper() for t in target_types]
        logger.log(f"Filter active: Showing only {target_types}")

    try:
        sources = get_all_summaries(qs_client.list_data_sources, QS_ACCOUNT_ID, 'DataSources')
        
        count = 0
        for ds in sources:
            ds_type = ds.get('Type', 'Unknown').upper()
            
            # filter logic
            if target_types:
                if 'AURORA' in target_types and 'AURORA' in ds_type:
                    pass
                elif ds_type in target_types:
                    pass
                else:
                    continue
            
            count += 1
            name = ds.get('Name', 'Unnamed')
            ds_id = ds.get('DataSourceId')
            status = ds.get('Status', 'Unknown')
            
            logger.log(f"[{ds_type}] {name}")
            logger.log(f"ID: {ds_id}")
            logger.log(f"Status: {status}")
            
            # fetch details
            try:
                details = qs_client.describe_data_source(AwsAccountId=QS_ACCOUNT_ID, DataSourceId=ds_id)
                params = details['DataSource'].get('DataSourceParameters', {})
                
                # 1. check for vpc connection usage
                vpc_arn = extract_vpc_arn(params)
                if vpc_arn:
                    logger.log(f"- Uses VPC Connection: {vpc_arn}")

                # 2. print specific db details
                if 'RdsParameters' in params:
                    rds = params['RdsParameters']
                    logger.log(f"- DB ID: {rds.get('InstanceId')}")
                    logger.log(f"- Database: {rds.get('Database')}")
                elif 'AuroraParameters' in params:
                    aur = params['AuroraParameters']
                    logger.log(f"- Host: {aur.get('Host')}")
                    logger.log(f"- Database: {aur.get('Database')}")
                elif 'AuroraPostgreSqlParameters' in params:
                    aur = params['AuroraPostgreSqlParameters']
                    logger.log(f"- Host: {aur.get('Host')}")
                    logger.log(f"- Database: {aur.get('Database')}")
                elif 'PostgreSqlParameters' in params:
                    pg = params['PostgreSqlParameters']
                    logger.log(f"- Host: {pg.get('Host')}")
                    logger.log(f"- Database: {pg.get('Database')}")
                elif 'AthenaParameters' in params:
                    ath = params['AthenaParameters']
                    logger.log(f"- Workgroup: {ath.get('WorkGroup')}")
                elif 'S3Parameters' in params:
                    s3 = params['S3Parameters']
                    manifest = s3.get('ManifestFileLocation', {}).get('Bucket', 'Unknown Bucket')
                    key = s3.get('ManifestFileLocation', {}).get('Key', 'Unknown Key')
                    logger.log(f"- Manifest: s3://{manifest}/{key}")
            
            except Exception as e:
                logger.log(f"Warning: Could not fetch deep details: {e}")
            
            logger.log("-" * 30)
            
        logger.log(f"Total data sources found: {count}\n")

    except Exception as e:
        logger.log(f"Error scanning data sources: {str(e)}")

def list_vpc_connections(qs_client):
    logger.log("\n--- SECTION 2: AVAILABLE VPC CONNECTIONS ---")
    try:
        connections = qs_client.list_vpc_connections(AwsAccountId=QS_ACCOUNT_ID).get('VPCConnectionSummaries', [])
        
        if not connections:
            logger.log("No VPC Connections configured.")
        else:
            for vpc in connections:
                logger.log(f"NAME: {vpc.get('Name')}")
                logger.log(f"ID: {vpc.get('VPCConnectionId')}")
                logger.log(f"Status: {vpc.get('Status')}")
                logger.log(f"ARN: {vpc.get('Arn')}")
                logger.log("-" * 30)
                
    except Exception as e:
        logger.log(f"Error scanning VPC connections: {str(e)}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="List QuickSight Data Sources")
    parser.add_argument('--type', nargs='+', help="Filter by type (e.g. AURORA, S3, ATHENA, POSTGRESQL)")
    args = parser.parse_args()

    try:
        qs = boto3.client('quicksight', region_name=REGION)
        logger.log("Connected to QuickSight.")
        
        list_data_sources(qs, target_types=args.type)
        list_vpc_connections(qs)
        
        logger.log(f"\nDONE. Check {OUTPUT_FILE}.")
        
    except Exception as e:
        logger.log(f"Connection Failed: {e}")