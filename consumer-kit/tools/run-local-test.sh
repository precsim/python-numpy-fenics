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

"$python_cmd" -B "$script_dir/verify-package.py" --root "$script_dir"

omp_override=""
openssl_preload=""
openssl_conf=""
openssl_modules=""
openssl_override=""
if [ "$(uname -s)" = "Darwin" ]; then
    omp_provider="$script_dir/runtime/fenics-runtime/conda/lib/libomp.dylib"
    if [ -f "$omp_provider" ]; then
        omp_override=$(mktemp -d "${TMPDIR:-/tmp}/reference-bridge-libomp.XXXXXX")
        ln -s "$omp_provider" "$omp_override/libomp.dylib"
        DYLD_LIBRARY_PATH="$omp_override"
        export DYLD_LIBRARY_PATH
        echo "reference_bridge_macos_openmp_provider=$omp_provider"
    fi
elif [ "$(uname -s)" = "Linux" ]; then
    openssl_root="$script_dir/runtime/fenics-runtime/conda"
    crypto_provider="$openssl_root/lib/libcrypto.so.3"
    ssl_provider="$openssl_root/lib/libssl.so.3"
    if [ -f "$crypto_provider" ] && [ -f "$ssl_provider" ]; then
        openssl_override=$(mktemp -d "${TMPDIR:-/tmp}/reference-bridge-openssl.XXXXXX")
        ln -s "$crypto_provider" "$openssl_override/libcrypto.so.3"
        ln -s "$ssl_provider" "$openssl_override/libssl.so.3"
        gcc_provider="$openssl_root/lib/libgcc_s.so.1"
        stdcpp_provider="$openssl_root/lib/libstdc++.so.6"
        blas_alias="$openssl_root/lib/libopenblas.so.0"
        blas_provider=$(readlink -f "$blas_alias" 2>/dev/null || true)
        [ -f "$gcc_provider" ] && [ -f "$stdcpp_provider" ] || {
            echo "reference_bridge_error=FEniCS toolchain runtime provider is missing" >&2
            exit 1
        }
        [ -n "$blas_provider" ] && [ -f "$blas_provider" ] || {
            echo "reference_bridge_error=FEniCS BLAS/LAPACK provider is missing: $blas_alias" >&2
            exit 1
        }
        case "$blas_provider" in
            "$openssl_root"/lib/libopenblas*.so*) ;;
            *)
                echo "reference_bridge_error=FEniCS BLAS/LAPACK provider escaped the verified runtime: $blas_provider" >&2
                exit 1
                ;;
        esac
        ln -s "$gcc_provider" "$openssl_override/libgcc_s.so.1"
        ln -s "$stdcpp_provider" "$openssl_override/libstdc++.so.6"
        ln -s "$blas_provider" "$openssl_override/libopenblas.so.0"
        openssl_preload="$openssl_override/libgcc_s.so.1:$openssl_override/libstdc++.so.6:$openssl_override/libopenblas.so.0:$openssl_override/libcrypto.so.3:$openssl_override/libssl.so.3"
        openssl_conf="$openssl_root/ssl/openssl.cnf"
        openssl_modules="$openssl_root/lib/ossl-modules"
        [ -f "$openssl_conf" ] || {
            echo "reference_bridge_error=FEniCS OpenSSL configuration is missing: $openssl_conf" >&2
            exit 1
        }
        echo "reference_bridge_linux_openssl_provider=$crypto_provider"
        echo "reference_bridge_linux_blas_provider=$blas_provider"
    fi
fi

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
cleanup() {
    rm -rf "$poison"
    if [ -n "$omp_override" ]; then
        rm -rf "$omp_override"
    fi
    if [ -n "$openssl_override" ]; then
        rm -rf "$openssl_override"
    fi
}
trap cleanup EXIT HUP INT TERM
mkdir -p "$poison/python" "$poison/home" "$poison/user" "$poison/jit cache"
printf '%s\n' 'raise RuntimeError("ambient numpy must not load")' > "$poison/python/numpy.py"

run_smoke() {
    fresh=$1
    status=0
    if [ -n "$openssl_preload" ]; then
        output=$(
            REFERENCE_BRIDGE_ROOT="$script_dir" \
            REFERENCE_BRIDGE_FRESH_ONLY="$fresh" \
            PYTHONHOME="$poison/home" \
            PYTHONPATH="$poison/python" \
            PYTHONSTARTUP= \
            PYTHONUSERBASE="$poison/user" \
            PYTHONDONTWRITEBYTECODE=1 \
            PYTHONNOUSERSITE=1 \
            XDG_CACHE_HOME="$poison/jit cache" \
            LD_PRELOAD="$openssl_preload" \
            OPENSSL_CONF="$openssl_conf" \
            OPENSSL_MODULES="$openssl_modules" \
            "$octave_cmd" --quiet --no-gui --no-init-file "$smoke" 2>&1
        ) || status=$?
    else
        output=$(
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
        ) || status=$?
    fi
    if [ "$status" -ne 0 ]; then
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
