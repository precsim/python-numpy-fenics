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

for tool in ditto file python3 shasum; do
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
    or manifest.get("python_abi") != "cp312"
    or manifest.get("profile") != "numpy"
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
ditto "$base_runtime" "$runtime"

site_packages="$runtime/Python.framework/Versions/3.12/lib/python3.12/site-packages"
staged_python="$runtime/Python.framework/Versions/3.12/bin/python3.12"
[[ -x "$staged_python" && -d "$site_packages/numpy" ]] || {
    echo "public NumPy base runtime is incomplete" >&2
    exit 1
}

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
ditto -x -k "$overlay_zip" "$runtime"

overlay_manifest="$runtime/fenics-embed-manifest.json"
[[ -f "$overlay_manifest" ]] || { echo "FEnICS overlay manifest is missing after extraction" >&2; exit 1; }
python3 - "$overlay_manifest" "$target_arch" <<'PY'
import json
import pathlib
import re
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
arch = sys.argv[2]
expected_platform = "macos-arm64" if arch == "arm64" else "macos-x64"
if (
    manifest.get("format_version") != 1
    or manifest.get("platform") != expected_platform
    or manifest.get("python_abi") != "cp312"
    or manifest.get("profile") != "fenics"
    or manifest.get("producer", {}).get("repository") != "precsim/python-numpy-fenics"
    or manifest.get("numpy", {}).get("version") != "2.5.2"
    or not manifest.get("numpy", {}).get("producer_payload_excluded")
):
    raise SystemExit(f"invalid FEnICS overlay manifest: {manifest!r}")
for entry in manifest.get("files", []):
    path = str(entry.get("path", ""))
    if re.match(
        r"^fenics-runtime/conda/lib/python3\.12/site-packages/"
        r"(numpy/|numpy\.libs/|numpy-[^/]+\.dist-info/)",
        path,
    ):
        raise SystemExit(f"overlay illegally owns NumPy payload: {path}")
PY

hash_numpy "$work/numpy-after.json"
cmp "$work/numpy-before.json" "$work/numpy-after.json"

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
    PETSC_DIR FENICS_JIT_ROOT FENICS_JIT_COMPILER CONDA_PREFIX || true
export PYTHONPATH="$poison"
export PYTHONUSERBASE="$poison"
export PYTHONNOUSERSITE=1
export PYTHONHOME="$runtime/Python.framework/Versions/3.12"
export XDG_CACHE_HOME="$cache"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"

# -I would ignore PYTHONHOME. -S keeps the relocated PYTHONHOME effective while
# preventing ambient sitecustomize execution; load the bootstrap by exact path
# so the poisoned PYTHONPATH cannot shadow it before activation.
"$staged_python" -S -P - "$runtime" <<'PY'
import importlib.util
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
if Path(sys.prefix).resolve() != root / "Python.framework/Versions/3.12":
    raise SystemExit(f"unexpected relocated prefix: {sys.prefix}")
bootstrap = (
    root
    / "Python.framework"
    / "Versions"
    / "3.12"
    / "lib"
    / "python3.12"
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
if activated != root:
    raise SystemExit(f"unexpected activated root: {activated} != {root}")
import numpy
import dolfinx
import petsc4py
from mpi4py import MPI
from dolfinx import mesh

if numpy.__version__ != "2.5.2":
    raise SystemExit(f"unexpected NumPy version: {numpy.__version__}")
if root not in Path(numpy.__file__).resolve().parents:
    raise SystemExit(f"NumPy escaped public runtime: {numpy.__file__}")
if root not in Path(dolfinx.__file__).resolve().parents:
    raise SystemExit(f"DOLFINx escaped public runtime: {dolfinx.__file__}")
mesh.create_unit_square(MPI.COMM_SELF, 4, 4)
print("fenics_public_overlay_import=pass")
PY

"$staged_python" -S -P - "$repo_root/tests/fenics/test_poisson.py" "$runtime" <<'PY'
import importlib.util
from pathlib import Path
import sys

script = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).resolve()
bootstrap = (
    root
    / "Python.framework"
    / "Versions"
    / "3.12"
    / "lib"
    / "python3.12"
    / "site-packages"
    / "fenics_embed_runtime.py"
)
spec = importlib.util.spec_from_file_location("fenics_embed_runtime", bootstrap)
if spec is None or spec.loader is None:
    raise SystemExit(f"cannot load staged FEniCS bootstrap: {bootstrap}")
fenics_embed_runtime = importlib.util.module_from_spec(spec)
sys.modules["fenics_embed_runtime"] = fenics_embed_runtime
spec.loader.exec_module(fenics_embed_runtime)
fenics_embed_runtime.activate()
code = compile(script.read_text(encoding="utf-8"), str(script), "exec")
exec(code, {"__name__": "__main__", "__file__": str(script)})
PY

if ! find "$cache" -type f \( -name '*.so' -o -name '*.dylib' \) -print -quit | grep -q .; then
    echo "fresh FFCx JIT produced no native cache module" >&2
    exit 1
fi

echo "fenics_public_overlay_test=pass"
echo "fenics_numpy_base_identity=pass"
echo "fenics_target_arch=$target_arch"
