#!/usr/bin/env bash
set -euo pipefail

kit_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The shared bash launcher uses tools available on both GitHub-hosted Linux and
# macOS runners (mktemp, cp, cmp, grep).
exec bash "$kit_dir/test-octave-linux.sh" "$@"
