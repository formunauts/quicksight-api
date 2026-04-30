import argparse
import json
import os
import re
from typing import Any, Dict, List, Optional


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "dataset"


def build_default_output_path(plan_file: str, plan: Dict[str, Any]) -> str:
    base, _ = os.path.splitext(plan_file)
    placeholder_dataset_names = [
        dataset.get("name")
        for dataset in plan.get("datasets", [])
        if dataset.get("placeholder_changes")
    ]
    unique_names = [
        name for name in dict.fromkeys(placeholder_dataset_names) if isinstance(name, str) and name
    ]
    if len(unique_names) == 1:
        return f"{base}__{slugify(unique_names[0])}_placeholder_restore.txt"
    return f"{base}_placeholder_restore.txt"


def write_restore_file(plan: Dict[str, Any], output_path: str) -> Optional[str]:
    sections: List[str] = []

    for dataset in plan.get("datasets", []):
        placeholder_changes = dataset.get("placeholder_changes") or []
        if not placeholder_changes:
            continue

        sections.append(f"DATASET: {dataset.get('name')} ({dataset.get('data_set_id')})")
        sections.append("")
        for index, change in enumerate(placeholder_changes, start=1):
            sections.append(f"FIELD {index}: {change.get('column_name')}")
            sections.append(f"Output type: {change.get('output_type')}")
            sections.append(f"Expression path: {change.get('expression_path')}")
            sections.append("Original expression:")
            sections.append(change.get("original_expression", ""))
            sections.append("")
            sections.append("=" * 80)
            sections.append("")

    if not sections:
        return None

    with open(output_path, "w", encoding="utf-8-sig", newline="\n") as handle:
        handle.write("\n".join(sections).rstrip() + "\n")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export placeholder replacement formulas from a migration plan into a paste-friendly text file."
    )
    parser.add_argument("--plan-file", required=True, help="Path to the dataset migration plan JSON file.")
    parser.add_argument("--output", help="Optional output path for the restore text file.")
    args = parser.parse_args()

    with open(args.plan_file, "r", encoding="utf-8") as handle:
        plan = json.load(handle)

    output_path = args.output or build_default_output_path(args.plan_file, plan)
    written_path = write_restore_file(plan, output_path)

    if written_path:
        print(f"Restore file: {written_path}")
    else:
        print("No placeholder changes found in the plan file.")


if __name__ == "__main__":
    main()
