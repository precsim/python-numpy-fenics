#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: scripts/macos/stage-numpy-runtime.sh --core-runtime <qualified-core-runtime> --output-runtime <numpy-runtime> --numpy-wheel <wheel> --numpy-url <exact-url> [--arch arm64|x86_64] [--census-output <json>]" >&2
    exit 2
}

core_runtime=""
output_runtime=""
numpy_wheel=""
numpy_url=""
target_arch="arm64"
census_output=""
while (($#)); do
    case "$1" in
        --core-runtime) core_runtime="$2"; shift 2 ;;
        --output-runtime) output_runtime="$2"; shift 2 ;;
        --numpy-wheel) numpy_wheel="$2"; shift 2 ;;
        --numpy-url) numpy_url="$2"; shift 2 ;;
        --arch) target_arch="$2"; shift 2 ;;
        --census-output) census_output="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

[[ "$target_arch" == arm64 || "$target_arch" == x86_64 ]] || {
    echo "unsupported macOS architecture: $target_arch" >&2
    exit 1
}
[[ -d "$core_runtime" ]] || { echo "qualified core runtime is missing: $core_runtime" >&2; exit 1; }
[[ -n "$output_runtime" ]] || { echo "NumPy output runtime is required" >&2; exit 1; }
[[ -f "$numpy_wheel" ]] || { echo "NumPy wheel is missing: $numpy_wheel" >&2; exit 1; }
[[ -n "$numpy_url" ]] || { echo "exact NumPy wheel URL is required" >&2; exit 1; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
for tool in ditto file lipo nm otool python3 shasum; do
    command -v "$tool" >/dev/null 2>&1 || { echo "$tool is required" >&2; exit 1; }
done
core_runtime="$(cd "$core_runtime" && pwd -P)"
output_runtime="$(python3 - "$output_runtime" <<'PY'
import pathlib
import sys

print(pathlib.Path(sys.argv[1]).resolve(strict=False))
PY
)"
python3 - "$core_runtime" "$output_runtime" <<'PY'
import pathlib
import sys

core = pathlib.Path(sys.argv[1]).resolve()
output = pathlib.Path(sys.argv[2]).resolve(strict=False)
if core == output or core in output.parents or output in core.parents:
    raise SystemExit(
        f"NumPy output runtime must not overlap retained core runtime: core={core} output={output}"
    )
PY
mkdir -p "$(dirname "$output_runtime")"
rm -rf "$output_runtime"
ditto "$core_runtime" "$output_runtime"
runtime="$output_runtime"
numpy_wheel="$(cd "$(dirname "$numpy_wheel")" && pwd -P)/$(basename "$numpy_wheel")"
[[ -n "$census_output" ]] || census_output="$runtime/numpy-native-census.json"

case "$target_arch" in
    arm64)
        expected_wheel="numpy-2.5.2-cp312-cp312-macosx_14_0_arm64.whl"
        expected_numpy_url="https://files.pythonhosted.org/packages/f3/f4/29e78102a80601cf034d4e9767022cffeca2c3b4c926e1754572ca95593d/numpy-2.5.2-cp312-cp312-macosx_14_0_arm64.whl"
        expected_numpy_size=5350202
        expected_numpy_sha="6e8172ddfcf5cf74b811d372b570b83c60bd2de87a6fbfbebdadb4a9bd9c6cbb"
        expected_tag="cp312-cp312-macosx_14_0_arm64"
        ;;
    x86_64)
        expected_wheel="numpy-2.5.2-cp312-cp312-macosx_14_0_x86_64.whl"
        expected_numpy_url="https://files.pythonhosted.org/packages/11/4b/dcd3b7eadaf4035d2c7a4289d232523a6964f602598ef7674e4bd7291f93/numpy-2.5.2-cp312-cp312-macosx_14_0_x86_64.whl"
        expected_numpy_size=6687736
        expected_numpy_sha="65f188481f1669e26f62b701e8205d19e460fa4a9b52a1414ba382330e4a3414"
        expected_tag="cp312-cp312-macosx_14_0_x86_64"
        ;;
esac

[[ "$(basename "$numpy_wheel")" == "$expected_wheel" ]] || {
    echo "unexpected NumPy wheel filename: $(basename "$numpy_wheel")" >&2
    exit 1
}
[[ "$numpy_url" == "$expected_numpy_url" ]] || {
    echo "unexpected NumPy wheel URL: $numpy_url" >&2
    exit 1
}
actual_numpy_size="$(python3 - "$numpy_wheel" <<'PY'
import pathlib
import sys
print(pathlib.Path(sys.argv[1]).stat().st_size)
PY
)"
[[ "$actual_numpy_size" == "$expected_numpy_size" ]] || {
    echo "NumPy wheel size mismatch: $actual_numpy_size != $expected_numpy_size" >&2
    exit 1
}
actual_numpy_sha="$(shasum -a 256 "$numpy_wheel" | awk '{print $1}')"
[[ "$actual_numpy_sha" == "$expected_numpy_sha" ]] || {
    echo "NumPy wheel SHA-256 mismatch: $actual_numpy_sha != $expected_numpy_sha" >&2
    exit 1
}

profile_file="$runtime/RUNTIME_PROFILE"
provenance="$runtime/build-provenance-macos.json"
site_packages="$runtime/Python.framework/Versions/3.12/lib/python3.12/site-packages"
staged_python="$runtime/Python.framework/Versions/3.12/bin/python3.12"
[[ -f "$profile_file" && "$(tr -d '\r\n' < "$profile_file")" == core ]] || {
    echo "NumPy staging requires a prepared core runtime" >&2
    exit 1
}
[[ -f "$provenance" ]] || { echo "prepared core provenance is missing" >&2; exit 1; }
[[ -d "$site_packages" ]] || { echo "prepared core site-packages is missing" >&2; exit 1; }
[[ -x "$staged_python" ]] || { echo "prepared core Python executable is missing" >&2; exit 1; }
if find "$site_packages" -mindepth 1 -print -quit | grep -q .; then
    echo "NumPy staging requires empty prepared core site-packages" >&2
    exit 1
fi

work="$root/build/macos-numpy-stage"
rm -rf "$work"
mkdir -p "$work/overlay"
core_map="$work/core-shared-map.json"
payload_manifest="$work/numpy-payload-manifest-macos.json"

core_provenance="$core_runtime/build-provenance-macos.json"
core_provenance_sha="$(shasum -a 256 "$core_provenance" | awk '{print $1}')"
core_profile="$core_runtime/RUNTIME_PROFILE"
[[ -f "$core_profile" && "$(tr -d '\r\n' < "$core_profile")" == core ]] || {
    echo "retained qualified core runtime profile marker is invalid" >&2
    exit 1
}
core_profile_sha="$(shasum -a 256 "$core_profile" | awk '{print $1}')"
python3 - "$core_runtime" "$core_provenance" "$core_map" "$target_arch" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

runtime = pathlib.Path(sys.argv[1]).resolve()
provenance_path = pathlib.Path(sys.argv[2]).resolve()
output = pathlib.Path(sys.argv[3])
target_arch = sys.argv[4]
provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
if provenance.get("runtime_profile") != "core" or provenance.get("numpy") != {"bundled": False}:
    raise SystemExit("prepared macOS provenance is not core-only")
if provenance.get("python", {}).get("architecture") != target_arch:
    raise SystemExit("prepared macOS provenance architecture mismatch")

excluded = {"RUNTIME_PROFILE", "build-provenance-macos.json"}
entries = {}
for path in sorted(runtime.rglob("*")):
    rel = path.relative_to(runtime).as_posix()
    if rel in excluded:
        continue
    if path.is_symlink():
        entries[rel] = {"kind": "symlink", "target": os.readlink(path)}
    elif path.is_file():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries[rel] = {"kind": "file", "sha256": digest}
output.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

python3 - "$runtime" "$core_map" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

runtime = pathlib.Path(sys.argv[1]).resolve()
expected = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
excluded = {"RUNTIME_PROFILE", "build-provenance-macos.json"}
actual = {}
for path in sorted(runtime.rglob("*")):
    rel = path.relative_to(runtime).as_posix()
    if rel in excluded:
        continue
    if path.is_symlink():
        actual[rel] = {"kind": "symlink", "target": os.readlink(path)}
    elif path.is_file():
        actual[rel] = {"kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
if actual != expected:
    raise SystemExit("NumPy runtime copy does not byte-match retained qualified core runtime")
PY

python3 - "$numpy_wheel" "$work/overlay" "$payload_manifest" "$expected_tag" \
    "$expected_numpy_url" "$expected_numpy_size" "$actual_numpy_sha" "$target_arch" <<'PY'
import base64
import csv
import hashlib
import io
import json
import os
import pathlib
import stat
import sys
import zipfile

wheel = pathlib.Path(sys.argv[1]).resolve()
overlay = pathlib.Path(sys.argv[2]).resolve()
manifest_path = pathlib.Path(sys.argv[3]).resolve()
expected_tag, wheel_url, wheel_size, wheel_sha, target_arch = sys.argv[4:9]

def safe_name(value: str) -> pathlib.PurePosixPath:
    name = pathlib.PurePosixPath(value)
    if not value or name.is_absolute() or ".." in name.parts or "" in name.parts:
        raise SystemExit(f"unsafe NumPy wheel path: {value!r}")
    return name

def record_digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")

with zipfile.ZipFile(wheel) as archive:
    infos = [info for info in archive.infolist() if not info.is_dir()]
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise SystemExit("NumPy wheel contains duplicate file paths")
    for name in names:
        safe_name(name)

    record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
    if record_names != ["numpy-2.5.2.dist-info/RECORD"]:
        raise SystemExit(f"unexpected NumPy RECORD identity: {record_names!r}")
    rows = list(csv.reader(io.StringIO(archive.read(record_names[0]).decode("utf-8"))))
    record = {}
    for row in rows:
        if len(row) != 3:
            raise SystemExit(f"invalid NumPy RECORD row: {row!r}")
        name, digest, size = row
        safe_name(name)
        if name in record:
            raise SystemExit(f"duplicate NumPy RECORD path: {name}")
        record[name] = (digest, size)
    if set(record) != set(names):
        missing = sorted(set(names) - set(record))
        extra = sorted(set(record) - set(names))
        raise SystemExit(f"NumPy RECORD/archive path mismatch: missing={missing!r} extra={extra!r}")

    entries = []
    for info in infos:
        name = info.filename
        data = archive.read(info)
        digest, size = record[name]
        if name == record_names[0]:
            if digest or size:
                raise SystemExit("NumPy RECORD self-entry must omit hash and size")
        else:
            if not digest.startswith("sha256=") or digest.removeprefix("sha256=") != record_digest(data):
                raise SystemExit(f"NumPy RECORD hash mismatch: {name}")
            if not size.isdigit() or int(size) != len(data):
                raise SystemExit(f"NumPy RECORD size mismatch: {name}")

        mode = (info.external_attr >> 16) & 0o177777
        kind = "symlink" if stat.S_ISLNK(mode) else "file"
        target = overlay.joinpath(*safe_name(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == "symlink":
            link = data.decode("utf-8")
            link_path = pathlib.PurePosixPath(link)
            if link_path.is_absolute() or ".." in link_path.parts:
                raise SystemExit(f"unsafe NumPy wheel symlink: {name} -> {link}")
            os.symlink(link, target)
            content_sha = hashlib.sha256(data).hexdigest()
            entry_size = len(data)
        else:
            target.write_bytes(data)
            if mode & 0o777:
                target.chmod(mode & 0o777)
            content_sha = hashlib.sha256(data).hexdigest()
            entry_size = len(data)
        entries.append(
            {
                "path": name,
                "kind": kind,
                "mode": oct(mode & 0o777),
                "size": entry_size,
                "sha256": content_sha,
            }
        )

dist_infos = list(overlay.glob("numpy-2.5.2.dist-info"))
if len(dist_infos) != 1:
    raise SystemExit("NumPy wheel does not contain exactly one numpy-2.5.2.dist-info")
dist_info = dist_infos[0]
wheel_text = (dist_info / "WHEEL").read_text(encoding="utf-8")
if f"Tag: {expected_tag}" not in wheel_text:
    raise SystemExit(f"NumPy wheel tag mismatch; expected {expected_tag}")
metadata = (dist_info / "METADATA").read_text(encoding="utf-8")
if "Name: numpy\n" not in metadata or "Version: 2.5.2\n" not in metadata:
    raise SystemExit("NumPy wheel metadata name/version mismatch")
license_files = [
    path for path in dist_info.rglob("*")
    if path.is_file() and "license" in path.name.lower()
]
if not license_files:
    raise SystemExit("NumPy wheel contains no retained license file")

entries.sort(key=lambda item: item["path"])
map_text = "".join(
    f"{item['kind']}\t{item['mode']}\t{item['size']}\t{item['sha256']}\t{item['path']}\n"
    for item in entries
)
manifest = {
    "format_version": 1,
    "architecture": target_arch,
    "wheel": {
        "url": wheel_url,
        "name": wheel.name,
        "size": int(wheel_size),
        "sha256": wheel_sha,
        "tag": expected_tag,
    },
    "record": record_names[0],
    "payload_file_count": len(entries),
    "payload_map_sha256": hashlib.sha256(map_text.encode("utf-8")).hexdigest(),
    "files": entries,
}
manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

ditto "$work/overlay" "$site_packages"
cp "$payload_manifest" "$runtime/numpy-payload-manifest-macos.json"

bash "$root/scripts/macos/inspect-macos-runtime.sh" \
    --root "$site_packages" \
    --output "$census_output" \
    --arch "$target_arch" \
    --strict

accelerate_static="$work/accelerate-static.json"
python3 - "$runtime" "$site_packages" "$census_output" "$accelerate_static" <<'PY'
import json
import pathlib
import subprocess
import sys

runtime = pathlib.Path(sys.argv[1]).resolve()
site = pathlib.Path(sys.argv[2]).resolve()
census = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
output = pathlib.Path(sys.argv[4]).resolve()
payload_manifest = json.loads(
    (runtime / "numpy-payload-manifest-macos.json").read_text(encoding="utf-8")
)
accelerate = "/System/Library/Frameworks/Accelerate.framework/Versions/A/Accelerate"

bundled_dylibs = [
    entry["path"]
    for entry in payload_manifest.get("files", [])
    if entry["path"].endswith(".dylib") or ".dylibs/" in entry["path"]
]
if bundled_dylibs:
    raise SystemExit(f"pinned NumPy wheel unexpectedly bundles dylibs: {bundled_dylibs!r}")

def inside(path: pathlib.Path) -> bool:
    path = path.resolve()
    return path == runtime or runtime in path.parents

def expand_rpath(value: str, binary: pathlib.Path):
    if value == "@loader_path":
        return binary.parent
    if value.startswith("@loader_path/"):
        return binary.parent / value.removeprefix("@loader_path/")
    if value.startswith(("/usr/lib/", "/System/Library/")):
        return None
    return pathlib.Path("/__unresolved__")

accelerate_entries = []
newlapack = {}
for entry in census.get("mach_o", []):
    binary = site / entry["path"]
    if accelerate in entry.get("dependencies", []):
        accelerate_entries.append(entry["path"])
    nm = subprocess.run(
        ["nm", "-u", str(binary)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    symbols = sorted(
        {
            line.strip()
            for line in nm.stdout.splitlines()
            if "NEWLAPACK" in line and "ILP64" in line
        }
    )
    if symbols:
        newlapack[entry["path"]] = symbols
    for dep in entry.get("dependencies", []):
        if dep.startswith(("/usr/lib/", "/System/Library/")):
            continue
        if dep.startswith("@loader_path/"):
            candidate = binary.parent / dep.removeprefix("@loader_path/")
            if not candidate.exists() or not inside(candidate):
                raise SystemExit(f"NumPy dependency does not resolve inside runtime: {entry['path']} -> {dep}")
            continue
        if dep.startswith("@rpath/"):
            suffix = dep.removeprefix("@rpath/")
            candidates = []
            for rpath in entry.get("rpaths", []):
                base = expand_rpath(rpath, binary)
                if base is not None:
                    candidates.append(base / suffix)
            resolved = [candidate for candidate in candidates if candidate.exists() and inside(candidate)]
            if not resolved:
                raise SystemExit(
                    f"NumPy @rpath dependency does not resolve inside runtime: {entry['path']} -> {dep}; "
                    f"candidates={[str(item) for item in candidates]!r}"
                )
            continue
        raise SystemExit(f"unsupported NumPy native dependency form: {entry['path']} -> {dep}")

multiarray = [
    path
    for path in accelerate_entries
    if path.startswith("numpy/_core/_multiarray_umath.")
]
if not multiarray:
    raise SystemExit(
        "NumPy core multiarray extension does not record the exact Apple Accelerate dependency"
    )
multiarray_newlapack = {
    path: newlapack[path]
    for path in multiarray
    if path in newlapack
}
if not multiarray_newlapack:
    raise SystemExit(
        "NumPy core multiarray extension exposes no NEWLAPACK ILP64 symbol evidence"
    )
evidence = {
    "accelerate_dependency": accelerate,
    "accelerate_macho_paths": sorted(accelerate_entries),
    "multiarray_accelerate_paths": sorted(multiarray),
    "multiarray_newlapack_ilp64_symbols": multiarray_newlapack,
    "newlapack_ilp64_symbols": newlapack,
    "bundled_dylibs": [],
}
output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

python3 - "$runtime" "$core_map" "$provenance" "$census_output" "$payload_manifest" \
    "$accelerate_static" "$expected_wheel" "$expected_numpy_url" "$actual_numpy_size" \
    "$actual_numpy_sha" "$expected_tag" "$target_arch" "$core_provenance_sha" \
    "$core_profile_sha" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

runtime = pathlib.Path(sys.argv[1]).resolve()
before = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
provenance_path = pathlib.Path(sys.argv[3]).resolve()
census_path = pathlib.Path(sys.argv[4]).resolve()
manifest_path = pathlib.Path(sys.argv[5]).resolve()
accelerate_path = pathlib.Path(sys.argv[6]).resolve()
(
    wheel_name,
    wheel_url,
    wheel_size,
    wheel_sha,
    wheel_tag,
    target_arch,
    core_provenance_sha,
    core_profile_sha,
) = sys.argv[7:15]

for rel, expected in before.items():
    path = runtime / rel
    if expected["kind"] == "symlink":
        if not path.is_symlink() or os.readlink(path) != expected["target"]:
            raise SystemExit(f"NumPy staging changed prepared core symlink: {rel}")
    else:
        if not path.is_file():
            raise SystemExit(f"NumPy staging removed prepared core file: {rel}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected["sha256"]:
            raise SystemExit(f"NumPy staging changed prepared core file: {rel}")

staged_site = runtime / "Python.framework/Versions/3.12/lib/python3.12/site-packages"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
expected_paths = {entry["path"] for entry in manifest.get("files", [])}
actual_paths = set()
for path in sorted(staged_site.rglob("*")):
    if path.is_file() or path.is_symlink():
        actual_paths.add(path.relative_to(staged_site).as_posix())
if actual_paths != expected_paths:
    raise SystemExit(
        "staged NumPy payload path set differs from verified RECORD: "
        f"missing={sorted(expected_paths - actual_paths)!r} extra={sorted(actual_paths - expected_paths)!r}"
    )
for entry in manifest["files"]:
    path = staged_site / entry["path"]
    if entry["kind"] == "symlink":
        if not path.is_symlink():
            raise SystemExit(f"staged NumPy symlink became a regular file: {entry['path']}")
        data = os.readlink(path).encode("utf-8")
        mode = oct(os.lstat(path).st_mode & 0o777)
    else:
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"staged NumPy file type mismatch: {entry['path']}")
        data = path.read_bytes()
        mode = oct(path.stat().st_mode & 0o777)
    if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise SystemExit(f"staged NumPy file/hash mismatch: {entry['path']}")
    if mode != entry["mode"]:
        raise SystemExit(f"staged NumPy mode mismatch: {entry['path']} {mode} != {entry['mode']}")

map_sha = manifest["payload_map_sha256"]
core_map_text = json.dumps(before, sort_keys=True, separators=(",", ":"))
core_map_sha = hashlib.sha256(core_map_text.encode("utf-8")).hexdigest()
licenses = [
    {"path": entry["path"], "sha256": entry["sha256"]}
    for entry in manifest["files"]
    if entry["path"].startswith("numpy-2.5.2.dist-info/licenses/")
]
census = json.loads(census_path.read_text(encoding="utf-8"))
accelerate = json.loads(accelerate_path.read_text(encoding="utf-8"))
measured_minos = census.get("target_measured_max_minos")
if measured_minos is None:
    raise SystemExit("NumPy native census did not record a macOS compatibility floor")
if tuple(int(part) for part in measured_minos.split(".")) > (14, 0):
    raise SystemExit(
        f"NumPy native payload raises the qualified macOS 14.0 floor: {measured_minos}"
    )
provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
provenance["runtime_profile"] = "numpy"
provenance["numpy"] = {
    "bundled": True,
    "version": "2.5.2",
    "wheel": wheel_name,
    "wheel_url": wheel_url,
    "wheel_size": int(wheel_size),
    "wheel_sha256": wheel_sha,
    "wheel_tag": wheel_tag,
    "payload_manifest": "numpy-payload-manifest-macos.json",
    "payload_file_count": manifest["payload_file_count"],
    "payload_map_sha256": map_sha,
    "license_files": licenses,
    "native_macho_count": census.get("macho_count"),
    "native_measured_max_minos": census.get("target_measured_max_minos"),
    "native_census": pathlib.Path(census_path).name,
    "accelerate_static": accelerate,
}
provenance["numpy"]["architecture"] = target_arch
provenance["core_provenance_sha256"] = core_provenance_sha
provenance["core_runtime_profile"] = "core"
provenance["core_runtime_profile_sha256"] = core_profile_sha
provenance["core_shared_file_count"] = len(before)
provenance["core_shared_map_sha256"] = core_map_sha
provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

printf '%s\n' numpy > "$profile_file"

staged_python_cmd=("$staged_python")
if [[ "$target_arch" == x86_64 && "$(uname -m)" == arm64 ]]; then
    staged_python_cmd=(/usr/bin/arch -x86_64 "$staged_python")
fi
poison="$work/ambient-poison"
mkdir -p "$poison"
printf '%s\n' 'raise RuntimeError("ambient NumPy must not be imported")' > "$poison/numpy.py"
env \
    PYTHONPATH="$poison" \
    PYTHONUSERBASE="$poison" \
    DYLD_LIBRARY_PATH="$poison" \
    DYLD_FALLBACK_LIBRARY_PATH="$poison" \
    PYTHONHOME="$runtime/Python.framework/Versions/3.12" \
    PYTHONNOUSERSITE=1 \
    "${staged_python_cmd[@]}" -I -S -P -B - "$runtime" "$provenance" <<'PY'
import ctypes
import json
import os
import pathlib
import platform
import sys

runtime = pathlib.Path(sys.argv[1]).resolve()
provenance_path = pathlib.Path(sys.argv[2]).resolve()
site = runtime / "Python.framework/Versions/3.12/lib/python3.12/site-packages"
sys.path.insert(0, str(site))

lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
lib._dyld_image_count.restype = ctypes.c_uint32
lib._dyld_get_image_name.argtypes = [ctypes.c_uint32]
lib._dyld_get_image_name.restype = ctypes.c_char_p

def images() -> set[pathlib.Path]:
    result = set()
    for index in range(lib._dyld_image_count()):
        raw = lib._dyld_get_image_name(index)
        if raw:
            result.add(pathlib.Path(os.fsdecode(raw)).resolve())
    return result

def image_names() -> set[str]:
    result = set()
    for index in range(lib._dyld_image_count()):
        raw = lib._dyld_get_image_name(index)
        if raw:
            result.add(os.fsdecode(raw))
    return result

before = images()
import numpy
import numpy._core._multiarray_umath as multiarray
if numpy.__version__ != "2.5.2":
    raise SystemExit(f"unexpected staged NumPy version: {numpy.__version__}")
for module_path in (numpy.__file__, multiarray.__file__):
    path = pathlib.Path(module_path).resolve()
    if site not in path.parents:
        raise SystemExit(f"NumPy module escaped staged site-packages: {path}")
matrix = numpy.array([[1.0, 2.0], [3.0, 4.0]], dtype=numpy.float64)
vector = numpy.array([1.0, 2.0], dtype=numpy.float64)
if not numpy.allclose(numpy.matmul(matrix, vector), numpy.array([5.0, 11.0])):
    raise SystemExit("NumPy matmul Accelerate probe failed")
system = numpy.array([[3.0, 1.0], [1.0, 2.0]], dtype=numpy.float64)
rhs = numpy.array([9.0, 8.0], dtype=numpy.float64)
if not numpy.allclose(numpy.linalg.solve(system, rhs), numpy.array([2.0, 3.0])):
    raise SystemExit("NumPy linalg.solve Accelerate probe failed")
after = images()
foreign = []
for path in sorted(after - before):
    if path == runtime or runtime in path.parents:
        continue
    text = str(path)
    if text.startswith(("/usr/lib/", "/System/Library/")):
        continue
    foreign.append(text)
if foreign:
    raise SystemExit(f"NumPy loaded ambient native providers: {foreign!r}")
accelerate_path = "/System/Library/Frameworks/Accelerate.framework/Versions/A/Accelerate"
if accelerate_path not in image_names():
    raise SystemExit("running NumPy process did not map Apple Accelerate")
provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
provenance["numpy"]["accelerate_runtime"] = {
    "provider_path": accelerate_path,
    "matmul": "pass",
    "linalg_solve": "pass",
    "ambient_native_provider_rejection": "pass",
}
provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"numpy_version={numpy.__version__}")
print(f"numpy_location={pathlib.Path(numpy.__file__).resolve()}")
print(f"numpy_machine={platform.machine()}")
print(f"numpy_accelerate_provider={accelerate_path}")
print("numpy_matmul=pass")
print("numpy_linalg_solve=pass")
print("numpy_native_provider_isolation=pass")
PY

python3 - "$core_runtime" "$core_map" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

runtime = pathlib.Path(sys.argv[1]).resolve()
expected = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
excluded = {"RUNTIME_PROFILE", "build-provenance-macos.json"}
actual = {}
for path in sorted(runtime.rglob("*")):
    rel = path.relative_to(runtime).as_posix()
    if rel in excluded:
        continue
    if path.is_symlink():
        actual[rel] = {"kind": "symlink", "target": os.readlink(path)}
    elif path.is_file():
        actual[rel] = {"kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
if actual != expected:
    raise SystemExit("retained qualified core runtime changed during NumPy staging/verification")
PY
after_core_provenance_sha="$(shasum -a 256 "$core_provenance" | awk '{print $1}')"
[[ "$after_core_provenance_sha" == "$core_provenance_sha" ]] || {
    echo "retained qualified core provenance changed during NumPy staging/verification" >&2
    exit 1
}
after_core_profile_sha="$(shasum -a 256 "$core_profile" | awk '{print $1}')"
[[ "$after_core_profile_sha" == "$core_profile_sha" && "$(tr -d '\r\n' < "$core_profile")" == core ]] || {
    echo "retained qualified core runtime profile marker changed during NumPy staging/verification" >&2
    exit 1
}

echo "runtime_profile=numpy"
echo "core_runtime=$core_runtime"
echo "numpy_runtime=$runtime"
echo "numpy_wheel=$expected_wheel"
echo "numpy_wheel_sha256=$actual_numpy_sha"
echo "numpy_native_census=$census_output"
echo "macos_numpy_runtime_staging=pass"
