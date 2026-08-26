"""Report dashboards with a published sheet filter control for customer columns.

The default targets are ``customer_id`` and ``customer_name``. The report only
includes a dashboard when a matching FilterGroup filter is linked to a
FilterControl on a sheet. It does not change any QuickSight asset.
"""

import argparse
import json
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)

DEFAULT_TARGET_FIELDS = ["customer_id", "customer_name"]


def parse_source_entity_arn(source_arn: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return the QuickSight resource type and id from a source entity ARN."""
    if not source_arn or ":" not in source_arn or "/" not in source_arn:
        return None, None

    resource_path = source_arn.split(":", 5)[-1]
    resource_type, _, resource_id = resource_path.partition("/")
    if not resource_type or not resource_id:
        return None, None
    return resource_type, resource_id


def iter_filter_columns(value: Any, path: str) -> Iterable[Tuple[Dict[str, Any], str]]:
    """Yield ColumnIdentifier objects nested in one QuickSight filter."""
    if isinstance(value, dict):
        column = value.get("Column")
        if isinstance(column, dict):
            yield column, f"{path}.Column"
        for key, child in value.items():
            if key != "Column":
                yield from iter_filter_columns(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_filter_columns(child, f"{path}[{index}]")


def filter_type(filter_definition: Dict[str, Any]) -> str:
    """Return the one-of key used for a QuickSight Filter object."""
    for key, value in filter_definition.items():
        if value is not None:
            return key
    return "Unknown"


def find_matching_filters(
    definition: Dict[str, Any], field_names: Iterable[str]
) -> List[Dict[str, Any]]:
    """Find FilterGroups that reference a target column exactly and case-sensitively."""
    target_fields = set(field_names)
    matches: List[Dict[str, Any]] = []
    for group_index, group in enumerate(definition.get("FilterGroups", [])):
        group_id = group.get("FilterGroupId", "")
        group_status = group.get("Status", "")
        scope = group.get("ScopeConfiguration", {})
        for filter_index, filter_definition in enumerate(group.get("Filters", [])):
            match_columns = []
            for column, column_path in iter_filter_columns(
                filter_definition,
                f"Definition.FilterGroups[{group_index}].Filters[{filter_index}]",
            ):
                if column.get("ColumnName") in target_fields:
                    match_columns.append(
                        {
                            "column_name": column.get("ColumnName"),
                            "data_set_identifier": column.get("DataSetIdentifier"),
                            "path": column_path,
                        }
                    )
            if not match_columns:
                continue

            filter_body = filter_definition.get(filter_type(filter_definition), {})
            matches.append(
                {
                    "filter_group_id": group_id,
                    "filter_group_status": group_status,
                    "filter_group_scope": scope,
                    "filter_type": filter_type(filter_definition),
                    "filter_id": filter_body.get("FilterId", "") if isinstance(filter_body, dict) else "",
                    "columns": match_columns,
                }
            )
    return matches


def find_filter_controls(
    definition: Dict[str, Any], matching_filter_ids: set[str]
) -> List[Dict[str, str]]:
    """Map matching source filters to the sheets that contain their controls."""
    controls: List[Dict[str, str]] = []
    for sheet in definition.get("Sheets", []):
        sheet_id = sheet.get("SheetId", "")
        sheet_name = sheet.get("Name") or sheet.get("Title") or sheet_id
        for control_index, control in enumerate(sheet.get("FilterControls", [])):
            for control_type, control_body in control.items():
                if not isinstance(control_body, dict):
                    continue
                source_filter_id = control_body.get("SourceFilterId", "")
                if source_filter_id in matching_filter_ids:
                    controls.append(
                        {
                            "sheet_id": sheet_id,
                            "sheet_name": sheet_name,
                            "control_type": control_type,
                            "control_id": control_body.get("FilterControlId", ""),
                            "control_title": control_body.get("Title", ""),
                            "source_filter_id": source_filter_id,
                            "path": f"Definition.Sheets[{sheet_id}].FilterControls[{control_index}]",
                        }
                    )
    return controls


def resolve_source_analysis(qs_client, dashboard_id: str) -> Dict[str, str]:
    """Get source-analysis metadata from a dashboard's published version."""
    response = qs_client.describe_dashboard(
        AwsAccountId=QS_ACCOUNT_ID,
        DashboardId=dashboard_id,
    )
    dashboard = response.get("Dashboard", {})
    version = dashboard.get("Version", {}) or {}
    source_arn = version.get("SourceEntityArn", "")
    source_type, source_id = parse_source_entity_arn(source_arn)
    result = {
        "source_entity_arn": source_arn,
        "source_type": source_type or "",
        "source_analysis_id": "",
        "source_analysis_name": "",
        "source_analysis_arn": "",
    }
    if source_type != "analysis" or not source_id:
        return result

    result["source_analysis_id"] = source_id
    analysis_response = qs_client.describe_analysis(
        AwsAccountId=QS_ACCOUNT_ID,
        AnalysisId=source_id,
    )
    analysis = analysis_response.get("Analysis", {})
    result["source_analysis_name"] = analysis.get("Name", "")
    result["source_analysis_arn"] = analysis.get("Arn", "")
    return result


def write_json_report(path: str, report: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Find published QuickSight dashboards with a sheet filter control for customer fields and report their "
            "source analyses. "
            "This is a read-only audit."
        )
    )
    parser.add_argument(
        "--field",
        action="append",
        help=(
            "Exact, case-sensitive QuickSight column name to find. Repeat for multiple fields. "
            "Defaults to customer_id and customer_name."
        ),
    )
    parser.add_argument(
        "--dashboard-name-contains",
        help="Optional case-insensitive dashboard-name substring to limit the scan.",
    )
    args = parser.parse_args()

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    target_fields = args.field or DEFAULT_TARGET_FIELDS
    log_path = build_log_path("quicksight_dashboard_customer_filter_control_audit")
    json_path = build_log_path("quicksight_dashboard_customer_filter_control_audit", "json")
    logger = Logger(log_path, "QUICKSIGHT DASHBOARD FILTER AUDIT")

    report: Dict[str, Any] = {
        "account_id": QS_ACCOUNT_ID,
        "region": QS_REGION,
        "target_fields": target_fields,
        "dashboard_name_contains": args.dashboard_name_contains or "",
        "dashboards": [],
        "errors": [],
        "summary": {},
    }

    try:
        qs_client = create_quicksight_client()
        dashboards = get_all_summaries(
            qs_client.list_dashboards,
            QS_ACCOUNT_ID,
            "DashboardSummaryList",
        )
        if args.dashboard_name_contains:
            needle = args.dashboard_name_contains.lower()
            dashboards = [
                dashboard for dashboard in dashboards if needle in dashboard.get("Name", "").lower()
            ]

        logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
        logger.log(f"Command: {' '.join(sys.argv)}")
        logger.log(f"Target fields (exact case-sensitive match): {', '.join(target_fields)}")
        logger.log(f"Dashboards in scope: {len(dashboards)}")
        logger.log(f"Text report: {log_path}")
        logger.log(f"JSON report: {json_path}")

        for index, dashboard_summary in enumerate(dashboards, start=1):
            if index == 1 or index % 25 == 0 or index == len(dashboards):
                logger.log(f"Scanning dashboards: {index}/{len(dashboards)}")

            dashboard_id = dashboard_summary.get("DashboardId", "")
            dashboard_name = dashboard_summary.get("Name", dashboard_id)
            try:
                response = qs_client.describe_dashboard_definition(
                    AwsAccountId=QS_ACCOUNT_ID,
                    DashboardId=dashboard_id,
                )
                definition = response.get("Definition", {})
                matching_filters = find_matching_filters(definition, target_fields)
            except Exception as exc:
                report["errors"].append(
                    {
                        "dashboard_id": dashboard_id,
                        "dashboard_name": dashboard_name,
                        "stage": "describe_dashboard_definition",
                        "error": str(exc),
                    }
                )
                continue

            matching_filter_ids = {item["filter_id"] for item in matching_filters if item["filter_id"]}
            controls = find_filter_controls(definition, matching_filter_ids)
            if not controls:
                continue

            controlled_filter_ids = {control["source_filter_id"] for control in controls}
            matching_filters = [
                item for item in matching_filters if item["filter_id"] in controlled_filter_ids
            ]
            try:
                source = resolve_source_analysis(qs_client, dashboard_id)
            except Exception as exc:
                source = {
                    "source_entity_arn": "",
                    "source_type": "",
                    "source_analysis_id": "",
                    "source_analysis_name": "",
                    "source_analysis_arn": "",
                }
                report["errors"].append(
                    {
                        "dashboard_id": dashboard_id,
                        "dashboard_name": dashboard_name,
                        "stage": "resolve_source_analysis",
                        "error": str(exc),
                    }
                )

            report["dashboards"].append(
                {
                    "dashboard_id": dashboard_id,
                    "dashboard_name": dashboard_name,
                    **source,
                    "matching_filters": matching_filters,
                    "matching_filter_controls": controls,
                    "has_sheet_filter_control": True,
                }
            )

        report["dashboards"].sort(key=lambda item: (item["dashboard_name"].lower(), item["dashboard_id"]))
        report["summary"] = {
            "dashboards_scanned": len(dashboards),
            "dashboards_with_matching_sheet_controls": len(report["dashboards"]),
            "matching_filters": sum(len(item["matching_filters"]) for item in report["dashboards"]),
            "matching_filter_controls": sum(
                len(item["matching_filter_controls"]) for item in report["dashboards"]
            ),
            "errors": len(report["errors"]),
        }

        logger.log("")
        logger.log(
            f"Dashboards with a sheet filter control for {', '.join(target_fields)}: "
            f"{report['summary']['dashboards_with_matching_sheet_controls']}"
        )
        for item in report["dashboards"]:
            analysis_name = item["source_analysis_name"] or "Source analysis unavailable"
            analysis_id = item["source_analysis_id"] or "N/A"
            logger.log(
                f"  Dashboard: {item['dashboard_name']} ({item['dashboard_id']}) | "
                f"Source analysis: {analysis_name} ({analysis_id}) | "
                f"{len(item['matching_filter_controls'])} sheet control(s) found"
            )

        logger.log(f"Errors: {report['summary']['errors']}")
        logger.log(f"JSON report: {json_path}")
        logger.log(f"Text report: {log_path}")
        write_json_report(json_path, report)
    except Exception as exc:
        report["fatal_error"] = str(exc)
        write_json_report(json_path, report)
        logger.log(f"FATAL ERROR: {exc}")
        logger.log(f"JSON report: {json_path}")


if __name__ == "__main__":
    main()
