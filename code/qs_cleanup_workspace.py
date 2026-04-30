import argparse
import os
import shutil
import stat
from typing import List


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT_DIR, "logs")
CODE_DIR = os.path.join(ROOT_DIR, "code")

DEFAULT_LOG_KEEP = {"commands.txt"}


def collect_log_targets(keep_names: List[str]) -> List[str]:
    if not os.path.isdir(LOG_DIR):
        return []
    keep = set(DEFAULT_LOG_KEEP)
    keep.update(keep_names)
    targets: List[str] = []
    for entry in os.scandir(LOG_DIR):
        if entry.name in keep:
            continue
        targets.append(entry.path)
    return sorted(targets)


def collect_python_cache_targets() -> List[str]:
    targets: List[str] = []
    for root, dirs, files in os.walk(ROOT_DIR):
        for dirname in list(dirs):
            if dirname == "__pycache__":
                targets.append(os.path.join(root, dirname))
        for filename in files:
            if filename.endswith(".pyc"):
                targets.append(os.path.join(root, filename))
    return sorted(set(targets))


def remove_path(path: str) -> None:
    def _onerror(func, failed_path, exc_info):
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except Exception:
            raise exc_info[1]

    if os.path.isdir(path):
        shutil.rmtree(path, onerror=_onerror)
    elif os.path.exists(path):
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean generated QuickSight toolbox artifacts such as logs and Python caches."
    )
    parser.add_argument(
        "--keep-log",
        nargs="+",
        default=[],
        help="Additional file or directory names inside logs/ to keep.",
    )
    parser.add_argument(
        "--skip-logs",
        action="store_true",
        help="Do not remove anything from logs/.",
    )
    parser.add_argument(
        "--skip-python-cache",
        action="store_true",
        help="Do not remove __pycache__ directories or .pyc files.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the collected paths. Without this flag the script only previews.",
    )
    args = parser.parse_args()

    targets: List[str] = []
    if not args.skip_logs:
        targets.extend(collect_log_targets(args.keep_log))
    if not args.skip_python_cache:
        targets.extend(collect_python_cache_targets())

    unique_targets = []
    seen = set()
    for path in targets:
        if path in seen:
            continue
        seen.add(path)
        unique_targets.append(path)

    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Targets: {len(unique_targets)}")
    for path in unique_targets:
        print(path)

    if not args.apply:
        return

    failed: List[str] = []
    for path in unique_targets:
        try:
            remove_path(path)
        except Exception:
            failed.append(path)

    print("Cleanup completed.")
    if failed:
        print(f"Failed to remove: {len(failed)}")
        for path in failed:
            print(path)


if __name__ == "__main__":
    main()
