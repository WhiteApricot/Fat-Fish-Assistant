#!/usr/bin/env bash
set -euo pipefail
umask 077
project=$(cd -- "$(dirname -- "$0")" && pwd)
root="${XDG_DATA_HOME:-$HOME/.local/share}/Fat-Fish-Assistant"
if [[ "${1:-}" == --root ]]; then
    root="$2"
    shift 2
fi
if (( $# == 0 )); then set -- start; fi
exec python3.12 -B "$project/ffa.py" --root "$root" "$@"
