import argparse
import csv
import html
import json
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def rich_text_to_plain_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def extract_title(payload: Dict[str, Any]) -> str:
    title = payload.get("Title")
    if not isinstance(title, dict):
        return ""

    format_text = title.get("FormatText")
    if isinstance(format_text, dict):
        plain_text = format_text.get("PlainText")
        if isinstance(plain_text, str):
            return plain_text.strip()
        rich_text = format_text.get("RichText")
        if isinstance(rich_text, str):
            normalized = rich_text_to_plain_text(rich_text)
            if normalized:
                return normalized

    plain_text = title.get("PlainText")
    if isinstance(plain_text, str):
        return plain_text.strip()

    rich_text = title.get("RichText")
    if isinstance(rich_text, str):
        normalized = rich_text_to_plain_text(rich_text)
        if normalized:
            return normalized

    return ""


def extract_visual_type_and_payload(visual_wrapper: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    for key, value in visual_wrapper.items():
        if key.endswith("Visual") and isinstance(value, dict):
            return key, value
    return "UnknownVisual", {}


def build_calculated_field_match_key(row: Dict[str, Any], mode: str) -> Optional[str]:
    name_norm = normalize_text(row.get("name", ""))
    expr_norm = normalize_text(row.get("expression", ""))
    dataset_identifier_norm = normalize_text(row.get("dataset_identifier", ""))

    if mode == "name-only":
        return name_norm or None
    if mode == "expression-only":
        return expr_norm or None

    if name_norm and expr_norm:
        return f"{dataset_identifier_norm}::{name_norm}::{expr_norm}"
    if name_norm:
        return f"{dataset_identifier_norm}::{name_norm}"
    if expr_norm:
        return f"{dataset_identifier_norm}::{expr_norm}"
    return None


def build_match_key(
    visual: Dict[str, Any],
    match_key: str,
    include_untitled: bool,
) -> Optional[str]:
    title_norm = normalize_text(visual.get("title", ""))
    visual_type = visual.get("visual_type", "UnknownVisual")
    sheet_name_norm = normalize_text(visual.get("sheet_name", ""))

    if match_key == "visual-id":
        return visual.get("visual_id")

    if match_key == "title-only":
        if title_norm:
            return title_norm
        if include_untitled:
            return f"untitled::{visual_type}::{sheet_name_norm}::{visual.get('visual_id', '')}"
        return None

    if match_key == "title-type":
        if title_norm:
            return f"{title_norm}::{visual_type}"
        if include_untitled:
            return f"untitled::{visual_type}::{sheet_name_norm}::{visual.get('visual_id', '')}"
        return None

    if title_norm:
        return f"{sheet_name_norm}::{title_norm}::{visual_type}"
    if include_untitled:
        return f"untitled::{sheet_name_norm}::{visual_type}::{visual.get('visual_id', '')}"
    return None


def extract_dashboard_visuals(
    definition: Dict[str, Any],
    match_key: str,
    include_untitled: bool,
) -> List[Dict[str, Any]]:
    visuals: List[Dict[str, Any]] = []
    sheets = definition.get("Sheets", [])

    for sheet_index, sheet in enumerate(sheets):
        sheet_id = sheet.get("SheetId", f"sheet_{sheet_index + 1}")
        sheet_name = sheet.get("Name", sheet_id)

        for visual_index, visual_wrapper in enumerate(sheet.get("Visuals", [])):
            visual_type, payload = extract_visual_type_and_payload(visual_wrapper)
            visual_id = payload.get("VisualId") or visual_wrapper.get("VisualId") or f"{sheet_id}_v{visual_index + 1}"
            title = extract_title(payload)

            row = {
                "sheet_id": sheet_id,
                "sheet_name": sheet_name,
                "visual_id": visual_id,
                "visual_type": visual_type,
                "title": title,
            }
            key = build_match_key(row, match_key, include_untitled)
            if key is None:
                continue
            row["match_key"] = key
            visuals.append(row)

    return visuals


def count_dashboard_visuals(definition: Dict[str, Any]) -> int:
    total = 0
    for sheet in definition.get("Sheets", []):
        total += len(sheet.get("Visuals", []))
    return total


def extract_dashboard_calculated_fields(
    definition: Dict[str, Any],
    calculated_field_match: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in definition.get("CalculatedFields", []):
        if not isinstance(item, dict):
            continue
        row = {
            "dataset_identifier": item.get("DataSetIdentifier", ""),
            "name": item.get("Name", ""),
            "expression": item.get("Expression", ""),
        }
        key = build_calculated_field_match_key(row, calculated_field_match)
        if key is None:
            continue
        row["match_key"] = key
        rows.append(row)
    return rows


def get_dashboard_summary_map(qs_client) -> Dict[str, Dict[str, str]]:
    rows = get_all_summaries(qs_client.list_dashboards, QS_ACCOUNT_ID, "DashboardSummaryList")
    return {
        row.get("DashboardId", ""): {
            "DashboardId": row.get("DashboardId", ""),
            "Name": row.get("Name", ""),
            "Arn": row.get("Arn", ""),
        }
        for row in rows
        if row.get("DashboardId")
    }


def resolve_reference_dashboard(
    dashboard_map: Dict[str, Dict[str, str]],
    reference_dashboard_id: Optional[str],
    reference_dashboard_name: Optional[str],
) -> Dict[str, str]:
    if reference_dashboard_id:
        row = dashboard_map.get(reference_dashboard_id)
        if not row:
            raise SystemExit(f"Reference dashboard id not found: {reference_dashboard_id}")
        return row

    matches = [row for row in dashboard_map.values() if row.get("Name") == reference_dashboard_name]
    if not matches:
        raise SystemExit(f"Reference dashboard name not found: {reference_dashboard_name}")
    if len(matches) > 1:
        raise SystemExit(
            "Reference dashboard name is not unique. Use --reference-dashboard-id instead. "
            f"Name: {reference_dashboard_name}, matches: {len(matches)}"
        )
    return matches[0]


def collect_target_dashboards(
    dashboard_map: Dict[str, Dict[str, str]],
    target_dashboard_ids: Optional[List[str]],
    target_dashboard_name_contains: Optional[str],
    limit: Optional[int],
    exclude_dashboard_id: Optional[str],
) -> List[Dict[str, str]]:
    targets: List[Dict[str, str]] = []

    if target_dashboard_ids:
        for dashboard_id in target_dashboard_ids:
            row = dashboard_map.get(dashboard_id)
            if not row:
                continue
            targets.append(row)
    else:
        needle = normalize_text(target_dashboard_name_contains or "")
        for row in dashboard_map.values():
            name = row.get("Name", "")
            if needle and needle not in normalize_text(name):
                continue
            targets.append(row)

    if exclude_dashboard_id:
        targets = [row for row in targets if row.get("DashboardId") != exclude_dashboard_id]

    targets.sort(key=lambda row: normalize_text(row.get("Name", "")))
    if limit is not None:
        targets = targets[:limit]

    return targets


def describe_dashboard_definition(qs_client, dashboard_id: str) -> Dict[str, Any]:
    response = qs_client.describe_dashboard_definition(
        AwsAccountId=QS_ACCOUNT_ID,
        DashboardId=dashboard_id,
    )
    return response.get("Definition", {})


def compare_visual_collections(
    reference_visuals: List[Dict[str, Any]],
    target_visuals: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return compare_rows_by_match_key(reference_visuals, target_visuals)


def compare_rows_by_match_key(
    reference_rows: List[Dict[str, Any]],
    target_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    reference_counter = Counter(item["match_key"] for item in reference_rows)
    target_counter = Counter(item["match_key"] for item in target_rows)

    reference_index: Dict[str, List[Dict[str, Any]]] = {}
    for item in reference_rows:
        reference_index.setdefault(item["match_key"], []).append(item)

    target_index: Dict[str, List[Dict[str, Any]]] = {}
    for item in target_rows:
        target_index.setdefault(item["match_key"], []).append(item)

    only_in_reference_counter = reference_counter - target_counter
    only_in_target_counter = target_counter - reference_counter
    in_both_counter = reference_counter & target_counter

    only_in_reference: List[Dict[str, Any]] = []
    for key, count in only_in_reference_counter.items():
        only_in_reference.extend(reference_index.get(key, [])[:count])

    only_in_target: List[Dict[str, Any]] = []
    for key, count in only_in_target_counter.items():
        only_in_target.extend(target_index.get(key, [])[:count])

    in_both_total = sum(in_both_counter.values())
    reference_total = sum(reference_counter.values())
    target_total = sum(target_counter.values())

    coverage_pct = 100.0
    if reference_total > 0:
        coverage_pct = (in_both_total / reference_total) * 100.0

    return {
        "reference_total": reference_total,
        "target_total": target_total,
        "in_both_total": in_both_total,
        "only_in_reference_total": sum(only_in_reference_counter.values()),
        "only_in_target_total": sum(only_in_target_counter.values()),
        "coverage_pct": round(coverage_pct, 2),
        "only_in_reference": only_in_reference,
        "only_in_target": only_in_target,
    }


def compare_calculated_fields(
    reference_rows: List[Dict[str, Any]],
    target_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return compare_rows_by_match_key(reference_rows, target_rows)


def sort_results_by_coverage_desc(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sort_results(
        results,
        primary_sort="visual-coverage",
        secondary_sort="target-name",
    )


def sort_results(
    results: List[Dict[str, Any]],
    primary_sort: str,
    secondary_sort: str,
) -> List[Dict[str, Any]]:
    def metric_value(item: Dict[str, Any], metric: str) -> float:
        diff = item.get("diff", {}) or {}
        cf_diff = item.get("calculated_field_diff", {}) or {}

        if metric == "visual-coverage":
            return float(diff.get("coverage_pct", 0.0))
        if metric == "calculated-field-coverage":
            if not isinstance(cf_diff, dict) or not cf_diff:
                return -1.0
            return float(cf_diff.get("coverage_pct", 0.0))
        if metric == "visual-only-in-target":
            return float(diff.get("only_in_target_total", 0.0))
        if metric == "calculated-field-only-in-target":
            if not isinstance(cf_diff, dict) or not cf_diff:
                return -1.0
            return float(cf_diff.get("only_in_target_total", 0.0))
        return 0.0

    return sorted(
        results,
        key=lambda item: (
            1 if item.get("error") else 0,
            -metric_value(item, primary_sort),
            -metric_value(item, secondary_sort) if secondary_sort != "target-name" else 0,
            normalize_text(item.get("target_dashboard", {}).get("Name", "")),
        ),
    )


def resolve_secondary_sort(
    secondary_sort: str,
    compare_calculated_fields: bool,
) -> str:
    if secondary_sort != "auto":
        return secondary_sort
    if compare_calculated_fields:
        return "calculated-field-coverage"
    return "target-name"


def validate_sort_configuration(
    primary_sort: str,
    secondary_sort: str,
    compare_calculated_fields: bool,
) -> None:
    cf_metrics = {"calculated-field-coverage", "calculated-field-only-in-target"}
    if primary_sort in cf_metrics and not compare_calculated_fields:
        raise SystemExit(
            f"--primary-sort {primary_sort} requires --compare-calculated-fields."
        )
    if secondary_sort in cf_metrics and not compare_calculated_fields:
        raise SystemExit(
            f"--secondary-sort {secondary_sort} requires --compare-calculated-fields."
        )


def log_visual_rows(logger: Logger, title: str, rows: List[Dict[str, Any]], max_rows: int) -> None:
    logger.log(title)
    if not rows:
        logger.log("  (none)")
        return

    for index, row in enumerate(rows[:max_rows], start=1):
        logger.log(
            "  "
            f"[{index}] sheet='{row.get('sheet_name', '')}' "
            f"type='{row.get('visual_type', '')}' "
            f"title='{row.get('title', '') or '(untitled)'}' "
            f"visual_id='{row.get('visual_id', '')}'"
        )

    remaining = len(rows) - max_rows
    if remaining > 0:
        logger.log(f"  ... {remaining} more rows omitted")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare visuals between one reference dashboard and a set of target dashboards. "
            "Shows which visuals are shared and which exist only on one side."
        )
    )

    reference_group = parser.add_mutually_exclusive_group(required=True)
    reference_group.add_argument("--reference-dashboard-id", help="Reference dashboard id.")
    reference_group.add_argument("--reference-dashboard-name", help="Reference dashboard name (must be unique).")

    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--target-dashboard-ids", nargs="+", help="Specific target dashboard ids.")
    target_group.add_argument(
        "--target-dashboard-name-contains",
        help="Compare against dashboards whose name contains this substring (case-insensitive).",
    )

    parser.add_argument(
        "--match-key",
        choices=["title-type", "title-only", "sheet-title-type", "visual-id"],
        default="title-type",
        help=(
            "How visuals are matched across dashboards. Default is title-type. "
            "Use visual-id for strict copy checks."
        ),
    )
    parser.add_argument(
        "--include-untitled",
        action="store_true",
        help="Include untitled visuals in matching by falling back to synthetic keys.",
    )
    parser.add_argument(
        "--compare-calculated-fields",
        action="store_true",
        help="Also compare dashboard calculated fields in addition to visuals.",
    )
    parser.add_argument(
        "--calculated-field-match",
        choices=["name-expression", "name-only", "expression-only"],
        default="name-expression",
        help=(
            "How calculated fields are matched across dashboards when --compare-calculated-fields is enabled. "
            "Default is name-expression."
        ),
    )
    parser.add_argument(
        "--primary-sort",
        choices=[
            "visual-coverage",
            "calculated-field-coverage",
            "visual-only-in-target",
            "calculated-field-only-in-target",
        ],
        default="visual-coverage",
        help=(
            "Primary descending sort metric for target dashboards. "
            "Default is visual-coverage."
        ),
    )
    parser.add_argument(
        "--secondary-sort",
        choices=[
            "auto",
            "visual-coverage",
            "calculated-field-coverage",
            "visual-only-in-target",
            "calculated-field-only-in-target",
            "target-name",
        ],
        default="auto",
        help=(
            "Secondary tie-breaker sort metric. auto uses calculated-field-coverage when "
            "--compare-calculated-fields is enabled, otherwise target-name."
        ),
    )
    parser.add_argument("--limit", type=int, help="Optional limit for number of target dashboards.")
    parser.add_argument(
        "--max-list-items",
        type=int,
        default=20,
        help="How many visual rows to print per diff section in the text report.",
    )

    return parser


def main() -> None:
    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    args = build_parser().parse_args()

    qs_client = create_quicksight_client()
    dashboard_map = get_dashboard_summary_map(qs_client)

    reference = resolve_reference_dashboard(
        dashboard_map,
        reference_dashboard_id=args.reference_dashboard_id,
        reference_dashboard_name=args.reference_dashboard_name,
    )

    targets = collect_target_dashboards(
        dashboard_map,
        target_dashboard_ids=args.target_dashboard_ids,
        target_dashboard_name_contains=args.target_dashboard_name_contains,
        limit=args.limit,
        exclude_dashboard_id=reference.get("DashboardId"),
    )

    if not targets:
        raise SystemExit("No target dashboards found with the given filters.")

    resolved_secondary_sort = resolve_secondary_sort(
        secondary_sort=args.secondary_sort,
        compare_calculated_fields=bool(args.compare_calculated_fields),
    )
    validate_sort_configuration(
        primary_sort=args.primary_sort,
        secondary_sort=resolved_secondary_sort,
        compare_calculated_fields=bool(args.compare_calculated_fields),
    )

    txt_path = build_log_path("dashboard_visual_diff", "txt")
    json_path = build_log_path("dashboard_visual_diff", "json")
    csv_path = build_log_path("dashboard_visual_diff_summary", "csv")
    logger = Logger(txt_path, "QUICKSIGHT DASHBOARD VISUAL COMPARISON")

    logger.log(f"Connected to QuickSight (Account: {QS_ACCOUNT_ID}, Region: {QS_REGION})")
    logger.log(f"Command: {' '.join(sys.argv)}")
    logger.log(f"Reference dashboard: {reference.get('Name', '')} ({reference.get('DashboardId', '')})")
    logger.log(f"Target dashboards: {len(targets)}")
    logger.log(f"Match key: {args.match_key}")
    logger.log(f"Include untitled visuals: {bool(args.include_untitled)}")
    logger.log(f"Compare calculated fields: {bool(args.compare_calculated_fields)}")
    if args.compare_calculated_fields:
        logger.log(f"Calculated field match: {args.calculated_field_match}")
    logger.log(f"Primary sort: {args.primary_sort}")
    logger.log(f"Secondary sort: {resolved_secondary_sort}")
    logger.log("")

    reference_definition = describe_dashboard_definition(qs_client, reference["DashboardId"])
    reference_visuals = extract_dashboard_visuals(
        reference_definition,
        match_key=args.match_key,
        include_untitled=args.include_untitled,
    )
    reference_visuals_total_unfiltered = count_dashboard_visuals(reference_definition)

    logger.log(f"Reference visual count (total in dashboard): {reference_visuals_total_unfiltered}")
    logger.log(f"Reference visual count (after match-key filtering): {len(reference_visuals)}")
    if reference_visuals_total_unfiltered > 0 and len(reference_visuals) == 0 and not args.include_untitled:
        logger.log(
            "WARNING: 0 visuals matched after filtering. This usually means titles are missing/empty "
            "for many visuals with the chosen --match-key. Try --include-untitled or --match-key visual-id."
        )
    reference_calculated_fields: List[Dict[str, Any]] = []
    if args.compare_calculated_fields:
        reference_calculated_fields = extract_dashboard_calculated_fields(
            reference_definition,
            calculated_field_match=args.calculated_field_match,
        )
        logger.log(
            "Reference calculated field count "
            f"(after match-key filtering): {len(reference_calculated_fields)}"
        )
    logger.log("")

    results: List[Dict[str, Any]] = []
    for index, target in enumerate(targets, start=1):
        dashboard_id = target["DashboardId"]
        logger.log(f"[{index}/{len(targets)}] Comparing target: {target.get('Name', '')} ({dashboard_id})")

        try:
            target_definition = describe_dashboard_definition(qs_client, dashboard_id)
            target_visuals = extract_dashboard_visuals(
                target_definition,
                match_key=args.match_key,
                include_untitled=args.include_untitled,
            )
        except Exception as exc:
            logger.log(f"  SKIPPED: {exc}")
            results.append(
                {
                    "target_dashboard": target,
                    "error": str(exc),
                }
            )
            logger.log("")
            continue

        diff = compare_visual_collections(reference_visuals, target_visuals)
        calculated_field_diff: Optional[Dict[str, Any]] = None
        if args.compare_calculated_fields:
            target_calculated_fields = extract_dashboard_calculated_fields(
                target_definition,
                calculated_field_match=args.calculated_field_match,
            )
            calculated_field_diff = compare_calculated_fields(
                reference_calculated_fields,
                target_calculated_fields,
            )

        logger.log(f"  visual_coverage={diff['coverage_pct']}%")
        if calculated_field_diff is not None:
            logger.log(f"  calculated_field_coverage={calculated_field_diff['coverage_pct']}%")
        logger.log("")

        result_row: Dict[str, Any] = {
            "target_dashboard": target,
            "diff": diff,
        }
        if calculated_field_diff is not None:
            result_row["calculated_field_diff"] = calculated_field_diff
        results.append(result_row)

    results = sort_results(
        results,
        primary_sort=args.primary_sort,
        secondary_sort=resolved_secondary_sort,
    )

    logger.log(
        "Sorted comparison results in descending order by "
        f"{args.primary_sort}, then {resolved_secondary_sort}."
    )
    logger.log("")
    for index, item in enumerate(results, start=1):
        target = item.get("target_dashboard", {})
        error = item.get("error")
        if error:
            logger.log(f"[{index}/{len(results)}] Target: {target.get('Name', '')} ({target.get('DashboardId', '')})")
            logger.log(f"  SKIPPED: {error}")
            logger.log("")
            continue

        diff = item.get("diff", {})
        logger.log(f"[{index}/{len(results)}] Target: {target.get('Name', '')} ({target.get('DashboardId', '')})")
        logger.log(
            "  "
            f"reference_total={diff['reference_total']} "
            f"target_total={diff['target_total']} "
            f"in_both={diff['in_both_total']} "
            f"only_in_reference={diff['only_in_reference_total']} "
            f"only_in_target={diff['only_in_target_total']} "
            f"visual_coverage={diff['coverage_pct']}%"
        )
        log_visual_rows(
            logger,
            "  Missing from target (present in reference):",
            diff["only_in_reference"],
            max_rows=args.max_list_items,
        )
        log_visual_rows(
            logger,
            "  Extra in target (not in reference):",
            diff["only_in_target"],
            max_rows=args.max_list_items,
        )

        calculated_field_diff = item.get("calculated_field_diff")
        if isinstance(calculated_field_diff, dict):
            logger.log(
                "  "
                f"calculated_fields_reference_total={calculated_field_diff['reference_total']} "
                f"calculated_fields_target_total={calculated_field_diff['target_total']} "
                f"calculated_fields_in_both={calculated_field_diff['in_both_total']} "
                f"calculated_fields_only_in_reference={calculated_field_diff['only_in_reference_total']} "
                f"calculated_fields_only_in_target={calculated_field_diff['only_in_target_total']} "
                f"calculated_field_coverage={calculated_field_diff['coverage_pct']}%"
            )
            log_visual_rows(
                logger,
                "  Calculated fields missing from target (present in reference):",
                calculated_field_diff["only_in_reference"],
                max_rows=args.max_list_items,
            )
            log_visual_rows(
                logger,
                "  Calculated fields extra in target (not in reference):",
                calculated_field_diff["only_in_target"],
                max_rows=args.max_list_items,
            )
        logger.log("")

    payload = {
        "generated_by": "qs_compare_dashboard_visuals.py",
        "account_id": QS_ACCOUNT_ID,
        "region": QS_REGION,
        "match_key": args.match_key,
        "include_untitled": bool(args.include_untitled),
        "compare_calculated_fields": bool(args.compare_calculated_fields),
        "calculated_field_match": args.calculated_field_match,
        "primary_sort": args.primary_sort,
        "secondary_sort": resolved_secondary_sort,
        "reference_dashboard": reference,
        "reference_visual_count": len(reference_visuals),
        "reference_calculated_field_count": len(reference_calculated_fields),
        "targets": results,
    }

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)

    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "reference_dashboard_id",
                "reference_dashboard_name",
                "target_dashboard_id",
                "target_dashboard_name",
                "reference_total",
                "target_total",
                "in_both_total",
                "only_in_reference_total",
                "only_in_target_total",
                "coverage_pct",
                "cf_reference_total",
                "cf_target_total",
                "cf_in_both_total",
                "cf_only_in_reference_total",
                "cf_only_in_target_total",
                "cf_coverage_pct",
                "error",
            ],
        )
        writer.writeheader()

        for item in results:
            target = item.get("target_dashboard", {})
            diff = item.get("diff", {})
            cf_diff = item.get("calculated_field_diff", {})
            writer.writerow(
                {
                    "reference_dashboard_id": reference.get("DashboardId", ""),
                    "reference_dashboard_name": reference.get("Name", ""),
                    "target_dashboard_id": target.get("DashboardId", ""),
                    "target_dashboard_name": target.get("Name", ""),
                    "reference_total": diff.get("reference_total", ""),
                    "target_total": diff.get("target_total", ""),
                    "in_both_total": diff.get("in_both_total", ""),
                    "only_in_reference_total": diff.get("only_in_reference_total", ""),
                    "only_in_target_total": diff.get("only_in_target_total", ""),
                    "coverage_pct": diff.get("coverage_pct", ""),
                    "cf_reference_total": cf_diff.get("reference_total", ""),
                    "cf_target_total": cf_diff.get("target_total", ""),
                    "cf_in_both_total": cf_diff.get("in_both_total", ""),
                    "cf_only_in_reference_total": cf_diff.get("only_in_reference_total", ""),
                    "cf_only_in_target_total": cf_diff.get("only_in_target_total", ""),
                    "cf_coverage_pct": cf_diff.get("coverage_pct", ""),
                    "error": item.get("error", ""),
                }
            )

    logger.log(f"JSON report: {json_path}")
    logger.log(f"CSV summary: {csv_path}")
    logger.log("")
    logger.log(f"DONE. Output saved to {txt_path}")


if __name__ == "__main__":
    main()
