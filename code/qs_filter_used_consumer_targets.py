import argparse
import datetime
import json
import os
from typing import Any, Dict, List


TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build_default_output_path(input_path: str) -> str:
    root, ext = os.path.splitext(input_path)
    return f"{root}_used_only{ext}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Filter a dataset consumer audit down to datasets that are used anywhere."
    )
    parser.add_argument("--consumer-audit-file", required=True, help="JSON output from qs_audit_dataset_consumers.py.")
    parser.add_argument("--output", help="Optional output JSON path.")
    args = parser.parse_args()

    payload = load_json(args.consumer_audit_file)
    rows: List[Dict[str, Any]] = payload.get("datasets", [])
    used_rows = [row for row in rows if row.get("is_used_anywhere")]
    unused_rows = [row for row in rows if not row.get("is_used_anywhere")]

    output = {
        "generated_at": datetime.datetime.now().isoformat(),
        "source_consumer_audit_file": os.path.abspath(args.consumer_audit_file),
        "summary": {
            "input_datasets": len(rows),
            "used_datasets": len(used_rows),
            "unused_datasets": len(unused_rows),
        },
        "datasets": used_rows,
        "unused_datasets": unused_rows,
    }

    output_path = args.output or build_default_output_path(args.consumer_audit_file)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)

    print(f"Used-only target plan: {output_path}")
    print(f"Used datasets: {len(used_rows)}")
    print(f"Unused datasets: {len(unused_rows)}")


if __name__ == "__main__":
    main()
