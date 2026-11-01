#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <conda-prefix> <arm64|x86_64>" >&2
    exit 2
fi

prefix="$(cd "$1" && pwd -P)"
target_arch="$2"
case "$target_arch" in
    arm64)
        target_triplet=arm64-apple-darwin20.0.0
        ;;
    x86_64)
        target_triplet=x86_64-apple-darwin13.4.0
        ;;
    *)
        echo "unsupported target architecture: $target_arch" >&2
        exit 2
        ;;
esac

compiler="$prefix/bin/${target_triplet}-clang"
linker="$prefix/bin/${target_triplet}-ld"
[[ -x "$compiler" ]] || { echo "locked target Clang is missing: $compiler" >&2; exit 1; }
[[ -x "$linker" ]] || { echo "locked target ld64 is missing: $linker" >&2; exit 1; }
[[ -x "$prefix/bin/python3.12" ]] || { echo "locked CPython 3.12 is missing" >&2; exit 1; }

# The locked conda-forge compiler does not carry an Apple SDK.  Capture the
# hosted runner SDK explicitly so this probe distinguishes compiler stability
# from the separate question of what SDK contract the final runtime can use.
sdk="$(/usr/bin/xcrun --sdk macosx --show-sdk-path)"
sdk_version="$(/usr/bin/xcrun --sdk macosx --show-sdk-version)"
[[ -d "$sdk" ]] || { echo "Apple SDK is missing: $sdk" >&2; exit 1; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
work="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/fenics macos locked clang prototype ${target_arch}"
wrapper_root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/fenics-locked-clang-bin-${target_arch}"
rm -rf "$work" "$wrapper_root"
mkdir -p "$work/cache" "$work/evidence" "$work/poison" "$wrapper_root"

wrapper="$wrapper_root/fenics-locked-clang-cc"
cat >"$wrapper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
args=()
while [[ \$# -gt 0 ]]; do
    case "\$1" in
        -arch)
            shift 2
            ;;
        -isysroot)
            shift 2
            ;;
        -mmacosx-version-min=*)
            shift
            ;;
        -Wl,-platform_version)
            shift
            [[ \$# -ge 3 ]] || { echo "truncated -Wl,-platform_version" >&2; exit 2; }
            shift 3
            ;;
        -Wl,-platform_version,*)
            shift
            ;;
        *)
            args+=("\$1")
            shift
            ;;
    esac
done
exec "$compiler" -isysroot "$sdk" -mmacosx-version-min=14.0 "\${args[@]}"
EOF
chmod +x "$wrapper"

for name in cc clang gcc xcrun ld; do
    cat >"$work/poison/$name" <<EOF
#!/usr/bin/env bash
echo "ambient tool invoked unexpectedly: $name" >&2
exit 97
EOF
    chmod +x "$work/poison/$name"
done

{
    echo "target_arch=$target_arch"
    echo "target_triplet=$target_triplet"
    echo "compiler=$compiler"
    echo "linker=$linker"
    echo "sdk=$sdk"
    echo "sdk_version=$sdk_version"
    "$compiler" --version
    file "$compiler"
    file "$linker"
    otool -L "$compiler"
    otool -l "$compiler" | awk '$1 == "minos" {print "compiler_minos=" $2; exit}'
} | tee "$work/evidence/toolchain.txt"

export PATH="$work/poison:$prefix/bin:/usr/bin:/bin"
export CC="$wrapper"
export CXX="$wrapper"
export CPP="$wrapper -E"
export LDSHARED="$wrapper -bundle -undefined dynamic_lookup"
export MACOSX_DEPLOYMENT_TARGET=14.0
export SDKROOT="$sdk"
export CONDA_BUILD_SYSROOT="$sdk"
export XDG_CACHE_HOME="$work/cache"
export PYTHONNOUSERSITE=1
export PYTHONPATH="$work/poison"
export PYTHONUSERBASE="$work/poison"
unset DYLD_LIBRARY_PATH DYLD_FALLBACK_LIBRARY_PATH CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH COMPILER_PATH GCC_EXEC_PREFIX

cat >"$work/probe.c" <<'EOF'
#include <math.h>
#include <stdint.h>
double fenics_probe(double x) { return sqrt(x) + (double)(uint64_t)1; }
EOF
"$wrapper" -c "$work/probe.c" -o "$work/probe.o"
file "$work/probe.o" | tee "$work/evidence/trivial-object.txt"
file "$work/probe.o" | grep -q "$target_arch"

"$prefix/bin/python3.12" -I - "$wrapper" "$compiler" "$sdk" <<'PY'
from __future__ import annotations

from pathlib import Path
import json
import os
import platform
import sys

import basix
import dolfinx
import ffcx
import mpi4py
import numpy
import petsc4py
import ufl

wrapper = Path(sys.argv[1]).resolve()
compiler = Path(sys.argv[2]).resolve()
sdk = Path(sys.argv[3]).resolve()
if platform.machine() != os.environ.get("TARGET_ARCH", platform.machine()):
    pass
if numpy.__version__ != "2.5.2":
    raise SystemExit(f"unexpected NumPy version: {numpy.__version__}")
if dolfinx.__version__ != "0.11.0" or ffcx.__version__ != "0.11.0" or basix.__version__ != "0.11.0":
    raise SystemExit("unexpected FEnIcs component version")
if ufl.__version__ != "2026.1.0":
    raise SystemExit(f"unexpected UFL version: {ufl.__version__}")
record = {
    "python": platform.python_version(),
    "machine": platform.machine(),
    "numpy": numpy.__version__,
    "dolfinx": dolfinx.__version__,
    "petsc4py": petsc4py.__version__,
    "mpi4py": mpi4py.__version__,
    "ffcx": ffcx.__version__,
    "basix": basix.__version__,
    "ufl": ufl.__version__,
    "cc": os.environ.get("CC"),
    "ldshared": os.environ.get("LDSHARED"),
    "compiler": str(compiler),
    "sdk": str(sdk),
}
if Path(record["cc"]).resolve() != wrapper:
    raise SystemExit(f"CC escaped locked wrapper: {record['cc']}")
print(json.dumps(record, sort_keys=True))
PY

"$prefix/bin/python3.12" -I "$repo_root/tests/fenics/test_poisson.py" 2>&1 | tee "$work/evidence/poisson.log"
grep -Eq '^L2 error:' "$work/evidence/poisson.log"

jit_modules="$work/evidence/jit-modules.txt"
find "$work/cache" -type f -name '*.so' -print | sort >"$jit_modules"
if [[ ! -s "$jit_modules" ]]; then
    echo "fresh FFCx JIT produced no Mach-O extension modules" >&2
    exit 1
fi

while IFS= read -r module; do
    file "$module" | tee -a "$work/evidence/jit-mach-o.txt"
    file "$module" | grep -q "$target_arch"
    otool -L "$module" | tee -a "$work/evidence/jit-otool-L.txt"
    if otool -L "$module" | grep -E '/opt/homebrew|/usr/local|Cellar'; then
        echo "fresh JIT module links to an ambient package-manager path: $module" >&2
        exit 1
    fi
    minos="$(otool -l "$module" | awk '$1 == "minos" {print $2; exit}')"
    [[ "$minos" == 14.0 ]] || {
        echo "fresh JIT module has unexpected macOS minimum $minos: $module" >&2
        exit 1
    }
done <"$jit_modules"

echo "macos_locked_clang_ffcx_jit_prototype=pass"
