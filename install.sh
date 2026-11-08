#!/usr/bin/env bash
set -euo pipefail
umask 077
project=$(cd -- "$(dirname -- "$0")" && pwd)
root="${XDG_DATA_HOME:-$HOME/.local/share}/Fat-Fish-Assistant"
cache="${XDG_CACHE_HOME:-$HOME/.cache}/Fat-Fish-Assistant"
mode=--install
args=()
while (( $# )); do
    case "$1" in
        --root) root="${2:?Missing --root path}"; shift ;;
        --cache) cache="${2:?Missing --cache path}"; shift ;;
        --owner) args+=(--owner "${2:?Missing --owner QQ}"); shift ;;
        --offline) mode=--offline; args+=(--offline) ;;
        --accept-external-licenses) args+=("$1") ;;
        --help) echo 'Usage: bash install.sh [--root DIR] [--cache DIR] [--owner QQ] [--offline] --accept-external-licenses'; exit ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
if (( EUID == 0 )); then
    echo 'Run as the service user. System prerequisites invoke sudo if needed.' >&2
    exit 1
fi
for location in "$root" "$cache"; do
    parent=$(realpath -m -- "$location")
    while [[ ! -e "$parent" ]]; do parent=$(dirname -- "$parent"); done
    free=$(df -Pk -- "$parent" | awk 'NR==2 {print $4}')
    if (( free < 2 * 1024 * 1024 )); then
        echo "At least 2 GiB free space is needed at $parent" >&2
        exit 1
    fi
done
bash "$project/scripts/linux/system-prerequisites.sh" "$mode" build "$cache/system"
python3.12 -B "$project/ffa.py" --root "$root" install --components chat,napcat --dependency-profile core --cache "$cache" --retain-cache "${args[@]}"
python3.12 -B "$project/ffa.py" --root "$root" status
