#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
octave_cmd=""
python_cmd=""

usage() {
    echo "usage: $0 [--octave /path/to/octave-cli] [--python /path/to/python3]" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --octave)
            [ "$#" -ge 2 ] || usage
            octave_cmd=$2
            shift 2
            ;;
        --python)
            [ "$#" -ge 2 ] || usage
            python_cmd=$2
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            usage
            ;;
    esac
done

if [ -z "$python_cmd" ]; then
    if command -v python3 >/dev/null 2>&1; then
        python_cmd=$(command -v python3)
    elif command -v python >/dev/null 2>&1; then
        python_cmd=$(command -v python)
    else
        echo "reference_bridge_error=bootstrap Python 3 was not found; pass --python" >&2
        exit 1
    fi
fi

"$python_cmd" "$script_dir/verify-package.py" --root "$script_dir"

if [ -z "$octave_cmd" ]; then
    if command -v octave-cli >/dev/null 2>&1; then
        octave_cmd=$(command -v octave-cli)
    elif command -v octave >/dev/null 2>&1; then
        octave_cmd=$(command -v octave)
    else
        echo "reference_bridge_error=GNU Octave was not found; pass --octave" >&2
        exit 1
    fi
fi

if command -v "$octave_cmd" >/dev/null 2>&1; then
    octave_cmd=$(command -v "$octave_cmd")
elif [ ! -x "$octave_cmd" ]; then
    echo "reference_bridge_error=Octave executable is not executable: $octave_cmd" >&2
    exit 1
fi

smoke="$script_dir/host/mock_smoke.m"
[ -f "$smoke" ] || { echo "reference_bridge_error=host/mock_smoke.m is missing" >&2; exit 1; }

poison=$(mktemp -d "${TMPDIR:-/tmp}/reference-bridge-poison.XXXXXX")
trap 'rm -rf "$poison"' EXIT HUP INT TERM
mkdir -p "$poison/python" "$poison/home" "$poison/user" "$poison/jit cache"
printf '%s\n' 'raise RuntimeError("ambient numpy must not load")' > "$poison/python/numpy.py"

run_smoke() {
    fresh=$1
    if output=$(
        REFERENCE_BRIDGE_ROOT="$script_dir" \
        REFERENCE_BRIDGE_FRESH_ONLY="$fresh" \
        PYTHONHOME="$poison/home" \
        PYTHONPATH="$poison/python" \
        PYTHONSTARTUP= \
        PYTHONUSERBASE="$poison/user" \
        PYTHONDONTWRITEBYTECODE=1 \
        PYTHONNOUSERSITE=1 \
        XDG_CACHE_HOME="$poison/jit cache" \
        "$octave_cmd" --quiet --no-gui --no-init-file "$smoke" 2>&1
    ); then
        :
    else
        status=$?
        printf '%s\n' "$output" >&2
        echo "reference_bridge_error=Octave smoke test failed" >&2
        exit "$status"
    fi
    printf '%s\n' "$output"
    printf '%s\n' "$output" | grep -Fx 'reference_bridge_result=pass' >/dev/null || {
        echo "reference_bridge_error=Octave smoke test did not emit reference_bridge_result=pass" >&2
        exit 1
    }
}

run_smoke 0
run_smoke 1
echo "reference_bridge_fresh_process=pass"
