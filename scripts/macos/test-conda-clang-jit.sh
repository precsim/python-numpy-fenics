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
        clang_exe="$prefix/bin/arm64-apple-darwin20.0.0-clang"
        linker_exe="$prefix/bin/arm64-apple-darwin20.0.0-ld"
        ;;
    x86_64)
        clang_exe="$prefix/bin/x86_64-apple-darwin13.4.0-clang"
        linker_exe="$prefix/bin/x86_64-apple-darwin13.4.0-ld"
        ;;
    *)
        echo "unsupported target architecture: $target_arch" >&2
        exit 2
        ;;
esac

[[ -x "$clang_exe" ]] || { echo "locked Clang executable is missing: $clang_exe" >&2; exit 1; }
[[ -x "$linker_exe" ]] || { echo "locked cctools linker is missing: $linker_exe" >&2; exit 1; }
[[ -x "$prefix/bin/python3.12" ]] || { echo "CPython 3.12 is missing: $prefix/bin/python3.12" >&2; exit 1; }

resolved_linker="$("$clang_exe" -B"$prefix/bin" -print-prog-name=ld)"
if [[ "$resolved_linker" != /* ]]; then
    resolved_linker="$prefix/bin/$resolved_linker"
fi
resolved_linker="$("$prefix/bin/python3.12" - "$resolved_linker" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).resolve())
PY
)"
expected_linker="$("$prefix/bin/python3.12" - "$linker_exe" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).resolve())
PY
)"
[[ "$resolved_linker" == "$expected_linker" ]] || {
    echo "locked Clang did not resolve the locked cctools linker: $resolved_linker != $expected_linker" >&2
    exit 1
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
work="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/fenics macos clang prototype ${target_arch}"
wrapper_root="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/fenics-clang-bin-${target_arch}"
rm -rf "$work"
rm -rf "$wrapper_root"
mkdir -p "$work/cache" "$work/evidence" "$work/poison" "$wrapper_root"

wrapper="$wrapper_root/fenics-clang-cc"
cat >"$wrapper" <<EOF
#!/usr/bin/env bash
set -euo pipefail
args=()
while [[ \$# -gt 0 ]]; do
    case "\$1" in
        -isysroot|-arch)
            shift 2
            ;;
        -isysroot*|-mmacosx-version-min=*|-Wl,-syslibroot,*|-Wl,-platform_version,macos,*)
            shift
            ;;
        *)
            args+=("\$1")
            shift
            ;;
    esac
done
sdkroot="\${SDKROOT:-}"
if [[ -z "\$sdkroot" || ! -d "\$sdkroot" ]]; then
    sdkroot="\$(/usr/bin/xcrun --sdk macosx --show-sdk-path)"
fi
[[ -d "\$sdkroot" ]] || { echo "macOS SDK root is unavailable: \$sdkroot" >&2; exit 1; }
exec "$clang_exe" -B"$prefix/bin" -isysroot "\$sdkroot" -mmacosx-version-min=14.0 "\${args[@]}"
EOF
chmod +x "$wrapper"

for name in cc clang gcc xcrun; do
    cat >"$work/poison/$name" <<EOF
#!/usr/bin/env bash
echo "ambient tool invoked unexpectedly: $name" >&2
exit 97
EOF
    chmod +x "$work/poison/$name"
done

python_cmd=("$prefix/bin/python3.12")
if [[ "$target_arch" == x86_64 && "$(uname -m)" == arm64 ]]; then
    python_cmd=(/usr/bin/arch -x86_64 "$prefix/bin/python3.12")
fi

"${python_cmd[@]}" -I - "$target_arch" <<'PY'
import platform
import sys

expected = sys.argv[1]
actual = platform.machine()
if actual != expected:
    raise SystemExit(f"target Python architecture mismatch: {actual} != {expected}")
print(f"python_architecture={actual}")
print(f"python_version={platform.python_version()}")
if sys.version_info[:2] != (3, 12):
    raise SystemExit(f"prototype requires CPython 3.12, got {platform.python_version()}")
PY

export PATH="$work/poison:$prefix/bin:/usr/bin:/bin"
export CC="$wrapper"
export CXX="$wrapper"
export CPP="$wrapper -E"
export LDSHARED="$wrapper -bundle -undefined dynamic_lookup"
export MACOSX_DEPLOYMENT_TARGET=14.0
export XDG_CACHE_HOME="$work/cache"
export PYTHONNOUSERSITE=1
export PYTHONPATH="$work/poison"
export PYTHONUSERBASE="$work/poison"
export DYLD_LIBRARY_PATH="$work/poison"
export DYLD_FALLBACK_LIBRARY_PATH="$work/poison"
unset SDKROOT DEVELOPER_DIR CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH COMPILER_PATH GCC_EXEC_PREFIX

"${python_cmd[@]}" -I - "$wrapper" "$clang_exe" "$linker_exe" <<'PY'
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
clang = Path(sys.argv[2]).resolve()
linker = Path(sys.argv[3]).resolve()
if numpy.__version__ != "2.5.2":
    raise SystemExit(f"unexpected NumPy version: {numpy.__version__}")
if dolfinx.__version__ != "0.11.0":
    raise SystemExit(f"unexpected DOLFINx version: {dolfinx.__version__}")
if ffcx.__version__ != "0.11.0":
    raise SystemExit(f"unexpected FFCx version: {ffcx.__version__}")
if basix.__version__ != "0.11.0":
    raise SystemExit(f"unexpected Basix version: {basix.__version__}")
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
    "clang": str(clang),
    "linker": str(linker),
}
if Path(record["cc"]).resolve() != wrapper:
    raise SystemExit(f"CC escaped bundled wrapper: {record['cc']}")
print(json.dumps(record, sort_keys=True))
PY

"${python_cmd[@]}" -I "$repo_root/tests/fenics/test_poisson.py" 2>&1 | tee "$work/evidence/poisson.log"
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

"$clang_exe" --version | tee "$work/evidence/clang-version.txt"
printf 'clang=%s\nlinker=%s\n' "$clang_exe" "$linker_exe" | tee "$work/evidence/clang-toolchain.txt"
echo "macos_conda_clang_ffcx_jit_prototype=pass"
