#!/usr/bin/env bash

set -u
set -o pipefail

INTERNAL_ROOT="${INTERNAL_ROOT:-/data/internal/DATASET}"
EXTERNAL_ROOT="${EXTERNAL_ROOT:-/data/external/DATASET}"
NAS_ROOT="${NAS_ROOT:-/mnt/nas/IL_Dataset/DATASET}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"

print_menu() {
    cat <<'EOF'
Select a sync task:

  1) Archived -> external Archived
  2) Full DATASET -> external DATASET
  3) DATASET -> external DATASET, excluding Archived
  4) Full DATASET -> NAS

EOF
}

print_command() {
    printf 'Command:'
    printf ' %q' "$@"
    printf '\n'
}

require_source() {
    local source_path="$1"

    if [[ ! -d "$source_path" ]]; then
        printf 'Error: source directory does not exist: %s\n' "$source_path" >&2
        exit 1
    fi
}

require_nas_mount() {
    local mount_point="/mnt/nas/IL_Dataset"

    if ! findmnt --mountpoint "$mount_point" >/dev/null 2>&1; then
        printf 'Error: NAS is not mounted at %s\n' "$mount_point" >&2
        exit 1
    fi

    local fs_type
    fs_type="$(findmnt --noheadings --output FSTYPE --mountpoint "$mount_point")"

    if [[ "$fs_type" != "cifs" ]]; then
        printf 'Error: %s is mounted as %s, not CIFS.\n' \
            "$mount_point" "$fs_type" >&2
        exit 1
    fi
}

prepare_h5_task_plan() {
    local internal_root="$1"
    local external_root="$2"
    local work_dir="$3"

    "$PYTHON_BIN" - "$internal_root" "$external_root" "$work_dir" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import h5py
except Exception as exc:  # pragma: no cover - depends on host environment.
    raise SystemExit(f"Error: failed to import h5py with {sys.executable}: {exc}")


TASK_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def decode_task_name(value: object) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str):
        raise RuntimeError("task_name must be a string")
    task_name = value.strip()
    if (
        not task_name
        or not TASK_NAME_PATTERN.fullmatch(task_name)
        or ".." in task_name
    ):
        raise RuntimeError(
            "task_name must start with an ASCII letter or digit, contain only "
            'ASCII letters, digits, ".", "_", or "-", and must not contain ".."'
        )
    return task_name


internal_root = Path(sys.argv[1])
external_root = Path(sys.argv[2])
work_dir = Path(sys.argv[3])

if not internal_root.is_dir():
    raise SystemExit(f"Error: source directory does not exist: {internal_root}")
if external_root.exists() and not external_root.is_dir():
    raise SystemExit(f"Error: destination exists but is not a directory: {external_root}")

commands_path = work_dir / "h5_commands.tsv"
missing_dirs_path = work_dir / "missing_task_dirs.txt"
summary_path = work_dir / "h5_summary.txt"
lists_dir = work_dir / "file-lists"
lists_dir.mkdir(parents=True, exist_ok=True)

groups: dict[str, dict[str, object]] = {}
errors: list[str] = []
h5_paths = sorted(path for path in internal_root.glob("*.h5") if path.is_file())

for h5_path in h5_paths:
    try:
        with h5py.File(h5_path, "r") as h5:
            if "task_name" not in h5.attrs:
                raise RuntimeError("missing root attr task_name")
            task_name = decode_task_name(h5.attrs["task_name"])
    except Exception as exc:
        errors.append(f"{h5_path}: {exc}")
        continue

    group = groups.setdefault(
        task_name,
        {
            "files": [],
            "h5_count": 0,
            "report_count": 0,
            "missing_report_count": 0,
        },
    )
    group["files"].append(h5_path.name)
    group["h5_count"] += 1

    report_path = h5_path.with_suffix(".build_report.json")
    if report_path.is_file():
        group["files"].append(report_path.name)
        group["report_count"] += 1
    else:
        group["missing_report_count"] += 1

if errors:
    print("Error: HDF5 task_name preflight failed:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    raise SystemExit(1)

missing_dirs: list[Path] = []
with commands_path.open("w", encoding="utf-8") as commands:
    for index, task_name in enumerate(sorted(groups)):
        group = groups[task_name]
        destination_dir = external_root / task_name
        if destination_dir.exists() and not destination_dir.is_dir():
            raise SystemExit(
                f"Error: task destination exists but is not a directory: {destination_dir}"
            )
        if not destination_dir.exists():
            missing_dirs.append(destination_dir)

        list_path = lists_dir / f"{index:04d}.files0"
        with list_path.open("wb") as file_list:
            for name in group["files"]:
                file_list.write(name.encode("utf-8"))
                file_list.write(b"\0")

        commands.write(
            "\t".join(
                (
                    task_name,
                    str(group["h5_count"]),
                    str(group["report_count"]),
                    str(group["missing_report_count"]),
                    list_path.as_posix(),
                    destination_dir.as_posix(),
                )
            )
            + "\n"
        )

missing_dirs_path.write_text(
    "".join(f"{path.as_posix()}\n" for path in missing_dirs),
    encoding="utf-8",
)

total_reports = sum(int(group["report_count"]) for group in groups.values())
missing_reports = sum(int(group["missing_report_count"]) for group in groups.values())
summary_lines = [
    "HDF5 task sync plan:",
    f"  top-level .h5 files: {len(h5_paths)}",
    f"  paired build reports: {total_reports}",
    f"  missing paired build reports: {missing_reports}",
    f"  task groups: {len(groups)}",
]
for task_name in sorted(groups):
    group = groups[task_name]
    destination_dir = external_root / task_name
    status = "exists" if destination_dir.is_dir() else "would create"
    summary_lines.append(
        "  - "
        f"{task_name}: h5={group['h5_count']}, "
        f"reports={group['report_count']}, "
        f"missing_reports={group['missing_report_count']}, "
        f"destination={destination_dir} ({status})"
    )

summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
PY
}

confirm_missing_task_dirs() {
    local missing_dirs_path="$1"

    if [[ ! -s "$missing_dirs_path" ]]; then
        return 0
    fi

    printf '\nMissing task destination directories:\n'
    while IFS= read -r destination_dir; do
        [[ -n "$destination_dir" ]] || continue
        printf '  %s\n' "$destination_dir"
    done < "$missing_dirs_path"

    local confirm
    read -r -p "Enter Y to create these task directories, or N to cancel: " confirm
    case "${confirm,,}" in
        y)
            while IFS= read -r destination_dir; do
                [[ -n "$destination_dir" ]] || continue
                mkdir -p "$destination_dir"
            done < "$missing_dirs_path"
            ;;
        *)
            printf 'Cancelled.\n'
            exit 0
            ;;
    esac
}

run_rsync_command() {
    local -a command=("$@")

    printf '\n'
    print_command "${command[@]}"
    printf '\n'

    "${command[@]}"
}

run_archived_sync() {
    local source_path="${INTERNAL_ROOT}/Archived/"
    local destination_path="${EXTERNAL_ROOT}/Archived/"

    if [[ ! -d "$source_path" ]]; then
        printf 'Archived source does not exist; skipping: %s\n' "$source_path"
        return 0
    fi

    if [[ "$dry_run" -eq 0 ]]; then
        mkdir -p "$destination_path"
    fi

    local -a exclude_args=(
        "--exclude=/.staging/"
        "--exclude=/batch_archive_report.json"
    )
    local -a command=(
        rsync
        "${rsync_args[@]}"
        "${exclude_args[@]}"
        "$source_path"
        "$destination_path"
    )

    run_rsync_command "${command[@]}"
}

run_recursive_sync() {
    local source_path="$1"
    local destination_path="$2"
    shift 2
    local -a exclude_args=("$@")

    if [[ "$dry_run" -eq 0 ]]; then
        mkdir -p "$destination_path"
    fi

    local -a command=(
        rsync
        "${rsync_args[@]}"
        "${exclude_args[@]}"
        "$source_path"
        "$destination_path"
    )

    run_rsync_command "${command[@]}"
}

run_h5_task_sync() {
    local commands_path="$1"

    if [[ ! -s "$commands_path" ]]; then
        printf '\nNo top-level .h5 files found under %s; skipping HDF5 task sync.\n' \
            "$INTERNAL_ROOT"
        return 0
    fi

    local task_name h5_count report_count missing_report_count list_path destination_dir
    while IFS=$'\t' read -r task_name h5_count report_count missing_report_count list_path destination_dir; do
        [[ -n "$task_name" ]] || continue
        if [[ "$dry_run" -eq 0 ]]; then
            mkdir -p "$destination_dir"
        fi

        local -a command=(
            rsync
            "${rsync_args[@]}"
            "--from0"
            "--files-from=$list_path"
            "${INTERNAL_ROOT}/"
            "${destination_dir}/"
        )

        printf '\nTask group: %s (h5=%s, reports=%s, missing_reports=%s)\n' \
            "$task_name" "$h5_count" "$report_count" "$missing_report_count"
        if [[ "$dry_run" -eq 1 && ! -d "$destination_dir" ]]; then
            printf '\n'
            print_command "${command[@]}"
            printf '\n'
            printf 'Dry-run: destination does not exist; would create: %s\n' \
                "$destination_dir"
            continue
        fi
        run_rsync_command "${command[@]}" || return $?
    done < "$commands_path"
}

sync_archived=0
sync_h5_tasks=0
sync_recursive=0
source_path=""
destination_path=""
recursive_exclude_args=()

print_menu
read -r -p "Enter 1, 2, 3, or 4: " task

case "$task" in
    1)
        description="Archived -> external Archived"
        sync_archived=1
        source_path="${INTERNAL_ROOT}/Archived/"
        destination_path="${EXTERNAL_ROOT}/Archived/"
        require_source "$source_path"
        ;;

    2)
        description="Full DATASET -> external DATASET"
        sync_archived=1
        sync_h5_tasks=1
        source_path="${INTERNAL_ROOT}/"
        destination_path="${EXTERNAL_ROOT}/"
        require_source "$source_path"
        ;;

    3)
        description="DATASET -> external DATASET, excluding Archived"
        sync_h5_tasks=1
        source_path="${INTERNAL_ROOT}/"
        destination_path="${EXTERNAL_ROOT}/"
        require_source "$source_path"
        ;;

    4)
        description="Full DATASET -> NAS"
        sync_recursive=1
        source_path="${INTERNAL_ROOT}/"
        destination_path="${NAS_ROOT}/"
        recursive_exclude_args=(
            "--exclude=/Archived/.staging/"
            "--exclude=/Archived/batch_archive_report.json"
            "--exclude=/batch_build_report.json"
            "--exclude=*.tmp"
        )
        require_source "$source_path"
        require_nas_mount
        ;;

    *)
        printf 'Invalid selection: %s\n' "$task" >&2
        exit 1
        ;;
esac

printf '\nSelected task: %s\n' "$description"
printf 'Source:        %s\n' "$source_path"
printf 'Destination:   %s\n\n' "$destination_path"

read -r -p "Enter Y to run, D for dry-run, or N to cancel: " action

rsync_args=(
    "-rtvh"
    "--partial"
    "--info=progress2"
)
dry_run=0

case "${action,,}" in
    y)
        ;;

    d)
        dry_run=1
        rsync_args+=(
            "--dry-run"
            "--itemize-changes"
        )
        ;;

    n)
        printf 'Cancelled.\n'
        exit 0
        ;;

    *)
        printf 'Invalid selection: %s\n' "$action" >&2
        exit 1
        ;;
esac

work_dir=""
if [[ "$sync_h5_tasks" -eq 1 ]]; then
    work_dir="$(mktemp -d "${TMPDIR:-/tmp}/rsync-helper.XXXXXX")"
    trap '[[ -z "${work_dir:-}" ]] || rm -rf "$work_dir"' EXIT
    prepare_h5_task_plan "$INTERNAL_ROOT" "$EXTERNAL_ROOT" "$work_dir" || exit $?

    printf '\n'
    sed -n '1,200p' "$work_dir/h5_summary.txt"

    if [[ "$dry_run" -eq 0 ]]; then
        confirm_missing_task_dirs "$work_dir/missing_task_dirs.txt"
    fi
fi

status=0

if [[ "$sync_archived" -eq 1 ]]; then
    run_archived_sync
    status=$?
    if [[ "$status" -ne 0 ]]; then
        printf '\nSync failed with exit code %d.\n' "$status" >&2
        exit "$status"
    fi
fi

if [[ "$sync_h5_tasks" -eq 1 ]]; then
    run_h5_task_sync "$work_dir/h5_commands.tsv"
    status=$?
    if [[ "$status" -ne 0 ]]; then
        printf '\nSync failed with exit code %d.\n' "$status" >&2
        exit "$status"
    fi
fi

if [[ "$sync_recursive" -eq 1 ]]; then
    run_recursive_sync "$source_path" "$destination_path" "${recursive_exclude_args[@]}"
    status=$?
    if [[ "$status" -ne 0 ]]; then
        printf '\nSync failed with exit code %d.\n' "$status" >&2
        exit "$status"
    fi
fi

printf '\nSync completed successfully.\n'
exit 0
