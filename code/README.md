# Script Catalog

This file is the detailed catalog for the QuickSight CLI toolbox.

Every script below includes:

- purpose
- mutation behavior
- expected inputs
- outputs
- a typical command

## Read This First

- Run commands from the repository root.
- Many scripts rely on `QS_AWS_ACCOUNT_ID` and `QS_AWS_REGION`.
- Scripts that write plans, reports, or backups write them into `logs/`.
- For mutating scripts, prefer the dry run first when available.

## Shared Foundation

### `qs_common.py`

- Purpose: shared helpers for newer and refactored toolbox scripts.
- Mutates: no.
- Provides:
  - `.env` loading
  - repository and `logs/` path resolution
  - QuickSight client creation
  - required-env validation
  - paginated `get_all_summaries(...)`
  - a small file-and-console logger
  - timestamped log path creation
- Note: this is an internal utility module, not a standalone CLI entrypoint.

## Audits And Discovery

### `quicksight_audit.py`

- Purpose: broad QuickSight inventory and search helper for datasets, calculated fields, analyses, and dashboards.
- Mutates: no.
- Notes: refactored onto `qs_common.py`.
- Inputs: CLI filters such as `--datasets`, `--dataset-name-contains`, `--calc-fields`, `--analysis`, `--dashboard`.
- Outputs: a timestamped text report in `logs/`.
- Example:

```powershell
python code\quicksight_audit.py --dataset-name-contains "marketplace" --calc-fields
```

### `qs_dashboard_source.py`

- Purpose: find the source entity behind one or more dashboards and resolve the linked analysis when available.
- Mutates: no.
- Inputs: one of `--dashboard-id`, `--dashboard-name`, or `--dashboard-name-contains`.
- Outputs: a timestamped text report in `logs/`.
- Example:

```powershell
python code\qs_dashboard_source.py --dashboard-name "Overall Gross Recurring Production Report"
python code\qs_dashboard_source.py --dashboard-name-contains "Gross Recurring"
```

### `quicksight_datasources.py`

- Purpose: list QuickSight data sources, types, connection details, and VPC connection usage.
- Mutates: no.
- Notes: refactored onto `qs_common.py`.
- Inputs: optional `--type` filter such as `POSTGRESQL`, `AURORA`, `ATHENA`, `S3`.
- Outputs: a timestamped text report in `logs/`.
- Example:

```powershell
python code\quicksight_datasources.py --type POSTGRESQL AURORA
```

### `qs_dataset_dependencies.py`

- Purpose: inspect which analyses appear to reference fields from a given dataset.
- Mutates: no.
- Notes: refactored onto `qs_common.py`.
- Inputs: one dataset id.
- Outputs: a text report in `logs/`.
- Example:

```powershell
python code\qs_dataset_dependencies.py "your-dataset-id"
```

### `qs_find_analysis_filters.py`

- Purpose: find analyses whose definitions contain a filter that matches a given column, operator, and optionally a literal value.
- Mutates: no.
- Notes: refactored onto `qs_common.py`.
- Inputs: `--field`, optionally `--operator`, `--value`, `--field-match`, `--profile`.
- Notes: by default the script uses the AWS credentials already active in your shell, for example from `awsume`. Use `--profile` only to override that.
- Outputs: timestamped text and JSON reports in `logs/`.
- Example:

```powershell
python code\qs_find_analysis_filters.py --field customer_id --operator EQUALS
```

### `qs_analysis_edit_history.py`

- Purpose: inspect CloudTrail for who edited one analysis and when, then show the analysis's current layout type.
- Mutates: no.
- Notes: by default the script uses the AWS credentials already active in your shell, for example from `awsume`. It relies on CloudTrail `LookupEvents`, so the searchable window is limited to the last 90 days.
- Inputs: one of `--analysis-id` or `--analysis-name`, optionally `--days` or `--start-date` with `--end-date`, `--cloudtrail-region`, `--include-read-only`, `--all-event-names`, `--event-names`, `--contains-text`, `--profile`, `--show-raw`.
- Outputs: timestamped text and JSON reports in `logs/`.
- Example:

```powershell
python code\qs_analysis_edit_history.py --analysis-id your-analysis-id
python code\qs_analysis_edit_history.py --analysis-name "Your Analysis Name" --days 30
python code\qs_analysis_edit_history.py --analysis-id your-analysis-id --start-date 2026-04-30 --end-date 2026-05-06 --include-read-only --all-event-names --contains-text export pdf csv xlsx download
```

### `qs_dashboard_activity_history.py`

- Purpose: inspect CloudTrail for dashboard activity in a time window, including broad export-like searches.
- Mutates: no.
- Notes: by default the script uses the AWS credentials already active in your shell, for example from `awsume`. It relies on CloudTrail `LookupEvents`, so the searchable window is limited to the last 90 days.
- Inputs: one of `--dashboard-id` or `--dashboard-name`, optionally `--days` or `--start-date` with `--end-date`, `--cloudtrail-region`, `--include-read-only`, `--all-event-names`, `--event-names`, `--contains-text`, `--profile`, `--show-raw`.
- Outputs: timestamped text and JSON reports in `logs/`.
- Example:

```powershell
python code\qs_dashboard_activity_history.py --dashboard-id your-dashboard-id
python code\qs_dashboard_activity_history.py --dashboard-id your-dashboard-id --start-date 2026-04-30 --end-date 2026-05-06 --include-read-only --all-event-names --contains-text export pdf csv xlsx download
```

### `qs_asset_access_report.py`

- Purpose: report the current principals and permission actions on one analysis, one published dashboard, and/or one shared folder.
- Mutates: no.
- Notes: this reports current access only. It does not reconstruct historical access at earlier points in time.
- Inputs: `--analysis-id` or `--analysis-name`, optionally `--dashboard-id`, `--folder-id`, or `--folder-name`.
- Outputs: timestamped text and JSON reports in `logs/`.
- Example:

```powershell
python code\qs_asset_access_report.py --analysis-id your-analysis-id
python code\qs_asset_access_report.py --dashboard-id your-dashboard-id
python code\qs_asset_access_report.py --analysis-id your-analysis-id --dashboard-id your-dashboard-id
python code\qs_asset_access_report.py --folder-id your-folder-id
```

### `qs_audit_dataset_consumers.py`

- Purpose: find which analyses and dashboards use datasets from a migration or target plan.
- Mutates: no.
- Inputs: `--plan-file`.
- Outputs: JSON and text reports in `logs/`.
- Example:

```powershell
python code\qs_audit_dataset_consumers.py --plan-file .\logs\paymentattempt_failed_refresh_targets_YYYYMMDD_HHMMSS_candidates.json
```

### `qs_find_table_references.py`

- Purpose: find QuickSight datasets whose physical tables or custom SQL reference one or more database tables, then report analyses and dashboards that consume those datasets.
- Mutates: no.
- Inputs: `--tables` or `--table-file`, optionally `--dataset-name-contains`, `--limit`, or `--skip-consumers`.
- Outputs: timestamped text and JSON reports in `logs/`.
- Notes: table matching is case-insensitive and identifier-aware, so `bureau_unfinisheddonation` does not match `bureau_unfinisheddonationrangeslider`.
- Example:

```powershell
python code\qs_find_table_references.py --tables bureau_unfinisheddonation bureau_unfinisheddonationadditionalsignature bureau_unfinisheddonationadditionaltextfield bureau_unfinisheddonationrangeslider bureau_unfinisheddonation_form_additional_checkboxes bureau_unfinisheddonation_form_additional_radio_buttons
python code\qs_find_table_references.py --table-file .\tables-to-check.txt
```

### `qs_migrate_unfinished_donation_tables.py`

- Purpose: migrate QuickSight dataset custom SQL from the old `bureau_unfinisheddonation` table to filtered `bureau_donation` subqueries after the Donation Table Merge.
- Mutates: yes, only with `--apply`.
- Inputs: `--audit-file` from `qs_find_table_references.py`, or explicit `--dataset-ids`.
- Outputs: full dataset backups, a migration plan JSON, and a text log in `logs/`.
- Notes: dry run first. The script flags `SELECT *` for review and will not apply warning-bearing changes unless `--allow-warnings` is provided. Use `--wait-for-refresh` to refresh SPICE datasets strictly one at a time; the script waits until each ingestion finishes before moving to the next dataset.
- Example:

```powershell
python code\qs_migrate_unfinished_donation_tables.py --audit-file .\logs\table_reference_audit_YYYYMMDD_HHMMSS.json
python code\qs_migrate_unfinished_donation_tables.py --audit-file .\logs\table_reference_audit_YYYYMMDD_HHMMSS.json --apply --allow-warnings --wait-for-refresh --between-refresh-delay-seconds 30 --continue-on-failure
```

### `qs_delete_unused_datasets.py`

- Purpose: delete datasets that a consumer audit marks as unused.
- Mutates: yes, only with `--apply`.
- Inputs: `--consumer-audit-file`, optionally `--dataset-ids`.
- Outputs: JSON and text reports in `logs/`.
- Example:

```powershell
python code\qs_delete_unused_datasets.py --consumer-audit-file .\logs\dataset_consumer_audit_YYYYMMDD_HHMMSS.json
python code\qs_delete_unused_datasets.py --consumer-audit-file .\logs\dataset_consumer_audit_YYYYMMDD_HHMMSS.json --apply
```

## Data Sources And Sharing

### `quicksight_create_source.py`

- Purpose: create a PostgreSQL QuickSight data source through an existing VPC connection.
- Mutates: yes, only with `--apply`.
- Inputs: `--data-source-id`, `--name`, `--db-user`, `--db-pass`, plus database and VPC values from `.env`.
- Outputs: console preview or a live create request followed by validation polling.
- Example:

```powershell
python code\quicksight_create_source.py --data-source-id ds-example --name "Example DS"
python code\quicksight_create_source.py --data-source-id ds-example --name "Example DS" --apply
```

### `quicksight_share_source.py`

- Purpose: grant one QuickSight user access to one data source.
- Mutates: yes, only with `--apply`.
- Inputs: `--data-source-id`, `--user-arn`.
- Outputs: console preview. If `--user-arn` is omitted, the script lists QuickSight users for lookup.
- Example:

```powershell
python code\quicksight_share_source.py --data-source-id ds-example
python code\quicksight_share_source.py --data-source-id ds-example --user-arn arn:aws:quicksight:... --apply
```

### `quicksight_share_with_team.py`

- Purpose: grant one data source to multiple QuickSight user ARNs.
- Mutates: yes, only with `--apply`.
- Inputs: `--data-source-id`, `--user-arns`, or `QS_TARGET_USERS` from `.env`.
- Outputs: console preview or a live permission update.
- Example:

```powershell
python code\quicksight_share_with_team.py --data-source-id ds-example --user-arns arn1 arn2
python code\quicksight_share_with_team.py --data-source-id ds-example --user-arns arn1 arn2 --apply
```

### `qs_grant_user_access.py`

- Purpose: grant a QuickSight user access to datasets, analyses, and dashboards by copying an existing action set from each resource.
- Mutates: yes, only with `--apply`.
- Inputs: `--user`, plus either `--all-datasets`, `--all-analyses`, `--all-dashboards`, or a dataset `--plan-file`.
- Notes: use `--continue-on-error` for broad grants so one failed resource does not stop the run. Retryable QuickSight throttling errors are retried with exponential backoff via `--retry-attempts` and `--retry-base-seconds`.
- Outputs: JSON and text reports in `logs/`.
- Example:

```powershell
python code\qs_grant_user_access.py --user daniel@formunauts.at --all-datasets --all-analyses --all-dashboards
python code\qs_grant_user_access.py --user daniel@formunauts.at --all-datasets --all-analyses --all-dashboards --apply --continue-on-error
```

## Paymentattempt Migration Helpers

These are intentionally kept, but they are campaign-specific tools for the `bureau_paymentattempt` migration family rather than general-purpose QuickSight tooling.

### `qs_find_failed_refresh_paymentattempt_datasets.py`

- Purpose: find datasets whose latest refresh failed and whose SQL references `bureau_paymentattempt`.
- Mutates: no.
- Inputs: none beyond normal environment setup.
- Outputs: audit JSON, audit text, and a target plan in `logs/`.
- Example:

```powershell
python code\qs_find_failed_refresh_paymentattempt_datasets.py
```

### `qs_filter_failed_refresh_paymentattempt_targets.py`

- Purpose: reduce a failed-refresh audit to the datasets that match the expected paymentattempt failure pattern.
- Mutates: no.
- Inputs: `--audit-file`.
- Outputs: a filtered JSON target file in `logs/`.
- Example:

```powershell
python code\qs_filter_failed_refresh_paymentattempt_targets.py --audit-file .\logs\paymentattempt_failed_refresh_audit_YYYYMMDD_HHMMSS.json
```

### `qs_filter_used_consumer_targets.py`

- Purpose: reduce a consumer audit to datasets that are still used by at least one analysis or dashboard.
- Mutates: no.
- Inputs: `--consumer-audit-file`.
- Outputs: a filtered JSON file in `logs/`.
- Example:

```powershell
python code\qs_filter_used_consumer_targets.py --consumer-audit-file .\logs\dataset_consumer_audit_YYYYMMDD_HHMMSS.json
```

### `qs_migrate_paymentattempt_datasets.py`

- Purpose: update dataset definitions away from legacy `donation_object_id` and `donation_content_type_id` assumptions.
- Mutates: yes, only with `--apply`.
- Inputs: optional `--plan-file`, `--datasets`, `--dataset-ids`, `--replace-oversized-with-placeholders`, `--preserve-exposed-field-names`, `--wait-for-refresh`.
- Outputs: migration plan JSON, text log, backup JSON, and placeholder restore text when applicable.
- Example:

```powershell
python code\qs_migrate_paymentattempt_datasets.py --dataset-ids your-dataset-id --replace-oversized-with-placeholders --preserve-exposed-field-names
python code\qs_migrate_paymentattempt_datasets.py --dataset-ids your-dataset-id --replace-oversized-with-placeholders --preserve-exposed-field-names --apply --wait-for-refresh
```

### `qs_refresh_datasets.py`

- Purpose: trigger SPICE ingestions strictly one dataset at a time.
- Mutates: yes, only with `--apply`.
- Inputs: `--plan-file` or `--dataset-ids`.
- Outputs: refresh log in `logs/`.
- Example:

```powershell
python code\qs_refresh_datasets.py --dataset-ids your-dataset-id --apply
```

### `qs_restore_datasets_from_backups.py`

- Purpose: restore dataset definitions from backup files referenced by a migration plan.
- Mutates: yes, only with `--apply`.
- Inputs: `--plan-file`.
- Outputs: restore log and JSON summary in `logs/`.
- Example:

```powershell
python code\qs_restore_datasets_from_backups.py --plan-file .\logs\paymentattempt_dataset_plan_YYYYMMDD_HHMMSS.json
python code\qs_restore_datasets_from_backups.py --plan-file .\logs\paymentattempt_dataset_plan_YYYYMMDD_HHMMSS.json --apply
```

### `qs_export_placeholder_restore_formulas.py`

- Purpose: turn placeholder formula entries from a migration plan into a paste-friendly text file.
- Mutates: no.
- Inputs: `--plan-file`.
- Outputs: a restore text file in `logs/`.
- Example:

```powershell
python code\qs_export_placeholder_restore_formulas.py --plan-file .\logs\paymentattempt_dataset_plan_YYYYMMDD_HHMMSS.json
```

### `qs_recover_current_placeholder_formulas.py`

- Purpose: find placeholder-valued calculated fields in live datasets and recover original formulas from older backups when possible.
- Mutates: no.
- Inputs: `--datasets`.
- Outputs: JSON and text recovery reports in `logs/`.
- Example:

```powershell
python code\qs_recover_current_placeholder_formulas.py --datasets "Marketplace_Dach_New" "IRC_Master"
```

### `qs_audit_paymentattempt_downstream.py`

- Purpose: audit analyses and dashboards for downstream references to legacy paymentattempt field names.
- Mutates: no.
- Inputs: `--plan-file`, optionally `--skip-analyses` or `--skip-dashboards`.
- Outputs: JSON and text reports in `logs/`.
- Example:

```powershell
python code\qs_audit_paymentattempt_downstream.py --plan-file .\logs\paymentattempt_dataset_plan_YYYYMMDD_HHMMSS.json
```

### `qs_migrate_paymentattempt_downstream.py`

- Purpose: rewrite downstream analysis and dashboard references for a full exposed-schema rename.
- Mutates: yes, only with `--apply`.
- Inputs: `--audit-file`, `--plan-file`.
- Outputs: migration plan JSON, log, and backups in `logs/`.
- Example:

```powershell
python code\qs_migrate_paymentattempt_downstream.py --audit-file .\logs\paymentattempt_downstream_audit_YYYYMMDD_HHMMSS.json --plan-file .\logs\paymentattempt_dataset_plan_YYYYMMDD_HHMMSS.json
```

## Networking And Connectivity

### `qs_firewall.py`

- Purpose: inspect likely QuickSight-related ingress rules on a database security group.
- Mutates: no.
- Inputs: environment variables for the target security group and account details.
- Outputs: console output and optional report text depending on script path.
- Example:

```powershell
python code\qs_firewall.py
```

### `qs_fix_firewall.py`

- Purpose: add a PostgreSQL ingress rule for a QuickSight connectivity scenario.
- Mutates: yes.
- Inputs: environment variables such as `QS_SECURITY_GROUP_ID` and `QS_PROD_CIDR`.
- Outputs: console status.
- Example:

```powershell
python code\qs_fix_firewall.py
```

### `qs_route_table_inspection.py`

- Purpose: inspect route tables around a QuickSight VPC connection.
- Mutates: no.
- Inputs: `VPC_CONN_ID` and related VPC environment values.
- Outputs: console output.
- Example:

```powershell
python code\qs_route_table_inspection.py
```

### `verify_peering_target.py`

- Purpose: verify that a VPC peering connection points at the expected data VPC.
- Mutates: no.
- Inputs: `QS_VPC_PEERING_ID` and `QS_DATA_VPC_ID`.
- Outputs: console output.
- Example:

```powershell
python code\verify_peering_target.py
```

## Workspace Cleanup

### `qs_cleanup_workspace.py`

- Purpose: remove generated logs, backup folders, `__pycache__` directories, and `.pyc` files.
- Mutates: yes, only with `--apply`.
- Inputs: optional `--keep-log`, `--skip-logs`, `--skip-python-cache`.
- Outputs: console preview or deletion summary.
- Example:

```powershell
python code\qs_cleanup_workspace.py
python code\qs_cleanup_workspace.py --apply
```
