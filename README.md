# QuickSight API Utilities

This repository contains small Python scripts for auditing and managing Amazon QuickSight assets, related data sources, and the AWS networking pieces that QuickSight needs to reach private databases.

Most scripts use `boto3` and the default AWS credential chain. Before running any script, assume the `QuickSightData` role in the same shell. The expected workflow is:

```powershell
awsume QuickSightData
```

The original workflow for this repo expects the appropriate Data Team and QuickSight permissions to be active before script execution.

## Setup

Run commands from the repository root unless a script section says otherwise.

```powershell
awsume QuickSightData
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a local `.env` file in the repository root for the values used by the scripts:

```dotenv
QS_AWS_ACCOUNT_ID=123456789012
QS_AWS_REGION=eu-central-1

# QuickSight data source scripts
QS_DATASOURCE_ID=your-quicksight-data-source-id
DATASOURCE_ID=your-quicksight-data-source-id
QS_TARGET_USERS=arn:aws:quicksight:eu-central-1:123456789012:user/default/user1@example.com,arn:aws:quicksight:eu-central-1:123456789012:user/default/user2@example.com

# Create PostgreSQL data source
DB_HOST=your-db-host.example.com
DB_NAME=your_database
DB_PORT=5432
DB_USER=your_db_user
DB_PASS=your_db_password
VPC_CONN_ARN=arn:aws:quicksight:eu-central-1:123456789012:vpcConnection/your-vpc-connection-id

# Networking and firewall checks
VPC_CONN_ID=your-vpc-connection-id
QS_VPC_PEERING_ID=pcx-xxxxxxxxxxxxxxxxx
QS_DATA_VPC_ID=vpc-xxxxxxxxxxxxxxxxx
CLUSTER_ID=your-rds-cluster-id
QS_SECURITY_GROUP_ID=sg-xxxxxxxxxxxxxxxxx
QS_PROD_CIDR=10.10.0.0/16
```

Do not commit `.env` files or real database credentials.

## Script Safety

Read-only audit/report scripts:

- `code/quicksight_audit.py`
- `code/quicksight_datasources.py`
- `code/qs_dataset_dependencies.py`
- `code/qs_firewall.py`
- `code/qs_route_table_inspection.py`
- `code/verify_peering_target.py`

Scripts that change AWS or QuickSight resources:

- `code/quicksight_create_source.py`
- `code/quicksight_share_source.py`
- `code/quicksight_share_with_team.py`
- `code/rename_fields_in_analyses.py`
- `code/qs_fix_firewall.py`

Review the configuration and script constants before running any mutating script. These scripts do not implement a dry-run mode.

## Common Environment Variables

| Variable | Used by | Purpose |
| --- | --- | --- |
| `QS_AWS_ACCOUNT_ID` | Most QuickSight scripts | AWS account ID passed to QuickSight API calls. |
| `QS_AWS_REGION` | All AWS clients | AWS region, usually `eu-central-1` for this repo. |
| `QS_DATASOURCE_ID` | `quicksight_share_with_team.py` | Data source ID to share with multiple users. |
| `DATASOURCE_ID` | `quicksight_share_source.py` | Data source ID to share interactively with one user. |
| `QS_TARGET_USERS` | `quicksight_share_with_team.py` | Comma-separated QuickSight user ARNs. |
| `DB_HOST`, `DB_NAME`, `DB_PORT`, `DB_USER`, `DB_PASS` | `quicksight_create_source.py` | PostgreSQL connection settings. |
| `VPC_CONN_ARN` | `quicksight_create_source.py` | Existing QuickSight VPC connection ARN. |
| `VPC_CONN_ID` | `qs_route_table_inspection.py` | QuickSight VPC connection ID to inspect. |
| `QS_VPC_PEERING_ID` | `verify_peering_target.py` | VPC peering connection ID to verify. |
| `QS_DATA_VPC_ID` | `verify_peering_target.py` | Expected data team VPC ID. |
| `CLUSTER_ID` | `qs_firewall.py` | RDS cluster identifier to inspect. |
| `QS_SECURITY_GROUP_ID` | `qs_fix_firewall.py` | Security group to update. |
| `QS_PROD_CIDR` | `qs_fix_firewall.py` | CIDR range to allow on PostgreSQL port `5432`. |

## `code/quicksight_audit.py`

Use this script to search QuickSight datasets, calculated fields, analyses, and dashboards. It writes a timestamped report to `logs/quicksight_audit_report_YYYYMMDD_HHMMSS.txt` and also prints progress to the console.

What it does:

- Lists datasets by exact name or case-insensitive name substring.
- Optionally extracts calculated fields from matching datasets.
- Searches calculated field names across all datasets or within a dataset filter.
- Searches analysis names by substring.
- Searches dashboard names by substring.
- Provides a built-in `--run-all` mode for the script's default datasets and default entity name.

Basic usage:

```powershell
python code\quicksight_audit.py --help
python code\quicksight_audit.py --run-all
```

Search exact dataset names:

```powershell
python code\quicksight_audit.py --datasets "Dataset_Name_1" "Dataset_Name_2"
```

Search datasets by name substring:

```powershell
python code\quicksight_audit.py --dataset-name-contains "marketplace_dach"
```

Include calculated field definitions for matching datasets:

```powershell
python code\quicksight_audit.py --dataset-name-contains "marketplace_dach" --calc-fields
```

Search calculated fields by field-name substring across all datasets:

```powershell
python code\quicksight_audit.py --calc-field-name-contains "dataflow"
```

Search calculated fields within specific datasets only:

```powershell
python code\quicksight_audit.py --datasets "Dataset_Name_1" "Dataset_Name_2" --calc-field-name-contains "margin"
```

Search analyses and dashboards:

```powershell
python code\quicksight_audit.py --analysis "Quality One-Pager"
python code\quicksight_audit.py --dashboard "Quality One-Pager"
```

Required configuration:

- `QS_AWS_ACCOUNT_ID`
- `QS_AWS_REGION`

AWS permissions used include:

- `quicksight:ListDataSets`
- `quicksight:DescribeDataSet`
- `quicksight:ListAnalyses`
- `quicksight:ListDashboards`

## `code/quicksight_datasources.py`

Use this script to inventory QuickSight data sources and VPC connections. It writes a timestamped report to `logs/quicksight_datasource_report_YYYYMMDD_HHMMSS.txt`.

What it does:

- Lists all QuickSight data sources in the account.
- Optionally filters by data source type.
- Describes each matching data source to show connection details.
- Extracts VPC connection ARNs when present.
- Prints database host/database details for RDS, Aurora, PostgreSQL, Athena, and S3-style sources when available.
- Lists configured QuickSight VPC connections.

Basic usage:

```powershell
python code\quicksight_datasources.py
```

Filter by source type:

```powershell
python code\quicksight_datasources.py --type AURORA S3 ATHENA
python code\quicksight_datasources.py --type POSTGRESQL
```

Required configuration:

- `QS_AWS_ACCOUNT_ID`
- `QS_AWS_REGION`

AWS permissions used include:

- `quicksight:ListDataSources`
- `quicksight:DescribeDataSource`
- `quicksight:ListVPCConnections`

## `code/qs_dataset_dependencies.py`

Use this script to audit where fields from a specific QuickSight dataset are referenced across analyses. It creates a dated report at `logs/usage_<dataset_id>_YYYYMMDD.txt`.

What it does:

- Lists all QuickSight analyses.
- Describes each analysis definition.
- Filters to analyses that reference the provided dataset ID.
- Recursively scans the analysis JSON for field-like references such as `FieldId`, `ColumnName`, and `Name`.
- Produces a per-analysis field list and a global usage count.

Usage:

```powershell
python code\qs_dataset_dependencies.py "your-dataset-id"
```

If the dataset ID contains a slash, quote it:

```powershell
python code\qs_dataset_dependencies.py "Marketplace_Dach_Billing_AT_DE_ONLY_GAAT/DE"
```

Required configuration:

- `QS_AWS_ACCOUNT_ID`
- `QS_AWS_REGION` optional; defaults to `eu-central-1` if omitted.

Additional Python dependency:

- `tqdm`

AWS permissions used include:

- `quicksight:ListAnalyses`
- `quicksight:DescribeAnalysisDefinition`

Notes:

- Run from the repository root so the `logs/` output path exists.
- The script catches and skips analyses that cannot be described with `DescribeAnalysisDefinition`.
- Field extraction is broad by design, so the output may include identifiers or names that are not final visible field names.

## `code/quicksight_create_source.py`

Use this script to create a QuickSight PostgreSQL data source that connects through an existing QuickSight VPC connection.

What it does:

- Creates a QuickSight data source with:
  - Name: `DataTeam_CrossAccount_DB`
  - ID: `ds-datateam-cross-account`
  - Type: `POSTGRESQL`
  - SSL enabled
- Uses database credentials from `.env`.
- Uses the VPC connection ARN from `.env`.
- Polls QuickSight for validation status after creation.
- If the data source already exists, it reports that and verifies the existing ID.

Usage:

```powershell
python code\quicksight_create_source.py
```

Required configuration:

- `QS_AWS_ACCOUNT_ID`
- `QS_AWS_REGION`
- `DB_HOST`
- `DB_NAME`
- `DB_PORT` optional; defaults to `5432`
- `DB_USER`
- `DB_PASS`
- `VPC_CONN_ARN`

AWS permissions used include:

- `quicksight:CreateDataSource`
- `quicksight:DescribeDataSource`

Important:

- This script creates or reuses a hard-coded data source ID, `ds-datateam-cross-account`.
- Edit the script before running if you need a different QuickSight data source name or ID.
- Database credentials are sent to QuickSight as a credential pair.

## `code/quicksight_share_source.py`

Use this script to share one existing QuickSight data source with one user interactively.

What it does:

- Describes the configured data source and prints its status.
- Lists QuickSight users in the `default` namespace.
- Prompts you to paste the target user's QuickSight ARN.
- Grants that user data source permissions.

Usage:

```powershell
python code\quicksight_share_source.py
```

Required configuration:

- `QS_AWS_ACCOUNT_ID`
- `QS_AWS_REGION`
- `DATASOURCE_ID`

Permissions granted to the target user:

- `quicksight:DescribeDataSource`
- `quicksight:DescribeDataSourcePermissions`
- `quicksight:PassDataSource`
- `quicksight:UpdateDataSource`
- `quicksight:DeleteDataSource`
- `quicksight:UpdateDataSourcePermissions`

AWS permissions used by the caller include:

- `quicksight:DescribeDataSource`
- `quicksight:ListUsers`
- `quicksight:UpdateDataSourcePermissions`

Important:

- The granted permission set is broad and includes update/delete permission on the data source.
- Use `quicksight_share_with_team.py` instead when you already have a list of user ARNs and do not want an interactive prompt.

## `code/quicksight_share_with_team.py`

Use this script to share one existing QuickSight data source with multiple users from a comma-separated `.env` value.

What it does:

- Describes the configured data source and prints its status.
- Reads target user ARNs from `QS_TARGET_USERS`.
- Grants all listed users the same data source permissions in one update call.

Usage:

```powershell
python code\quicksight_share_with_team.py
```

Required configuration:

- `QS_AWS_ACCOUNT_ID`
- `QS_AWS_REGION`
- `QS_DATASOURCE_ID`
- `QS_TARGET_USERS`

Example `QS_TARGET_USERS`:

```dotenv
QS_TARGET_USERS=arn:aws:quicksight:eu-central-1:123456789012:user/default/alex@example.com,arn:aws:quicksight:eu-central-1:123456789012:user/default/sam@example.com
```

Permissions granted to each target user:

- `quicksight:DescribeDataSource`
- `quicksight:DescribeDataSourcePermissions`
- `quicksight:PassDataSource`
- `quicksight:UpdateDataSource`
- `quicksight:DeleteDataSource`
- `quicksight:UpdateDataSourcePermissions`

AWS permissions used by the caller include:

- `quicksight:DescribeDataSource`
- `quicksight:UpdateDataSourcePermissions`

Important:

- The granted permission set is broad and includes update/delete permission on the data source.
- Empty entries in `QS_TARGET_USERS` are ignored.

## `code/rename_fields_in_analyses.py`

Use this script to update field identifiers inside a specific QuickSight analysis definition. This is a targeted repair script, not a general CLI.

What it does:

- Describes one hard-coded analysis definition.
- Extracts dataset ARNs used by the analysis.
- Grants the configured user `DescribeDataSet`, `DescribeDataSetPermissions`, and `PassDataSet` on those datasets.
- Serializes the full analysis definition to JSON.
- Replaces every occurrence of one hard-coded field ID string with another hard-coded field ID string.
- Updates the analysis with the modified definition.

Usage:

```powershell
python code\rename_fields_in_analyses.py
```

Required configuration:

- `QS_AWS_ACCOUNT_ID`
- `QS_AWS_REGION`

Hard-coded values to review before running:

- `USER_ARN`
- `ANALYSIS_ID`
- `OLD_ID`
- `NEW_ID`

AWS permissions used include:

- `quicksight:DescribeAnalysisDefinition`
- `quicksight:UpdateAnalysis`
- `quicksight:UpdateDataSetPermissions`

Important:

- This script mutates an analysis and dataset permissions.
- It uses a global string replacement across the serialized analysis definition. Review `OLD_ID` and `NEW_ID` carefully before running.
- There is no dry-run mode and no backup export in the script. Consider exporting or copying the analysis first.

## `code/qs_firewall.py`

Use this script to inspect security groups attached to an RDS cluster and check whether PostgreSQL access appears to be allowed from production-style CIDRs.

What it does:

- Describes the configured RDS cluster.
- Reads attached VPC security groups.
- Describes those security groups in EC2.
- Prints PostgreSQL or all-traffic rules.
- Flags whether any allowed CIDR starts with `10.10.`.
- Prints referenced security groups from ingress rules.

Usage:

```powershell
python code\qs_firewall.py
```

Required configuration:

- `QS_AWS_REGION`
- `CLUSTER_ID`

AWS permissions used include:

- `rds:DescribeDBClusters`
- `ec2:DescribeSecurityGroups`

Notes:

- This script is read-only.
- The production CIDR check is hard-coded to CIDRs beginning with `10.10.`.

## `code/qs_fix_firewall.py`

Use this script to add an inbound PostgreSQL rule to a security group.

What it does:

- Adds an EC2 security group ingress rule allowing TCP port `5432`.
- Uses `QS_PROD_CIDR` as the allowed source range.
- Adds the description `Allow QuickSight prod access`.
- Treats duplicate rules as a non-fatal condition.

Usage:

```powershell
python code\qs_fix_firewall.py
```

Required configuration:

- `QS_AWS_REGION`
- `QS_SECURITY_GROUP_ID`
- `QS_PROD_CIDR`

AWS permissions used include:

- `ec2:AuthorizeSecurityGroupIngress`

Important:

- This script changes a security group.
- Verify `QS_SECURITY_GROUP_ID` and `QS_PROD_CIDR` before running.
- It only adds ingress; it does not remove or tighten existing rules.

## `code/qs_route_table_inspection.py`

Use this script to inspect the route tables associated with a QuickSight VPC connection and determine whether traffic can route through VPC peering or a transit gateway.

What it does:

- Describes the configured QuickSight VPC connection.
- Reads its VPC ID and network interface subnet IDs.
- Looks up route tables explicitly associated with those subnets.
- Falls back to the VPC main route table if no explicit subnet route table is found.
- Prints route destinations and targets.
- Flags whether any route points to a `pcx-` VPC peering connection or `tgw-` transit gateway.

Usage:

```powershell
python code\qs_route_table_inspection.py
```

Required configuration:

- `QS_AWS_ACCOUNT_ID`
- `QS_AWS_REGION`
- `VPC_CONN_ID`

AWS permissions used include:

- `quicksight:DescribeVPCConnection`
- `ec2:DescribeRouteTables`

Notes:

- This script is read-only.
- It is useful when diagnosing whether QuickSight can reach resources through a network path that depends on peering or transit gateway routing.

## `code/verify_peering_target.py`

Use this script to confirm whether a specific VPC peering connection includes the expected data team VPC.

What it does:

- Describes one VPC peering connection.
- Prints the peering status.
- Prints requester and accepter VPC IDs.
- Checks whether either side matches `QS_DATA_VPC_ID`.

Usage:

```powershell
python code\verify_peering_target.py
```

Required configuration:

- `QS_AWS_REGION`
- `QS_VPC_PEERING_ID`
- `QS_DATA_VPC_ID`

AWS permissions used include:

- `ec2:DescribeVpcPeeringConnections`

Notes:

- This script is read-only.
- It is a focused sanity check for peering IDs used in QuickSight-to-database networking work.

## Typical Workflows

Audit QuickSight assets:

```powershell
python code\quicksight_datasources.py
python code\quicksight_audit.py --dataset-name-contains "marketplace" --calc-fields
python code\qs_dataset_dependencies.py "your-dataset-id"
```

Investigate QuickSight private database connectivity:

```powershell
python code\quicksight_datasources.py --type POSTGRESQL AURORA
python code\qs_route_table_inspection.py
python code\verify_peering_target.py
python code\qs_firewall.py
```

Create and share a PostgreSQL data source:

```powershell
python code\quicksight_create_source.py
python code\quicksight_share_with_team.py
```

## Troubleshooting

`Loaded .env file: False`

The script did not find a `.env` file from the current working directory. Run from the repository root or export the required variables in your shell.

`NoCredentialsError` or AWS access denied errors

Confirm that you assumed the right AWS role in the same terminal session and that the role has both QuickSight and any required EC2/RDS permissions.

`ResourceNotFoundException`

Check the account ID, region, resource ID, and namespace. QuickSight resources are region-specific.

Reports are not created where expected

Most report scripts write to `logs/`. Run commands from the repository root and make sure the `logs/` directory exists.
