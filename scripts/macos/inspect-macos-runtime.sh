#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: scripts/macos/inspect-macos-runtime.sh --root <tree> --output <json> --arch arm64|x86_64 [--executable <python3.12>] [--max-minos 14.0] [--strict]" >&2
    exit 2
}

root=""
output=""
arch=""
strict=false
max_minos="14.0"
executable=""
while (($#)); do
    case "$1" in
        --root) root="$2"; shift 2 ;;
        --output) output="$2"; shift 2 ;;
        --arch) arch="$2"; shift 2 ;;
        --executable) executable="$2"; shift 2 ;;
        --max-minos) max_minos="$2"; shift 2 ;;
        --strict) strict=true; shift ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

[[ -d "$root" ]] || { echo "inspection root is missing: $root" >&2; exit 1; }
[[ -n "$output" ]] || { echo "inspection output is required" >&2; exit 1; }
[[ "$arch" == arm64 || "$arch" == x86_64 ]] || { echo "unsupported architecture: $arch" >&2; exit 1; }
for tool in file lipo otool python3; do
    command -v "$tool" >/dev/null 2>&1 || { echo "$tool is required" >&2; exit 1; }
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
args=(
    "$script_dir/audit_macos_runtime.py"
    --root "$root"
    --output "$output"
    --arch "$arch"
    --max-minos "$max_minos"
)
if [[ "$strict" == true ]]; then
    args+=(--strict)
fi
if [[ -n "$executable" ]]; then
    args+=(--executable "$executable")
fi
python3 "${args[@]}"
