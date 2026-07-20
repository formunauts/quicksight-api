import argparse
import json
from typing import Any, Dict, List, Optional, Set, Tuple

from boto3.session import Session
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

from qs_common import (
    QS_ACCOUNT_ID,
    QS_REGION,
    Logger,
    build_log_path,
    create_quicksight_client,
    get_all_summaries,
    require_env,
)


AUTH_ERROR_CODES = {
    "UnrecognizedClientException",
    "InvalidClientTokenId",
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidSignatureException",
}


def is_auth_error(exc: Exception) -> bool:
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code", "")
    return code in AUTH_ERROR_CODES


def is_credentials_error(exc: Exception) -> bool:
    return isinstance(exc, (NoCredentialsError, PartialCredentialsError))


def get_target_regions(all_regions: bool) -> List[str]:
    if not all_regions:
        return [QS_REGION]

    session = Session()
    qs_supported = set(session.get_available_regions("quicksight"))
    if not qs_supported:
        return [QS_REGION]

    try:
        ec2 = session.client("ec2", region_name="us-east-1")
        response = ec2.describe_regions(AllRegions=True)
        active = {
            row.get("RegionName")
            for row in response.get("Regions", [])
            if row.get("OptInStatus") in {"opt-in-not-required", "opted-in"}
        }
        selected = sorted(region for region in qs_supported if region in active)
        selected = selected or sorted(qs_supported)
        if QS_REGION in selected:
            selected = [QS_REGION] + [region for region in selected if region != QS_REGION]
        return selected
    except Exception:
        selected = sorted(qs_supported)
        if QS_REGION in selected:
            selected = [QS_REGION] + [region for region in selected if region != QS_REGION]
        return selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan QuickSight datasets and inspect recent ingestions plus refresh schedules, "
            "matching against provided mystery IDs."
        )
    )
    parser.add_argument(
        "--mystery-ids",
        nargs="+",
        required=True,
        help="One or more ingestion or schedule IDs to look for.",
    )
    parser.add_argument(
        "--dataset-name-contains",
        help="Optional case-insensitive dataset name filter before scanning.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional limit on datasets to inspect after filtering.",
    )
    parser.add_argument(
        "--max-ingestions-per-dataset",
        type=int,
        default=25,
        help="How many most-recent ingestions to inspect per dataset (default: 25).",
    )
    parser.add_argument(
        "--all-regions",
        action="store_true",
        help="Run the scan in every account-active AWS region that supports QuickSight.",
    )
    return parser.parse_args()


def list_recent_ingestions(qs_client, dataset_id: str, max_items: int) -> List[Dict[str, Any]]:
    paginator = qs_client.get_paginator("list_ingestions")
    ingestions: List[Dict[str, Any]] = []

    for page in paginator.paginate(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
        PaginationConfig={"PageSize": 100},
    ):
        ingestions.extend(page.get("Ingestions", []))
        if len(ingestions) >= max_items:
            break

    ingestions.sort(
        key=lambda item: item.get("CreatedTime") or item.get("UpdatedTime"),
        reverse=True,
    )
    return ingestions[:max_items]


def list_refresh_schedules(qs_client, dataset_id: str) -> List[Dict[str, Any]]:
    response = qs_client.list_refresh_schedules(
        AwsAccountId=QS_ACCOUNT_ID,
        DataSetId=dataset_id,
    )
    return response.get("RefreshSchedules", [])


def iso_or_none(value: Any) -> Optional[str]:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def is_not_found_error(exc: Exception) -> bool:
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code", "")
    return code in {"ResourceNotFoundException", "UnsupportedUserEditionException"}


def scan_dataset(
    qs_client,
    dataset: Dict[str, Any],
    mystery_ids: Set[str],
    max_ingestions: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, str]]]:
    dataset_id = dataset.get("DataSetId", "")
    dataset_name = dataset.get("Name", "")

    ingestion_matches: List[Dict[str, Any]] = []
    schedule_matches: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    try:
        ingestions = list_recent_ingestions(qs_client, dataset_id, max_ingestions)
        for ingestion in ingestions:
            ingestion_id = ingestion.get("IngestionId", "")
            if ingestion_id not in mystery_ids:
                continue

            ingestion_matches.append(
                {
                    "dataset_id": dataset_id,
                    "dataset_name": dataset_name,
                    "kind": "ingestion",
                    "match_id": ingestion_id,
                    "status": ingestion.get("IngestionStatus"),
                    "created_time": iso_or_none(ingestion.get("CreatedTime")),
                    "updated_time": iso_or_none(ingestion.get("UpdatedTime")),
                }
            )
    except Exception as exc:
        errors.append(
            {
                "dataset_id": dataset_id,
                "dataset_name": dataset_name,
                "stage": "list_ingestions",
                "error": str(exc),
            }
        )

    try:
        schedules = list_refresh_schedules(qs_client, dataset_id)
        for schedule in schedules:
            schedule_id = schedule.get("ScheduleId", "")
            if schedule_id not in mystery_ids:
                continue

            schedule_matches.append(
                {
                    "dataset_id": dataset_id,
                    "dataset_name": dataset_name,
                    "kind": "refresh_schedule",
                    "match_id": schedule_id,
                    "refresh_type": schedule.get("RefreshType"),
                    "schedule": schedule.get("ScheduleFrequency"),
                    "start_after": iso_or_none(schedule.get("StartAfterDateTime")),
                    "arn": schedule.get("Arn"),
                }
            )
    except Exception as exc:
        if not is_not_found_error(exc):
            errors.append(
                {
                    "dataset_id": dataset_id,
                    "dataset_name": dataset_name,
                    "stage": "list_refresh_schedules",
                    "error": str(exc),
                }
            )

    return ingestion_matches, schedule_matches, errors


def main() -> None:
    args = parse_args()

    require_env("QS_AWS_ACCOUNT_ID", QS_ACCOUNT_ID)
    require_env("QS_AWS_REGION", QS_REGION)

    text_log_path = build_log_path("dataset_refresh_audit", "txt")
    json_log_path = build_log_path("dataset_refresh_audit", "json")
    logger = Logger(text_log_path, "QUICKSIGHT DATASET REFRESH AUDIT")

    try:
        target_regions = get_target_regions(args.all_regions)

        mystery_ids = set(args.mystery_ids)

        logger.log(f"Connected to QuickSight account: {QS_ACCOUNT_ID}")
        logger.log(f"Regions selected: {target_regions}")
        logger.log(f"Mystery IDs provided: {len(mystery_ids)}")
        logger.log(f"Max ingestions per dataset: {args.max_ingestions_per_dataset}")

        all_ingestion_matches: List[Dict[str, Any]] = []
        all_schedule_matches: List[Dict[str, Any]] = []
        all_errors: List[Dict[str, str]] = []
        total_datasets_scanned = 0
        region_statuses: List[Dict[str, str]] = []

        for region in target_regions:
            logger.log("")
            logger.log("=" * 80)
            logger.log(f"REGION: {region}")
            logger.log("=" * 80)

            try:
                qs_client = create_quicksight_client(region=region)
                datasets = get_all_summaries(qs_client.list_data_sets, QS_ACCOUNT_ID, "DataSetSummaries")
                if args.dataset_name_contains:
                    needle = args.dataset_name_contains.lower()
                    datasets = [dataset for dataset in datasets if needle in dataset.get("Name", "").lower()]
                if args.limit:
                    datasets = datasets[: args.limit]

                logger.log(f"Datasets selected for scan: {len(datasets)}")
                total_datasets_scanned += len(datasets)

                for index, dataset in enumerate(datasets, start=1):
                    if index == 1 or index % 25 == 0:
                        logger.log(f"Progress: inspected {index}/{len(datasets)} datasets")

                    ingestion_matches, schedule_matches, errors = scan_dataset(
                        qs_client,
                        dataset,
                        mystery_ids,
                        args.max_ingestions_per_dataset,
                    )

                    for match in ingestion_matches:
                        match["region"] = region
                        all_ingestion_matches.append(match)

                    for match in schedule_matches:
                        match["region"] = region
                        all_schedule_matches.append(match)

                    for error in errors:
                        error["region"] = region
                        all_errors.append(error)

                    if errors:
                        first_error = errors[0]
                        raise RuntimeError(
                            "Dataset scan failed at "
                            f"{first_error.get('stage')} for "
                            f"{first_error.get('dataset_name')} "
                            f"({first_error.get('dataset_id')}): "
                            f"{first_error.get('error')}"
                        )

                    for match in ingestion_matches:
                        logger.log("")
                        logger.log(f"INGESTION MATCH: {match['match_id']}")
                        logger.log(f"  Region: {region}")
                        logger.log(f"  Dataset: {match['dataset_name']} ({match['dataset_id']})")
                        logger.log(f"  Status: {match.get('status')}")
                        logger.log(f"  Created: {match.get('created_time')}")

                    for match in schedule_matches:
                        logger.log("")
                        logger.log(f"SCHEDULE MATCH: {match['match_id']}")
                        logger.log(f"  Region: {region}")
                        logger.log(f"  Dataset: {match['dataset_name']} ({match['dataset_id']})")
                        logger.log(f"  RefreshType: {match.get('refresh_type')}")
                        logger.log(f"  ScheduleFrequency: {match.get('schedule')}")

                region_statuses.append({"region": region, "status": "ok"})
            except Exception as region_exc:
                error_text = str(region_exc)
                all_errors.append(
                    {
                        "region": region,
                        "stage": "region_scan",
                        "dataset_id": "",
                        "dataset_name": "",
                        "error": error_text,
                    }
                )
                region_statuses.append({"region": region, "status": "error", "error": error_text})
                logger.log(f"REGION ERROR: {region_exc}")
                if is_credentials_error(region_exc):
                    raise RuntimeError(
                        "AWS credentials are missing or incomplete. "
                        "Refresh your AWS credentials and retry."
                    ) from region_exc
                if is_auth_error(region_exc):
                    if args.all_regions:
                        logger.log(f"Skipping region due to auth token rejection: {region}")
                        continue
                    raise RuntimeError(
                        "Authentication token was rejected while scanning regions. "
                        "Refresh your AWS credentials and retry."
                    ) from region_exc
                if not args.all_regions:
                    raise

        payload = {
            "account_id": QS_ACCOUNT_ID,
            "regions_selected": target_regions,
            "mystery_ids": sorted(mystery_ids),
            "datasets_scanned": total_datasets_scanned,
            "ingestion_matches": all_ingestion_matches,
            "schedule_matches": all_schedule_matches,
            "errors": all_errors,
            "region_statuses": region_statuses,
            "summary": {
                "ingestion_matches": len(all_ingestion_matches),
                "schedule_matches": len(all_schedule_matches),
                "errors": len(all_errors),
            },
        }

        with open(json_log_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)

        logger.log("")
        logger.log("SUMMARY")
        logger.log(f"Ingestion matches: {len(all_ingestion_matches)}")
        logger.log(f"Schedule matches: {len(all_schedule_matches)}")
        logger.log(f"Errors: {len(all_errors)}")
        logger.log(f"Text report: {text_log_path}")
        logger.log(f"JSON report: {json_log_path}")
    except Exception as exc:
        logger.log(f"FATAL ERROR: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()