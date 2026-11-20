#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: scripts/macos/test-fenics-overlay.sh --zip <overlay.zip> --base-runtime <numpy-runtime> --arch arm64|x86_64" >&2
    exit 2
}

overlay_zip=""
base_runtime=""
target_arch=""
while (($#)); do
    case "$1" in
        --zip) overlay_zip="$2"; shift 2 ;;
        --base-runtime) base_runtime="$2"; shift 2 ;;
        --arch) target_arch="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

[[ "$target_arch" == arm64 || "$target_arch" == x86_64 ]] || usage
[[ -f "$overlay_zip" ]] || { echo "overlay ZIP is missing: $overlay_zip" >&2; exit 1; }
[[ -d "$base_runtime" ]] || { echo "NumPy base runtime is missing: $base_runtime" >&2; exit 1; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
overlay_zip="$(cd "$(dirname "$overlay_zip")" && pwd -P)/$(basename "$overlay_zip")"
base_runtime="$(cd "$base_runtime" && pwd -P)"

for tool in ditto file otool python3 shasum; do
    command -v "$tool" >/dev/null 2>&1 || { echo "$tool is required" >&2; exit 1; }
done

base_profile="$base_runtime/RUNTIME_PROFILE"
base_manifest="$base_runtime/runtime-manifest.json"
[[ -f "$base_profile" && "$(tr -d '\r\n' < "$base_profile")" == numpy ]] || {
    echo "FEnICS overlay requires a public NumPy base runtime" >&2
    exit 1
}
[[ -f "$base_manifest" ]] || { echo "public NumPy base manifest is missing" >&2; exit 1; }
python3 - "$base_manifest" "$target_arch" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
arch = sys.argv[2]
expected_platform = "macos-arm64" if arch == "arm64" else "macos-x64"
if (
    manifest.get("format_version") != 1
    or manifest.get("platform") != expected_platform
    or manifest.get("python_abi") != "cp314"
    or manifest.get("profile") != "numpy"
    or manifest.get("tcl_tk", {}).get("version") != "8.6.18"
    or manifest.get("tcl_tk", {}).get("ownership") != "producer"
    or manifest.get("producer", {}).get("repository") != "precsim/python-numpy-fenics"
):
    raise SystemExit(f"invalid public NumPy base manifest: {manifest!r}")
PY

work="$(mktemp -d "${RUNNER_TEMP:-/tmp}/fenics public overlay test.XXXXXX")"
runtime="$work/runtime with spaces"
cache="$work/jit cache with spaces"
poison="$work/ambient poison"
mkdir -p "$runtime" "$cache" "$poison"
trap 'rm -rf "$work"' EXIT
phase_start=$SECONDS
log_phase() {
    printf 'overlay_qualification_phase_seconds %s %s\n' "$1" "$((SECONDS - phase_start))"
    phase_start=$SECONDS
}
ditto "$base_runtime" "$runtime"
log_phase base_runtime_copy

site_packages="$runtime/Python.framework/Versions/3.14/lib/python3.14/site-packages"
staged_python="$runtime/Python.framework/Versions/3.14/bin/python3.14"
[[ -x "$staged_python" && -d "$site_packages/numpy" ]] || {
    echo "public NumPy base runtime is incomplete" >&2
    exit 1
}
python_cmd=("$staged_python")
if [[ "$target_arch" == x86_64 && "$(uname -m)" == arm64 ]]; then
    python_cmd=(/usr/bin/arch -x86_64 "$staged_python")
fi

hash_numpy() {
    python3 - "$site_packages" "$1" <<'PY'
import hashlib
import json
import pathlib
import sys

site = pathlib.Path(sys.argv[1])
output = pathlib.Path(sys.argv[2])
roots = [
    site / "numpy",
    site / "numpy.libs",
    site / "numpy-2.5.2.dist-info",
]
entries = {}
for root in roots:
    if not root.exists():
        continue
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            entries[path.relative_to(site).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_symlink():
            entries[path.relative_to(site).as_posix()] = {"symlink": path.readlink().as_posix()}
if not entries:
    raise SystemExit("public NumPy base has no retained NumPy payload")
output.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

hash_numpy "$work/numpy-before.json"
log_phase numpy_before_hash
ditto -x -k "$overlay_zip" "$runtime"
log_phase overlay_zip_extraction

overlay_manifest="$runtime/fenics-embed-manifest.json"
[[ -f "$overlay_manifest" ]] || { echo "FEnICS overlay manifest is missing after extraction" >&2; exit 1; }
python3 - "$overlay_manifest" "$target_arch" "$base_manifest" "$work/numpy-before.json" <<'PY'
import hashlib
import json
import pathlib
import re
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
arch = sys.argv[2]
base_manifest = pathlib.Path(sys.argv[3])
numpy_before = json.loads(pathlib.Path(sys.argv[4]).read_text(encoding="utf-8"))
expected_platform = "macos-arm64" if arch == "arm64" else "macos-x64"
tree = hashlib.sha256()
for relative, identity in sorted(numpy_before.items()):
    if isinstance(identity, dict):
        record = f"L\0{relative}\0{identity['symlink']}\n"
    else:
        record = f"F\0{relative}\0{identity}\n"
    tree.update(record.encode("utf-8"))
numpy_identity = manifest.get("numpy", {})
base_tcl_tk = json.loads(base_manifest.read_text(encoding="utf-8"))["tcl_tk"]
if (
    manifest.get("format_version") != 1
    or manifest.get("platform") != expected_platform
    or manifest.get("python_abi") != "cp314"
    or manifest.get("profile") != "fenics"
    or manifest.get("tcl_tk") != base_tcl_tk
    or manifest.get("producer", {}).get("repository") != "precsim/python-numpy-fenics"
    or numpy_identity.get("version") != "2.5.2"
    or not numpy_identity.get("producer_payload_excluded")
    or numpy_identity.get("base_manifest_sha256") != hashlib.sha256(base_manifest.read_bytes()).hexdigest()
    or numpy_identity.get("base_tree_sha256") != tree.hexdigest()
    or numpy_identity.get("base_entry_count") != len(numpy_before)
):
    raise SystemExit(f"invalid FEnICS overlay manifest: {manifest!r}")
for entry in manifest.get("files", []):
    path = str(entry.get("path", ""))
    if re.match(
        r"^fenics-runtime/conda/lib/python3\.14/site-packages/"
        r"(numpy/|numpy\.libs/|numpy-[^/]+\.dist-info/)",
        path,
    ):
        raise SystemExit(f"overlay illegally owns NumPy payload: {path}")
PY

hash_numpy "$work/numpy-after.json"
cmp "$work/numpy-before.json" "$work/numpy-after.json"
log_phase manifest_and_numpy_identity

tool_bin="$runtime/fenics-runtime/conda/bin"
if [[ "$target_arch" == arm64 ]]; then
    clang_exe="$tool_bin/arm64-apple-darwin20.0.0-clang"
    linker_exe="$tool_bin/arm64-apple-darwin20.0.0-ld"
else
    clang_exe="$tool_bin/x86_64-apple-darwin13.4.0-clang"
    linker_exe="$tool_bin/x86_64-apple-darwin13.4.0-ld"
fi
[[ -x "$clang_exe" ]] || { echo "packaged Clang executable is missing: $clang_exe" >&2; exit 1; }
[[ -x "$linker_exe" ]] || { echo "packaged cctools linker is missing: $linker_exe" >&2; exit 1; }
toolchain_started=$SECONDS
resolved_linker="$("$clang_exe" -B"$tool_bin" -print-prog-name=ld)"
printf 'overlay_toolchain_phase_seconds clang_linker_discovery %s\n' "$((SECONDS - toolchain_started))"
if [[ "$resolved_linker" != /* ]]; then
    resolved_linker="$tool_bin/$resolved_linker"
fi
toolchain_started=$SECONDS
resolved_linker="$(python3 - "$resolved_linker" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).resolve())
PY
)"
expected_linker="$(python3 - "$linker_exe" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).resolve())
PY
)"
printf 'overlay_toolchain_phase_seconds resolved_linker_path_checks %s\n' "$((SECONDS - toolchain_started))"
[[ "$resolved_linker" == "$expected_linker" ]] || {
    echo "packaged Clang did not resolve the packaged cctools linker: $resolved_linker != $expected_linker" >&2
    exit 1
}
toolchain_started=$SECONDS
"$clang_exe" --version
printf 'overlay_toolchain_phase_seconds clang_version %s\n' "$((SECONDS - toolchain_started))"
if [[ "$target_arch" == x86_64 && "$(uname -m)" == arm64 ]]; then
    warmup_sdkroot="$(/usr/bin/xcrun --sdk macosx --show-sdk-path)"
    [[ -d "$warmup_sdkroot" ]] || { echo "macOS SDK root is unavailable: $warmup_sdkroot" >&2; exit 1; }
    warmup_source="$work/rosetta-clang-warmup.c"
    warmup_object="$work/rosetta-clang-warmup.o"
    printf 'int fenics_rosetta_warmup(int value) { return value + 1; }\n' > "$warmup_source"
    toolchain_started=$SECONDS
    "$clang_exe" -B"$tool_bin" -isysroot "$warmup_sdkroot" -mmacosx-version-min=14.0 \
        -x c -std=c17 -O2 -g0 -c "$warmup_source" -o "$warmup_object"
    printf 'overlay_toolchain_phase_seconds clang_rosetta_object_warmup %s\n' "$((SECONDS - toolchain_started))"
fi
printf 'jit_clang=%s\njit_linker=%s\n' "$clang_exe" "$linker_exe"
log_phase compiler_and_linker_validation

cat > "$poison/numpy.py" <<'PY'
raise RuntimeError("ambient numpy poison imported")
PY
cat > "$poison/dolfinx.py" <<'PY'
raise RuntimeError("ambient dolfinx poison imported")
PY
cat > "$poison/fenics_embed_runtime.py" <<'PY'
raise RuntimeError("ambient fenics bootstrap poison imported")
PY
cat > "$poison/sitecustomize.py" <<'PY'
raise RuntimeError("ambient sitecustomize poison imported")
PY

unset CC CXX CPP LD LDSHARED SDKROOT CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH \
    LIBRARY_PATH DYLD_LIBRARY_PATH DYLD_FALLBACK_LIBRARY_PATH \
    PETSC_DIR FENICS_JIT_ROOT FENICS_JIT_COMPILER FENICS_JIT_CLANG \
    FENICS_JIT_LINKER CONDA_PREFIX || true
export PYTHONPATH="$poison"
export PYTHONUSERBASE="$poison"
export PYTHONNOUSERSITE=1
export PYTHONHOME="$runtime/Python.framework/Versions/3.14"
export XDG_CACHE_HOME="$cache"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"
export TCL_LIBRARY="$runtime/tcl/tcl8.6"
export TK_LIBRARY="$runtime/tcl/tk8.6"
[[ -f "$TCL_LIBRARY/init.tcl" && -f "$TK_LIBRARY/tk.tcl" ]] || {
    echo "qualified public Tcl/Tk 8.6.18 script providers are missing after composition" >&2
    exit 1
}

# -I would ignore PYTHONHOME. -S keeps the relocated PYTHONHOME effective while
# preventing ambient sitecustomize execution; load the bootstrap by exact path
# so the poisoned PYTHONPATH cannot shadow it before activation.
"${python_cmd[@]}" -S -P - "$repo_root/tests/fenics/test_poisson.py" "$runtime" "$target_arch" <<'PY'
import _imp
import importlib.util
import os
import platform
from pathlib import Path
import sys
import time

phase_start = time.monotonic()

def log_phase(name):
    global phase_start
    now = time.monotonic()
    print(f"overlay_qualification_python_phase_seconds {name} {now - phase_start:.3f}", flush=True)
    phase_start = now

def log_import_phase(name, started):
    print(
        f"overlay_runtime_import_seconds {name} {time.monotonic() - started:.3f}",
        flush=True,
    )

native_extension_targets = {
    "basix._basixcpp",
    "dolfinx.cpp",
    "mpi4py.MPI",
    "numpy._core._multiarray_umath",
    "petsc4py.PETSc",
}
original_create_dynamic = _imp.create_dynamic
original_exec_dynamic = _imp.exec_dynamic

def profiled_create_dynamic(spec):
    if spec.name not in native_extension_targets:
        return original_create_dynamic(spec)
    started = time.monotonic()
    try:
        return original_create_dynamic(spec)
    finally:
        elapsed = time.monotonic() - started
        print(
            f"overlay_native_extension_seconds {spec.name} create_dynamic {elapsed:.3f}",
            flush=True,
        )
        print(f"overlay_native_extension_origin {spec.name} {spec.origin}", flush=True)

def profiled_exec_dynamic(module):
    module_spec = getattr(module, "__spec__", None)
    name = getattr(module_spec, "name", None) or getattr(module, "__name__", "<unknown>")
    if name not in native_extension_targets:
        return original_exec_dynamic(module)
    started = time.monotonic()
    try:
        return original_exec_dynamic(module)
    finally:
        elapsed = time.monotonic() - started
        print(
            f"overlay_native_extension_seconds {name} exec_dynamic {elapsed:.3f}",
            flush=True,
        )

script = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).resolve()
expected_arch = sys.argv[3]
if Path(sys.prefix).resolve() != root / "Python.framework/Versions/3.14":
    raise SystemExit(f"unexpected relocated prefix: {sys.prefix}")
if platform.machine() != expected_arch:
    raise SystemExit(f"unexpected runtime architecture: {platform.machine()} != {expected_arch}")
if sys.version_info[:3] != (3, 14, 10):
    raise SystemExit(f"unexpected runtime Python version: {platform.python_version()}")
bootstrap = (
    root
    / "Python.framework"
    / "Versions"
    / "3.14"
    / "lib"
    / "python3.14"
    / "site-packages"
    / "fenics_embed_runtime.py"
)
spec = importlib.util.spec_from_file_location("fenics_embed_runtime", bootstrap)
if spec is None or spec.loader is None:
    raise SystemExit(f"cannot load staged FEniCS bootstrap: {bootstrap}")
fenics_embed_runtime = importlib.util.module_from_spec(spec)
sys.modules["fenics_embed_runtime"] = fenics_embed_runtime
spec.loader.exec_module(fenics_embed_runtime)
activated = Path(fenics_embed_runtime.activate()).resolve()
log_phase("bootstrap_activation")
if activated != root:
    raise SystemExit(f"unexpected activated root: {activated} != {root}")
import_started = time.monotonic()
import _tkinter
log_import_phase("_tkinter", import_started)
import_started = time.monotonic()
import tkinter
log_import_phase("tkinter", import_started)

import_started = time.monotonic()
if _tkinter.TCL_VERSION != "8.6" or _tkinter.TK_VERSION != "8.6":
    raise SystemExit("FEniCS overlay changed inherited Tcl/Tk API versions")
tcl = tkinter.Tcl()
if tcl.eval("info patchlevel") != "8.6.18":
    raise SystemExit("FEniCS overlay changed the inherited Tcl runtime version")
tcl_library = Path(tcl.eval("info library")).resolve()
if root not in tcl_library.parents:
    raise SystemExit(f"FEniCS overlay Tcl scripts escaped the public runtime: {tcl_library}")
tool_names = {
    "arm64": ("arm64-apple-darwin20.0.0-clang", "arm64-apple-darwin20.0.0-ld"),
    "x86_64": ("x86_64-apple-darwin13.4.0-clang", "x86_64-apple-darwin13.4.0-ld"),
}[expected_arch]
for variable, tool_name in zip(("FENICS_JIT_CLANG", "FENICS_JIT_LINKER"), tool_names):
    value = Path(os.environ.get(variable, "")).resolve()
    expected = root / "fenics-runtime" / "conda" / "bin" / tool_name
    if value != expected.resolve():
        raise SystemExit(f"{variable} escaped relocated runtime: {value} != {expected}")
cc = Path(os.environ.get("CC", "")).resolve()
if not cc.is_file():
    raise SystemExit(f"FEniCS JIT compiler wrapper is missing: {cc}")
if Path(os.environ.get("CXX", "")).resolve() != cc:
    raise SystemExit("CXX does not use the FEniCS JIT compiler wrapper")
if os.environ.get("CPP") != f"{cc} -E":
    raise SystemExit("CPP does not use the FEniCS JIT compiler wrapper")
if os.environ.get("LDSHARED") != f"{cc} -bundle -undefined dynamic_lookup":
    raise SystemExit("LDSHARED does not use the FEniCS JIT compiler wrapper")
if os.environ.get("MACOSX_DEPLOYMENT_TARGET") != "14.0":
    raise SystemExit("FEniCS JIT deployment target is not macOS 14.0")
private_prefix = root / "fenics-runtime" / "conda"
if Path(os.environ.get("PETSC_DIR", "")).resolve() != private_prefix.resolve():
    raise SystemExit("PETSC_DIR escaped the relocated private FEniCS prefix")
if os.environ.get("PETSC_ARCH", "") != "":
    raise SystemExit("PETSC_ARCH must be empty for the relocated FEniCS runtime")
log_import_phase("tcl_and_toolchain_checks", import_started)
_imp.create_dynamic = profiled_create_dynamic
_imp.exec_dynamic = profiled_exec_dynamic
try:
    import_started = time.monotonic()
    import basix
    log_import_phase("basix", import_started)
    import_started = time.monotonic()
    import numpy
    log_import_phase("numpy", import_started)
    import_started = time.monotonic()
    import dolfinx
    log_import_phase("dolfinx", import_started)
finally:
    _imp.create_dynamic = original_create_dynamic
    _imp.exec_dynamic = original_exec_dynamic
import_started = time.monotonic()
import ffcx
log_import_phase("ffcx", import_started)
import_started = time.monotonic()
import mpi4py
log_import_phase("mpi4py", import_started)
import_started = time.monotonic()
import petsc4py
log_import_phase("petsc4py", import_started)
import_started = time.monotonic()
import ufl
log_import_phase("ufl", import_started)

import_started = time.monotonic()
expected_versions = {
    "NumPy": (numpy.__version__, "2.5.2"),
    "DOLFINx": (dolfinx.__version__, "0.11.0"),
    "FFCx": (ffcx.__version__, "0.11.0"),
    "Basix": (basix.__version__, "0.11.0"),
    "UFL": (ufl.__version__, "2026.1.0"),
    "mpi4py": (mpi4py.__version__, "4.1.2"),
    "petsc4py": (petsc4py.__version__, "3.25.6"),
}
for name, (actual, expected) in expected_versions.items():
    if actual != expected:
        raise SystemExit(f"unexpected {name} version: {actual} != {expected}")
if root not in Path(numpy.__file__).resolve().parents:
    raise SystemExit(f"NumPy escaped public runtime: {numpy.__file__}")
if root not in Path(dolfinx.__file__).resolve().parents:
    raise SystemExit(f"DOLFINx escaped public runtime: {dolfinx.__file__}")
log_import_phase("version_and_path_checks", import_started)
log_phase("imported_runtime_and_version_checks")
print("fenics_public_overlay_import=pass")
code = compile(script.read_text(encoding="utf-8"), str(script), "exec")
exec(code, {"__name__": "__main__", "__file__": str(script)})
log_phase("poisson_jit_and_petsc_solve")
PY
log_phase relocated_python_qualification

jit_modules="$work/jit-modules.txt"
find "$cache" -type f -name '*.so' -print | sort > "$jit_modules"
if [[ ! -s "$jit_modules" ]]; then
    echo "fresh FFCx JIT produced no Mach-O extension modules" >&2
    exit 1
fi

while IFS= read -r module; do
    module_file="$(file "$module")"
    echo "$module_file"
    grep -q "$target_arch" <<< "$module_file"
    module_dependencies="$(otool -L "$module")"
    echo "$module_dependencies"
    if grep -E '/opt/homebrew|/usr/local|Cellar' <<< "$module_dependencies"; then
        echo "fresh JIT module links to an ambient package-manager path: $module" >&2
        exit 1
    fi
    minos="$(otool -l "$module" | awk '$1 == "minos" {print $2; exit}')"
    [[ "$minos" == 14.0 ]] || {
        echo "fresh JIT module has unexpected macOS minimum $minos: $module" >&2
        exit 1
    }
    echo "jit_module_minos=$minos module=$module"
done < "$jit_modules"
log_phase jit_module_linkage_audit

echo "fenics_public_overlay_test=pass"
echo "fenics_numpy_base_identity=pass"
echo "fenics_target_arch=$target_arch"
