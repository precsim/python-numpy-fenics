#!/usr/bin/env bash
set -euo pipefail

kit_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
arguments=("$@")
package_root=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --package-root)
      package_root="$2"
      shift 2
      ;;
    --octave)
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done
if [[ -z "$package_root" ]]; then
  echo "--package-root is required" >&2
  exit 2
fi
package_root="$(cd "$package_root" && pwd)"

omp_provider="$package_root/runtime/fenics-runtime/conda/lib/libomp.dylib"
omp_override=""
cleanup() {
  if [[ -n "$omp_override" ]]; then
    rm -rf "$omp_override"
  fi
}
trap cleanup EXIT

if [[ -f "$omp_provider" ]]; then
  omp_override="$(mktemp -d "${TMPDIR:-/tmp}/reference-bridge-libomp.XXXXXX")"
  ln -s "$omp_provider" "$omp_override/libomp.dylib"
  export DYLD_LIBRARY_PATH="$omp_override"
  echo "reference_bridge_macos_openmp_provider=$omp_provider"
fi

# The shared bash launcher uses tools available on both GitHub-hosted Linux and
# macOS runners (mktemp, cp, cmp, grep).  On a FEniCS layout DYLD_LIBRARY_PATH
# contains only the verified package libomp alias, so Octave and the embedded
# runtime resolve one OpenMP provider from process launch.
bash "$kit_dir/test-octave-linux.sh" "${arguments[@]}"
