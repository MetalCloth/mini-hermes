#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(dirname "$(dirname "$(readlink -f -- "$0")")")"
logs_dir="$repo_dir/logs"
mode="run"
if [[ "${1:-}" == "run" || "${1:-}" == "follow" ]]; then
    mode="$1"
    shift
fi

case "$mode" in
    run)
        mkdir -p "$logs_dir"
        log_file="$logs_dir/computer-$(date -u +%Y%m%dT%H%M%SZ)-$$.jsonl"
        (umask 077; : > "$log_file")
        chmod 600 "$log_file"
        export ORYN_COMPUTER_LOG_FILE="$log_file"
        printf 'Computer trace: %s\n' "$log_file" >&2
        printf 'Follow it live in another terminal with: %s follow\n' "$0" >&2
        exec "$repo_dir/oryn" "$@"
        ;;
    follow)
        mkdir -p "$logs_dir"
        if (($#)); then
            log_file="$1"
        else
            shopt -s nullglob
            log_files=("$logs_dir"/computer-*.jsonl)
            if ((${#log_files[@]} == 0)); then
                echo "No computer logs yet. Start Oryn with: $0 run" >&2
                exit 1
            fi
            log_file="$(printf '%s\n' "${log_files[@]}" | sort | tail -n 1)"
        fi
        exec tail -n +1 -F -- "$log_file"
        ;;
esac
