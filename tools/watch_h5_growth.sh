#!/usr/bin/env bash

# bash watch_h5_growth.sh [date_or_prefix ...]
# bash watch_h5_growth.sh 20260625 20260701

set -euo pipefail

dir="/data/internal/DATASET"
interval=30

prefixes=()

if (($# == 0)); then
    prefixes=("demo_$(date +%Y%m%d)")
else
    for arg in "$@"; do
        if [[ "$arg" == demo_* ]]; then
            prefixes+=("$arg")
        else
            prefixes+=("demo_$arg")
        fi
    done
fi

state="/tmp/watch_h5_growth_${UID}.tsv"
time_state="/tmp/watch_h5_growth_${UID}.time"
lock="/tmp/watch_h5_growth_${UID}.lock"

cleanup_intermediate() {
    rm -f -- \
        "${state}.raw."* \
        "${state}.current."* \
        "${state}.rows."* \
        "${state}.status."* \
        "${state}.display."* \
        "${state}.new."* \
        "${time_state}.new."*
}

# 防止同时运行多个监控实例。
exec 9>"$lock"

if ! flock -n 9; then
    echo "watch_h5_growth 已经在运行"
    exit 1
fi

# 每次启动时清除本脚本留下的旧状态。
# 不会删除 DATASET 中的 .h5.tmp 文件。
cleanup_intermediate
rm -f -- "$state" "$time_state"

trap cleanup_intermediate EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

started_at=$(date +%s)

while true; do
    now=$(date +%s)

    raw=$(mktemp "${state}.raw.XXXXXX")
    current=$(mktemp "${state}.current.XXXXXX")
    rows=$(mktemp "${state}.rows.XXXXXX")
    status=$(mktemp "${state}.status.XXXXXX")
    display=$(mktemp "${state}.display.XXXXXX")
    new_state=$(mktemp "${state}.new.XXXXXX")
    new_time=$(mktemp "${time_state}.new.XXXXXX")
    find_args=("$dir" -maxdepth 1 -type f "(")
    first_prefix=1

    for prefix in "${prefixes[@]}"; do
        if ((first_prefix)); then
            first_prefix=0
        else
            find_args+=(-o)
        fi

        find_args+=(
            -name "${prefix}*.h5"
            -o -name "${prefix}*.h5.tmp"
        )
    done

    find_args+=(")" -print0)

    # 生成四列：
    #   排序组、文件名、大小、完整路径
    #
    # 排序组：
    #   0：已经完成的 .h5
    #   1：正在生成的 .h5.tmp
    while IFS= read -r -d '' file; do
        filename=${file##*/}

        # 文件可能恰好在 find 和 du 之间由 .tmp 重命名为 .h5。
        if ! size=$(du -sm -- "$file" 2>/dev/null | cut -f1); then
            continue
        fi

        if [[ "$filename" == *.h5.tmp ]]; then
            group=1
        else
            group=0
        fi

        printf '%d\t%s\t%s\t%s\n' \
            "$group" \
            "$filename" \
            "$size" \
            "$file"
    done < <(
        find "${find_args[@]}"
    ) > "$raw"

    # 已完成文件在前，临时文件在后；
    # 每组内部按 demo 时间排序。
    sort -t $'\t' -k1,1n -k2,2 "$raw" |
        cut -f3- > "$current"

    has_previous=0
    interval_elapsed=0
    total_elapsed=$((now - started_at))
    all_zero=0

    if [[ -f "$state" && -f "$time_state" ]]; then
        previous_time=""

        read -r previous_time < "$time_state" || true

        if [[ "$previous_time" =~ ^[0-9]+$ ]]; then
            interval_elapsed=$((now - previous_time))
            has_previous=1
        fi
    fi

    if (( has_previous )); then
        awk -F '\t' \
            -v interval_elapsed="$interval_elapsed" \
            -v status_file="$status" '
            NR == FNR {
                key = $2
                sub(/\.h5(\.tmp)?$/, "", key)

                previous[key] = $1
                next
            }

            {
                size = $1
                file = $2

                key = file
                sub(/\.h5(\.tmp)?$/, "", key)

                count++

                if (key in previous) {
                    delta = size - previous[key]
                    matched++

                    if (delta != 0)
                        nonzero = 1

                    if (interval_elapsed > 0) {
                        printf "%s\t%s\t%+d MiB\t%.2f MiB/s\n",
                               size,
                               file,
                               delta,
                               delta / interval_elapsed
                    } else {
                        printf "%s\t%s\t%+d MiB\t-\n",
                               size,
                               file,
                               delta
                    }
                } else {
                    nonzero = 1

                    printf "%s\t%s\tNEW\t-\n",
                           size,
                           file
                }
            }

            END {
                if (count > 0 && matched == count && nonzero == 0)
                    print 1 > status_file
                else
                    print 0 > status_file
            }
        ' "$state" "$current" > "$rows"

        read -r all_zero < "$status"
    else
        awk -F '\t' '
            {
                printf "%s\t%s\tNEW\t-\n", $1, $2
            }
        ' "$current" > "$rows"
    fi

    {
        printf 'SIZE(MiB)\tFILE\tDELTA\tRATE\n'
        cat "$rows"
    } |
        column -t -s $'\t' > "$display"

    # 原子更新状态，供下一轮计算差值。
    cat "$current" > "$new_state"
    printf '%s\n' "$now" > "$new_time"

    mv -f -- "$new_state" "$state"
    mv -f -- "$new_time" "$time_state"

    # 刷新终端显示，不启用差异高亮。
    printf '\033[H\033[2J'
    printf '%s\n\n' "$(date '+%F %T')"
    cat "$display"

    printf '\nruntime=%02d:%02d:%02d' \
        $((total_elapsed / 3600)) \
        $(((total_elapsed % 3600) / 60)) \
        $((total_elapsed % 60))

    if (( has_previous )); then
        printf '  sample_interval=%d s\n' "$interval_elapsed"
    else
        printf '  initial sample\n'
    fi

    rm -f -- \
        "$raw" \
        "$current" \
        "$rows" \
        "$status" \
        "$display"

    # 第一轮不参与判断。
    # 从第二轮开始，如果所有文件的 delta 都为 0，
    # 停止定时采样并保持当前输出，直到 Ctrl+C 或 kill。
    if (( has_previous && all_zero == 1 )); then
        printf '\nAll deltas are zero. Monitoring stopped; press Ctrl+C to exit.\n'

        while true; do
            sleep 3600
        done
    fi

    sleep "$interval"
done
