import boto3
import argparse
import os
import json
import datetime
from tqdm import tqdm
from dotenv import load_dotenv

# Load env
load_dotenv()
QS_ACCOUNT_ID = os.getenv('QS_AWS_ACCOUNT_ID')
REGION = os.getenv('QS_AWS_REGION', 'eu-central-1')

def extract_fields_from_json(obj, target_fields=None):
    """Recursively crawls the JSON definition to find field references."""
    if target_fields is None:
        target_fields = set()
    
    if isinstance(obj, dict):
        # QuickSight JSON typically stores field IDs/Names in these keys
        for key in ['FieldId', 'ColumnName', 'Name']:
            if key in obj and isinstance(obj[key], str):
                target_fields.add(obj[key])
        for v in obj.values():
            extract_fields_from_json(v, target_fields)
    elif isinstance(obj, list):
        for item in obj:
            extract_fields_from_json(item, target_fields)
    return target_fields

def audit_field_usage(dataset_id):
    qs = boto3.client('quicksight', region_name=REGION)
    log_file = f"logs/usage_{dataset_id.replace('/', '_')}_{datetime.datetime.now().strftime('%Y%m%d')}.txt"
    
    global_usage_stats = {}

    with open(log_file, 'w', encoding='utf-8') as f:
        f.write(f"QUICKSIGHT FIELD USAGE AUDIT\n")
        f.write(f"Dataset ID: {dataset_id}\n")
        f.write(f"Date: {datetime.datetime.now()}\n")
        f.write("="*60 + "\n\n")

        try:
            # 1. Get List of Analyses
            analyses = []
            next_token = None
            while True:
                kwargs = {'AwsAccountId': QS_ACCOUNT_ID}
                if next_token: kwargs['NextToken'] = next_token
                resp = qs.list_analyses(**kwargs)
                analyses.extend(resp.get('AnalysisSummaryList', []))
                next_token = resp.get('NextToken')
                if not next_token: break

            f.write(f"Total Analyses Scanned: {len(analyses)}\n\n")

            for summary in tqdm(analyses):
                analysis_id = summary['AnalysisId']
                
                # We need the 'Definition' to see specific fields
                try:
                    # Note: describe_analysis_definition is the deep-dive API
                    details = qs.describe_analysis_definition(
                        AwsAccountId=QS_ACCOUNT_ID, 
                        AnalysisId=analysis_id
                    )
                except Exception:
                    continue # Some analyses might not support definition (old versions)

                # Check if this analysis even uses our dataset
                ds_arns = details.get('Definition', {}).get('DataSetIdentifierDeclarations', [])
                if not any(dataset_id in str(ds) for ds in ds_arns):
                    continue

                # Crawl the definition for fields
                fields_found = extract_fields_from_json(details['Definition'])
                
                if fields_found:
                    f.write(f"📊 ANALYSIS: {summary['Name']} ({analysis_id})\n")
                    f.write(f"   Fields used: {', '.join(sorted(fields_found))}\n")
                    f.write("-" * 40 + "\n")
                    
                    for field in fields_found:
                        global_usage_stats[field] = global_usage_stats.get(field, 0) + 1

            # 2. Final Global Summary
            f.write("\n\n" + "="*60 + "\n")
            f.write("GLOBAL FIELD USAGE SUMMARY (Across all Analyses)\n")
            f.write("="*60 + "\n")
            for field, count in sorted(global_usage_stats.items(), key=lambda x: x[1], reverse=True):
                f.write(f"{field}: used in {count} analyses\n")

            print(f"✅ Audit complete. Results saved to: {log_file}")

        except Exception as e:
            f.write(f"\nFATAL ERROR: {str(e)}")
            print(f"❌ Error: {e}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_id")
    args = parser.parse_args()
    audit_field_usage(args.dataset_id)