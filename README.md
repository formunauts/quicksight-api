# QuickSight CLI Toolbox

This repository is a CLI-first toolbox for Amazon QuickSight administration, dataset auditing, access management, networking checks, and targeted migration work.

The repository now follows a simple split:

- `code/`: Python toolbox scripts
- `code/README.md`: detailed script catalog with usage examples
- `logs/`: generated plans, reports, backups, and restore notes
- `scripts/`: miscellaneous shell helpers that are not part of the main QuickSight toolbox
- `docs/`: loose reference material and notes

## Setup

Run commands from the repository root.

```powershell
awsume QuickSightAdmin
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The toolbox depends on:

- `boto3`
- `python-dotenv`
- `tqdm`

Put your environment values either in a repository-root `.env` file or in `code/.env`.

Typical values:

```dotenv
QS_AWS_ACCOUNT_ID=123456789012
QS_AWS_REGION=eu-central-1

QS_DATASOURCE_ID=your-quicksight-data-source-id
QS_DATASOURCE_NAME=your-quicksight-data-source-name
DATASOURCE_ID=your-quicksight-data-source-id
QS_TARGET_USERS=arn1,arn2
QS_PRINCIPAL_ARN=arn:aws:quicksight:eu-central-1:123456789012:user/default/user@example.com

DB_HOST=your-db-host
DB_NAME=your_database
DB_PORT=5432
DB_USER=your_db_user
DB_PASS=your_db_password
VPC_CONN_ARN=arn:aws:quicksight:eu-central-1:123456789012:vpcConnection/your-vpc-connection-id

VPC_CONN_ID=your-vpc-connection-id
QS_VPC_PEERING_ID=pcx-xxxxxxxxxxxxxxxxx
QS_DATA_VPC_ID=vpc-xxxxxxxxxxxxxxxxx
CLUSTER_ID=your-rds-cluster-id
QS_SECURITY_GROUP_ID=sg-xxxxxxxxxxxxxxxxx
QS_PROD_CIDR=10.10.0.0/16
```

## Safety

Most audit scripts are read-only. Most mutating scripts either:

- are dry-run by default and need `--apply`, or
- are narrow older helpers that now document their assumptions explicitly

Before using a mutating script:

1. Run its `--help`.
2. Run the dry run first if it supports one.
3. Keep the generated `logs/` plan or report until you have verified the result.

## Repository Layout

### Core toolbox

The main toolbox lives under `code/`.

Use [code/README.md](code/README.md) for the full catalog. It documents:

- what each script does
- whether it mutates QuickSight or AWS state
- what inputs it expects
- what outputs it writes
- example commands

The shared helper module [code/qs_common.py](code/qs_common.py) is now the base for newer and refactored scripts, so future toolbox work should build on that instead of duplicating env loading, pagination, and logging.

### Generated artifacts

`logs/` is intentionally disposable. It contains:

- audit reports
- migration plans
- dataset backup JSON files
- placeholder restore notes
- refresh logs

The cleanup helper keeps `logs/commands.txt` by default and removes the rest.

### Miscellaneous shell scripts

`scripts/` is not part of the main QuickSight CLI toolbox. At the moment it only contains one unrelated operational helper, documented in [scripts/README.md](scripts/README.md).

## Recommended Workflows

### Audit a dataset or asset family

```powershell
python code\quicksight_audit.py --dataset-name-contains "marketplace" --calc-fields
python code\qs_dataset_dependencies.py "your-dataset-id"
```

### Grant a user access to everything relevant

```powershell
python code\qs_grant_user_access.py --user daniel@formunauts.at --all-datasets --all-analyses --all-dashboards
python code\qs_grant_user_access.py --user daniel@formunauts.at --all-datasets --all-analyses --all-dashboards --apply
```

### Clean up generated plans and caches

```powershell
python code\qs_cleanup_workspace.py
python code\qs_cleanup_workspace.py --apply
```

### Run the paymentattempt migration workflow

These helpers are still kept because they proved useful, but they are deliberately documented as campaign-specific tooling in the script catalog:

```powershell
python code\qs_find_failed_refresh_paymentattempt_datasets.py
python code\qs_filter_failed_refresh_paymentattempt_targets.py --audit-file .\logs\paymentattempt_failed_refresh_audit_YYYYMMDD_HHMMSS.json
python code\qs_audit_dataset_consumers.py --plan-file .\logs\paymentattempt_failed_refresh_targets_YYYYMMDD_HHMMSS_candidates.json
python code\qs_migrate_paymentattempt_datasets.py --dataset-ids your-dataset-id --replace-oversized-with-placeholders --preserve-exposed-field-names --apply --wait-for-refresh
```

## Cleanup Notes

This repo cleanup did four things:

- removed the hard-coded one-off `code/rename_fields_in_analyses.py`
- added a reusable workspace cleanup script
- reduced log clutter and Python cache clutter
- clarified which scripts are general-purpose toolbox scripts versus campaign-specific helpers

If logs pile up again:

```powershell
python code\qs_cleanup_workspace.py --apply
```
