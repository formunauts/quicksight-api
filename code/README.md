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
- Inputs: CLI filters such as `--datasets`, `--dataset-name-contains`, `--calc-fields`, `--analysis`, `--dashboard`; optional `--all-regions` for cross-region scans.
- Outputs: a timestamped text report in `logs/`.
- Example:

```powershell
python code\quicksight_audit.py --dataset-name-contains "marketplace" --calc-fields
python code\quicksight_audit.py --lookup-ids your-id --all-regions
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

### `qs_dashboard_report_schedule_audit.py`

- Purpose: audit dashboard snapshot/report schedules and executions using CloudTrail (`StartDashboardSnapshotJob`, `StartDashboardSnapshotJobSchedule`) plus snapshot job detail APIs.
- Mutates: no.
- Inputs: optional `--mystery-ids`, `--days` or `--start-date`/`--end-date`, optional `--dashboard-id-contains`, `--limit-dashboards`, `--all-regions`, `--cloudtrail-region`.
- Outputs: timestamped text and JSON reports in `logs/`, including execution timestamps/status when resolvable.
- Notes: QuickSight currently exposes start/describe snapshot-job APIs but no list-schedules API in this SDK model, so schedule visibility is reconstructed from CloudTrail events.
- Example:

```powershell
python code\qs_dashboard_report_schedule_audit.py --days 14
python code\qs_dashboard_report_schedule_audit.py --mystery-ids abc123 def456 --all-regions
python code\qs_dashboard_report_schedule_audit.py --start-date 2026-07-01 --end-date 2026-07-20 --dashboard-id-contains a1904e
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

### `qs_datasource_dependencies.py`

- Purpose: find datasets that use one or more QuickSight data sources, then list analyses that consume those datasets.
- Mutates: no.
- Inputs: one of `--data-source-id`, `--data-source-name`, or `--data-source-name-contains`, optionally `--dataset-name-contains`, `--limit`, `--skip-analyses`.
- Outputs: timestamped text and JSON reports in `logs/`.
- Example:

```powershell
python code\qs_datasource_dependencies.py --data-source-name "LiveDataBase"
python code\qs_datasource_dependencies.py --data-source-name-contains "Live" --dataset-name-contains payment
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

### `qs_dataset_refresh_audit.py`

- Purpose: scan datasets across the account, inspect recent ingestion history and refresh schedules, and match provided mystery IDs.
- Mutates: no.
- Inputs: required `--mystery-ids`, optionally `--dataset-name-contains`, `--limit`, `--max-ingestions-per-dataset`, `--all-regions`.
- Outputs: timestamped text and JSON reports in `logs/`.
- Example:

```powershell
python code\qs_dataset_refresh_audit.py --mystery-ids id_1 id_2 id_3 id_4
python code\qs_dataset_refresh_audit.py --mystery-ids id_1 --dataset-name-contains payment --max-ingestions-per-dataset 50
python code\qs_dataset_refresh_audit.py --mystery-ids id_1 --all-regions
```

### `qs_inspect_dataset_table_maps.py`

- Purpose: inspect `PhysicalTableMap` and `LogicalTableMap` across datasets, including table IDs and source metadata.
- Mutates: no.
- Inputs: optional `--dataset-ids`, `--dataset-name-contains`, `--physical-table-ids`, `--logical-table-ids`, `--table-id-contains`, `--limit`, `--all-regions`.
- Error handling: fail-fast by default; use `--continue-on-error` to keep scanning after describe failures.
- Outputs: timestamped text and JSON reports in `logs/`.
- Example:

```powershell
python code\qs_inspect_dataset_table_maps.py
python code\qs_inspect_dataset_table_maps.py --table-id-contains physical --dataset-name-contains payment
python code\qs_inspect_dataset_table_maps.py --physical-table-ids pt_123 pt_456 --continue-on-error
python code\qs_inspect_dataset_table_maps.py --table-id-contains lt_ --all-regions
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

### `qs_user_dashboard_access.py`

- Purpose: list dashboards a specific QuickSight user can access (directly or via group membership), including dashboard names and the linked source analysis names when available.
- Mutates: no.
- Inputs: one of `--user-arn`, `--user-email`, or `--user-name`; optional `--dashboard-name-contains`, `--retry-attempts`, `--retry-base-seconds`, `--between-dashboard-seconds`.
- Outputs: timestamped text and JSON reports in `logs/`.
- Notes: includes retry/backoff for QuickSight throttling and can pace dashboard permission calls to avoid burst limits.
- Example:

```powershell
python code\qs_user_dashboard_access.py --user-email user@example.com
python code\qs_user_dashboard_access.py --user-name your-qs-username --dashboard-name-contains payment
python code\qs_user_dashboard_access.py --user-email user@example.com --retry-attempts 10 --retry-base-seconds 1.5 --between-dashboard-seconds 0.25
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

### `qs_find_field_usage.py`

- Purpose: scan QuickSight datasets, analyses, and dashboards for usage of one or more table names and/or column names.
- Mutates: no.
- Inputs: `--tables` and/or `--columns`, optionally `--table-file`, `--column-file`, `--dataset-name-contains`, `--limit`, `--skip-analyses`, `--skip-dashboards`, `--only-consumers-of-matched-datasets`, `--write-triage-csv`, `--check-refresh-schedules`, `--check-dataset-calculated-fields`, `--check-analysis-calculated-fields`, `--check-custom-sql-column-mentions`.
- Outputs: timestamped text and JSON reports in `logs/`; optional CSV triage report with one row per matched dataset.
- Notes: this is the broadest read-only impact check for schema changes. It can find references in dataset SQL and in downstream analysis/dashboard definitions. The summary includes `hard_dependency_datasets` versus `string_only_datasets`, refresh-schedule coverage, and `CRITICAL`/`HIGH`/`MEDIUM`/`LOW` priority labels across datasets, analyses, and dashboards. `CRITICAL` is assigned when dataset/analysis calculated fields reference target columns, or when target columns are directly mentioned in dataset custom SQL.
- Example:

```powershell
python code\qs_find_field_usage.py --tables bureau_donation --columns was_callback self_checkout_journey bureau_donation.was_callback bureau_donation.self_checkout_journey
python code\qs_find_field_usage.py --column-file .\columns-to-check.txt --only-consumers-of-matched-datasets
python code\qs_find_field_usage.py --columns was_callback self_checkout_journey --write-triage-csv
python code\qs_find_field_usage.py --columns was_callback self_checkout_journey --write-triage-csv --check-refresh-schedules
python code\qs_find_field_usage.py --columns was_callback self_checkout_journey bureau_donation.was_callback bureau_donation.self_checkout_journey --write-triage-csv --check-refresh-schedules --check-dataset-calculated-fields --check-analysis-calculated-fields --check-custom-sql-column-mentions
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

### `qs_migrate_custom_sql_datasource.py`

- Purpose: update dataset physical tables so entries still using one legacy QuickSight data source are switched to a target data source.
- Mutates: yes, only with `--apply`.
- Inputs: required `--dataset-ids`, `--legacy-data-source-name`, and `--target-data-source-name`.
- Outputs: timestamped text and JSON plan/result files in `logs/`.
- Notes: dry run first. The script rewrites `PhysicalTableMap.*.(CustomSql|RelationalTable|S3Source).DataSourceArn` values that match the resolved legacy source ARN exactly.
- Example:

```powershell
python code\qs_migrate_custom_sql_datasource.py --dataset-ids ds_1 ds_2 --legacy-data-source-name "LiveDataBase" --target-data-source-name "formunauts-prod-rds-data-source"
python code\qs_migrate_custom_sql_datasource.py --dataset-ids ds_1 ds_2 --legacy-data-source-name "LiveDataBase" --target-data-source-name "formunauts-prod-rds-data-source" --apply
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

### `qs_update_datasource_connection.py`

- Purpose: update one QuickSight data source connection target in place (for example switch writer host to reader host or writer instance id to reader instance id) while keeping the same data source id and ARN.
- Mutates: yes, only with `--apply`.
- Inputs: one target selector (`--data-source-id` or `--data-source-name`) plus one connection source (`--new-host`, `--new-instance-id`, `--from-data-source-id`, or `--from-data-source-name`), optionally `--new-port`, `--new-database`.
- Outputs: timestamped text and JSON reports in `logs/`.
- Notes: this is the safest path when many datasets depend on one data source, because datasets remain attached to the same data source ARN. If the role lacks `quicksight:CopyDataSourceCredentials`, provide `DB_USER` and `DB_PASS` in environment or use `--db-user` and `--db-pass` so the update can use `CredentialPair` instead of credential copy.
- Example:

```powershell
python code\qs_update_datasource_connection.py --data-source-name "LiveDataBase" --from-data-source-name "formunauts-prod-rds-data-source"
python code\qs_update_datasource_connection.py --data-source-name "LiveDataBase" --from-data-source-name "formunauts-prod-rds-data-source" --apply
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
