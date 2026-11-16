#!/usr/bin/env bash
set -euo pipefail

package_root=""
octave="${OCTAVE_CLI:-octave-cli}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --package-root) package_root="$2"; shift 2 ;;
    --octave) octave="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$package_root" ]]; then
  echo "--package-root is required" >&2
  exit 2
fi
package_root="$(cd "$package_root" && pwd)"
source_smoke="$package_root/host/mock_smoke.m"
source_mex="$package_root/bridge.mex"
[[ -f "$source_smoke" ]] || { echo "missing Octave smoke file: $source_smoke" >&2; exit 1; }
[[ -f "$source_mex" ]] || { echo "missing reference bridge MEX: $source_mex" >&2; exit 1; }

poison="$(mktemp -d "${TMPDIR:-/tmp}/reference-bridge-poison.XXXXXX")"
relocation_parent="$(mktemp -d "${TMPDIR:-/tmp}/reference-bridge-relocation.XXXXXX")"
trap 'rm -rf "$poison" "$relocation_parent"' EXIT
mkdir -p "$poison/python" "$poison/home" "$poison/user" "$poison/jit cache"
printf '%s\n' 'raise RuntimeError("ambient numpy must not load")' > "$poison/python/numpy.py"

relocated_root="$relocation_parent/package with spaces"
cp -a "$package_root" "$relocated_root"
relocated_mex="$relocated_root/bridge.mex"
[[ -f "$relocated_mex" ]] || { echo "relocated reference bridge MEX is missing: $relocated_mex" >&2; exit 1; }
cmp -s "$source_mex" "$relocated_mex" || {
  echo "relocating the test layout changed bridge.mex bytes" >&2
  exit 1
}
smoke="$relocated_root/host/mock_smoke.m"

run_smoke() {
  local fresh="$1"
  local output
  local status
  if output="$(
    REFERENCE_BRIDGE_ROOT="$relocated_root" \
    REFERENCE_BRIDGE_FRESH_ONLY="$fresh" \
    PYTHONHOME="$poison/home" \
    PYTHONPATH="$poison/python" \
    PYTHONUSERBASE="$poison/user" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    XDG_CACHE_HOME="$poison/jit cache" \
    "$octave" --quiet --no-gui --no-init-file "$smoke" 2>&1
  )"; then
    status=0
  else
    status=$?
  fi
  printf '%s\n' "$output"
  if [[ "$status" -ne 0 ]]; then
    if [[ "$status" -eq 139 ]] && command -v gdb >/dev/null 2>&1; then
      local gdb_output
      echo "reference_bridge_gdb_backtrace=begin" >&2
      if gdb_output="$(
        gdb --quiet --batch \
          -ex "set environment REFERENCE_BRIDGE_ROOT $relocated_root" \
          -ex "set environment REFERENCE_BRIDGE_FRESH_ONLY $fresh" \
          -ex "set environment PYTHONHOME $poison/home" \
          -ex "set environment PYTHONPATH $poison/python" \
          -ex "set environment PYTHONUSERBASE $poison/user" \
          -ex 'set environment PYTHONDONTWRITEBYTECODE 1' \
          -ex 'set environment PYTHONNOUSERSITE 1' \
          -ex "set environment XDG_CACHE_HOME $poison/jit cache" \
          -ex run \
          -ex 'thread apply all bt' \
          --args "$octave" --quiet --no-gui --no-init-file "$smoke" 2>&1
      )"; then
        :
      else
        :
      fi
      printf '%s\n' "$gdb_output" >&2
      echo "reference_bridge_gdb_backtrace=end" >&2
    fi
    return "$status"
  fi
  grep -q '^reference_bridge_result=pass$' <<<"$output"
}

run_smoke 0
run_smoke 1
echo "reference_bridge_relocation=pass"
echo "reference_bridge_fresh_process=pass"
