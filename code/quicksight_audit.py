'''
QuickSight Audit Script
This script connects to AWS QuickSight using boto3 and performs the following tasks:
1. Identifies specific datasets by name and extracts their calculated fields.
2. Scans all analyses to find a specific analysis by name.

IMPORTANT: 
1) awsume DataTeamAdmin and awsume QuickSightData must be run in the terminal first
2) For this to work, you must have these profiles in your AWS credentials file with appropriate permissions.

How to use:
    Run default config: 
        python quicksight_audit.py --run-all

    List specific datasets and their calculated fields:
        python quicksight_audit.py --datasets "Dataset_Name_1" "Dataset_Name_2" --calc-fields

    Search datasets by dataset name substring:
        python quicksight_audit.py --dataset-name-contains "marketplace_dach" --calc-fields

    Search calculated fields across all datasets by field name substring:
        python quicksight_audit.py --calc-field-name-contains "dataflow"
    
    List specific analyses:
        python quicksight_audit.py --analysis "Analysis_Name_Substring"
    
    List specific dashboards:
        python quicksight_audit.py --dashboard "Dashboard_Name_Substring"
'''
import boto3
import argparse
import datetime
import sys
import os
from dotenv import load_dotenv
# Load env
env_loaded = load_dotenv()
print(f'Loaded .env file: {env_loaded}')

# Default config if nothing is passed
QS_ACCOUNT_ID = os.getenv('QS_AWS_ACCOUNT_ID')
REGION = os.getenv('QS_AWS_REGION')
ROOT_DIR = sys.path[0].rsplit('\\code', 1)[0]
timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
OUTPUT_FILE = f'{ROOT_DIR}/logs/quicksight_audit_report_{timestamp}.txt'

DEFAULT_DATASETS = [
    "Marketplace_Dach_Billing_AT_DE_ONLY_GAAT/DE",
    "Marketplace_Dach_Billing_AT_DE_NOT_GA_ATDE",
    "Marketplace_Dach_Billing_ONLY_GA_CH",
    "Marketplace_Dach_Billing_CH_NOT_GACH",
]
DEFAULT_ENTITY_NAME = "Quality One-Pager"

# Logging setup
class Logger:
    def __init__(self, filename):
        self.filename = filename
        with open(self.filename, 'w', encoding='utf-8') as f:
            f.write(f"QUICKSIGHT AUDIT REPORT\n")
            f.write(f"Generated on: {datetime.datetime.now()}\n")
            f.write("="*60 + "\n\n")

    def log(self, message):
        print(message)
        with open(self.filename, 'a', encoding='utf-8') as f:
            f.write(message + "\n")

logger = Logger(OUTPUT_FILE)

# Helpers
def get_all_summaries(func, account_id, key_name):
    """Generic pagination helper."""
    items = []
    next_token = None
    page_count = 1
    
    logger.log(f"(Scanning pages for {key_name}...)")
    
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
        
        page_count += 1
        
    return items

def select_datasets(all_datasets, target_names=None, name_contains=None):
    """Filter datasets by exact names and/or a case-insensitive substring."""
    selected = []
    exact_names = set(target_names or [])
    substring = name_contains.lower() if name_contains else None

    for ds in all_datasets:
        name = ds['Name']
        exact_match = name in exact_names if exact_names else True
        substring_match = substring in name.lower() if substring else True
        if exact_match and substring_match:
            selected.append(ds)

    return selected

def search_datasets(qs_client, target_names=None, name_contains=None, show_calc_fields=False):
    logger.log(f"\n" + "-"*40)
    logger.log(f"DATASET SEARCH")
    if target_names:
        logger.log(f"Looking for exact dataset names: {target_names}")
    if name_contains:
        logger.log(f"Looking for dataset names containing: '{name_contains}'")
    logger.log("-"*40)

    all_datasets = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, 'DataSetSummaries')

    selected_datasets = select_datasets(all_datasets, target_names=target_names, name_contains=name_contains)
    exact_name_matches = {ds['Name'] for ds in selected_datasets}

    if target_names:
        missing_datasets = sorted(set(target_names) - exact_name_matches)
        for missing_name in missing_datasets:
            logger.log(f"Dataset not found: {missing_name}")

    found_datasets = {}
    for ds in selected_datasets:
        found_datasets[ds['Name']] = ds['DataSetId']
        logger.log(f"FOUND: {ds['Name']} (ID: {ds['DataSetId']})")

    if not found_datasets:
        logger.log("No matching datasets found.")
        return

    if show_calc_fields:
        logger.log(f"\n" + "-"*40)
        logger.log(f"EXTRACTING CALCULATED FIELDS")
        logger.log("-"*40)

        for name, ds_id in found_datasets.items():
            try:
                details = qs_client.describe_data_set(AwsAccountId=QS_ACCOUNT_ID, DataSetId=ds_id)
                logical_map = details['DataSet'].get('LogicalTableMap', {})
                
                logger.log(f"\nDATASET: {name}")
                count = 0
                for key, value in logical_map.items():
                    if 'DataTransforms' in value:
                        for transform in value['DataTransforms']:
                            if 'CreateColumnsOperation' in transform:
                                for col in transform['CreateColumnsOperation']['Columns']:
                                    logger.log(f"🔹 {col['ColumnName']}")
                                    logger.log(f"= {col['Expression']}")
                                    count += 1
                if count == 0:
                    logger.log("      (No calculated fields)")
            except Exception as e:
                logger.log(f"Error describing dataset '{name}': {e}")

def search_calculated_fields_by_name(qs_client, field_name_contains, dataset_names=None, dataset_name_contains=None):
    logger.log(f"\n" + "-"*40)
    logger.log("CALCULATED FIELD SEARCH")
    logger.log(f"Searching for calculated field names containing: '{field_name_contains}'")
    if dataset_names:
        logger.log(f"Restricting search to exact dataset names: {dataset_names}")
    if dataset_name_contains:
        logger.log(f"Restricting search to dataset names containing: '{dataset_name_contains}'")
    if not dataset_names and not dataset_name_contains:
        logger.log("Scanning all datasets")
    logger.log("-"*40)

    all_datasets = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, 'DataSetSummaries')
    datasets_to_scan = select_datasets(
        all_datasets,
        target_names=dataset_names,
        name_contains=dataset_name_contains
    )

    if dataset_names:
        matched_exact_names = {ds['Name'] for ds in datasets_to_scan}
        missing_datasets = sorted(set(dataset_names) - matched_exact_names)
        for missing_name in missing_datasets:
            logger.log(f"Dataset not found: {missing_name}")

    logger.log(f"Datasets to scan: {len(datasets_to_scan)}")

    found = False
    search_term = field_name_contains.lower()

    for ds in datasets_to_scan:
        try:
            details = qs_client.describe_data_set(
                AwsAccountId=QS_ACCOUNT_ID,
                DataSetId=ds['DataSetId']
            )
            logical_map = details['DataSet'].get('LogicalTableMap', {})

            matches = []
            for value in logical_map.values():
                for transform in value.get('DataTransforms', []):
                    if 'CreateColumnsOperation' not in transform:
                        continue
                    for col in transform['CreateColumnsOperation'].get('Columns', []):
                        column_name = col.get('ColumnName', '')
                        if search_term in column_name.lower():
                            matches.append({
                                'ColumnName': column_name,
                                'Expression': col.get('Expression', '')
                            })

            if matches:
                found = True
                logger.log(f"\nDATASET: {ds['Name']} (ID: {ds['DataSetId']})")
                for match in matches:
                    logger.log(f"- {match['ColumnName']}")
                    logger.log(f"  = {match['Expression']}")

        except Exception as e:
            logger.log(f"Error describing dataset '{ds['Name']}': {e}")

    if not found:
        logger.log(f"No calculated fields found matching '{field_name_contains}'")

def search_analyses(qs_client, search_term):
    logger.log(f"\n" + "-"*40)
    logger.log(f"ANALYSIS SEARCH")
    logger.log(f"Searching for name containing: '{search_term}'")
    logger.log("-"*40)

    all_items = get_all_summaries(qs_client.list_analyses, QS_ACCOUNT_ID, 'AnalysisSummaryList')
    
    found = False
    for item in all_items:
        if search_term.lower() in item['Name'].lower():
            logger.log(f"FOUND: '{item['Name']}'")
            logger.log(f"ID: {item['AnalysisId']}")
            logger.log(f"Status: {item.get('Status', 'Unknown')}")
            found = True
    
    if not found:
        logger.log(f"No analyses found matching '{search_term}'")

def search_dashboards(qs_client, search_term):
    logger.log(f"\n" + "-"*40)
    logger.log(f"DASHBOARD SEARCH")
    logger.log(f"Searching for name containing: '{search_term}'")
    logger.log("-"*40)

    # Note: different API key for dashboards
    all_items = get_all_summaries(qs_client.list_dashboards, QS_ACCOUNT_ID, 'DashboardSummaryList')
    
    found = False
    for item in all_items:
        if search_term.lower() in item['Name'].lower():
            logger.log(f"FOUND: '{item['Name']}'")
            logger.log(f"ID: {item['DashboardId']}")
            logger.log(f"Published Version: {item.get('PublishedVersionNumber', 'N/A')}")
            found = True
    
    if not found:
        logger.log(f"No dashboards found matching '{search_term}'")

# Main execution
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="QuickSight Audit CLI Tool")
    
    # Define Arguments
    parser.add_argument('--run-all', action='store_true', help="Run all default checks (Datasets + Analysis search)")
    parser.add_argument('--datasets', nargs='+', help="List of dataset names to search for")
    parser.add_argument('--dataset-name-contains', help="Search datasets by dataset name substring")
    parser.add_argument('--calc-fields', action='store_true', help="Fetch calculated fields for the found datasets")
    parser.add_argument('--calc-field-name-contains', help="Search calculated fields by name substring across all datasets, or within --datasets if provided")
    parser.add_argument('--analysis', help="Search for an Analysis by name (substring)")
    parser.add_argument('--dashboard', help="Search for a Dashboard by name (substring)")
    
    args = parser.parse_args()

    try:
        # Initialize client
        qs = boto3.client('quicksight', region_name=REGION)
        logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID})")
        
        # print full command
        logger.log(f"Command: {' '.join(sys.argv)}")
        
        # arguments handling
        if args.run_all:
            search_datasets(qs, DEFAULT_DATASETS, show_calc_fields=True)
            search_analyses(qs, DEFAULT_ENTITY_NAME)
            search_dashboards(qs, DEFAULT_ENTITY_NAME)
            
        else:
            # If manual flags are used
            if args.calc_field_name_contains:
                search_calculated_fields_by_name(
                    qs,
                    args.calc_field_name_contains,
                    dataset_names=args.datasets,
                    dataset_name_contains=args.dataset_name_contains
                )
            
            elif args.datasets or args.dataset_name_contains:
                search_datasets(
                    qs,
                    target_names=args.datasets,
                    name_contains=args.dataset_name_contains,
                    show_calc_fields=args.calc_fields
                )
            
            if args.analysis:
                search_analyses(qs, args.analysis)
                
            if args.dashboard:
                search_dashboards(qs, args.dashboard)

            # If no args provided
            if not any([args.datasets, args.dataset_name_contains, args.analysis, args.dashboard, args.calc_field_name_contains]):
                print("No action selected. Use --run-all or specific flags. Use --help for info.")

        logger.log(f"\nDONE. Output saved to {OUTPUT_FILE}")

    except Exception as e:
        logger.log(f"FATAL ERROR: {str(e)}")
