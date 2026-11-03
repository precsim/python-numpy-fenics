#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: scripts/linux/test-fenics-overlay.sh --archive <overlay.tar.gz> --base-runtime <numpy-runtime> --base-qualification <qualification.json> [--evidence <standalone-qualification.json>]" >&2
    exit 2
}

archive=""
base_runtime=""
base_qualification=""
evidence=""
while (($#)); do
    case "$1" in
        --archive) archive="$2"; shift 2 ;;
        --base-runtime) base_runtime="$2"; shift 2 ;;
        --base-qualification) base_qualification="$2"; shift 2 ;;
        --evidence) evidence="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

[[ -f "$archive" ]] || { echo "FEniCS overlay archive is missing: $archive" >&2; exit 1; }
[[ -d "$base_runtime" ]] || { echo "NumPy base runtime is missing: $base_runtime" >&2; exit 1; }
[[ -f "$base_qualification" ]] || { echo "NumPy base qualification is missing: $base_qualification" >&2; exit 1; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
archive="$(cd "$(dirname "$archive")" && pwd -P)/$(basename "$archive")"
base_runtime="$(cd "$base_runtime" && pwd -P)"
base_qualification="$(cd "$(dirname "$base_qualification")" && pwd -P)/$(basename "$base_qualification")"

for tool in bash cmp cp find getconf grep ldd python3 readelf sha256sum tar; do
    command -v "$tool" >/dev/null 2>&1 || { echo "$tool is required" >&2; exit 1; }
done
[[ "$(getconf GNU_LIBC_VERSION 2>/dev/null || true)" == "glibc 2.17" ]] || {
    echo "Linux FEniCS qualification must execute on glibc 2.17" >&2
    exit 1
}
[[ "$(uname -m)" == x86_64 ]] || { echo "Linux FEniCS qualification requires x86_64" >&2; exit 1; }

base_manifest="$base_runtime/runtime-manifest.json"
base_core_provenance="$base_runtime/build-provenance-linux.json"
base_provenance="$base_runtime/build-provenance-linux-numpy.json"
base_numpy_payload="$base_runtime/numpy-payload-manifest-linux.json"
for required in "$base_manifest" "$base_core_provenance" "$base_provenance" "$base_numpy_payload" "$base_runtime/bin/python3.14"; do
    [[ -e "$required" ]] || { echo "qualified NumPy base input is missing: $required" >&2; exit 1; }
done
verify_inherited_base_tree() {
    local root="$1"
    local allow_extra="$2"
    python3 - "$root" "$base_manifest" "$allow_extra" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
manifest_path = pathlib.Path(sys.argv[2]).resolve()
allow_extra = sys.argv[3] == "yes"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

expected = {}
for entry in manifest.get("files", []):
    rel = pathlib.PurePosixPath(str(entry.get("path", "")))
    if not rel.parts or rel.is_absolute() or ".." in rel.parts:
        raise SystemExit(f"unsafe inherited-base manifest path: {rel}")
    key = rel.as_posix()
    if key == "runtime-manifest.json" or key in expected:
        raise SystemExit(f"ambiguous inherited-base manifest path: {key}")
    expected[key] = entry
if not expected:
    raise SystemExit("inherited-base runtime manifest has no file inventory")

actual = set()
for path in root.rglob("*"):
    if path.is_dir() and not path.is_symlink():
        continue
    rel = path.relative_to(root).as_posix()
    if rel == "runtime-manifest.json":
        continue
    actual.add(rel)
missing = sorted(set(expected) - actual)
extra = sorted(actual - set(expected))
if missing or (extra and not allow_extra):
    raise SystemExit(f"inherited-base tree mismatch: missing={missing!r} extra={extra!r}")

for rel, entry in expected.items():
    path = root.joinpath(*pathlib.PurePosixPath(rel).parts)
    if entry.get("kind") == "symlink":
        if not path.is_symlink() or os.readlink(path) != entry.get("target"):
            raise SystemExit(f"inherited-base symlink changed: {rel}")
    elif entry.get("kind") == "file":
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"inherited-base file changed type: {rel}")
        if path.stat().st_size != entry.get("bytes") or sha256(path) != entry.get("sha256"):
            raise SystemExit(f"inherited-base file bytes changed: {rel}")
    else:
        raise SystemExit(f"unsupported inherited-base manifest kind: {rel}: {entry.get('kind')!r}")

staged_manifest = root / "runtime-manifest.json"
if not staged_manifest.is_file() or staged_manifest.read_bytes() != manifest_path.read_bytes():
    raise SystemExit("inherited runtime-manifest.json changed")
print("inherited_base_manifest_identity=pass")
PY
}
python3 - "$base_manifest" "$base_core_provenance" "$base_provenance" "$base_qualification" <<'PY'
import hashlib
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
core_provenance = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
provenance = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
qualification = json.loads(pathlib.Path(sys.argv[4]).read_text(encoding="utf-8"))
if (
    manifest.get("format_version") != 1
    or manifest.get("platform") != "linux-x64"
    or manifest.get("profile") != "numpy"
    or manifest.get("python_abi") != "cp314"
    or manifest.get("producer", {}).get("repository") != "precsim/python-numpy-fenics"
):
    raise SystemExit(f"invalid public NumPy base manifest: {manifest!r}")
tcl_tk = manifest.get("tcl_tk")
if (
    not isinstance(tcl_tk, dict)
    or tcl_tk.get("version") != "8.6.18"
    or tcl_tk.get("ownership") != "producer"
    or not isinstance(tcl_tk.get("provenance_path"), str)
):
    raise SystemExit(f"invalid public NumPy Tcl/Tk contract: {tcl_tk!r}")
tcl_tk_path = pathlib.Path(sys.argv[1]).parent.joinpath(*pathlib.PurePosixPath(tcl_tk["provenance_path"]).parts)
if (
    not tcl_tk_path.is_file()
    or tcl_tk_path.is_symlink()
    or hashlib.sha256(tcl_tk_path.read_bytes()).hexdigest() != tcl_tk.get("provenance_sha256")
):
    raise SystemExit("public NumPy Tcl/Tk provenance identity mismatch")
baseline = provenance.get("baseline", {})
if baseline.get("architecture") != "x86_64" or str(baseline.get("glibc")) != "2.17":
    raise SystemExit(f"invalid public NumPy base ABI baseline: {baseline!r}")
checks = qualification.get("checks")
required_checks = {
    "pinned_manylinux",
    "glibc_floor",
    "inherited_core_identity",
    "numpy_source_build",
    "private_openblas",
    "blas_lapack_functional",
    "native_closure",
}
if (
    qualification.get("format_version") != 1
    or qualification.get("producer_repository") != "precsim/python-numpy-fenics"
    or qualification.get("platform") != "linux-x64"
    or qualification.get("profile") != "numpy"
    or qualification.get("producer_revision") != manifest.get("producer", {}).get("revision")
    or not isinstance(checks, dict)
    or any(checks.get(name) != "pass" for name in required_checks)
):
    raise SystemExit(f"invalid public NumPy qualification: {qualification!r}")
if provenance.get("numpy", {}).get("version") != "2.5.2":
    raise SystemExit("public NumPy base provenance does not identify NumPy 2.5.2")
if core_provenance.get("python", {}).get("version") != "3.14.10":
    raise SystemExit("public NumPy base provenance does not identify CPython 3.14.10")
PY

work="$(mktemp -d "${RUNNER_TEMP:-/tmp}/fenics linux overlay test.XXXXXX")"
runtime="$work/runtime with spaces"
cache="$work/fresh jit cache"
poison="$work/hostile ambient state"
mkdir -p "$runtime" "$cache" "$poison/bin" "$poison/python" "$poison/native" "$poison/home"
trap 'rm -rf "$work"' EXIT
cp -a "$base_runtime"/. "$runtime"/
verify_inherited_base_tree "$runtime" no

site_packages="$runtime/lib/python3.14/site-packages"
staged_python="$runtime/bin/python3.14"
[[ -x "$staged_python" && -d "$site_packages/numpy" ]] || {
    echo "public NumPy base runtime is incomplete" >&2
    exit 1
}

hash_numpy() {
    "$staged_python" -I -S - "$site_packages" "$1" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

site = pathlib.Path(sys.argv[1])
output = pathlib.Path(sys.argv[2])
roots = [site / "numpy", site / "numpy.libs", *sorted(site.glob("numpy-*.dist-info"))]
entries = {}
for root in roots:
    if not root.exists():
        continue
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(site).as_posix()
        if path.is_symlink():
            entries[rel] = {"symlink": os.readlink(path)}
        elif path.is_file():
            entries[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
if not entries:
    raise SystemExit("public NumPy base has no retained NumPy payload")
output.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

hash_numpy "$work/numpy-before.json"
tar -xzf "$archive" -C "$runtime"
hash_numpy "$work/numpy-after.json"
cmp "$work/numpy-before.json" "$work/numpy-after.json"
verify_inherited_base_tree "$runtime" yes

overlay_manifest="$runtime/fenics-embed-manifest.json"
[[ -f "$overlay_manifest" ]] || { echo "FEniCS overlay manifest is missing" >&2; exit 1; }
python3 - "$overlay_manifest" "$work/numpy-after.json" "$runtime" "$base_manifest" "$base_core_provenance" "$base_provenance" "$base_numpy_payload" "$base_qualification" <<'PY'
import hashlib
import json
import pathlib
import re
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
numpy_entries = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
root = pathlib.Path(sys.argv[3]).resolve()
base_manifest = pathlib.Path(sys.argv[4])
base_core_provenance = pathlib.Path(sys.argv[5])
base_provenance = pathlib.Path(sys.argv[6])
base_numpy_payload = pathlib.Path(sys.argv[7])
base_qualification = pathlib.Path(sys.argv[8])
base_manifest_data = json.loads(base_manifest.read_text(encoding="utf-8"))

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def rooted_file(relative, label):
    path = pathlib.PurePosixPath(str(relative))
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"unsafe native rebuild candidate {label} path: {relative!r}")
    return root.joinpath(*path.parts)

producer = manifest.get("producer", {})
candidate = producer.get("native_rebuild_candidate")
aggregate = hashlib.sha256()
for rel, identity in sorted(numpy_entries.items()):
    if isinstance(identity, dict):
        aggregate.update(b"L\0" + rel.encode() + b"\0" + identity["symlink"].encode() + b"\n")
    else:
        aggregate.update(b"F\0" + rel.encode() + b"\0" + identity.encode() + b"\n")
if (
    manifest.get("format_version") != 1
    or manifest.get("platform") != "linux-x64"
    or manifest.get("profile") != "fenics"
    or manifest.get("architecture") != "x86_64"
    or manifest.get("python_abi") != "cp314"
    or manifest.get("consumer_python") != "3.14.10"
    or manifest.get("stock_package_python_abi") != "cp312"
    or manifest.get("tcl_tk") != base_manifest_data.get("tcl_tk")
    or producer.get("repository") != "precsim/python-numpy-fenics"
    or manifest.get("numpy", {}).get("version") != "2.5.2"
    or manifest.get("numpy", {}).get("producer_payload_excluded") is not True
    or manifest.get("numpy", {}).get("base_tree_sha256") != aggregate.hexdigest()
    or manifest.get("native", {}).get("glibc_ceiling") != "2.17"
    or manifest.get("native", {}).get("glibcxx_ceiling") != "3.4.19"
    or manifest.get("native", {}).get("cxxabi_ceiling") != "1.3.7"
    or manifest.get("native", {}).get("gcc_ceiling") != "4.8.0"
    or manifest.get("native", {}).get("mpi", {}).get("implementation") != "mpich"
    or manifest.get("native", {}).get("mpi", {}).get("provider_count") != 1
    or producer.get("source_rebuilds") != []
):
    raise SystemExit(f"invalid Linux FEniCS overlay manifest: {manifest!r}")
if candidate is None:
    if producer.get("omitted_packages") not in (None, []):
        raise SystemExit("stock overlay unexpectedly declares native-rebuild omissions")
else:
    expected_qualification = {
        "full_native_closure": "not-run",
        "hosted_linux_glibc217": "not-run",
        "jit": "not-run",
        "poisson": "not-run",
        "relocation": "not-run",
    }
    if (
        candidate.get("status") != "candidate-unqualified"
        or candidate.get("source_rebuild_provenance_eligible") is not False
        or candidate.get("qualification") != expected_qualification
        or set(producer.get("omitted_packages", [])) != {"kahip", "libadios2", "zeromq"}
    ):
        raise SystemExit(f"invalid unqualified native rebuild candidate metadata: {candidate!r}")
    handoff = candidate.get("handoff", {})
    stock_preflight = candidate.get("stock_abi_preflight", {})
    for label, record in (("handoff", handoff), ("stock ABI preflight", stock_preflight)):
        relative = record.get("path")
        expected_sha = record.get("sha256")
        if not isinstance(relative, str) or not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha or "")):
            raise SystemExit(f"native rebuild candidate has invalid {label} identity: {record!r}")
        path = rooted_file(relative, label)
        if not path.is_file() or sha(path) != expected_sha:
            raise SystemExit(f"native rebuild candidate {label} hash mismatch: {path}")
    replacements = candidate.get("replacements")
    if not isinstance(replacements, list) or not replacements:
        raise SystemExit("native rebuild candidate has no staged replacements")
    destinations = [str(item.get("destination", "")) for item in replacements if isinstance(item, dict)]
    if len(destinations) != len(replacements) or destinations != sorted(destinations) or len(destinations) != len(set(destinations)):
        raise SystemExit("native rebuild candidate replacement destinations are not uniquely sorted")
    for item in replacements:
        relative = pathlib.PurePosixPath(str(item["destination"]))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise SystemExit(f"unsafe native rebuild replacement destination: {relative}")
        path = root.joinpath("fenics-runtime", "conda", *relative.parts)
        if not path.is_file() or sha(path) != item.get("sha256") or path.stat().st_size != item.get("bytes"):
            raise SystemExit(f"native rebuild candidate replacement identity mismatch: {relative}")
toolchain = manifest.get("jit_toolchain", {})
packages = toolchain.get("packages", {})
for name in (
    "gcc",
    "gcc_impl_linux-64",
    "conda-gcc-specs",
    "ld_impl_linux-64",
    "binutils_impl_linux-64",
    "sysroot_linux-64",
    "kernel-headers_linux-64",
):
    if name not in packages or not packages[name].get("sha256"):
        raise SystemExit(f"overlay manifest is missing locked JIT toolchain identity: {name}")
if packages["sysroot_linux-64"].get("version") != "2.17":
    raise SystemExit("overlay manifest does not bind the glibc-2.17 JIT sysroot")
specs = toolchain.get("conda_specs_template", {})
if specs.get("prefix_token") != "@FENICS_PRIVATE_PREFIX@" or not specs.get("prefix_replacement_count"):
    raise SystemExit(f"overlay manifest has invalid conda GCC specs relocation evidence: {specs!r}")
for entry in manifest.get("files", []):
    path = str(entry.get("path", ""))
    if re.match(
        r"^fenics-runtime/conda/lib/python3\.12/site-packages/"
        r"(numpy/|numpy\.libs/|numpy-[^/]+\.dist-info/)",
        path,
    ):
        raise SystemExit(f"overlay illegally owns NumPy payload: {path}")
for required in (
    manifest.get("provenance", {}).get("packages"),
    manifest.get("provenance", {}).get("sources"),
    manifest.get("native", {}).get("census"),
    manifest.get("licenses", {}).get("inventory"),
):
    if not required:
        raise SystemExit("overlay manifest is missing required evidence paths")
package_provenance_path = root / manifest["provenance"]["packages"]
source_provenance_path = root / manifest["provenance"]["sources"]
census_path = root / manifest["native"]["census"]
license_path = root / manifest["licenses"]["inventory"]
for path, expected in (
    (package_provenance_path, manifest["provenance"]["packages_sha256"]),
    (source_provenance_path, manifest["provenance"]["sources_sha256"]),
    (census_path, manifest["native"]["census_sha256"]),
    (license_path, manifest["licenses"]["inventory_sha256"]),
):
    if not path.is_file() or sha(path) != expected:
        raise SystemExit(f"overlay evidence hash mismatch: {path}")
source_provenance = json.loads(source_provenance_path.read_text(encoding="utf-8"))
base = source_provenance.get("numpy_base", {})
for path, key in (
    (base_manifest, "manifest_sha256"),
    (base_core_provenance, "core_provenance_sha256"),
    (base_provenance, "provenance_sha256"),
    (base_numpy_payload, "numpy_payload_manifest_sha256"),
    (base_qualification, "qualification_sha256"),
):
    if base.get(key) != sha(path):
        raise SystemExit(f"overlay does not bind supplied NumPy base evidence: {key}")
package_provenance = json.loads(package_provenance_path.read_text(encoding="utf-8"))
if package_provenance.get("solver_used_by_packager") is not False or package_provenance.get("source_rebuilds") != []:
    raise SystemExit("overlay provenance indicates solver use or source rebuilding")
if candidate is None:
    if package_provenance.get("native_rebuild_candidate") is not None or source_provenance.get("native_rebuild_candidate") is not None:
        raise SystemExit("stock overlay provenance unexpectedly contains a native rebuild candidate")
else:
    if package_provenance.get("native_rebuild_candidate") != candidate or source_provenance.get("native_rebuild_candidate") != candidate:
        raise SystemExit("native rebuild candidate identity differs across manifest and provenance")
relocation = package_provenance.get("prefix_relocation", {})
entries = relocation.get("entries")
entry_count = relocation.get("entry_count")
text_entry_count = relocation.get("text_entry_count")
binary_entry_count = relocation.get("binary_entry_count")
if (
    relocation.get("format_version") != 1
    or relocation.get("generic_marker") != "@FENICS_RUNTIME_PREFIX@"
    or not isinstance(entries, list)
    or not all(isinstance(value, int) and value >= 0 for value in (entry_count, text_entry_count, binary_entry_count))
    or len(entries) != entry_count
    or text_entry_count + binary_entry_count != entry_count
    or not entries
):
    raise SystemExit(f"invalid metadata-driven prefix relocation evidence: {relocation!r}")
if candidate is None and (
    entry_count != 396
    or text_entry_count != 173
    or binary_entry_count != 223
):
    raise SystemExit(f"stock prefix relocation inventory changed unexpectedly: {relocation!r}")
by_path = {entry.get("path"): entry for entry in entries if isinstance(entry, dict)}
if len(by_path) != len(entries):
    raise SystemExit("prefix relocation provenance contains duplicate or invalid paths")
replacement_paths = set()
if candidate is not None:
    replacement_paths = {
        (pathlib.PurePosixPath("fenics-runtime/conda") / pathlib.PurePosixPath(item["destination"])).as_posix()
        for item in candidate["replacements"]
    }
    overlap = sorted(replacement_paths.intersection(by_path))
    if overlap:
        raise SystemExit(f"rebuilt replacement paths were also prefix-relocated from stock metadata: {overlap!r}")
required_relocations = {
    "fenics-runtime/conda/bin/x86_64-conda-linux-gnu.cfg": ("sysroot_linux-64", "text", "@FENICS_RUNTIME_PREFIX@"),
    "fenics-runtime/conda/lib/gcc/x86_64-conda-linux-gnu/16.2.0/conda.specs": ("conda-gcc-specs", "text", "@FENICS_PRIVATE_PREFIX@"),
    "fenics-runtime/conda/lib/python3.14/site-packages/dolfinx/cpp.abi3.so": ("fenics-dolfinx", "binary", "@FENICS_RUNTIME_PREFIX@"),
    "fenics-runtime/conda/lib/python3.14/site-packages/slepc4py/lib/slepc.cfg": ("slepc4py", "text", "@FENICS_RUNTIME_PREFIX@"),
}
for relative, expected in required_relocations.items():
    if relative in replacement_paths:
        continue
    entry = by_path.get(relative)
    actual = None if entry is None else (entry.get("package"), entry.get("mode"), entry.get("replacement"))
    if actual != expected:
        raise SystemExit(f"required prefix relocation evidence mismatch: {relative}: {actual!r} != {expected!r}")
for relative, entry in by_path.items():
    path = root / relative
    if not path.is_file() or sha(path) != entry.get("final_sha256"):
        raise SystemExit(f"prefix relocation final hash mismatch: {relative}")
    if not entry.get("occurrences") or not entry.get("placeholder_bytes"):
        raise SystemExit(f"prefix relocation evidence is incomplete: {relative}: {entry!r}")
    for key in ("cache_sha256", "installed_sha256_in_prefix", "prefix_relocated_sha256", "final_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get(key, ""))):
            raise SystemExit(f"prefix relocation evidence has invalid {key}: {relative}: {entry!r}")
cfg = root / "fenics-runtime" / "conda" / "bin" / "x86_64-conda-linux-gnu.cfg"
expected_cfg = "--sysroot=@FENICS_RUNTIME_PREFIX@/x86_64-conda-linux-gnu/sysroot"
if not cfg.is_file() or cfg.read_text(encoding="utf-8").strip() != expected_cfg:
    raise SystemExit(f"protected JIT target config was not metadata-relocated: {cfg}")
if (root / "fenics-runtime" / "conda" / "bin" / "ffcx").exists():
    raise SystemExit("generated producer-prefix-bound ffcx command script was retained")
excluded = set(manifest.get("producer", {}).get("excluded_packages", []))
for entry in manifest.get("files", []):
    if excluded.intersection(entry.get("owners", [])):
        raise SystemExit(f"overlay retains a producer-excluded package-owned file: {entry}")
PY

python3 - "$overlay_manifest" "$runtime" "$poison/native" <<'PY'
import json
from pathlib import Path
import sys

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path(sys.argv[2]).resolve()
poison = Path(sys.argv[3])
census = json.loads((root / manifest["native"]["census"]).read_text(encoding="utf-8"))
sonames = {"libstdc++.so.6"}
mpi_provider = manifest["native"]["mpi"]["provider"]
for record in census.get("census", []):
    if record.get("path") == mpi_provider and record.get("soname"):
        sonames.add(record["soname"])
    if "openblas" in Path(record.get("path", "")).name.lower() and record.get("soname"):
        sonames.add(record["soname"])
if len(sonames) < 3:
    raise SystemExit(f"unable to identify native-provider poison SONAMEs from census: {sorted(sonames)}")
for soname in sorted(sonames):
    (poison / soname).write_bytes(b"hostile ambient native provider\n")
print("hostile_native_sonames=" + ",".join(sorted(sonames)))
PY

cat > "$poison/python/numpy.py" <<'PY'
raise RuntimeError("ambient numpy poison imported")
PY
cat > "$poison/python/dolfinx.py" <<'PY'
raise RuntimeError("ambient dolfinx poison imported")
PY
cat > "$poison/python/petsc4py.py" <<'PY'
raise RuntimeError("ambient petsc4py poison imported")
PY
cat > "$poison/python/fenics_embed_runtime.py" <<'PY'
raise RuntimeError("ambient bootstrap poison imported")
PY
cat > "$poison/python/sitecustomize.py" <<'PY'
raise RuntimeError("ambient sitecustomize poison imported")
PY
for compiler in gcc g++ cc c++ x86_64-conda-linux-gnu-gcc pkg-config ld as ar ranlib; do
    cat > "$poison/bin/$compiler" <<'SH'
#!/usr/bin/env bash
echo "ambient compiler/tool poison executed: $0" >&2
exit 97
SH
    chmod +x "$poison/bin/$compiler"
done

export PYTHONPATH="$poison/python"
export PYTHONUSERBASE="$poison/home"
export PYTHONNOUSERSITE=1
export PYTHONHOME="$runtime"
export XDG_CACHE_HOME="$cache"
export HOME="$poison/home"
export PATH="$poison/bin:/usr/bin:/bin"
export LD_LIBRARY_PATH="$poison/native"
export CC="$poison/bin/gcc"
export CXX="$poison/bin/g++"
export CPP="$poison/bin/gcc -E"
export LDSHARED="$poison/bin/gcc -shared"
export PETSC_DIR="$poison"
export PETSC_ARCH="poison"

probe="$work/runtime-probe.py"
cat > "$probe" <<'PY'
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
bootstrap = root / "lib" / "python3.14" / "site-packages" / "fenics_embed_runtime.py"
spec = importlib.util.spec_from_file_location("fenics_embed_runtime", bootstrap)
if spec is None or spec.loader is None:
    raise SystemExit(f"cannot load staged FEniCS bootstrap: {bootstrap}")
module = importlib.util.module_from_spec(spec)
sys.modules["fenics_embed_runtime"] = module
spec.loader.exec_module(module)
activated = Path(module.activate()).resolve()
if activated != root:
    raise SystemExit(f"unexpected activated runtime root: {activated} != {root}")

private = root / "fenics-runtime" / "conda"
public_site = root / "lib" / "python3.14" / "site-packages"
private_site = private / "lib" / "python3.14" / "site-packages"
if [Path(p).resolve() for p in sys.path[:2]] != [private_site.resolve(), public_site.resolve()]:
    raise SystemExit(f"authoritative Python sites are not first: {sys.path[:4]}")

if mode == "numpy-first":
    import numpy
    from petsc4py import PETSc
else:
    from petsc4py import PETSc
    import numpy
import basix
import dolfinx
import ffcx
import petsc4py
import slepc4py
import ufl
from dolfinx import mesh
from mpi4py import MPI

if MPI.COMM_WORLD.size != 1:
    raise SystemExit(f"serial qualification requires MPI COMM_WORLD size 1, got {MPI.COMM_WORLD.size}")

expected_versions = {
    "numpy": (numpy.__version__, "2.5.2"),
    "dolfinx": (dolfinx.__version__, "0.11.0"),
    "basix": (basix.__version__, "0.11.0"),
    "ffcx": (ffcx.__version__, "0.11.0"),
    "ufl": (ufl.__version__, "2026.1.0"),
    "petsc4py": (petsc4py.__version__, "3.25.6"),
    "slepc4py": (slepc4py.__version__, "3.25.2"),
}
for name, (actual, expected) in expected_versions.items():
    if actual != expected:
        raise SystemExit(f"unexpected {name} version: {actual} != {expected}")
matrix = numpy.array([[3.0, 1.0], [1.0, 2.0]])
rhs = numpy.array([9.0, 8.0])
if not numpy.allclose(numpy.linalg.solve(matrix, rhs), numpy.array([2.0, 3.0])):
    raise SystemExit("public NumPy base numerical probe failed after FEniCS activation")
if root not in Path(numpy.__file__).resolve().parents or private in Path(numpy.__file__).resolve().parents:
    raise SystemExit(f"NumPy escaped the public base runtime: {numpy.__file__}")
for package in (dolfinx, basix, ffcx, ufl, petsc4py, slepc4py):
    if private not in Path(package.__file__).resolve().parents:
        raise SystemExit(f"private FEniCS package escaped overlay: {package.__name__}: {package.__file__}")
if Path(os.environ["PETSC_DIR"]).resolve() != private or os.environ.get("PETSC_ARCH") != "":
    raise SystemExit("PETSc environment was not relocated to private prefix")
if Path(petsc4py.get_config()["PETSC_DIR"]).resolve() != private:
    raise SystemExit("petsc4py.get_config did not follow relocated PETSC_DIR")
if Path(os.environ["SLEPC_DIR"]).resolve() != private:
    raise SystemExit("SLEPc environment was not relocated to private prefix")
slepc_config = slepc4py.get_config()
if (
    Path(slepc_config["SLEPC_DIR"]).resolve() != private
    or Path(slepc_config["PETSC_DIR"]).resolve() != private
    or slepc_config.get("PETSC_ARCH") != ""
):
    raise SystemExit(f"slepc4py.get_config did not follow relocated private prefix: {slepc_config!r}")

cc = Path(os.environ["CC"])
if not cc.is_file() or "hostile ambient state" in str(cc):
    raise SystemExit(f"activation retained ambient compiler: {cc}")
gcc = Path(os.environ["FENICS_JIT_GCC"]).resolve()
expected_gcc = (private / "bin" / "x86_64-conda-linux-gnu-gcc").resolve()
if gcc != expected_gcc or not gcc.is_file():
    raise SystemExit(f"activation did not bind the locked private GCC: {gcc} != {expected_gcc}")
specs_template = Path(os.environ["FENICS_JIT_CONDA_SPECS_TEMPLATE"]).resolve()
expected_specs = private / "lib" / "gcc" / "x86_64-conda-linux-gnu" / "16.2.0" / "conda.specs"
if (
    specs_template != expected_specs.resolve()
    or "@FENICS_PRIVATE_PREFIX@" not in specs_template.read_text(encoding="utf-8")
):
    raise SystemExit(f"relocatable conda GCC specs template is invalid: {specs_template}")
for variable, basename in (
    ("LD", "x86_64-conda-linux-gnu-ld"),
    ("AS", "x86_64-conda-linux-gnu-as"),
    ("AR", "x86_64-conda-linux-gnu-ar"),
    ("RANLIB", "x86_64-conda-linux-gnu-ranlib"),
):
    actual = Path(os.environ[variable]).resolve()
    expected = (private / "bin" / basename).resolve()
    if actual != expected or not actual.is_file():
        raise SystemExit(f"activation did not bind private {variable}: {actual} != {expected}")
sysroot = Path(subprocess.check_output([str(cc), "-print-sysroot"], text=True).strip()).resolve()
expected_sysroot = private / "x86_64-conda-linux-gnu" / "sysroot"
if sysroot != expected_sysroot.resolve():
    raise SystemExit(f"bundled GCC reports wrong sysroot: {sysroot} != {expected_sysroot}")
subprocess.check_call([str(cc), "--version"], stdout=subprocess.DEVNULL)

mesh.create_unit_square(MPI.COMM_SELF, 4, 4)
library_version = MPI.Get_library_version()
if "MPICH" not in library_version.upper():
    raise SystemExit(f"mpi4py did not load MPICH: {library_version}")

mapped = set()
for line in Path("/proc/self/maps").read_text(errors="replace").splitlines():
    fields = line.split()
    if fields and fields[-1].startswith("/"):
        try:
            mapped.add(Path(fields[-1]).resolve())
        except OSError:
            pass
mpi = {path for path in mapped if path.name.startswith("libmpi.so")}
if len(mpi) != 1 or not all(private in path.parents for path in mpi):
    raise SystemExit(f"expected exactly one mapped private MPICH provider: {sorted(map(str, mpi))}")
for path in mapped:
    lower = path.as_posix().lower()
    if any(marker in lower for marker in ("openmpi", "mvapich", "/cuda/", "/rocm/")):
        raise SystemExit(f"forbidden MPI/GPU runtime provider mapped: {path}")

for soname in ("libgcc_s.so.1", "libstdc++.so.6"):
    providers = {
        path
        for path in mapped
        if path.name == soname or path.name.startswith(soname + ".")
    }
    if len(providers) != 1 or not all(private in path.parents for path in providers):
        raise SystemExit(
            f"expected one authoritative private {soname} provider: {sorted(map(str, providers))}"
        )

openblas = {path for path in mapped if "openblas" in path.name.lower()}
numpy_blas = {path for path in openblas if root / "lib" / "numpy-private" in path.parents}
fenics_blas = {path for path in openblas if private in path.parents}
if not numpy_blas or not fenics_blas:
    raise SystemExit(
        "NumPy private and FEniCS generic BLAS providers did not coexist: "
        f"numpy={sorted(map(str, numpy_blas))} fenics={sorted(map(str, fenics_blas))}"
    )
print(f"fenics_import_order_{mode}=pass")
PY

"$staged_python" -S -P "$probe" "$runtime" numpy-first
"$staged_python" -S -P "$probe" "$runtime" petsc-first

poisson_runner="$work/poisson-runner.py"
cat > "$poisson_runner" <<'PY'
import importlib.util
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
script = Path(sys.argv[2]).resolve()
bootstrap = root / "lib" / "python3.14" / "site-packages" / "fenics_embed_runtime.py"
spec = importlib.util.spec_from_file_location("fenics_embed_runtime", bootstrap)
if spec is None or spec.loader is None:
    raise SystemExit(f"cannot load staged FEniCS bootstrap: {bootstrap}")
module = importlib.util.module_from_spec(spec)
sys.modules["fenics_embed_runtime"] = module
spec.loader.exec_module(module)
module.activate()
code = compile(script.read_text(encoding="utf-8"), str(script), "exec")
exec(code, {"__name__": "__main__", "__file__": str(script)})
PY
rm -rf "$cache"/*
"$staged_python" -S -P "$poisson_runner" "$runtime" "$repo_root/tests/fenics/test_poisson.py"

mapfile -t jit_modules < <(find "$cache" -type f -name '*.so' -print | LC_ALL=C sort)
if ((${#jit_modules[@]} == 0)); then
    echo "fresh FFCx JIT produced no native cache module" >&2
    exit 1
fi
python3 - "${jit_modules[@]}" <<'PY'
import pathlib
import re
import subprocess
import sys

maxima = {"GLIBC": None, "GLIBCXX": None, "CXXABI": None, "GCC": None}
ceilings = {
    "GLIBC": (2, 17),
    "GLIBCXX": (3, 4, 19),
    "CXXABI": (1, 3, 7),
    "GCC": (4, 8, 0),
}
allowed_nonnumeric = {"CXXABI_TM_1"}
nonnumeric = set()
violations = []

def numeric(token, namespace):
    prefix = namespace + "_"
    if not token.startswith(prefix):
        return None
    suffix = token[len(prefix):]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", suffix):
        return None
    return tuple(int(part) for part in suffix.split("."))

for raw in sys.argv[1:]:
    path = pathlib.Path(raw)
    text = subprocess.check_output(["readelf", "--version-info", "-W", str(path)], text=True)
    needs = text.split("Version needs section", 1)[1] if "Version needs section" in text else ""
    tokens = set(re.findall(r"\bName: ((?:GLIBC|CXXABI|GLIBCXX|GCC)_[A-Za-z0-9_.]+)", needs))
    for token in tokens:
        matched_numeric = False
        for namespace in maxima:
            value = numeric(token, namespace)
            if value is not None:
                matched_numeric = True
                if maxima[namespace] is None or value > maxima[namespace]:
                    maxima[namespace] = value
            if value is not None and value > ceilings[namespace]:
                violations.append((str(path), token, namespace + "_" + ".".join(map(str, ceilings[namespace]))))
        if not matched_numeric:
            nonnumeric.add(token)
    dynamic = subprocess.check_output(["readelf", "-dW", str(path)], text=True)
    for packed in re.findall(r"\((?:RUNPATH|RPATH)\).*?\[([^\]]*)\]", dynamic):
        for entry in packed.split(":"):
            if entry and not entry.startswith("$ORIGIN"):
                raise SystemExit(f"fresh FFCx JIT module has absolute/non-relative RPATH: {path}: {entry}")
unexpected_nonnumeric = sorted(nonnumeric - allowed_nonnumeric)
if unexpected_nonnumeric:
    raise SystemExit(
        "fresh FFCx JIT module requires unsupported nonnumeric ABI symbol versions: "
        + repr(unexpected_nonnumeric)
    )
if violations:
    raise SystemExit("fresh FFCx JIT module exceeds required native symbol ceilings: " + repr(violations))
for namespace, value in maxima.items():
    print(
        f"fresh_ffcx_jit_{namespace.lower()}_max="
        + ("none" if value is None else ".".join(map(str, value)))
    )
if nonnumeric:
    print("fresh_ffcx_jit_allowed_nonnumeric_versions=" + ",".join(sorted(nonnumeric)))
PY

"$staged_python" -I -S - "$runtime" "$overlay_manifest" <<'PY'
import json
from pathlib import Path
import re
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
census_path = root / manifest["native"]["census"]
census = json.loads(census_path.read_text(encoding="utf-8"))
for record in census.get("census", []):
    path = root / record["path"]
    completed = subprocess.run(
        ["ldd", str(path)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={"PATH": "/usr/bin:/bin"}, check=False,
    )
    if completed.returncode != 0 or "not found" in completed.stdout:
        raise SystemExit(f"runtime ELF dependency resolution failed for {path}:\n{completed.stdout}")
    resolved = {}
    for line in completed.stdout.splitlines():
        match = re.match(r"\s*(\S+)\s+=>\s+(\S+)\s+\(", line)
        if match:
            resolved[match.group(1)] = Path(match.group(2)).resolve()
    for dependency in record.get("dependencies", []):
        if dependency.get("resolution") != "bundled":
            continue
        actual = resolved.get(dependency["name"])
        expected = (root / dependency["provider"]).resolve()
        if actual != expected:
            raise SystemExit(
                f"bundled dependency resolved outside overlay: {record['path']} -> "
                f"{dependency['name']} -> {actual} != {expected}"
            )
print("fenics_native_dependency_resolution=pass")
PY

if [[ -n "$evidence" ]]; then
    mkdir -p "$(dirname "$evidence")"
    python3 - "$archive" "$overlay_manifest" "$evidence" <<'PY'
import hashlib
import json
from pathlib import Path
import re
import sys

archive = Path(sys.argv[1]).resolve()
manifest_path = Path(sys.argv[2]).resolve()
output = Path(sys.argv[3]).resolve()
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
candidate = manifest.get("producer", {}).get("native_rebuild_candidate")

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

checks = {
    "glibc_floor": "pass",
    "architecture": "pass",
    "public_numpy_base_identity": "pass",
    "hostile_ambient_state": "pass",
    "relocation": "pass",
    "native_symbol_ceilings": "pass",
    "native_dependency_resolution": "pass",
    "fresh_ffcx_jit": "pass",
    "serial_petsc_poisson": "pass",
}
payload = {
    "format_version": 1,
    "kind": "linux-fenics-standalone-overlay-qualification",
    "artifact": archive.name,
    "artifact_sha256": sha256(archive),
    "manifest_sha256": sha256(manifest_path),
    "platform": "linux-x64",
    "architecture": "x86_64",
    "glibc": "2.17",
    "producer_revision": manifest.get("producer", {}).get("revision"),
    "standalone_runtime_qualified": True,
    "source_rebuild_provenance_eligible": candidate is None,
    "release_eligible": candidate is None,
    "checks": checks,
}
if candidate is not None:
    handoff = candidate.get("handoff", {})
    inputs = candidate.get("inputs")
    native_closure = inputs.get("native_closure") if isinstance(inputs, dict) else None
    binding = native_closure.get("candidate_output_binding_sha256") if isinstance(native_closure, dict) else None
    if not re.fullmatch(r"[0-9a-f]{64}", str(handoff.get("sha256", ""))):
        raise SystemExit("qualified native rebuild candidate has no valid handoff hash")
    if not re.fullmatch(r"[0-9a-f]{64}", str(binding or "")):
        raise SystemExit("qualified native rebuild candidate has no valid output-binding hash")
    payload["native_rebuild_candidate"] = {
        "status": candidate.get("status"),
        "handoff_sha256": handoff["sha256"],
        "candidate_output_binding_sha256": binding,
        "conditional_omissions": [item.get("package") for item in candidate.get("conditional_omissions", [])],
    }
output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"standalone_qualification={output}")
print(f"standalone_runtime_qualified={str(payload['standalone_runtime_qualified']).lower()}")
print(f"release_eligible={str(payload['release_eligible']).lower()}")
PY
fi

echo "fenics_public_overlay_test=pass"
echo "fenics_numpy_base_identity=pass"
echo "fenics_fresh_ffcx_jit=pass"
echo "fenics_serial_petsc_poisson=pass"
