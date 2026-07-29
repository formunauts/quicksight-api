import argparse
import csv
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
)


def normalize(value: str) -> str:
    return value.strip().lower()


def list_all_users(qs_client, account_id: str, namespace: str) -> List[Dict[str, Any]]:
    users: List[Dict[str, Any]] = []
    next_token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {
            "AwsAccountId": account_id,
            "Namespace": namespace,
        }
        if next_token:
            kwargs["NextToken"] = next_token
        response = qs_client.list_users(**kwargs)
        users.extend(response.get("UserList", []))
        next_token = response.get("NextToken")
        if not next_token:
            return users


def user_display(user: Dict[str, Any]) -> str:
    email = str(user.get("Email") or "").strip()
    user_name = str(user.get("UserName") or "").strip()
    return email if email else user_name


def matches_any_pattern(user: Dict[str, Any], patterns: List[str]) -> bool:
    email = normalize(str(user.get("Email") or ""))
    user_name = normalize(str(user.get("UserName") or ""))
    return any(pattern in email or pattern in user_name for pattern in patterns)


def apply_user_filters(
    users: List[Dict[str, Any]],
    user_email_contains: Optional[str],
    user_contains: Optional[List[str]],
    exclude_user_contains: Optional[List[str]],
    max_users: Optional[int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    include_patterns: List[str] = []
    if user_email_contains:
        include_patterns.append(normalize(user_email_contains))
    if user_contains:
        include_patterns.extend(normalize(item) for item in user_contains if str(item).strip())

    exclude_patterns: List[str] = []
    if exclude_user_contains:
        exclude_patterns.extend(normalize(item) for item in exclude_user_contains if str(item).strip())

    kept_after_include: List[Dict[str, Any]] = []
    excluded_rows: List[Dict[str, Any]] = []

    for user in users:
        display = user_display(user)
        if not display:
            continue

        if include_patterns and not matches_any_pattern(user, include_patterns):
            excluded_rows.append(
                {
                    "category": "excluded_by_include_filter",
                    "username_email": display,
                    "user_name": str(user.get("UserName") or ""),
                    "email": str(user.get("Email") or ""),
                    "arn": str(user.get("Arn") or ""),
                    "identity_type": str(user.get("IdentityType") or ""),
                    "role": str(user.get("Role") or ""),
                    "active": user.get("Active"),
                }
            )
            continue

        kept_after_include.append(user)

    kept_after_exclude: List[Dict[str, Any]] = []
    for user in kept_after_include:
        display = user_display(user)
        if exclude_patterns and matches_any_pattern(user, exclude_patterns):
            excluded_rows.append(
                {
                    "category": "excluded_by_exclude_filter",
                    "username_email": display,
                    "user_name": str(user.get("UserName") or ""),
                    "email": str(user.get("Email") or ""),
                    "arn": str(user.get("Arn") or ""),
                    "identity_type": str(user.get("IdentityType") or ""),
                    "role": str(user.get("Role") or ""),
                    "active": user.get("Active"),
                }
            )
            continue
        kept_after_exclude.append(user)

    if max_users is None:
        return kept_after_exclude, excluded_rows

    kept_final = kept_after_exclude[:max_users]
    for user in kept_after_exclude[max_users:]:
        display = user_display(user)
        excluded_rows.append(
            {
                "category": "excluded_by_max_users",
                "username_email": display,
                "user_name": str(user.get("UserName") or ""),
                "email": str(user.get("Email") or ""),
                "arn": str(user.get("Arn") or ""),
                "identity_type": str(user.get("IdentityType") or ""),
                "role": str(user.get("Role") or ""),
                "active": user.get("Active"),
            }
        )

    return kept_final, excluded_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify which QuickSight users are not present in a qs_user_dashboard_dataset_filter_report output, "
            "and list why they are excluded."
        )
    )
    parser.add_argument("--report-json", required=True, help="Path to user_dashboard_dataset_filter_report JSON.")
    parser.add_argument("--namespace", help="QuickSight namespace override. Defaults to report namespace or 'default'.")
    parser.add_argument("--user-email-contains", help="Override include filter by email substring.")
    parser.add_argument(
        "--user-contains",
        nargs="+",
        help="Override include filter by username/email substrings (any match keeps user).",
    )
    parser.add_argument(
        "--exclude-user-contains",
        nargs="+",
        help="Override exclusion filter by username/email substrings (any match removes user).",
    )
    parser.add_argument("--max-users", type=int, help="Override max users cap after filtering.")
    parser.add_argument(
        "--ignore-report-filters",
        action="store_true",
        help="Ignore filters saved in the report header unless explicitly provided via CLI.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    with open(args.report_json, "r", encoding="utf-8") as handle:
        report = json.load(handle)

    namespace = args.namespace or report.get("namespace") or "default"

    if args.ignore_report_filters:
        user_email_contains = args.user_email_contains
        user_contains = args.user_contains
        exclude_user_contains = args.exclude_user_contains
        max_users = args.max_users
    else:
        user_email_contains = args.user_email_contains if args.user_email_contains is not None else report.get("user_email_contains")
        user_contains = args.user_contains if args.user_contains is not None else report.get("user_contains")
        exclude_user_contains = (
            args.exclude_user_contains if args.exclude_user_contains is not None else report.get("exclude_user_contains")
        )
        max_users = args.max_users if args.max_users is not None else report.get("max_users")

    account_id = QS_ACCOUNT_ID or os.getenv("QS_AWS_ACCOUNT_ID") or report.get("account_id")
    if not account_id:
        raise SystemExit("Missing account id. Set QS_AWS_ACCOUNT_ID or provide report JSON containing account_id.")

    txt_path = build_log_path("verify_missing_report_users", "txt")
    json_path = build_log_path("verify_missing_report_users", "json")
    csv_path = build_log_path("verify_missing_report_users", "csv")
    logger = Logger(txt_path, "VERIFY REPORT MISSING USERS")

    qs_client = create_quicksight_client(region=QS_REGION)
    all_users = list_all_users(qs_client, account_id=account_id, namespace=namespace)
    scoped_users, excluded_rows = apply_user_filters(
        all_users,
        user_email_contains=user_email_contains,
        user_contains=user_contains,
        exclude_user_contains=exclude_user_contains,
        max_users=max_users,
    )

    report_rows = report.get("rows", []) if isinstance(report.get("rows"), list) else []
    users_in_rows = {
        normalize(str(row.get("username_email") or ""))
        for row in report_rows
        if normalize(str(row.get("username_email") or ""))
    }

    scoped_user_displays = {normalize(user_display(user)) for user in scoped_users if user_display(user)}

    missing_in_rows: List[Dict[str, Any]] = []
    for user in scoped_users:
        display = user_display(user)
        if not display:
            continue
        if normalize(display) in users_in_rows:
            continue
        missing_in_rows.append(
            {
                "category": "in_scope_missing_from_report_rows",
                "username_email": display,
                "user_name": str(user.get("UserName") or ""),
                "email": str(user.get("Email") or ""),
                "arn": str(user.get("Arn") or ""),
                "identity_type": str(user.get("IdentityType") or ""),
                "role": str(user.get("Role") or ""),
                "active": user.get("Active"),
                "likely_reason": "No effective dashboard access found by the report logic.",
            }
        )

    row_users_not_in_scope = sorted(
        [display for display in users_in_rows if display not in scoped_user_displays]
    )

    combined_rows = excluded_rows + missing_in_rows
    combined_rows.sort(key=lambda row: (str(row.get("category", "")), normalize(str(row.get("username_email", "")))))

    summary = {
        "report_json": args.report_json,
        "account_id": account_id,
        "region": QS_REGION,
        "namespace": namespace,
        "applied_filters": {
            "user_email_contains": user_email_contains,
            "user_contains": user_contains,
            "exclude_user_contains": exclude_user_contains,
            "max_users": max_users,
            "ignore_report_filters": bool(args.ignore_report_filters),
        },
        "counts": {
            "all_users_in_namespace": len(all_users),
            "scoped_users_after_filters": len(scoped_users),
            "unique_users_in_report_rows": len(users_in_rows),
            "excluded_users": len(excluded_rows),
            "in_scope_missing_from_report_rows": len(missing_in_rows),
            "row_users_not_in_scope": len(row_users_not_in_scope),
        },
        "excluded_users": excluded_rows,
        "in_scope_missing_from_report_rows": missing_in_rows,
        "row_users_not_in_scope": row_users_not_in_scope,
    }

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)

    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "category",
            "username_email",
            "user_name",
            "email",
            "arn",
            "identity_type",
            "role",
            "active",
            "likely_reason",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in combined_rows:
            writer.writerow(row)

    logger.log(f"Account: {account_id}")
    logger.log(f"Region: {QS_REGION}")
    logger.log(f"Namespace: {namespace}")
    logger.log("")
    logger.log(f"All users in namespace: {len(all_users)}")
    logger.log(f"Scoped users after filters: {len(scoped_users)}")
    logger.log(f"Unique users found in report rows: {len(users_in_rows)}")
    logger.log(f"Excluded users: {len(excluded_rows)}")
    logger.log(f"In-scope users missing from report rows: {len(missing_in_rows)}")
    logger.log(f"Report users not in current scoped set: {len(row_users_not_in_scope)}")
    logger.log("")
    logger.log(f"CSV output: {csv_path}")
    logger.log(f"JSON output: {json_path}")
    logger.log(f"Text log: {txt_path}")


if __name__ == "__main__":
    main()
