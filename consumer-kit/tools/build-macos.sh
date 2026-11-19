#!/usr/bin/env bash
set -euo pipefail

kit_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${REFERENCE_BRIDGE_TARGET:-}"
if [[ -n "$target" && "$target" != "macos-arm64" && "$target" != "macos-x64" ]]; then
  echo "REFERENCE_BRIDGE_TARGET must be macos-arm64 or macos-x64: $target" >&2
  exit 2
fi
if [[ -z "$target" ]]; then
  arch="$(uname -m)"
  if [[ "$arch" == "arm64" ]]; then
    target="macos-arm64"
  elif [[ "$arch" == "x86_64" ]]; then
    target="macos-x64"
  else
    echo "unsupported macOS architecture: $arch" >&2
    exit 2
  fi
fi

python_bin="${PYTHON:-python3}"
output_dir=""
manifest=""
expect_output=0
expect_manifest=0
for argument in "$@"; do
  if [[ "$expect_output" == "1" ]]; then
    output_dir="$argument"
    expect_output=0
    continue
  fi
  if [[ "$expect_manifest" == "1" ]]; then
    manifest="$argument"
    expect_manifest=0
    continue
  fi
  case "$argument" in
    --target|--target=*)
      echo "build-macos.sh selects the target from REFERENCE_BRIDGE_TARGET or uname; do not pass --target" >&2
      exit 2
      ;;
    --output-dir)
      expect_output=1
      ;;
    --output-dir=*)
      output_dir="${argument#--output-dir=}"
      ;;
    --manifest)
      expect_manifest=1
      ;;
    --manifest=*)
      manifest="${argument#--manifest=}"
      ;;
  esac
done
if [[ "$expect_output" == "1" || "$expect_manifest" == "1" || -z "$output_dir" ]]; then
  echo "build-macos.sh requires --output-dir so the exact candidate can be audited" >&2
  exit 2
fi
if [[ -z "$manifest" ]]; then
  echo "build-macos.sh requires --manifest so the exact declared export surface can be audited" >&2
  exit 2
fi

"$python_bin" "$kit_dir/build_bridge.py" --target "$target" "$@"
"$python_bin" "$kit_dir/audit-mex.py" \
  --target "$target" \
  --manifest "$manifest" \
  --mex "$output_dir/bridge.mex" \
  --output "$output_dir/native-audit.json"
