#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: scripts/macos/stage-core-runtime.sh --python <python3.12> --python-archive <archive> --python-hashes <hashes.sha256> [--arch arm64|x86_64]" >&2
    exit 2
}

python=""
python_archive=""
python_hashes=""
target_arch="arm64"
while (($#)); do
    case "$1" in
        --python) python="$2"; shift 2 ;;
        --python-archive) python_archive="$2"; shift 2 ;;
        --python-hashes) python_hashes="$2"; shift 2 ;;
        --arch) target_arch="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

[[ "$target_arch" == arm64 || "$target_arch" == x86_64 ]] || {
    echo "unsupported macOS architecture: $target_arch" >&2
    exit 1
}

[[ -x "$python" ]] || { echo "selected Python is not executable: $python" >&2; exit 1; }
[[ -f "$python_archive" ]] || { echo "CPython archive is missing: $python_archive" >&2; exit 1; }
[[ -f "$python_hashes" ]] || { echo "CPython checksum manifest is missing: $python_hashes" >&2; exit 1; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
build="$root/build/macos-core-$target_arch"
runtime="$root/out/macos-$target_arch/core"
hash_asset_id=244701301
case "$target_arch" in
    arm64)
        archive_name="python-3.12.10-darwin-arm64.tar.gz"
        archive_asset_id=244701302
        ;;
    x86_64)
        archive_name="python-3.12.10-darwin-x64.tar.gz"
        archive_asset_id=244701305
        ;;
esac

[[ "$(basename "$python_archive")" == "$archive_name" ]] || {
    echo "unexpected CPython archive filename: $(basename "$python_archive")" >&2
    exit 1
}
for tool in codesign ditto install_name_tool otool pkgutil shasum tar unzip; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "required macOS staging tool is missing: $tool" >&2
        exit 1
    }
done

expected_archive_sha="$(awk -v name="$archive_name" '$2 == name || $2 == "*" name { print $1 }' "$python_hashes")"
expected_archive_sha="$(printf '%s' "$expected_archive_sha" | tr '[:upper:]' '[:lower:]')"
[[ "$expected_archive_sha" =~ ^[0-9a-fA-F]{64}$ ]] || {
    echo "checksum manifest has no unique entry for $archive_name" >&2
    exit 1
}
archive_sha="$(shasum -a 256 "$python_archive" | awk '{print $1}')"
[[ "$archive_sha" == "$expected_archive_sha" ]] || {
    echo "CPython archive SHA256 mismatch: $archive_sha != $expected_archive_sha" >&2
    exit 1
}

map_file="$build/python-info.txt"
rm -rf "$build"
mkdir -p "$build"
"$python" -I -S - >"$map_file" <<'PY'
import pathlib
import platform
import struct
import sys
print(platform.python_implementation())
print(platform.python_version())
print(8 * struct.calcsize("P"))
print(platform.machine())
print(pathlib.Path(sys.prefix).resolve())
print(pathlib.Path(sys.executable).resolve())
PY
pyinfo=()
while IFS= read -r line; do pyinfo+=("$line"); done < "$map_file"
[[ ${#pyinfo[@]} -eq 6 ]] || { echo "selected Python metadata is incomplete" >&2; exit 1; }
[[ "${pyinfo[0]}" == CPython && "${pyinfo[1]}" == 3.12.10 && "${pyinfo[2]}" == 64 && "${pyinfo[3]}" == "$target_arch" ]] || {
    echo "expected $target_arch CPython 3.12.10, got ${pyinfo[*]}" >&2
    exit 1
}
selected_prefix="${pyinfo[4]}"
selected_executable="${pyinfo[5]}"

payload="$build/python-archive"
expanded="$build/python-pkg"
mkdir -p "$payload"
tar -xzf "$python_archive" -C "$payload"
pkg_candidates=("$payload"/*.pkg)
if [[ ${#pkg_candidates[@]} -ne 1 || ! -f "${pkg_candidates[0]}" ]]; then
    echo "expected exactly one top-level Python pkg in verified archive" >&2
    find "$payload" -maxdepth 2 -print >&2
    exit 1
fi
pkgutil --expand-full "${pkg_candidates[0]}" "$expanded"

package_paths="$build/package-paths.json"
"$python" - "$expanded" "$selected_prefix" "$package_paths" <<'PY'
import hashlib
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

import _tkinter

expanded = pathlib.Path(sys.argv[1]).resolve()
selected = pathlib.Path(sys.argv[2]).resolve()
output = pathlib.Path(sys.argv[3])
framework_pkg = expanded / "Python_Framework.pkg"
package_info = framework_pkg / "PackageInfo"
payload = framework_pkg / "Payload"
if not package_info.is_file() or not payload.is_dir():
    raise SystemExit("expanded installer is missing Python_Framework.pkg payload metadata")
install_location = ET.parse(package_info).getroot().attrib.get("install-location")
if install_location != "/Library/Frameworks/Python.framework":
    raise SystemExit(f"unexpected Python framework install location: {install_location!r}")

def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def payload_file(relative):
    path = (payload / relative).resolve()
    if not path.is_file():
        raise SystemExit(f"verified Python framework payload file is missing: {relative}")
    return path

package_python = payload_file("Versions/3.12/Python")
package_exe = payload_file("Versions/3.12/bin/python3.12")
selected_python = selected / "Python"
selected_exe = selected / "bin/python3.12"
for package_file, selected_file, label in (
    (package_python, selected_python, "framework"),
    (package_exe, selected_exe, "executable"),
):
    if sha(package_file) != sha(selected_file):
        raise SystemExit(f"selected {label} does not byte-match verified package payload")

tkinter_matches = list((payload / "Versions/3.12/lib/python3.12/lib-dynload").glob("_tkinter*.so"))
if len(tkinter_matches) != 1:
    raise SystemExit(f"expected one packaged _tkinter, found {tkinter_matches}")
selected_tkinter = next((selected / "lib/python3.12/lib-dynload").glob("_tkinter*.so"))
if sha(tkinter_matches[0]) != sha(selected_tkinter):
    raise SystemExit("selected _tkinter does not byte-match verified package payload")

tcl_binary = payload_file("Versions/3.12/lib/libtcl8.6.dylib")
tk_binary = payload_file("Versions/3.12/lib/libtk8.6.dylib")
tcl_script = payload_file("Versions/3.12/lib/tcl8.6/init.tcl")
tk_script = payload_file("Versions/3.12/lib/tk8.6/tk.tcl")
selected_tcl = selected / "lib/libtcl8.6.dylib"
selected_tk = selected / "lib/libtk8.6.dylib"
for package_file, selected_file, label in (
    (tcl_binary, selected_tcl, "Tcl provider"),
    (tk_binary, selected_tk, "Tk provider"),
):
    if sha(package_file) != sha(selected_file):
        raise SystemExit(f"selected {label} does not byte-match verified package payload")

tcl_script_matches = [tcl_script]
tk_script_matches = [tk_script]
tcl_text = tcl_script_matches[0].read_text(encoding="utf-8", errors="strict")
tk_text = tk_script_matches[0].read_text(encoding="utf-8", errors="strict")
tcl_versions = re.findall(
    r"^package\s+require\s+-exact\s+Tcl\s+([^\s]+)\s*$",
    tcl_text,
    re.MULTILINE,
)
tcl_versions = list(dict.fromkeys(tcl_versions))
if len(tcl_versions) != 1:
    raise SystemExit(
        f"could not determine exact private Tcl runtime version: {tcl_versions}"
    )
tk_versions = re.findall(
    r"^package\s+(?:require\s+-exact|provide)\s+Tk\s+([^\s]+)\s*$",
    tk_text,
    re.MULTILINE,
)
tk_versions = list(dict.fromkeys(tk_versions))
if len(tk_versions) != 1:
    raise SystemExit(f"could not determine exact private Tk runtime version: {tk_versions}")
data = {
    "package_python": str(package_python),
    "package_executable": str(package_exe),
    "package_tkinter": str(tkinter_matches[0].resolve()),
    "package_prefix": str(package_python.parent),
    "package_install_location": install_location,
    "tcl_binary": str(tcl_binary),
    "tk_binary": str(tk_binary),
    "tcl_script_root": str(tcl_script_matches[0].parent.resolve()),
    "tk_script_root": str(tk_script_matches[0].parent.resolve()),
    "tcl_api_version": str(_tkinter.TCL_VERSION),
    "tk_api_version": str(_tkinter.TK_VERSION),
    "tcl_runtime_version": tcl_versions[0],
    "tk_runtime_version": tk_versions[0],
    "source_hashes": {
        "python": sha(package_python),
        "executable": sha(package_exe),
        "tkinter": sha(tkinter_matches[0]),
        "tcl": sha(tcl_binary),
        "tk": sha(tk_binary),
    },
}
output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

rm -rf "$runtime"
mkdir -p "$runtime/Python.framework/Versions" "$runtime/tcl"

package_prefix="$("$python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["package_prefix"])' "$package_paths")"
ditto "$package_prefix" "$runtime/Python.framework/Versions/3.12"

staged_tkinter=("$runtime"/Python.framework/Versions/3.12/lib/python3.12/lib-dynload/_tkinter*.so)
[[ ${#staged_tkinter[@]} -eq 1 && -f "${staged_tkinter[0]}" ]] || {
    echo "staged _tkinter is missing or ambiguous" >&2
    exit 1
}
tkinter_so="${staged_tkinter[0]}"
before_tkinter_sha="$(shasum -a 256 "$tkinter_so" | awk '{print $1}')"
install_changes="$build/tcltk-install-name-changes.tsv"
: > "$install_changes"

rewrite_dylib_dep() {
    local binary="$1"
    local dylib="$2"
    local replacement="$3"
    local dep
    dep="$(otool -L "$binary" | awk -v needle="/$dylib" '$2 == "(compatibility" && index($1, needle) { print $1 }' | sort -u)"
    [[ -n "$dep" ]] || {
        echo "$binary does not reference $dylib" >&2
        otool -L "$binary" >&2
        exit 1
    }
    if [[ "$dep" == *$'\n'* ]]; then
        echo "$binary has architecture-dependent references for $dylib: $dep" >&2
        exit 1
    fi
    printf '%s\t%s\t%s\n' "$binary" "$dep" "$replacement" >> "$install_changes"
    install_name_tool -change "$dep" "$replacement" "$binary"
}

rewrite_dylib_dep "$tkinter_so" libtcl8.6.dylib '@loader_path/../../libtcl8.6.dylib'
rewrite_dylib_dep "$tkinter_so" libtk8.6.dylib '@loader_path/../../libtk8.6.dylib'
staged_tcl_binary="$runtime/Python.framework/Versions/3.12/lib/libtcl8.6.dylib"
staged_tk_binary="$runtime/Python.framework/Versions/3.12/lib/libtk8.6.dylib"
staged_python_executable="$runtime/Python.framework/Versions/3.12/bin/python3.12"
rewrite_dylib_dep "$staged_python_executable" Python '@loader_path/../Python'
codesign --force --sign - "$tkinter_so" >/dev/null
codesign --force --sign - "$staged_python_executable" >/dev/null
codesign --verify --strict "$tkinter_so"
codesign --verify --strict "$staged_python_executable"

for binary in "$tkinter_so" "$staged_python_executable"; do
    if otool -L "$binary" | grep -Eq '/Library/Frameworks/Python\.framework/.*/lib/lib(tcl|tk)8\.6\.dylib|/Library/Frameworks/(Tcl|Tk)\.framework/'; then
        echo "ambient Tcl/Tk dependency remains after relocation: $binary" >&2
        otool -L "$binary" >&2
        exit 1
    fi
done
if otool -L "$staged_python_executable" | awk 'NR > 1 { print $1 }' | grep -Fq '/Library/Frameworks/Python.framework/'; then
    echo "ambient Python framework dependency remains after launcher relocation" >&2
    otool -L "$staged_python_executable" >&2
    exit 1
fi

tcl_scripts="$runtime/Python.framework/Versions/3.12/lib/tcl8.6"
tk_scripts="$runtime/Python.framework/Versions/3.12/lib/tk8.6"
[[ -d "$tcl_scripts" && -d "$tk_scripts" ]] || { echo "private Tcl/Tk script roots are missing" >&2; exit 1; }
ditto "$tcl_scripts" "$runtime/tcl/tcl8.6"
ditto "$tk_scripts" "$runtime/tcl/tk8.6"

site_packages="$runtime/Python.framework/Versions/3.12/lib/python3.12/site-packages"
rm -rf "$site_packages"
mkdir -p "$site_packages"
printf '%s\n' core > "$runtime/RUNTIME_PROFILE"

after_tkinter_sha="$(shasum -a 256 "$tkinter_so" | awk '{print $1}')"
staged_python_sha="$(shasum -a 256 "$runtime/Python.framework/Versions/3.12/Python" | awk '{print $1}')"
staged_executable_sha="$(shasum -a 256 "$runtime/Python.framework/Versions/3.12/bin/python3.12" | awk '{print $1}')"
staged_tcl_sha="$(shasum -a 256 "$staged_tcl_binary" | awk '{print $1}')"
staged_tk_sha="$(shasum -a 256 "$staged_tk_binary" | awk '{print $1}')"

"$python" - "$package_paths" "$install_changes" "$runtime/build-provenance-macos.json" \
    "$archive_sha" "$before_tkinter_sha" "$after_tkinter_sha" \
    "$staged_python_sha" "$staged_executable_sha" "$staged_tcl_sha" "$staged_tk_sha" \
    "$selected_prefix" "$selected_executable" "$target_arch" "$archive_asset_id" \
    "$archive_name" <<'PY'
import json
import pathlib
import sys

package = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
changes = []
for line in pathlib.Path(sys.argv[2]).read_text(encoding="utf-8").splitlines():
    binary, original, replacement = line.split("\t", 2)
    changes.append(
        {"binary": binary, "original": original, "replacement": replacement}
    )
output = pathlib.Path(sys.argv[3])
target_arch = sys.argv[13]
archive_asset_id = int(sys.argv[14])
archive_name = sys.argv[15]
data = {
    "format_version": 1,
    "runtime_profile": "core",
    "python": {
        "implementation": "CPython",
        "version": "3.12.10",
        "architecture": target_arch,
        "release": "3.12.10-14343898437",
        "archive_asset_id": archive_asset_id,
        "checksum_asset_id": 244701301,
        "archive_name": archive_name,
        "archive_sha256": sys.argv[4],
        "selected_prefix": sys.argv[11],
        "selected_executable": sys.argv[12],
        "package_source_hashes": package["source_hashes"],
        "staged_framework_sha256": sys.argv[7],
        "staged_executable_sha256": sys.argv[8],
        "staging_source": "verified-actions-python-versions-package-payload",
    },
    "numpy": {"bundled": False},
    "tcl_tk": {
        "tcl_api_version": package["tcl_api_version"],
        "tk_api_version": package["tk_api_version"],
        "tcl_runtime_version": package["tcl_runtime_version"],
        "tk_runtime_version": package["tk_runtime_version"],
        "source_hashes": {
            "tcl": package["source_hashes"]["tcl"],
            "tk": package["source_hashes"]["tk"],
            "tkinter": package["source_hashes"]["tkinter"],
        },
        "staged_hashes": {
            "tcl": sys.argv[9],
            "tk": sys.argv[10],
            "tkinter_before_rewrite": sys.argv[5],
            "tkinter_after_rewrite": sys.argv[6],
        },
        "install_name_changes": changes,
        "loader_strategy": "runtime-relative-loader-path-dylibs",
    },
}
output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

staged_python_home="$runtime/Python.framework/Versions/3.12"
env -u PYTHONPATH -u PYTHONUSERBASE \
    PYTHONHOME="$staged_python_home" PYTHONNOUSERSITE=1 \
    "$staged_python_executable" -S -P - <<'PY'
import pathlib
import sys

home = pathlib.Path(sys.executable).resolve().parent.parent
if pathlib.Path(sys.prefix).resolve() != home:
    raise SystemExit(f"staged Python prefix mismatch: {sys.prefix} != {home}")
if pathlib.Path(sys.base_prefix).resolve() != home:
    raise SystemExit(f"staged Python base prefix mismatch: {sys.base_prefix} != {home}")
if not (home / "lib/python3.12/encodings").is_dir():
    raise SystemExit("staged Python stdlib is incomplete")
foreign = []
for entry in sys.path:
    if not entry:
        continue
    path = pathlib.Path(entry).resolve()
    if path != home and home not in path.parents:
        foreign.append(path)
if foreign:
    raise SystemExit(f"staged Python search path escaped runtime: {foreign!r}")
print(f"staged_python_prefix={sys.prefix}")
PY

core_census="$runtime/core-native-census-macos.json"
bash "$root/scripts/macos/inspect-macos-runtime.sh" \
    --root "$runtime" \
    --output "$core_census" \
    --arch "$target_arch" \
    --max-minos 14.0 \
    --strict

core_relocation="$runtime/core-relocation-qualification-macos.json"
bash "$root/scripts/macos/qualify-relocated-runtime.sh" \
    --runtime "$runtime" \
    --arch "$target_arch" \
    --profile core \
    --output "$core_relocation"

python3 - "$runtime/build-provenance-macos.json" "$core_census" "$core_relocation" <<'PY'
import hashlib
import json
import pathlib
import sys

provenance_path = pathlib.Path(sys.argv[1]).resolve()
census_path = pathlib.Path(sys.argv[2]).resolve()
relocation_path = pathlib.Path(sys.argv[3]).resolve()

def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
census = json.loads(census_path.read_text(encoding="utf-8"))
relocation = json.loads(relocation_path.read_text(encoding="utf-8"))
provenance["native_closure"] = {
    "status": "pass",
    "census_path": census_path.name,
    "census_sha256": sha256(census_path),
    "macho_count": census["macho_count"],
    "bundled_dependency_count": census["bundled_dependency_count"],
    "system_dependency_count": census["system_dependency_count"],
    "max_allowed_minos": census["max_allowed_minos"],
    "target_measured_max_minos": census["target_measured_max_minos"],
}
provenance["relocation_qualification"] = {
    "status": "pass",
    "evidence_path": relocation_path.name,
    "evidence_sha256": sha256(relocation_path),
    "relocated_tcl_tk": relocation["status"],
}
provenance["tcl_tk"]["relocation_qualification"] = {
    "status": relocation["status"],
    "tcl_script_root": relocation["tcl"]["script_root"],
    "tk_script_root": relocation["tk"]["script_root"],
    "tcl_provider": relocation["tcl"]["provider"],
    "tk_provider": relocation["tk"]["provider"],
}
provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

python3 "$root/scripts/macos/verify-runtime-evidence.py" \
    --runtime "$runtime" \
    --profile core \
    --arch "$target_arch"

echo "macos_runtime=$runtime"
echo "python_archive_asset_id=$archive_asset_id"
echo "python_checksum_asset_id=$hash_asset_id"
echo "python_archive_sha256=$archive_sha"
echo "runtime_profile=core"
echo "numpy_bundled=false"
echo "tcl_tk_staging=pass"
echo "native_closure=pass"
echo "macos_floor=pass"
echo "relocated_tcl_tk=pass"
echo "macos_runtime_staging=pass"
