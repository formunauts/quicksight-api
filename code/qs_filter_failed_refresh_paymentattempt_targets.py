import argparse
import datetime
import json
import os
import re
from typing import Any, Dict, List, Optional


TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def build_default_output_path(audit_file: str, suffix: str) -> str:
    root, ext = os.path.splitext(audit_file)
    return f"{root}_{suffix}{ext}"


def extract_missing_column(error_message: str) -> Optional[str]:
    match = re.search(r'column "([^"]+)" does not exist', error_message or "")
    if match:
        return match.group(1)
    return None


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)


def filter_rows(
    datasets: List[Dict[str, Any]],
    missing_column: Optional[str],
    error_contains: Optional[str],
    exclude_names: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    matched: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    exclude_set = set(exclude_names)

    for row in datasets:
        name = row.get("name")
        if name in exclude_set:
            excluded.append(
                {
                    "name": name,
                    "data_set_id": row.get("data_set_id"),
                    "reason": "Excluded by exact dataset name filter.",
                }
            )
            continue

        latest = row.get("latest_ingestion") or {}
        message = latest.get("error_message") or ""
        row_missing_column = extract_missing_column(message)

        if missing_column and row_missing_column != missing_column:
            excluded.append(
                {
                    "name": name,
                    "data_set_id": row.get("data_set_id"),
                    "reason": f'Missing column was "{row_missing_column}" instead of "{missing_column}".',
                }
            )
            continue

        if error_contains and error_contains not in message:
            excluded.append(
                {
                    "name": name,
                    "data_set_id": row.get("data_set_id"),
                    "reason": f'Error message did not contain "{error_contains}".',
                }
            )
            continue

        matched.append(row)

    return {"matched": matched, "excluded": excluded}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Filter the failed-refresh paymentattempt audit down to the datasets you want to automate."
    )
    parser.add_argument("--audit-file", required=True, help="Audit JSON file from qs_find_failed_refresh_paymentattempt_datasets.py.")
    parser.add_argument(
        "--missing-column",
        help='Keep only datasets whose latest refresh failed with `column "<name>" does not exist`.',
    )
    parser.add_argument(
        "--error-contains",
        help="Keep only datasets whose latest error message contains this substring.",
    )
    parser.add_argument(
        "--exclude-names",
        nargs="+",
        default=[],
        help="Exact dataset names to exclude from the filtered target plan.",
    )
    parser.add_argument(
        "--output",
        help="Optional output JSON path. Defaults to a sibling file next to the audit JSON.",
    )
    args = parser.parse_args()

    audit = load_json(args.audit_file)
    datasets = audit.get("datasets", [])
    filtered = filter_rows(
        datasets,
        missing_column=args.missing_column,
        error_contains=args.error_contains,
        exclude_names=args.exclude_names,
    )

    output_path = args.output or build_default_output_path(args.audit_file, "filtered_targets")
    payload = {
        "generated_at": datetime.datetime.now().isoformat(),
        "source_audit_file": os.path.abspath(args.audit_file),
        "filters": {
            "missing_column": args.missing_column,
            "error_contains": args.error_contains,
            "exclude_names": args.exclude_names,
        },
        "summary": {
            "input_datasets": len(datasets),
            "matched_datasets": len(filtered["matched"]),
            "excluded_datasets": len(filtered["excluded"]),
        },
        "datasets": filtered["matched"],
        "excluded": filtered["excluded"],
    }
    write_json(output_path, payload)
    print(f"Filtered target plan: {output_path}")
    print(f"Matched datasets: {len(filtered['matched'])}")
    print(f"Excluded datasets: {len(filtered['excluded'])}")


if __name__ == "__main__":
    main()
