#!/usr/bin/env bash
# Prepare the Ubuntu system dependencies for the Linux installer.
set -euo pipefail
mode=${1:---check}
profile=${2:-runtime}
case "$mode" in --check|--install|--offline) ;; *) exit 2 ;; esac
case "$profile" in runtime|build) ;; *) exit 2 ;; esac
if [[ "$mode" != --check && $# != 3 ]]; then
    echo "Usage: $0 --install|--offline runtime|build CACHE_DIR" >&2
    exit 2
fi
. /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 || "$(uname -m)" != x86_64 ]]; then
    echo "Expected Ubuntu 24.04 x86_64" >&2
    exit 1
fi
source_root=$(cd -- "$(dirname -- "$0")/../.." && pwd)
lock="$source_root/manifests/linux-x86_64/ubuntu-24.04.tsv"
missing=()
files=()
if [[ "$mode" != --check ]]; then
    mkdir -p -- "$3"
    cache=$(cd -- "$3" && pwd)
fi
while IFS=$'\t' read -r group package version archive hash; do
    [[ "$group" == runtime || ( "$group" == build && "$profile" == build ) ]] || continue
    current=$(dpkg-query -W -f='${db:Status-Status} ${Version}' "$package" 2>/dev/null || true)
    [[ "$current" == "installed $version" ]] && continue
    if [[ "$current" == installed* ]]; then
        echo "Version mismatch: $package ($current, lock $version). Review before replacing installed packages." >&2
        exit 1
    fi
    missing+=("$package=$version")
    [[ "$mode" != --check ]] || continue
    file="$cache/${archive##*/}"
    if [[ ! -e "$file" && "$mode" == --install ]]; then
        command -v curl >/dev/null || { echo "Install curl or provide the verified offline archive: $archive" >&2; exit 1; }
        pending=$(mktemp "$cache/.ubuntu-package.XXXXXX")
        trap 'rm -f -- "$pending"' EXIT
        downloaded=false
        for mirror in https://mirrors.tuna.tsinghua.edu.cn/ubuntu https://archive.ubuntu.com/ubuntu https://security.ubuntu.com/ubuntu; do
            if curl --fail --location --silent --show-error --connect-timeout 10 --max-time 180                 "$mirror/$archive" -o "$pending"; then
                printf '%s  %s\n' "$hash" "$pending" | sha256sum --check --status || {
                    echo "SHA256 mismatch: $archive" >&2
                    exit 1
                }
                mv -f -- "$pending" "$file"
                downloaded=true
                break
            fi
        done
        if [[ "$downloaded" != true ]]; then
            echo "Download failed: $archive" >&2
            exit 1
        fi
        trap - EXIT
    fi
    if [[ ! -f "$file" ]]; then
        echo "Missing cached archive: $file" >&2
        exit 1
    fi
    printf '%s  %s\n' "$hash" "$file" | sha256sum --check --status || {
        echo "SHA256 mismatch: $file" >&2
        exit 1
    }
    files+=("$file")
done < "$lock"
if (( ${#missing[@]} )); then
    if [[ "$mode" == --check ]]; then
        printf 'Missing: %s\n' "${missing[@]}" >&2
        exit 1
    fi
    apt=(apt-get)
    (( EUID == 0 )) || apt=(sudo apt-get)
    if [[ "$mode" == --offline ]]; then
        apt+=(--no-download)
    else
        "${apt[@]}" update
    fi
    "${apt[@]}" install -y --no-install-recommends "${files[@]}"
    exec bash "$0" --check "$profile"
fi
probe=$(mktemp -d)
trap 'rm -rf -- "$probe"' EXIT
python3.12 -m venv "$probe/venv"
"$probe/venv/bin/python" -m pip --version
dbus-run-session -- xvfb-run -a true
if [[ "$profile" == build ]]; then
    g++ -x c++ - -o "$probe/compiler" <<< 'int main() { return 0; }'
    "$probe/compiler"
fi
printf 'Ubuntu prerequisites verified (%s)\n' "$profile"
