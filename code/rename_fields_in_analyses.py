import boto3
import os
import json
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

qs = boto3.client('quicksight', region_name=os.getenv('QS_AWS_REGION'))
account_id = os.getenv('QS_AWS_ACCOUNT_ID')

# Use your QuickSight User ARN for permissions
USER_ARN = "arn:aws:quicksight:eu-central-1:395443580020:user/default/daniel@formunauts.at"

ANALYSIS_ID = "6eb9968b-3c6e-463e-a992-b39270e6c6d4" 
OLD_ID = "name[Organization]"
NEW_ID = "organization_name"

def datetime_handler(x):
    if isinstance(x, datetime):
        return x.isoformat()
    raise TypeError("Unknown type")

def grant_dataset_permissions(dataset_arns):
    """Iterates through all datasets in the analysis and grants PassDataSet."""
    for arn in dataset_arns:
        dataset_id = arn.split('/')[-1]
        print(f"  --> Ensuring permissions on dataset: {dataset_id}")
        try:
            qs.update_data_set_permissions(
                AwsAccountId=account_id,
                DataSetId=dataset_id,
                GrantPermissions=[
                    {
                        'Principal': USER_ARN,
                        'Actions': [
                            'quicksight:DescribeDataSet',
                            'quicksight:DescribeDataSetPermissions',
                            'quicksight:PassDataSet'
                        ]
                    }
                ]
            )
        except Exception as e:
            print(f"      Warning: Could not update permissions for {dataset_id}: {e}")

def fix_analysis_fields(analysis_id):
    print(f"Starting auto-fix for analysis: {analysis_id}")
    
    # 1. Fetch the current definition
    response = qs.describe_analysis_definition(
        AwsAccountId=account_id,
        AnalysisId=analysis_id
    )
    
    # 2. AUTOMATIC PERMISSION CHECK
    # Extract all Dataset ARNs used in this analysis
    dataset_arns = [ds['DataSetArn'] for ds in response['Definition']['DataSetIdentifierDeclarations']]
    grant_dataset_permissions(dataset_arns)
    
    # 3. String replacement logic
    definition = response['Definition']
    def_json_str = json.dumps(definition, default=datetime_handler)
    fixed_json_str = def_json_str.replace(OLD_ID, NEW_ID)
    fixed_definition = json.loads(fixed_json_str)
    
    # 4. Update the analysis
    try:
        update_response = qs.update_analysis(
            AwsAccountId=account_id,
            AnalysisId=analysis_id,
            Name=response['Name'],
            Definition=fixed_definition
        )
        print(f"Successfully updated! Status: {update_response['UpdateStatus']}")
    except Exception as e:
        print(f"Failed to update analysis: {e}")

if __name__ == "__main__":
    fix_analysis_fields(ANALYSIS_ID)