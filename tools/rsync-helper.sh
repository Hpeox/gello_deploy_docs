#!/usr/bin/env bash

set -u
set -o pipefail

INTERNAL_ROOT="/data/internal/DATASET"
EXTERNAL_ROOT="/data/external/DATASET"
NAS_ROOT="/mnt/nas/IL_Dataset/DATASET"

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

print_menu
read -r -p "Enter 1, 2, 3, or 4: " task

case "$task" in
    1)
        description="Archived -> external Archived"
        source_path="${INTERNAL_ROOT}/Archived/"
        destination_path="${EXTERNAL_ROOT}/Archived/"

        exclude_args=(
            "--exclude=/.staging/"
            "--exclude=/batch_archive_report.json"
        )
        ;;

    2)
        description="Full DATASET -> external DATASET"
        source_path="${INTERNAL_ROOT}/"
        destination_path="${EXTERNAL_ROOT}/"

        exclude_args=(
            "--exclude=/Archived/.staging/"
            "--exclude=/Archived/batch_archive_report.json"
            "--exclude=/batch_build_report.json"
            "--exclude=*.tmp"
        )
        ;;

    3)
        description="DATASET -> external DATASET, excluding Archived"
        source_path="${INTERNAL_ROOT}/"
        destination_path="${EXTERNAL_ROOT}/"

        exclude_args=(
            "--exclude=/Archived/"
            "--exclude=/batch_build_report.json"
            "--exclude=*.tmp"
        )
        ;;

    4)
        description="Full DATASET -> NAS"
        source_path="${INTERNAL_ROOT}/"
        destination_path="${NAS_ROOT}/"

        exclude_args=(
            "--exclude=/Archived/.staging/"
            "--exclude=/Archived/batch_archive_report.json"
            "--exclude=/batch_build_report.json"
            "--exclude=*.tmp"
        )

        require_nas_mount
        ;;

    *)
        printf 'Invalid selection: %s\n' "$task" >&2
        exit 1
        ;;
esac

require_source "$source_path"

printf '\nSelected task: %s\n' "$description"
printf 'Source:        %s\n' "$source_path"
printf 'Destination:   %s\n\n' "$destination_path"

read -r -p "Enter Y to run, D for dry-run, or N to cancel: " action

rsync_args=(
    "-rtvh"
    "--partial"
    "--info=progress2"
)

case "${action,,}" in
    y)
        mkdir -p "$destination_path"
        ;;

    d)
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

command=(
    rsync
    "${rsync_args[@]}"
    "${exclude_args[@]}"
    "$source_path"
    "$destination_path"
)

printf '\n'
print_command "${command[@]}"
printf '\n'

"${command[@]}"
status=$?

printf '\n'

if [[ "$status" -eq 0 ]]; then
    printf 'Sync completed successfully.\n'
else
    printf 'Sync failed with exit code %d.\n' "$status" >&2
fi

exit "$status"