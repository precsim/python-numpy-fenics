#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: scripts/macos/qualify-relocated-runtime.sh --runtime <runtime> --arch arm64|x86_64 --profile core|numpy --output <json>" >&2
    exit 2
}

runtime=""
target_arch=""
profile=""
output=""
while (($#)); do
    case "$1" in
        --runtime) runtime="$2"; shift 2 ;;
        --arch) target_arch="$2"; shift 2 ;;
        --profile) profile="$2"; shift 2 ;;
        --output) output="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

[[ -d "$runtime" ]] || { echo "runtime is missing: $runtime" >&2; exit 1; }
[[ "$target_arch" == arm64 || "$target_arch" == x86_64 ]] || usage
[[ "$profile" == core || "$profile" == numpy ]] || usage
[[ -n "$output" ]] || usage
for tool in ditto python3; do
    command -v "$tool" >/dev/null 2>&1 || { echo "$tool is required" >&2; exit 1; }
done

runtime="$(cd "$runtime" && pwd -P)"
output="$(python3 - "$output" <<'PY'
import pathlib
import sys
print(pathlib.Path(sys.argv[1]).resolve(strict=False))
PY
)"

work="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/macos-relocated-runtime-$profile-$target_arch-$$"
poison="$work/ambient poison"
relocated="$work/relocated runtime with spaces"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT
rm -rf "$work"
mkdir -p "$poison/python" "$poison/user" "$poison/native"
printf '%s\n' 'raise RuntimeError("ambient sitecustomize imported")' > "$poison/python/sitecustomize.py"
printf '%s\n' 'raise RuntimeError("ambient usercustomize imported")' > "$poison/python/usercustomize.py"
ditto "$runtime" "$relocated"

python_executable="$relocated/Python.framework/Versions/3.12/bin/python3.12"
python_home="$relocated/Python.framework/Versions/3.12"
tcl_library="$relocated/tcl/tcl8.6"
tk_library="$relocated/tcl/tk8.6"
[[ -x "$python_executable" && -d "$tcl_library" && -d "$tk_library" ]] || {
    echo "relocated runtime is missing Python or Tcl/Tk resources" >&2
    exit 1
}

python_cmd=("$python_executable")
if [[ "$target_arch" == x86_64 && "$(uname -m)" == arm64 ]]; then
    python_cmd=(/usr/bin/arch -x86_64 "$python_executable")
fi

mkdir -p "$(dirname "$output")"
env \
    PYTHONPATH="$poison/python" \
    PYTHONUSERBASE="$poison/user" \
    DYLD_LIBRARY_PATH="$poison/native" \
    DYLD_FALLBACK_LIBRARY_PATH="$poison/native" \
    PYTHONHOME="$python_home" \
    PYTHONNOUSERSITE=1 \
    TCL_LIBRARY="$tcl_library" \
    TK_LIBRARY="$tk_library" \
    "${python_cmd[@]}" -I -S -P -B - "$relocated" "$target_arch" "$profile" "$output" <<'PY'
import ctypes
import hashlib
import json
import os
import pathlib
import platform
import re
import sys
import _tkinter
import tkinter

runtime = pathlib.Path(sys.argv[1]).resolve()
target_arch = sys.argv[2]
profile = sys.argv[3]
output = pathlib.Path(sys.argv[4])
python_home = runtime / "Python.framework/Versions/3.12"
tcl_library = runtime / "tcl/tcl8.6"
tk_library = runtime / "tcl/tk8.6"
provenance = json.loads((runtime / "build-provenance-macos.json").read_text(encoding="utf-8"))
tcl_tk = provenance.get("tcl_tk", {})

if pathlib.Path(sys.prefix).resolve() != python_home:
    raise SystemExit(f"relocated sys.prefix mismatch: {sys.prefix} != {python_home}")
if pathlib.Path(sys.base_prefix).resolve() != python_home:
    raise SystemExit(f"relocated sys.base_prefix mismatch: {sys.base_prefix} != {python_home}")
if pathlib.Path(sys.executable).resolve() != (python_home / "bin/python3.12"):
    raise SystemExit(f"relocated executable mismatch: {sys.executable}")
if platform.machine() != target_arch:
    raise SystemExit(f"relocated architecture mismatch: {platform.machine()} != {target_arch}")
if (runtime / "RUNTIME_PROFILE").read_text(encoding="utf-8").strip() != profile:
    raise SystemExit("relocated runtime profile marker mismatch")

interp = tkinter.Tcl()
tcl_version = interp.eval("info patchlevel")
tcl_info_library = pathlib.Path(interp.eval("info library")).resolve()
if tcl_info_library != tcl_library:
    raise SystemExit(f"relocated Tcl script root mismatch: {tcl_info_library} != {tcl_library}")
if pathlib.Path(os.environ.get("TCL_LIBRARY", "")).resolve() != tcl_library:
    raise SystemExit("TCL_LIBRARY does not identify relocated scripts")
if pathlib.Path(os.environ.get("TK_LIBRARY", "")).resolve() != tk_library:
    raise SystemExit("TK_LIBRARY does not identify relocated scripts")
if not (tk_library / "tk.tcl").is_file():
    raise SystemExit("relocated Tk script root is incomplete")
if str(_tkinter.TCL_VERSION) != str(tcl_tk.get("tcl_api_version")):
    raise SystemExit("relocated Tcl API version differs from staged provenance")
if str(_tkinter.TK_VERSION) != str(tcl_tk.get("tk_api_version")):
    raise SystemExit("relocated Tk API version differs from staged provenance")
if tcl_version != str(tcl_tk.get("tcl_runtime_version")):
    raise SystemExit("relocated Tcl runtime version differs from staged provenance")
tk_text = (tk_library / "tk.tcl").read_text(encoding="utf-8", errors="strict")
tk_versions = re.findall(
    r"^package\s+(?:require\s+-exact|provide)\s+Tk\s+([^\s]+)\s*$",
    tk_text,
    re.MULTILINE,
)
tk_versions = list(dict.fromkeys(tk_versions))
if len(tk_versions) != 1 or tk_versions[0] != str(tcl_tk.get("tk_runtime_version")):
    raise SystemExit(
        f"relocated Tk script version differs from staged provenance: {tk_versions!r}"
    )

lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
lib._dyld_image_count.restype = ctypes.c_uint32
lib._dyld_get_image_name.argtypes = [ctypes.c_uint32]
lib._dyld_get_image_name.restype = ctypes.c_char_p
images = set()
for index in range(lib._dyld_image_count()):
    raw = lib._dyld_get_image_name(index)
    if raw:
        images.add(pathlib.Path(os.fsdecode(raw)).resolve())

expected_tcl = (python_home / "lib/libtcl8.6.dylib").resolve()
expected_tk = (python_home / "lib/libtk8.6.dylib").resolve()
provider_hashes = tcl_tk.get("staged_hashes", {})
for label, provider in (("Tcl", expected_tcl), ("Tk", expected_tk)):
    if provider not in images:
        raise SystemExit(f"relocated {label} provider was not mapped: {provider}")
provider_sha = {
    "tcl": hashlib.sha256(expected_tcl.read_bytes()).hexdigest(),
    "tk": hashlib.sha256(expected_tk.read_bytes()).hexdigest(),
}
if provider_sha["tcl"] != provider_hashes.get("tcl") or provider_sha["tk"] != provider_hashes.get("tk"):
    raise SystemExit("relocated Tcl/Tk provider bytes differ from staged provenance")

foreign = []
for path in sorted(images):
    if path == runtime or runtime in path.parents:
        continue
    text = str(path)
    if text.startswith(("/usr/lib/", "/System/Library/")):
        continue
    foreign.append(text)
if foreign:
    raise SystemExit(f"relocated runtime mapped ambient native providers: {foreign!r}")

payload = {
    "format_version": 1,
    "status": "pass",
    "profile": profile,
    "architecture": target_arch,
    "python_prefix": "Python.framework/Versions/3.12",
    "tcl": {
        "api_version": str(_tkinter.TCL_VERSION),
        "runtime_version": tcl_version,
        "script_root": "tcl/tcl8.6",
        "provider": "Python.framework/Versions/3.12/lib/libtcl8.6.dylib",
        "provider_sha256": provider_sha["tcl"],
    },
    "tk": {
        "api_version": str(_tkinter.TK_VERSION),
        "runtime_version": tk_versions[0],
        "script_root": "tcl/tk8.6",
        "provider": "Python.framework/Versions/3.12/lib/libtk8.6.dylib",
        "provider_sha256": provider_sha["tk"],
    },
    "hostile_environment": {
        "python_path": "poisoned",
        "user_base": "poisoned",
        "dyld_library_path": "poisoned",
        "dyld_fallback_library_path": "poisoned",
    },
}
output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"relocated_tcl_version={tcl_version}")
print(f"relocated_tcl_provider={expected_tcl.relative_to(runtime).as_posix()}")
print(f"relocated_tk_provider={expected_tk.relative_to(runtime).as_posix()}")
print("relocated_tcl_tk=pass")
PY

echo "relocation_evidence=$output"
