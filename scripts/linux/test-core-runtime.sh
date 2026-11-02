#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: scripts/linux/test-core-runtime.sh [--runtime <runtime-root>] [--image <manylinux-image@sha256:digest>]" >&2
    exit 2
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
runtime="${PY_RUNTIME_OUTPUT:-$repo_root/out/linux-x64/core}"
expected_image="quay.io/pypa/manylinux2014_x86_64@sha256:296069bc05d85275248f168f6e3b34ba8aa06181b67e40cd7447c64d56d3a851"
image="$expected_image"

while (($#)); do
    case "$1" in
        --runtime)
            (($# >= 2)) || usage
            runtime="$2"
            shift 2
            ;;
        --image)
            (($# >= 2)) || usage
            image="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "unknown argument: $1" >&2
            usage
            ;;
    esac
done

[[ "$image" == "$expected_image" ]] || {
    echo "runtime qualification must use the pinned manylinux2014 image: $expected_image" >&2
    exit 1
}
[[ "${PY_RUNTIME_BUILD_IMAGE_REF:-}" == "$expected_image" ]] || {
    echo "PY_RUNTIME_BUILD_IMAGE_REF must identify the executing pinned image: $expected_image" >&2
    exit 1
}

for tool in getconf uname readelf ldd sha256sum find grep sed rpm rpm2cpio cpio; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "required runtime qualification tool is missing: $tool" >&2
        exit 1
    }
done

[[ "$(uname -m)" == "x86_64" ]] || {
    echo "runtime qualification host must be x86_64; observed $(uname -m)" >&2
    exit 1
}
glibc_identity="$(getconf GNU_LIBC_VERSION)"
[[ "$glibc_identity" == "glibc 2.17" ]] || {
    echo "runtime qualification must execute on glibc 2.17; observed $glibc_identity" >&2
    exit 1
}

runtime="$(cd "$runtime" && pwd -P)"
python="$runtime/bin/python3.14"
libpython="$runtime/lib/libpython3.14.so.1.0"
stdlib="$runtime/lib/python3.14"
dynload="$stdlib/lib-dynload"
site_packages="$stdlib/site-packages"
tcl_root="$runtime/tcl/tcl8.6"
tk_root="$runtime/tcl/tk8.6"
native_tcltk="$runtime/lib/tcltk"
provenance="$runtime/build-provenance-linux.json"

required=(
    "$python"
    "$libpython"
    "$stdlib"
    "$dynload"
    "$site_packages"
    "$tcl_root/init.tcl"
    "$tk_root/tk.tcl"
    "$native_tcltk"
    "$runtime/ssl/cert.pem"
    "$runtime/ssl/openssl.cnf"
    "$runtime/LICENSES/Certifi.txt"
    "$runtime/LICENSES/Libxcrypt/LICENSING.txt"
    "$runtime/LICENSES/Libxcrypt/COPYING.LIB.txt"
    "$provenance"
)
for path in "${required[@]}"; do
    [[ -e "$path" ]] || {
        echo "controlled runtime is missing required path: $path" >&2
        exit 1
    }
done

if find "$site_packages" -mindepth 1 -print -quit | grep -q .; then
    echo "controlled core runtime site-packages must be empty" >&2
    exit 1
fi
if find "$runtime" -mindepth 1 \( -iname 'numpy' -o -iname 'numpy-*' -o -iname 'numpy.*' -o -iname '*numpy*.so*' -o -iname '*numpy*.a' \) -print -quit | grep -q .; then
    echo "NumPy payload is forbidden in the controlled core runtime" >&2
    exit 1
fi

mapfile -t tkinter_candidates < <(find "$dynload" -maxdepth 1 -type f -name '_tkinter*.so' -print | sort)
[[ ${#tkinter_candidates[@]} -eq 1 ]] || {
    echo "expected exactly one staged _tkinter extension; found ${#tkinter_candidates[@]}" >&2
    exit 1
}
tkinter_so="${tkinter_candidates[0]}"
tcl_provider="$(find -L "$native_tcltk" -maxdepth 1 -type f -name 'libtcl8.6.so*' -print | sort | sed -n '1p')"
tk_provider="$(find -L "$native_tcltk" -maxdepth 1 -type f -name 'libtk8.6.so*' -print | sort | sed -n '1p')"
[[ -n "$tcl_provider" && -n "$tk_provider" ]] || {
    echo "staged Tcl/Tk 8.6 native providers are incomplete" >&2
    exit 1
}

tkinter_needed="$(readelf -dW "$tkinter_so" | sed -n 's/.*(NEEDED).*Shared library: \[\([^]]*\)\].*/\1/p')"
grep -Eq '^libtcl8\.6\.so([.]0)?$' <<<"$tkinter_needed" || {
    echo "_tkinter does not dynamically depend on Tcl 8.6" >&2
    exit 1
}
grep -Eq '^libtk8\.6\.so([.]0)?$' <<<"$tkinter_needed" || {
    echo "_tkinter does not dynamically depend on Tk 8.6" >&2
    exit 1
}
tk_needed="$(readelf -dW "$tk_provider" | sed -n 's/.*(NEEDED).*Shared library: \[\([^]]*\)\].*/\1/p')"
grep -Fx 'libX11.so.6' <<<"$tk_needed" >/dev/null || {
    echo "staged Tk must keep X11 as a dynamic system provider" >&2
    exit 1
}
if find "$runtime" -mindepth 1 \( -name 'libX11.so*' -o -name 'libX11*.a' -o -name 'libxcb.so*' -o -name 'libxcb*.so*' -o -name 'libxcb*.a' \) -print -quit | grep -q .; then
    echo "controlled runtime must not bundle a competing X11/XCB provider" >&2
    exit 1
fi

poison="$(mktemp -d)"
cleanup() {
    rm -rf "$poison"
}
trap cleanup EXIT
mkdir -p "$poison/python" "$poison/user" "$poison/home" "$poison/native"
cat > "$poison/python/numpy.py" <<'PY'
raise RuntimeError("ambient NumPy poison imported")
PY
cat > "$poison/python/sitecustomize.py" <<'PY'
raise RuntimeError("ambient sitecustomize poison imported")
PY
cat > "$poison/python/usercustomize.py" <<'PY'
raise RuntimeError("ambient usercustomize poison imported")
PY

env -u SSL_CERT_FILE -u SSL_CERT_DIR -u OPENSSL_CONF -u OPENSSL_MODULES \
PYTHONHOME="$poison/home" \
PYTHONPATH="$poison/python" \
PYTHONUSERBASE="$poison/user" \
LD_LIBRARY_PATH="$poison/native" \
TCL_LIBRARY="$tcl_root" \
TK_LIBRARY="$tk_root" \
"$python" -I - "$runtime" "$provenance" "$expected_image" <<'PY'
import hashlib
import importlib.util
import json
import os
import pathlib
import platform
import re
import subprocess
import sys

runtime = pathlib.Path(sys.argv[1]).resolve()
provenance_path = pathlib.Path(sys.argv[2]).resolve()
expected_image = sys.argv[3]
PEP599_EXTERNAL = {
    "libgcc_s.so.1", "libstdc++.so.6", "libm.so.6", "libdl.so.2",
    "librt.so.1", "libc.so.6", "libnsl.so.1", "libutil.so.1",
    "libpthread.so.0", "libresolv.so.2", "libX11.so.6", "libXext.so.6",
    "libXrender.so.1", "libICE.so.6", "libSM.so.6", "libGL.so.1",
    "libgobject-2.0.so.0", "libgthread-2.0.so.0", "libglib-2.0.so.0",
}
SOURCE_PINS = (
    ("Python-3.14.10.tar.xz",
     "https://www.python.org/ftp/python/3.14.10/Python-3.14.10.tar.xz",
     "de414a57119abef52784d8993e341d53aed635fc2b05c2607d9f98a2c5b72032"),
    ("openssl-3.5.9.tar.gz",
     "https://github.com/openssl/openssl/releases/download/openssl-3.5.9/openssl-3.5.9.tar.gz",
     "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"),
    ("libxcrypt-4.5.2.tar.xz",
     "https://github.com/besser82/libxcrypt/releases/download/v4.5.2/libxcrypt-4.5.2.tar.xz",
     "71513a31c01a428bccd5367a32fd95f115d6dac50fb5b60c779d5c7942aec071"),
    ("tcl8.6.18-src.tar.gz",
     "https://prdownloads.sourceforge.net/tcl/tcl8.6.18-src.tar.gz",
     "14f9af32b1767ff718477a8f974ad03c34341097e6b43f4ce54644ee974e268e"),
    ("tk8.6.18-src.tar.gz",
     "https://prdownloads.sourceforge.net/tcl/tk8.6.18-src.tar.gz",
     "95cd528a80f5e4bdb557af9b14a7197d6860793a3894e25e7c9fad2ed05d4c3c"),
)
DEV_RPM_PINS = (
    ("bzip2-devel", "1.0.6-13.el7", "bzip2-libs", "1.0.6-13.el7", "bzip2-devel-1.0.6-13.el7.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/bzip2-devel-1.0.6-13.el7.x86_64.rpm", "28e0eef4dfa7e5a79bd4e46a5174091c87027a2b2e33e1e9e19958bcc1ac6700"),
    ("gdbm-devel", "1.10-8.el7", "gdbm", "1.10-8.el7", "gdbm-devel-1.10-8.el7.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/gdbm-devel-1.10-8.el7.x86_64.rpm", "fc02a803f89c0d68de96bcc7a46d7cdf8768e62577b27aed9c531e6fb2818c30"),
    ("libffi-devel", "3.0.13-19.el7", "libffi", "3.0.13-19.el7", "libffi-devel-3.0.13-19.el7.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/libffi-devel-3.0.13-19.el7.x86_64.rpm", "a31ba33f74252929d396e4d8557fe1c40be95d777aad0660771b14f1187b3b2e"),
    ("libuuid-devel", "2.23.2-65.el7_9.1", "libuuid", "2.23.2-65.el7_9.1", "libuuid-devel-2.23.2-65.el7_9.1.x86_64.rpm", "https://vault.centos.org/7.9.2009/updates/x86_64/Packages/libuuid-devel-2.23.2-65.el7_9.1.x86_64.rpm", "edc0594488f23ae7a46857065d3cd4d74e61a711b96dd5b99713419b033816a4"),
    ("ncurses-devel", "5.9-14.20130511.el7_4", "ncurses-libs", "5.9-14.20130511.el7_4", "ncurses-devel-5.9-14.20130511.el7_4.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/ncurses-devel-5.9-14.20130511.el7_4.x86_64.rpm", "1c93fc77242cf1129f1aa44a8e31fa118511681776d17f0ffb2c3e84bb811a6d"),
    ("readline-devel", "6.2-11.el7", "readline", "6.2-11.el7", "readline-devel-6.2-11.el7.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/readline-devel-6.2-11.el7.x86_64.rpm", "dd4f93b315f8a2facdd3508ac5b7fe070d663d4ebf70cdfa2781ffb6abd25160"),
    ("xz-devel", "5.2.2-2.el7_9", "xz-libs", "5.2.2-2.el7_9", "xz-devel-5.2.2-2.el7_9.x86_64.rpm", "https://vault.centos.org/7.9.2009/updates/x86_64/Packages/xz-devel-5.2.2-2.el7_9.x86_64.rpm", "8c62263350421e7c3081b4a531f8e15ef5bfb5e410a34687ca4a799ff23362ef"),
)
LICENSE_RPM_PINS = (
    ("bzip2-libs", "bzip2-libs", "1.0.6-13.el7", "x86_64", "BSD", "bzip2-libs-1.0.6-13.el7.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/bzip2-libs-1.0.6-13.el7.x86_64.rpm", "1fd32866a2a82f5abc19142b59dce8fd961bc1d32e3bedfa0de7a950066fdaa5", "usr/share/doc/bzip2-libs-1.0.6/LICENSE", "4919cfb14a73cd64fcef67b107613970cf1659a09aa675dba31314f373bc7204"),
    ("gdbm", "gdbm", "1.10-8.el7", "x86_64", "GPLv3+", "gdbm-1.10-8.el7.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/gdbm-1.10-8.el7.x86_64.rpm", "65f28fdefa291bfe53e9c8646ce0f7510f312a4fee232af36e3d4fe678b60591", "usr/share/doc/gdbm-1.10/COPYING", "690d762f2e8e149ab1e2d6a409a3853b6151a2533b2382fae549a176d6bedecf"),
    ("libffi", "libffi", "3.0.13-19.el7", "x86_64", "MIT and Public Domain", "libffi-3.0.13-19.el7.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/libffi-3.0.13-19.el7.x86_64.rpm", "670463ed744b7243cfe32f0c4aa6676657a6078a6e2009a51fdb7b907b98ef71", "usr/share/doc/libffi-3.0.13/LICENSE", "414f460aed519486beb78e92ea712c9ccf87c1acfb735a82d301f4cdf06f48e3"),
    ("libuuid", "libuuid", "2.23.2-65.el7_9.1", "x86_64", "BSD", "libuuid-2.23.2-65.el7_9.1.x86_64.rpm", "https://vault.centos.org/7.9.2009/updates/x86_64/Packages/libuuid-2.23.2-65.el7_9.1.x86_64.rpm", "f530a65c0efca53f618f63e02801129283b12266fbf21ac221e35617a45b0d4b", "usr/share/doc/libuuid-2.23.2/COPYING", "18a4a3931b5b703202793f7ec14b07639dd6b502162049f66250aa6445032ae3"),
    ("ncurses-libs", "ncurses", "5.9-14.20130511.el7_4", "x86_64", "MIT", "ncurses-5.9-14.20130511.el7_4.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/ncurses-5.9-14.20130511.el7_4.x86_64.rpm", "59773dff8c5aa8f084fd2edc88b5dc34f574f254ec98280fc2b0adafe07f85a2", "usr/share/doc/ncurses-5.9/README", "37e56186af1edbc4b0c41b85e224295fe2ef114399a488651ebc658f57bf80c7"),
    ("readline", "readline", "6.2-11.el7", "x86_64", "GPLv3+", "readline-6.2-11.el7.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/readline-6.2-11.el7.x86_64.rpm", "d9e52b61beb1ac9c3006604d9ebd4f930f667b64478a61fd78de7ed74922f52e", "usr/share/doc/readline-6.2/COPYING", "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903"),
    ("xz-libs", "xz-libs", "5.2.2-2.el7_9", "x86_64", "LGPLv2+", "xz-libs-5.2.2-2.el7_9.x86_64.rpm", "https://vault.centos.org/7.9.2009/updates/x86_64/Packages/xz-libs-5.2.2-2.el7_9.x86_64.rpm", "4b698de5fd7e0a64306106f3018e9d00dedc1f7a46d354339f012c97d004bd0c", "usr/share/doc/xz-libs-5.2.2/COPYING.LGPLv2.1", "dc626520dcd53a22f727af3ee42c770e56c97a64fe3adb063799d8ab032fe551"),
    ("zlib", "zlib", "1.2.7-21.el7_9", "x86_64", "zlib and Boost", "zlib-1.2.7-21.el7_9.x86_64.rpm", "https://vault.centos.org/7.9.2009/updates/x86_64/Packages/zlib-1.2.7-21.el7_9.x86_64.rpm", "2bee86d12902723756eafa82ba6cc322136c4a67a78e0104ac988e14eb7fb5a1", "usr/share/doc/zlib-1.2.7/README", "a4dfc7f4225a68315763aafb536428852aff6dd3135ab4e0cdc9e3e6a65fd796"),
)

def fail(message):
    raise SystemExit(message)

def run(*args, env=None):
    proc = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, env=env, check=False)
    if proc.returncode:
        fail(f"command failed ({proc.returncode}): {' '.join(map(str, args))}\n{proc.stdout}")
    return proc.stdout

def under(path, root):
    path = pathlib.Path(path).resolve()
    root = pathlib.Path(root).resolve()
    return path == root or root in path.parents

def sha256(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def extract_rpm_member(archive_path, member):
    rpm2cpio = subprocess.Popen(
        ["rpm2cpio", str(archive_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert rpm2cpio.stdout is not None
    assert rpm2cpio.stderr is not None
    member_extract = subprocess.run(
        ["cpio", "-i", "--to-stdout", f"./{member}"],
        stdin=rpm2cpio.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    rpm2cpio.stdout.close()
    rpm2cpio_stderr = rpm2cpio.stderr.read().decode("utf-8", errors="replace")
    rpm2cpio_returncode = rpm2cpio.wait()
    if rpm2cpio_returncode != 0 or member_extract.returncode != 0:
        fail(
            f"could not extract pinned legal-evidence member {member}: "
            f"rpm2cpio={rpm2cpio_returncode} {rpm2cpio_stderr!r}; "
            f"cpio={member_extract.returncode} {member_extract.stderr.decode('utf-8', errors='replace')!r}"
        )
    return member_extract.stdout

def helper_dynamic_extension_baseline():
    helper_python = pathlib.Path("/opt/python/cp314-cp314/bin/python")
    if not helper_python.is_file():
        fail(f"pinned-image helper CPython 3.14 is missing: {helper_python}")
    helper_code = r'''
import importlib.machinery
import json
import pathlib
import sys
import sysconfig

if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 14):
    raise SystemExit(
        f"expected pinned-image helper CPython 3.14, got "
        f"{sys.implementation.name} {sys.version_info.major}.{sys.version_info.minor}"
    )
prefix = pathlib.Path(sys.prefix).resolve()
expected_prefix = pathlib.Path("/opt/python/cp314-cp314").resolve()
if prefix != expected_prefix:
    raise SystemExit(f"unexpected pinned-image helper prefix: {prefix}")
destshared = sysconfig.get_config_var("DESTSHARED")
dynload = (
    pathlib.Path(destshared)
    if destshared
    else pathlib.Path(sysconfig.get_path("stdlib")) / "lib-dynload"
).resolve()
expected_dynload = expected_prefix / "lib/python3.14/lib-dynload"
if dynload != expected_dynload or not dynload.is_dir():
    raise SystemExit(f"unexpected pinned-image helper lib-dynload: {dynload}")
suffixes = sorted(set(importlib.machinery.EXTENSION_SUFFIXES), key=len, reverse=True)
modules = set()
for path in sorted(dynload.iterdir()):
    if not path.is_file():
        continue
    for suffix in suffixes:
        if path.name.endswith(suffix):
            name = path.name[:-len(suffix)]
            if not name:
                raise SystemExit(f"empty module name from helper extension filename: {path.name}")
            modules.add(name)
            break
if not modules:
    raise SystemExit(f"pinned-image helper lib-dynload is empty: {dynload}")
print(json.dumps({
    "helper_python": str(pathlib.Path(sys.executable).resolve()),
    "helper_version": sys.version.split()[0],
    "lib_dynload": str(dynload),
    "extension_suffixes": suffixes,
    "modules": sorted(modules),
}, sort_keys=True))
'''
    helper_env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "LD_LIBRARY_PATH", "LD_PRELOAD"):
        helper_env.pop(key, None)
    output = run(str(helper_python), "-I", "-S", "-c", helper_code, env=helper_env)
    try:
        baseline = json.loads(output)
    except json.JSONDecodeError as exc:
        fail(f"could not decode pinned-image helper dynamic-extension baseline: {exc}: {output!r}")
    modules = baseline.get("modules") if isinstance(baseline, dict) else None
    if (
        not isinstance(modules, list)
        or not modules
        or any(not isinstance(name, str) or not name for name in modules)
        or modules != sorted(set(modules))
    ):
        fail(f"invalid pinned-image helper dynamic-extension baseline: {baseline!r}")
    return baseline

def strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value

def dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from dicts(item)

def nonempty(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None

def dynamic(path):
    text = run("readelf", "-dW", str(path))
    needed = re.findall(r"\(NEEDED\).*?Shared library: \[([^\]]+)\]", text)
    soname_match = re.search(r"\(SONAME\).*?Library soname: \[([^\]]+)\]", text)
    runpaths = re.findall(r"\((?:RUNPATH|RPATH)\).*?\[([^\]]*)\]", text)
    return needed, soname_match.group(1) if soname_match else None, runpaths

def vtuple(text):
    return tuple(int(part) for part in text.split("."))

def check_versions(path):
    text = run("readelf", "--version-info", "-W", str(path))
    ceilings = {
        "GLIBC": (2, 17),
        "CXXABI": (1, 3, 7),
        "GLIBCXX": (3, 4, 19),
        "GCC": (4, 8, 0),
    }
    tokens = set(re.findall(r"\b(?:GLIBC|CXXABI|GLIBCXX|GCC)_[A-Za-z0-9_.]+\b", text))
    for token in tokens:
        if token == "CXXABI_TM_1":
            continue
        prefix, version = token.split("_", 1)
        ceiling = ceilings[prefix]
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", version):
            fail(f"{path.relative_to(runtime)} uses unsupported version namespace: {token}")
        value = vtuple(version)
        padded = value + (0,) * (len(ceiling) - len(value))
        if padded > ceiling:
            fail(f"{path.relative_to(runtime)} exceeds {prefix}_{'.'.join(map(str, ceiling))}: {token}")

if sys.implementation.name != "cpython" or platform.python_version() != "3.14.10":
    fail(f"staged interpreter is not CPython 3.14.10: {platform.python_version()}")
if platform.machine() != "x86_64":
    fail(f"staged interpreter architecture is not x86_64: {platform.machine()}")
if not under(sys.executable, runtime) or not under(sys.base_prefix, runtime):
    fail("staged interpreter prefix/executable escaped the controlled runtime")

data = json.loads(provenance_path.read_text(encoding="utf-8"))
if data.get("format_version") != 2:
    fail(f"unexpected controlled-runtime provenance format: {data.get('format_version')!r}")

baseline = data.get("baseline", {})
expected_digest = expected_image.rsplit("@", 1)[1]
if baseline.get("image") != expected_image:
    fail(f"provenance image mismatch: {baseline.get('image')!r}")
if baseline.get("image_repository") != "quay.io/pypa/manylinux2014_x86_64":
    fail(f"provenance image repository mismatch: {baseline.get('image_repository')!r}")
if baseline.get("image_digest") != expected_digest:
    fail(f"provenance image digest mismatch: {baseline.get('image_digest')!r}")
if baseline.get("architecture") != "x86_64" or baseline.get("glibc") != "2.17":
    fail(f"provenance baseline identity mismatch: {baseline!r}")
if set(baseline.get("pep599_external_allowlist", [])) != PEP599_EXTERNAL:
    fail("provenance PEP 599 external-library allowlist differs from the adopted manylinux2014 baseline")
if baseline.get("symbol_ceilings") != {
    "GLIBC": "2.17",
    "CXXABI": "1.3.7",
    "CXXABI_additional_allowed": ["CXXABI_TM_1"],
    "GLIBCXX": "3.4.19",
    "GCC": "4.8.0",
}:
    fail(f"provenance symbol ceilings are invalid: {baseline.get('symbol_ceilings')!r}")

expected_sources = {
    "cpython": {
        "version": "3.14.10",
        "filename": "Python-3.14.10.tar.xz",
        "url": "https://www.python.org/ftp/python/3.14.10/Python-3.14.10.tar.xz",
        "sha256": "de414a57119abef52784d8993e341d53aed635fc2b05c2607d9f98a2c5b72032",
    },
    "openssl": {
        "version": "3.5.9",
        "filename": "openssl-3.5.9.tar.gz",
        "url": "https://github.com/openssl/openssl/releases/download/openssl-3.5.9/openssl-3.5.9.tar.gz",
        "sha256": "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a",
    },
    "libxcrypt": {
        "version": "4.5.2",
        "filename": "libxcrypt-4.5.2.tar.xz",
        "url": "https://github.com/besser82/libxcrypt/releases/download/v4.5.2/libxcrypt-4.5.2.tar.xz",
        "sha256": "71513a31c01a428bccd5367a32fd95f115d6dac50fb5b60c779d5c7942aec071",
        "usage": "license-provenance-only",
    },
    "tcl": {
        "version": "8.6.18",
        "filename": "tcl8.6.18-src.tar.gz",
        "url": "https://prdownloads.sourceforge.net/tcl/tcl8.6.18-src.tar.gz",
        "sha256": "14f9af32b1767ff718477a8f974ad03c34341097e6b43f4ce54644ee974e268e",
    },
    "tk": {
        "version": "8.6.18",
        "filename": "tk8.6.18-src.tar.gz",
        "url": "https://prdownloads.sourceforge.net/tcl/tk8.6.18-src.tar.gz",
        "sha256": "95cd528a80f5e4bdb557af9b14a7197d6860793a3894e25e7c9fad2ed05d4c3c",
    },
}
sources = data.get("sources", {})
for component, expected in expected_sources.items():
    observed = sources.get(component, {})
    for key, value in expected.items():
        if observed.get(key) != value:
            fail(f"provenance source mismatch for {component}.{key}: {observed.get(key)!r}")
    if observed.get("verified_archive_sha256") != expected["sha256"]:
        fail(f"verified source archive hash mismatch for {component}")

license_rpm_input_index = {}
observed_license_rpms = sources.get("license_rpms", [])
if not isinstance(observed_license_rpms, list) or len(observed_license_rpms) != len(LICENSE_RPM_PINS):
    fail(f"provenance license/provenance RPM source set is invalid: {observed_license_rpms!r}")
observed_license_rpm_index = {
    (record.get("package"), record.get("version_release"), record.get("arch")): record
    for record in observed_license_rpms
    if isinstance(record, dict)
}
if len(observed_license_rpm_index) != len(LICENSE_RPM_PINS):
    fail(f"provenance license/provenance RPM source set has invalid or duplicate identities: {observed_license_rpms!r}")
for package, archive_package, version_release, arch, license_id, filename, url, digest, member, member_digest in LICENSE_RPM_PINS:
    key = (package, version_release, arch)
    observed = observed_license_rpm_index.get(key)
    if not isinstance(observed, dict):
        fail(f"missing license/provenance RPM source record: {key}")
    expected_fields = {
        "package": package,
        "archive_package": archive_package,
        "version_release": version_release,
        "arch": arch,
        "license": license_id,
        "filename": filename,
        "url": url,
        "sha256": digest,
        "verified_archive_sha256": digest,
        "member": member,
        "member_sha256": member_digest,
        "source_sha256": member_digest,
        "usage": "license-provenance-only",
    }
    for field, expected_value in expected_fields.items():
        if observed.get(field) != expected_value:
            fail(f"license/provenance RPM source mismatch for {key}.{field}: {observed.get(field)!r}")
    archive_path = pathlib.Path(str(observed.get("verified_archive_path", "")))
    source_path = pathlib.Path(str(observed.get("source_path", "")))
    if not archive_path.is_absolute() or not archive_path.is_file() or sha256(archive_path) != digest:
        fail(f"license/provenance RPM archive is missing or changed: {key}: {archive_path}")
    if not source_path.is_absolute() or not source_path.is_file() or sha256(source_path) != member_digest:
        fail(f"license/provenance RPM extracted member is missing or changed: {key}: {source_path}")
    archive_identity = subprocess.run(
        ["rpm", "-qp", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\t%{LICENSE}", str(archive_path)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if archive_identity.returncode != 0 or archive_identity.stdout.rstrip("\n").split("\t") != [
        archive_package, version_release, arch, license_id,
    ]:
        fail(f"license/provenance RPM identity mismatch for {key}: {archive_identity.stdout!r}")
    member_bytes = extract_rpm_member(archive_path, member)
    if hashlib.sha256(member_bytes).hexdigest() != member_digest or source_path.read_bytes() != member_bytes:
        fail(f"license/provenance RPM member does not match pinned bytes: {key}: {member}")
    license_rpm_input_index[key] = {"record": observed, "member_bytes": member_bytes}

expected_dev_rpms = [
    {
        "package": package,
        "version_release": version_release,
        "runtime_package": runtime_package,
        "runtime_version_release": runtime_version_release,
        "arch": "x86_64",
        "filename": filename,
        "url": url,
        "sha256": digest,
        "verified_archive_sha256": digest,
        "usage": "build-only-development-overlay",
    }
    for package, version_release, runtime_package, runtime_version_release, filename, url, digest
    in DEV_RPM_PINS
]
if sources.get("development_rpms") != expected_dev_rpms:
    fail(f"provenance development RPM source set is invalid: {sources.get('development_rpms')!r}")
for record in expected_dev_rpms:
    installed = run(
        "rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}\t%{ARCH}", record["runtime_package"]
    ).strip()
    expected_installed = f"{record['runtime_version_release']}\tx86_64"
    if installed != expected_installed:
        fail(
            f"pinned-image runtime package identity changed for {record['runtime_package']}: "
            f"{installed!r} != {expected_installed!r}"
        )

toolchain = data.get("toolchain", {})
for name in ("gcc", "ld", "as", "ar", "ranlib", "strip", "pkg_config", "make", "patchelf"):
    record = toolchain.get(name)
    if not isinstance(record, dict):
        fail(f"provenance is missing toolchain identity: {name}")
    tool_path = pathlib.Path(record.get("path", ""))
    tool_hash = record.get("sha256")
    if not tool_path.is_absolute() or not tool_path.is_file():
        fail(f"provenance toolchain path is invalid for {name}: {tool_path}")
    if not isinstance(tool_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", tool_hash):
        fail(f"provenance toolchain hash is invalid for {name}: {tool_hash!r}")
    if sha256(tool_path) != tool_hash:
        fail(f"toolchain byte identity changed for {name}: {tool_path}")
    if not isinstance(record.get("version_output"), str) or not record["version_output"].strip():
        fail(f"provenance toolchain version output is empty for {name}")

build = data.get("build", {})
expected_configure = {
    "openssl_configure": {
        "--prefix=<build>/openssl-prefix",
        "--openssldir=<build>/openssl-prefix",
        "--libdir=lib",
        "no-afalgeng",
    },
    "tcl_configure": {"--enable-shared", "--enable-threads", "--enable-64bit"},
    "tk_configure": {"--with-tcl=<source>/tcl8.6.18/unix", "--enable-shared", "--enable-threads", "--enable-64bit"},
    "cpython_configure": {
        "--enable-shared",
        "--without-static-libpython",
        "--with-ensurepip=no",
        "--with-openssl-rpath=no",
        "--with-openssl=<source-built-openssl-prefix>",
    },
}
for key, expected in expected_configure.items():
    if set(build.get(key, [])) != expected:
        fail(f"provenance configure flags differ for {key}: {build.get(key)!r}")
for key in ("cflags", "ldflags", "closed_input_policy"):
    if not isinstance(build.get(key), str) or not build[key].strip():
        fail(f"provenance build metadata is missing {key}")
if (
    "one hash-verified libxcrypt license/provenance archive" not in build["closed_input_policy"]
    or "seven hash-verified matching CentOS 7 development RPMs installed offline" not in build["closed_input_policy"]
    or "eight hash-verified CentOS 7 runtime RPMs used only for fallback license/provenance evidence" not in build["closed_input_policy"]
):
    fail(f"provenance closed-input policy is unexpected: {build['closed_input_policy']!r}")
if build.get("development_header_overlay") != expected_dev_rpms:
    fail(f"provenance development header overlay is invalid: {build.get('development_header_overlay')!r}")
if build.get("cpython_pkg_config_path") != [
    "<build>/openssl-prefix/lib/pkgconfig",
    "<build>/openssl-prefix/lib64/pkgconfig",
    "<build>/tcltk-prefix/lib/pkgconfig",
    "/usr/local/lib/pkgconfig",
]:
    fail(f"provenance CPython pkg-config search path is invalid: {build.get('cpython_pkg_config_path')!r}")
if build.get("cpython_pkg_config_libdir") != [
    "/usr/lib64/pkgconfig",
    "/usr/lib/pkgconfig",
    "/usr/share/pkgconfig",
]:
    fail(
        "provenance CPython pkg-config default-library search path is invalid: "
        f"{build.get('cpython_pkg_config_libdir')!r}"
    )
if not pathlib.Path("/usr/local/lib/pkgconfig").is_dir():
    fail("pinned-image /usr/local/lib/pkgconfig is missing during qualification")
openssl_perl_compat = build.get("openssl_perl_compat", {})
if openssl_perl_compat.get("scope") != (
    "OpenSSL Configure executable discovery and Unix Makefile release-date formatting only"
):
    fail(f"OpenSSL Perl compatibility provenance is invalid: {openssl_perl_compat!r}")
compat_modules = openssl_perl_compat.get("modules")
if not isinstance(compat_modules, list) or len(compat_modules) != 2:
    fail(f"OpenSSL Perl compatibility module provenance is invalid: {compat_modules!r}")
compat_index = {
    entry.get("module"): entry
    for entry in compat_modules
    if isinstance(entry, dict) and isinstance(entry.get("module"), str)
}
if set(compat_index) != {"IPC::Cmd", "Time::Piece"}:
    fail(f"OpenSSL Perl compatibility module set is invalid: {compat_modules!r}")
for module, record in compat_index.items():
    if (
        record.get("source") != "scripts/linux/build-core-runtime.sh:inline"
        or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))
    ):
        fail(f"OpenSSL Perl compatibility module provenance is invalid for {module}: {record!r}")
toolchain = data.get("toolchain", {})
perl_identity = toolchain.get("perl", {})
perl_path = pathlib.Path(str(perl_identity.get("path", "")))
if not perl_path.is_absolute() or not perl_path.is_file():
    fail(f"provenance Perl tool identity path is invalid: {perl_path}")
if perl_identity.get("sha256") != sha256(perl_path):
    fail(f"provenance Perl tool byte identity mismatch: {perl_path}")
if not isinstance(perl_identity.get("version_output"), str) or not perl_identity["version_output"].strip():
    fail("provenance Perl version identity is missing")
openssl_preflight = build.get("openssl_image_baseline", {})
for path_key, hash_key in (
    ("helper_ssl_extension", "helper_ssl_extension_sha256"),
    ("ssl_provider", "ssl_provider_sha256"),
    ("crypto_provider", "crypto_provider_sha256"),
):
    source = pathlib.Path(openssl_preflight.get(path_key, ""))
    expected_hash = openssl_preflight.get(hash_key)
    if not source.is_absolute() or not source.is_file():
        fail(f"OpenSSL preflight source path is invalid: {path_key}={source}")
    if not isinstance(expected_hash, str) or sha256(source) != expected_hash:
        fail(f"OpenSSL preflight byte identity mismatch: {path_key}={source}")
if not isinstance(openssl_preflight.get("version"), str) or not openssl_preflight["version"].strip():
    fail("OpenSSL preflight version identity is missing")
if not openssl_preflight["version"].startswith("OpenSSL 3.5.9 "):
    fail(f"pinned-image helper OpenSSL version is unexpected: {openssl_preflight['version']!r}")
if not str(openssl_preflight.get("ssl_needed_name", "")).startswith("libssl.so"):
    fail("OpenSSL preflight libssl DT_NEEDED identity is missing")
if not str(openssl_preflight.get("crypto_needed_name", "")).startswith("libcrypto.so"):
    fail("OpenSSL preflight libcrypto DT_NEEDED identity is missing")

python_meta = data.get("python", {})
if python_meta.get("implementation") != "CPython" or python_meta.get("version") != "3.14.10":
    fail(f"provenance Python identity is invalid: {python_meta!r}")
if python_meta.get("executable") != "bin/python3.14" or python_meta.get("libpython") != "lib/libpython3.14.so.1.0":
    fail("provenance Python runtime paths are invalid")
if python_meta.get("site_packages") != "lib/python3.14/site-packages" or python_meta.get("site_packages_empty") is not True:
    fail("provenance does not attest an empty controlled site-packages directory")
if python_meta.get("numpy_bundled") is not False:
    fail("provenance unexpectedly marks NumPy as bundled")
if python_meta.get("executable_sha256") != sha256(runtime / "bin/python3.14"):
    fail("staged Python executable differs from provenance")
if python_meta.get("libpython_sha256") != sha256(runtime / "lib/libpython3.14.so.1.0"):
    fail("staged libpython differs from provenance")
helper_extension_baseline = helper_dynamic_extension_baseline()
expected_dynamic_extensions = helper_extension_baseline["modules"]
if python_meta.get("dynamic_extension_baseline") != expected_dynamic_extensions:
    fail(
        "provenance dynamic-extension baseline differs from the pinned-image "
        f"helper CPython 3.14 lib-dynload inventory: "
        f"expected={expected_dynamic_extensions!r}, "
        f"recorded={python_meta.get('dynamic_extension_baseline')!r}"
    )
expected_dynamic_extension_source = {
    key: helper_extension_baseline[key]
    for key in ("helper_python", "helper_version", "lib_dynload", "extension_suffixes")
}
if python_meta.get("dynamic_extension_baseline_source") != expected_dynamic_extension_source:
    fail(
        "provenance dynamic-extension baseline source differs from the executing "
        f"pinned image: expected={expected_dynamic_extension_source!r}, "
        f"recorded={python_meta.get('dynamic_extension_baseline_source')!r}"
    )
for name in expected_dynamic_extensions:
    spec = importlib.util.find_spec(name)
    if spec is None:
        fail(f"staged Python is missing helper-baseline native stdlib capability: {name}")
    if spec.origin in ("built-in", "frozen"):
        continue
    if not isinstance(spec.origin, str) or not under(spec.origin, runtime):
        fail(
            "staged Python helper-baseline native stdlib capability escaped "
            f"the controlled runtime: {name} -> {spec.origin!r}"
        )
expected_retained = {
    "ssl", "_ssl", "sqlite3", "_sqlite3", "ctypes", "_ctypes", "bz2", "_bz2",
    "lzma", "_lzma", "zlib", "readline", "curses", "_curses", "curses.panel", "encodings",
    "_curses_panel", "multiprocessing", "zoneinfo", "hashlib", "decimal", "dbm",
    "_dbm", "_gdbm", "uuid", "_uuid", "pyexpat", "xml.parsers.expat", "tkinter", "_tkinter",
}
if set(python_meta.get("retained_module_probe", [])) != expected_retained:
    fail("provenance retained-module surface differs from the phase-01 contract")

license_paths = set()
tcl_tk = data.get("tcl_tk", {})
if (
    tcl_tk.get("version") != "8.6.18"
    or tcl_tk.get("ownership") != "producer"
    or tcl_tk.get("tcl_version") != "8.6.18"
    or tcl_tk.get("tk_version") != "8.6.18"
    or tcl_tk.get("dynamic_provider_model") is not True
    or tcl_tk.get("native_loader_strategy") != "runtime-relative-dt-runpath"
):
    fail(f"provenance Tcl/Tk model is invalid: {tcl_tk!r}")
for key, expected_path in (
    ("tcl_provider", "lib/tcltk/libtcl8.6.so"),
    ("tk_provider", "lib/tcltk/libtk8.6.so"),
):
    record = tcl_tk.get(key, {})
    if record.get("path") != expected_path:
        fail(f"provenance {key} path is invalid: {record.get('path')!r}")
    if record.get("sha256") != sha256(runtime / expected_path):
        fail(f"staged {key} differs from provenance")

ssl_runtime = data.get("ssl_runtime", {})
if ssl_runtime.get("policy") != "runtime-relative-pinned-image-certifi":
    fail(f"provenance SSL runtime policy is invalid: {ssl_runtime!r}")
certifi = ssl_runtime.get("certifi", {})
expected_certifi_source = pathlib.Path("/opt/_internal/vendor/certifi/cacert.pem")
expected_certifi_license = pathlib.Path(
    "/opt/_internal/vendor/certifi-2026.10.23.dist-info/licenses/LICENSE"
)
if (
    certifi.get("version") != "2026.10.23"
    or certifi.get("license") != "MPL-2.0"
    or certifi.get("image") != expected_image
    or certifi.get("source_path") != str(expected_certifi_source)
    or certifi.get("license_source_path") != str(expected_certifi_license)
):
    fail(f"pinned-image certifi provenance is invalid: {certifi!r}")
for source, hash_key in (
    (expected_certifi_source, "source_sha256"),
    (expected_certifi_license, "license_source_sha256"),
):
    if not source.is_file() or certifi.get(hash_key) != sha256(source):
        fail(f"pinned-image certifi source identity mismatch: {source}")
for rel_key, hash_key in (
    ("staged_path", "staged_sha256"),
    ("license_staged_path", "license_staged_sha256"),
):
    rel = certifi.get(rel_key)
    staged = (runtime / str(rel)).resolve()
    if not under(staged, runtime) or not staged.is_file() or certifi.get(hash_key) != sha256(staged):
        fail(f"staged certifi provenance mismatch: {rel!r}")
    if rel_key == "license_staged_path":
        license_paths.add(staged)

openssl_config = ssl_runtime.get("openssl_config", {})
if openssl_config.get("source") != "openssl-3.5.9/apps/openssl.cnf":
    fail(f"OpenSSL config provenance source is invalid: {openssl_config!r}")
openssl_config_staged = (runtime / "ssl/openssl.cnf").resolve()
if (
    openssl_config.get("staged_path") != "ssl/openssl.cnf"
    or not openssl_config_staged.is_file()
    or openssl_config.get("staged_sha256") != sha256(openssl_config_staged)
    or openssl_config.get("source_sha256") != openssl_config.get("staged_sha256")
):
    fail(f"OpenSSL config staged provenance is invalid: {openssl_config!r}")

python_ssl_patch = ssl_runtime.get("python_ssl_patch", {})
ssl_py = (runtime / "lib/python3.14/ssl.py").resolve()
if (
    python_ssl_patch.get("source") != "Python-3.14.10/Lib/ssl.py"
    or python_ssl_patch.get("staged_path") != "lib/python3.14/ssl.py"
    or not ssl_py.is_file()
    or python_ssl_patch.get("staged_sha256") != sha256(ssl_py)
    or python_ssl_patch.get("patched_source_sha256") != python_ssl_patch.get("staged_sha256")
):
    fail(f"CPython ssl.py patch provenance is invalid: {python_ssl_patch!r}")
ssl_py_text = ssl_py.read_text(encoding="utf-8")
for expected_line in (
    'os.environ.setdefault("SSL_CERT_FILE", os.path.join(sys.prefix, "ssl", "cert.pem"))',
    'os.environ.setdefault("OPENSSL_CONF", os.path.join(sys.prefix, "ssl", "openssl.cnf"))',
):
    if ssl_py_text.count(expected_line) != 1:
        fail(f"staged ssl.py runtime-relative default patch is missing or duplicated: {expected_line}")

all_strings = set(strings(data))
if expected_digest not in all_strings and expected_image not in all_strings:
    fail("provenance does not record the pinned manylinux2014 image digest")
if not any("quay.io/pypa/manylinux2014_x86_64" in value for value in all_strings):
    fail("provenance does not identify the manylinux2014_x86_64 image")
for filename, url, digest in SOURCE_PINS:
    if filename not in all_strings or url not in all_strings or digest not in all_strings:
        fail(f"provenance does not record the exact pinned source identity for {filename}")
for _, _, _, _, filename, url, digest in DEV_RPM_PINS:
    if filename not in all_strings or url not in all_strings or digest not in all_strings:
        fail(f"provenance does not record the exact pinned development RPM identity for {filename}")

key_paths = []
def walk_keys(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            path = f"{prefix}.{name}" if prefix else name
            key_paths.append((path.lower(), item))
            walk_keys(item, path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            walk_keys(item, f"{prefix}[{index}]")
walk_keys(data)

if not any(("compiler" in key or "gcc" in key or key.endswith(".cc")) and nonempty(value)
           for key, value in key_paths):
    fail("provenance does not record compiler identity")
if not any(("binutils" in key or "linker" in key or key.endswith(".ld")) and nonempty(value)
           for key, value in key_paths):
    fail("provenance does not record linker/binutils identity")
if sum(1 for key, value in key_paths if "configure" in key and nonempty(value)) < 4:
    fail("provenance must record configure inputs for CPython, OpenSSL, Tcl, and Tk")

source_licenses = data.get("licenses", {}).get("source_components", {})
for component, relative in {
    "cpython": "LICENSES/CPython.txt",
    "openssl": "LICENSES/OpenSSL.txt",
    "tcl": "LICENSES/TclTk/Tcl.txt",
    "tk": "LICENSES/TclTk/Tk.txt",
}.items():
    record = source_licenses.get(component, {})
    path = runtime / relative
    if record.get("path") != relative or not path.is_file():
        fail(f"source license provenance is incomplete for {component}")
    if record.get("sha256") != sha256(path):
        fail(f"source license hash mismatch for {component}: {relative}")
    license_paths.add(path.resolve())

libxcrypt_license = source_licenses.get("libxcrypt", {})
if (
    libxcrypt_license.get("path") != "LICENSES/Libxcrypt/LICENSING.txt"
    or libxcrypt_license.get("source") != "libxcrypt-4.5.2/LICENSING"
    or libxcrypt_license.get("usage") != "license-provenance-only"
):
    fail(f"libxcrypt license-only source provenance is invalid: {libxcrypt_license!r}")
additional_libxcrypt = libxcrypt_license.get("additional_paths", [])
if not isinstance(additional_libxcrypt, list) or {
    (entry.get("source"), entry.get("path"))
    for entry in additional_libxcrypt
    if isinstance(entry, dict)
} != {
    ("libxcrypt-4.5.2/COPYING.LIB", "LICENSES/Libxcrypt/COPYING.LIB.txt"),
    ("libxcrypt-4.5.2/AUTHORS", "LICENSES/Libxcrypt/AUTHORS.txt"),
}:
    fail(f"libxcrypt additional license provenance set is invalid: {additional_libxcrypt!r}")
for record in [libxcrypt_license, *additional_libxcrypt]:
    rel = record.get("path")
    if not isinstance(rel, str) or not rel.startswith("LICENSES/Libxcrypt/"):
        fail(f"invalid libxcrypt license path: {record!r}")
    staged = (runtime / rel).resolve()
    if not under(staged, runtime) or not staged.is_file():
        fail(f"missing staged libxcrypt license evidence: {rel}")
    if record.get("sha256") != sha256(staged):
        fail(f"staged libxcrypt license hash mismatch: {rel}")
    license_paths.add(staged)

image_packages = data.get("licenses", {}).get("image_packages", [])
package_index = {}
for package in image_packages:
    package_key = (package.get("name"), package.get("version_release"), package.get("arch"))
    if not all(isinstance(item, str) and item for item in package_key):
        fail(f"invalid pinned-image package provenance record: {package!r}")
    if package.get("provider") != "pinned-manylinux-image-rpm" or package.get("image") != expected_image:
        fail(f"invalid pinned-image package source identity: {package!r}")
    entries = package.get("licenses", [])
    if not entries:
        fail(f"pinned-image package lacks redistributed license evidence: {package_key}")
    archive_input = license_rpm_input_index.get(package_key)
    archive = package.get("license_evidence_archive")
    archive_member_bytes = None
    archive_record = None
    if archive is not None:
        if archive_input is None:
            fail(f"unexpected archive-backed legal evidence: {package_key}")
        if "license_evidence_source_package" in package:
            fail(f"archive-backed legal evidence unexpectedly names an installed source package: {package_key}")
        archive_record = archive_input["record"]
        if archive != archive_record:
            fail(f"archive-backed legal evidence differs from the independently validated input: {package_key}")
        if len(entries) != 1:
            fail(f"archive-backed package must have exactly one legal-evidence file: {package_key}: {entries!r}")
        archive_member_bytes = archive_input["member_bytes"]
        expected_source_package = None
    else:
        if "license_evidence_source_package" in package:
            fail(f"unexpected alternate installed legal-evidence source: {package_key}")
        expected_source_package = {
            "name": package_key[0],
            "version_release": package_key[1],
            "arch": package_key[2],
            "license": package.get("license"),
        }
    for entry in entries:
        rel = entry.get("staged_path")
        if not isinstance(rel, str) or not rel:
            fail(f"invalid staged package license path: {entry!r}")
        staged = (runtime / rel).resolve()
        if not under(staged, runtime) or not staged.is_file():
            fail(f"staged package license is missing or escapes runtime: {rel}")
        if entry.get("staged_sha256") != sha256(staged):
            fail(f"staged package license hash mismatch: {rel}")
        source = pathlib.Path(str(entry.get("source_path", "")))
        if not source.is_absolute() or not source.is_file():
            fail(f"source package license evidence is missing: {package_key}: {source}")
        source_hash = sha256(source)
        if entry.get("source_sha256") != source_hash or entry.get("staged_sha256") != source_hash:
            fail(f"source/staged package license evidence hash mismatch: {package_key}: {source}")
        if expected_source_package is None:
            if archive_record is None or archive_member_bytes is None:
                fail(f"archive-backed legal-evidence validation state is incomplete: {package_key}")
            expected_source = pathlib.Path(str(archive_record["source_path"])).resolve()
            if source.resolve() != expected_source:
                fail(f"archive-backed legal-evidence source path is invalid: {package_key}: {source}")
            if source_hash != archive_record["member_sha256"]:
                fail(f"archive-backed legal-evidence member hash mismatch: {package_key}: {source}")
            if source.read_bytes() != archive_member_bytes or staged.read_bytes() != archive_member_bytes:
                fail(f"archive-backed legal-evidence bytes differ from the exact RPM member: {package_key}")
            license_paths.add(staged)
            continue
        source_owner = subprocess.run(
            ["rpm", "-qf", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\t%{LICENSE}", str(source)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if source_owner.returncode != 0:
            fail(f"unable to re-derive legal-evidence package ownership: {package_key}: {source}")
        source_fields = source_owner.stdout.rstrip("\n").split("\t")
        expected_fields = [
            expected_source_package["name"], expected_source_package["version_release"],
            expected_source_package["arch"], expected_source_package["license"],
        ]
        if source_fields != expected_fields:
            fail(
                f"legal-evidence source package identity mismatch for {package_key}: "
                f"observed={source_fields!r}, expected={expected_fields!r}"
            )
        license_paths.add(staged)
    package_index[package_key] = package

required_archive_package_keys = set(license_rpm_input_index)
observed_archive_package_keys = {
    package_key
    for package_key, package in package_index.items()
    if isinstance(package.get("license_evidence_archive"), dict)
}
if observed_archive_package_keys != required_archive_package_keys:
    fail(
        "archive-backed legal-evidence package set mismatch: "
        f"missing={sorted(required_archive_package_keys - observed_archive_package_keys)}, "
        f"stale={sorted(observed_archive_package_keys - required_archive_package_keys)}"
    )

native_provider_meta = data.get("native_providers", {})
expected_source_built = {
    "lib/libpython3.14.so.1.0": "cpython",
    "lib/libssl.so.3": "openssl",
    "lib/libcrypto.so.3": "openssl",
    "lib/tcltk/libtcl8.6.so": "tcl",
    "lib/tcltk/libtk8.6.so": "tk",
}
source_built = {}
for entry in native_provider_meta.get("source_built", []):
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        fail(f"invalid source-built native provider record: {entry!r}")
    relative = entry["path"]
    if relative in source_built:
        fail(f"duplicate source-built native provider path: {relative}")
    source_built[relative] = entry
if set(source_built) != set(expected_source_built):
    fail(
        "source-built native provider provenance set mismatch: "
        f"missing={sorted(set(expected_source_built) - set(source_built))}, "
        f"stale={sorted(set(source_built) - set(expected_source_built))}"
    )
for relative, component in expected_source_built.items():
    record = source_built.get(relative)
    if record.get("component") != component or record.get("sha256") != sha256(runtime / relative):
        fail(f"source-built native provider provenance mismatch: {relative}")

pinned_image_provider_paths = set()
for record in native_provider_meta.get("pinned_image", []):
    rel = record.get("staged_path")
    if not isinstance(rel, str) or not rel.startswith("lib/native/"):
        fail(f"invalid pinned-image native provider path: {record!r}")
    staged = (runtime / rel).resolve()
    if not under(staged, runtime) or not staged.is_file():
        fail(f"pinned-image native provider is missing: {rel}")
    if record.get("staged_sha256") != sha256(staged):
        fail(f"pinned-image native provider staged hash mismatch: {rel}")
    source_path = pathlib.Path(str(record.get("source_path", "")))
    if not source_path.is_absolute() or not source_path.is_file():
        fail(f"pinned-image native provider source is missing from executing image: {rel}: {source_path}")
    if record.get("source_sha256") != sha256(source_path):
        fail(f"pinned-image native provider source hash mismatch: {rel}: {source_path}")
    provider = record.get("provider", {})
    if provider.get("image") != expected_image:
        fail(f"native provider image identity mismatch: {rel}: {provider!r}")
    kind = provider.get("kind")
    if kind == "pinned-manylinux-image-rpm":
        package_key = (provider.get("name"), provider.get("version_release"), provider.get("arch"))
        owner = subprocess.run(
            ["rpm", "-qf", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\t%{LICENSE}\n", str(source_path)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if owner.returncode != 0:
            fail(f"unable to re-derive RPM ownership for pinned-image provider: {rel}: {source_path}")
        owner_fields = owner.stdout.rstrip("\n").split("\t")
        if len(owner_fields) != 4:
            fail(f"unexpected RPM ownership metadata for pinned-image provider: {rel}: {owner.stdout!r}")
        if tuple(owner_fields[:3]) != package_key or owner_fields[3] != provider.get("license"):
            fail(f"native provider RPM identity mismatch: {rel}: observed={owner_fields!r}, recorded={provider!r}")
        package = package_index.get(package_key)
        if package is None:
            fail(f"native provider package lacks redistribution provenance: {rel}: {package_key}")
        if provider.get("license") != package.get("license") or provider.get("licenses") != package.get("licenses"):
            fail(f"native provider RPM license provenance mismatch: {rel}")
    elif kind == "pinned-manylinux-image-sqlite":
        if provider.get("component") != "sqlite" or provider.get("version") != "3.54.1":
            fail(f"pinned-image SQLite provider identity is invalid: {rel}: {provider!r}")
        if provider.get("needed_name") != "libsqlite3.so" or record.get("needed_name") != "libsqlite3.so":
            fail(f"pinned-image SQLite DT_NEEDED identity is invalid: {rel}: {provider!r}")
        if provider.get("legal_status") != "public-domain":
            fail(f"pinned-image SQLite legal status is invalid: {rel}: {provider!r}")
        source_text = str(provider.get("source_path", ""))
        if (
            source_text != "/opt/_internal/sqlite3/lib/libsqlite3.so.3.54.1"
            or source_text != str(record.get("source_path", ""))
        ):
            fail(f"pinned-image SQLite source path is invalid: {rel}: {source_text!r}")
        if provider.get("source_sha256") != record.get("source_sha256"):
            fail(f"pinned-image SQLite source hash mismatch: {rel}")
        metadata_path = pathlib.Path(str(provider.get("metadata_path", "")))
        if metadata_path != pathlib.Path("/opt/_internal/sqlite3/lib/pkgconfig/sqlite3.pc") or not metadata_path.is_file():
            fail(f"pinned-image SQLite metadata path is invalid: {rel}: {metadata_path}")
        if provider.get("metadata_sha256") != sha256(metadata_path):
            fail(f"pinned-image SQLite metadata hash mismatch: {rel}")
        metadata_text = metadata_path.read_text(encoding="utf-8")
        if not re.search(r"^Version:\s*3\.54\.1\s*$", metadata_text, re.M):
            fail(f"pinned-image SQLite metadata version mismatch: {rel}")
    elif kind == "pinned-manylinux-image-libxcrypt":
        if provider.get("component") != "libxcrypt" or provider.get("version") != "4.5.2":
            fail(f"pinned-image libxcrypt identity is invalid: {rel}: {provider!r}")
        needed_name = provider.get("needed_name")
        if needed_name != "libcrypt.so.2" or record.get("needed_name") != needed_name:
            fail(f"pinned-image libxcrypt DT_NEEDED identity is invalid: {rel}: {provider!r}")
        if provider.get("license") != (
            "LGPL-2.1-or-later AND BSD-3-Clause AND BSD-2-Clause AND "
            "BSD-2-Clause-FreeBSD AND 0BSD AND CC0-1.0 AND "
            "LicenseRef-Fedora-Public-Domain"
        ):
            fail(f"pinned-image libxcrypt license identity is invalid: {rel}: {provider!r}")
        source_text = str(provider.get("source_path", ""))
        source_obj = pathlib.Path(source_text)
        expected_source = {
            "libcrypt.so.2": pathlib.Path("/usr/local/lib/libcrypt.so.2"),
        }[needed_name]
        if source_obj != expected_source or source_text != str(record.get("source_path", "")):
            fail(f"pinned-image libxcrypt source path is invalid: {rel}: {source_text!r}")
        if provider.get("source_sha256") != record.get("source_sha256") or provider.get("source_sha256") != sha256(source_obj):
            fail(f"pinned-image libxcrypt source hash mismatch: {rel}")
        metadata_path = pathlib.Path(str(provider.get("metadata_path", "")))
        if metadata_path != pathlib.Path("/usr/local/lib/pkgconfig/libxcrypt.pc") or not metadata_path.is_file():
            fail(f"pinned-image libxcrypt metadata path is invalid: {rel}: {metadata_path}")
        if provider.get("metadata_sha256") != sha256(metadata_path):
            fail(f"pinned-image libxcrypt metadata hash mismatch: {rel}")
        metadata_text = metadata_path.read_text(encoding="utf-8")
        if not re.search(r"^Version:\s*4\.5\.2\s*$", metadata_text, re.M):
            fail(f"pinned-image libxcrypt metadata version mismatch: {rel}")
        license_source = provider.get("license_source", {})
        if license_source != {
            "filename": "libxcrypt-4.5.2.tar.xz",
            "url": "https://github.com/besser82/libxcrypt/releases/download/v4.5.2/libxcrypt-4.5.2.tar.xz",
            "sha256": "71513a31c01a428bccd5367a32fd95f115d6dac50fb5b60c779d5c7942aec071",
            "usage": "license-provenance-only",
        }:
            fail(f"pinned-image libxcrypt license-source provenance is invalid: {rel}: {license_source!r}")
        licenses = provider.get("licenses", [])
        expected_license_members = {
            "AUTHORS": "LICENSES/Libxcrypt/AUTHORS.txt",
            "COPYING.LIB": "LICENSES/Libxcrypt/COPYING.LIB.txt",
            "LICENSING": "LICENSES/Libxcrypt/LICENSING.txt",
        }
        if len(licenses) != len(expected_license_members):
            fail(f"pinned-image libxcrypt license evidence is incomplete: {rel}: {licenses!r}")
        observed_members = {
            entry.get("source_archive_path"): entry.get("staged_path")
            for entry in licenses
            if isinstance(entry, dict)
        }
        if observed_members != expected_license_members:
            fail(f"pinned-image libxcrypt license member set is invalid: {rel}: {observed_members!r}")
        for entry in licenses:
            staged_rel = entry.get("staged_path")
            staged_license = (runtime / str(staged_rel)).resolve()
            if not under(staged_license, runtime) or not staged_license.is_file():
                fail(f"missing pinned-image libxcrypt staged license evidence: {rel}: {staged_rel}")
            if entry.get("staged_sha256") != sha256(staged_license) or entry.get("source_sha256") != entry.get("staged_sha256"):
                fail(f"pinned-image libxcrypt license hash mismatch: {rel}: {entry!r}")
            license_paths.add(staged_license)
    else:
        fail(f"unknown pinned-image native provider kind: {rel}: {kind!r}")
    if rel in pinned_image_provider_paths:
        fail(f"duplicate pinned-image provider provenance path: {rel}")
    pinned_image_provider_paths.add(rel)

glibc_loader = native_provider_meta.get("glibc_loader_external", {})
if (
    glibc_loader.get("kind") != "glibc-loader-external"
    or glibc_loader.get("image") != expected_image
    or glibc_loader.get("needed_name") != "ld-linux-x86-64.so.2"
    or glibc_loader.get("interpreter_path") != "/lib64/ld-linux-x86-64.so.2"
):
    fail(f"glibc loader provenance is invalid: {glibc_loader!r}")
glibc_loader_consumers = glibc_loader.get("consumers")
if (
    not isinstance(glibc_loader_consumers, list)
    or not glibc_loader_consumers
    or any(not isinstance(rel, str) or not rel for rel in glibc_loader_consumers)
    or glibc_loader_consumers != sorted(set(glibc_loader_consumers))
):
    fail(f"glibc loader consumer provenance is invalid: {glibc_loader_consumers!r}")
program_headers = run("readelf", "-lW", str(runtime / "bin/python3.14"))
interp_match = re.search(r"Requesting program interpreter: ([^\]]+)\]", program_headers)
if interp_match is None or interp_match.group(1) != glibc_loader["interpreter_path"]:
    fail(f"staged Python PT_INTERP differs from provenance: {interp_match.group(1) if interp_match else None!r}")
loader_path = pathlib.Path(glibc_loader["interpreter_path"])
loader_resolved = loader_path.resolve()
if str(loader_resolved) != glibc_loader.get("resolved_path") or not loader_resolved.is_file():
    fail(f"glibc loader resolved path differs from provenance: {loader_resolved}")
if glibc_loader.get("resolved_sha256") != sha256(loader_resolved):
    fail("glibc loader byte identity differs from provenance")
loader_package = glibc_loader.get("package", {})
owner = subprocess.run(
    ["rpm", "-qf", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\t%{LICENSE}\n", str(loader_resolved)],
    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
)
if owner.returncode != 0:
    fail(f"unable to re-derive glibc loader RPM identity: {loader_resolved}")
owner_fields = owner.stdout.rstrip("\n").split("\t")
if len(owner_fields) != 4 or owner_fields[0] != "glibc":
    fail(f"unexpected glibc loader RPM ownership: {owner.stdout!r}")
if loader_package != {
    "name": owner_fields[0],
    "version_release": owner_fields[1],
    "arch": owner_fields[2],
    "license": owner_fields[3],
}:
    fail(f"glibc loader RPM identity differs from provenance: {loader_package!r} != {owner_fields!r}")
if any(path.name.startswith("ld-linux") for path in runtime.rglob("*") if path.is_file()):
    fail("controlled runtime must not bundle the glibc dynamic loader")

observed_symlinks = {}
for link in sorted(runtime.rglob("*")):
    if not link.is_symlink():
        continue
    rel = link.relative_to(runtime).as_posix()
    try:
        resolved = link.resolve(strict=True)
    except OSError as exc:
        fail(f"staged symlink is broken: {rel}: {exc}")
    if not under(resolved, runtime):
        fail(f"staged symlink escapes controlled runtime: {rel} -> {resolved}")
    observed_symlinks[rel] = os.readlink(link)
provenance_symlinks = {
    entry.get("path"): entry.get("target")
    for entry in data.get("symlinks", [])
    if isinstance(entry, dict)
}
if provenance_symlinks != observed_symlinks:
    fail("provenance symlink census does not match the staged runtime")

elfs = []
for path in sorted(runtime.rglob("*")):
    if not path.is_file() or path.is_symlink():
        continue
    try:
        if path.open("rb").read(4) == b"\x7fELF":
            elfs.append(path)
    except OSError:
        pass
if not elfs:
    fail("controlled runtime contains no ELF objects")
native_elf_paths = {
    path.relative_to(runtime).as_posix()
    for path in elfs
    if path.relative_to(runtime).as_posix().startswith("lib/native/")
}
if native_elf_paths != pinned_image_provider_paths:
    fail(
        "pinned-image provider provenance does not cover lib/native ELF closure: "
        f"unrecorded={sorted(native_elf_paths - pinned_image_provider_paths)}, "
        f"stale={sorted(pinned_image_provider_paths - native_elf_paths)}"
    )

provenance_dicts = list(dicts(data))
provenance_census = {}
for entry in data.get("elf_census", []):
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        fail(f"invalid ELF census record in provenance: {entry!r}")
    if entry["path"] in provenance_census:
        fail(f"duplicate ELF census path in provenance: {entry['path']}")
    provenance_census[entry["path"]] = entry
observed_elf_paths = {path.relative_to(runtime).as_posix() for path in elfs}
if set(provenance_census) != observed_elf_paths:
    missing = sorted(observed_elf_paths - set(provenance_census))
    stale = sorted(set(provenance_census) - observed_elf_paths)
    fail(f"provenance ELF census does not match staged closure: missing={missing}, stale={stale}")
if data.get("elf_summary", {}).get("count") != len(elfs):
    fail("provenance ELF summary count does not match staged closure")

native_names = {}
elf_dynamic = {}
for path in elfs:
    header = run("readelf", "-hW", str(path))
    machine = re.search(r"^\s*Machine:\s*(.+)$", header, re.MULTILINE)
    elf_class = re.search(r"^\s*Class:\s*(.+)$", header, re.MULTILINE)
    if (
        machine is None
        or machine.group(1).strip() != "Advanced Micro Devices X86-64"
        or elf_class is None
        or elf_class.group(1).strip() != "ELF64"
    ):
        fail(f"non-x86_64 ELF in controlled runtime: {path.relative_to(runtime)}")
    needed, soname, runpaths = dynamic(path)
    elf_dynamic[path] = (needed, soname, runpaths)
    native_names.setdefault(path.name, []).append(path)
    if soname:
        native_names.setdefault(soname, []).append(path)
    for needed_name in needed:
        if "/" in needed_name:
            fail(f"non-relocatable DT_NEEDED entry in {path.relative_to(runtime)}: {needed_name}")
    if not runpaths:
        fail(f"staged ELF lacks runtime-relative RPATH/RUNPATH: {path.relative_to(runtime)}")
    for packed in runpaths:
        for entry in packed.split(":"):
            if not entry or not entry.startswith("$ORIGIN"):
                fail(f"non-runtime-relative RPATH/RUNPATH in {path.relative_to(runtime)}: {entry!r}")
            suffix = entry[len("$ORIGIN"):]
            if suffix and not suffix.startswith("/"):
                fail(f"invalid $ORIGIN RPATH/RUNPATH in {path.relative_to(runtime)}: {entry}")
            if not under(path.parent / suffix.lstrip("/"), runtime):
                fail(f"RPATH/RUNPATH escapes controlled runtime in {path.relative_to(runtime)}: {entry}")
    check_versions(path)

    digest = sha256(path)
    rel = path.relative_to(runtime).as_posix()
    census = provenance_census[rel]
    if census.get("sha256") != digest:
        fail(f"ELF census hash mismatch: {rel}")
    if census.get("machine") != "Advanced Micro Devices X86-64" or census.get("elf_class") != "ELF64":
        fail(f"ELF census architecture mismatch: {rel}")
    if census.get("needed") != needed:
        fail(f"ELF census DT_NEEDED mismatch: {rel}")
    if census.get("runpath") != runpaths:
        fail(f"ELF census RUNPATH/RPATH mismatch: {rel}")
    dependencies = census.get("dependencies")
    if not isinstance(dependencies, list) or [entry.get("name") for entry in dependencies if isinstance(entry, dict)] != needed:
        fail(f"ELF census dependency list mismatch: {rel}")
    if not any(digest in set(strings(record)) and
               (rel in set(strings(record)) or path.name in set(strings(record)))
               for record in provenance_dicts):
        fail(f"provenance does not account for staged native byte identity: {rel} ({digest})")

observed_glibc_loader_consumers = sorted(
    path.relative_to(runtime).as_posix()
    for path, (needed, _, _) in elf_dynamic.items()
    if glibc_loader["needed_name"] in needed
)
if glibc_loader_consumers != observed_glibc_loader_consumers:
    fail(
        "glibc loader consumer provenance differs from staged ELF closure: "
        f"recorded={glibc_loader_consumers!r}, observed={observed_glibc_loader_consumers!r}"
    )

clean_env = os.environ.copy()
clean_env.pop("LD_LIBRARY_PATH", None)
for path in elfs:
    needed, _, _ = elf_dynamic[path]
    rel = path.relative_to(runtime).as_posix()
    dependencies = provenance_census[rel]["dependencies"]
    text = run("ldd", str(path), env=clean_env)
    if "not found" in text:
        fail(f"unresolved native dependency for {path.relative_to(runtime)}:\n{text}")
    resolved = {}
    for line in text.splitlines():
        match = re.match(r"\s*(\S+)\s+=>\s+(\S+)\s+\(", line)
        if match:
            resolved[match.group(1)] = match.group(2)
            continue
        match = re.match(r"\s*(/\S+)\s+\(", line)
        if match:
            resolved[pathlib.Path(match.group(1)).name] = match.group(1)
    for needed_name, dependency in zip(needed, dependencies):
        actual = resolved.get(needed_name)
        resolution = dependency.get("resolution")
        if resolution == "bundled":
            if actual is None or not under(actual, runtime):
                fail(f"bundled dependency did not resolve internally: {rel} -> {needed_name} -> {actual}")
            actual_path = pathlib.Path(actual).resolve()
            actual_rel = actual_path.relative_to(runtime).as_posix()
            if dependency.get("provider") != actual_rel:
                fail(
                    f"bundled dependency provider path differs from provenance: "
                    f"{rel} -> {needed_name}: observed={actual_rel}, recorded={dependency.get('provider')!r}"
                )
            if dependency.get("provider_sha256") != sha256(actual_path):
                fail(f"bundled dependency provider hash differs from provenance: {rel} -> {needed_name}")
        elif resolution == "pep599-external":
            if needed_name not in PEP599_EXTERNAL:
                fail(f"provenance classifies non-PEP599 dependency as external: {rel} -> {needed_name}")
            if actual is None:
                fail(f"PEP 599 external dependency did not resolve: {rel} -> {needed_name}")
            if under(actual, runtime):
                fail(f"PEP 599 external dependency unexpectedly resolved inside runtime: {rel} -> {needed_name} -> {actual}")
        elif resolution == "glibc-loader-external":
            if needed_name != "ld-linux-x86-64.so.2" or rel not in glibc_loader_consumers:
                fail(f"invalid glibc-loader external dependency classification: {rel} -> {needed_name}")
            if actual is None:
                fail(f"glibc loader dependency did not resolve: {rel} -> {needed_name}")
            actual_path = pathlib.Path(actual).resolve()
            if actual_path != loader_resolved or under(actual_path, runtime):
                fail(f"glibc loader dependency resolved unexpectedly: {rel} -> {needed_name} -> {actual_path}")
            if dependency.get("provider") != str(loader_resolved):
                fail(f"glibc loader provider path differs from provenance: {rel} -> {needed_name}")
            if dependency.get("provider_sha256") != sha256(loader_resolved):
                fail(f"glibc loader provider hash differs from provenance: {rel} -> {needed_name}")
            if dependency.get("package") != loader_package:
                fail(f"glibc loader package identity differs from provenance: {rel} -> {needed_name}")
        else:
            fail(f"unknown ELF dependency provenance classification: {rel} -> {needed_name}: {resolution!r}")

print(f"linux_glibc217_elf_count={len(elfs)}")
print(f"linux_glibc217_license_evidence_count={len(license_paths)}")
print("linux_glibc217_native_census=pass")
PY

env -u SSL_CERT_FILE -u SSL_CERT_DIR -u OPENSSL_CONF -u OPENSSL_MODULES \
PYTHONHOME="$poison/home" \
PYTHONPATH="$poison/python" \
PYTHONUSERBASE="$poison/user" \
LD_LIBRARY_PATH="$poison/native" \
TCL_LIBRARY="$tcl_root" \
TK_LIBRARY="$tk_root" \
"$python" -I - "$runtime" <<'PY'
import _tkinter
import bz2
import ctypes
import encodings
import hashlib
import importlib
import lzma
import multiprocessing
import os
import pathlib
import sqlite3
import ssl
import sys
import tkinter
import zoneinfo
import zlib

runtime = pathlib.Path(sys.argv[1]).resolve()
stdlib = (runtime / "lib/python3.14").resolve()
dynload = (stdlib / "lib-dynload").resolve()
native = (runtime / "lib/tcltk").resolve()
tcl_scripts = (runtime / "tcl/tcl8.6").resolve()

def under(path, root):
    path = pathlib.Path(path).resolve()
    root = pathlib.Path(root).resolve()
    return path == root or root in path.parents

if pathlib.Path(sys.executable).resolve() != (runtime / "bin/python3.14").resolve():
    raise SystemExit(f"unexpected staged executable: {sys.executable}")
for entry in sys.path:
    if entry and not under(entry, runtime):
        raise SystemExit(f"ambient Python path leaked into isolated interpreter: {entry}")
if not under(tkinter.__file__, stdlib):
    raise SystemExit(f"tkinter escaped staged stdlib: {tkinter.__file__}")
if not under(_tkinter.__file__, dynload) or _tkinter.__spec__.origin == "built-in":
    raise SystemExit(f"_tkinter is not the staged dynamic extension: {_tkinter.__file__}")

try:
    import numpy  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    raise SystemExit("controlled core runtime unexpectedly imports NumPy")

retained_modules = {
    "ssl", "_ssl", "sqlite3", "_sqlite3", "ctypes", "_ctypes", "bz2", "_bz2",
    "lzma", "_lzma", "zlib", "readline", "curses", "_curses", "curses.panel",
    "_curses_panel", "multiprocessing", "zoneinfo", "hashlib", "decimal", "dbm",
    "_dbm", "_gdbm", "uuid", "_uuid", "pyexpat", "xml.parsers.expat", "tkinter", "_tkinter",
}
for name in sorted(retained_modules):
    importlib.import_module(name)

ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
if not ssl.OPENSSL_VERSION.startswith("OpenSSL 3.5.9 "):
    raise SystemExit(f"unexpected staged OpenSSL runtime: {ssl.OPENSSL_VERSION}")
expected_cert = (runtime / "ssl/cert.pem").resolve()
expected_conf = (runtime / "ssl/openssl.cnf").resolve()
if pathlib.Path(os.environ.get("SSL_CERT_FILE", "")).resolve() != expected_cert:
    raise SystemExit(f"runtime-relative SSL_CERT_FILE default is missing: {os.environ.get('SSL_CERT_FILE')!r}")
if pathlib.Path(os.environ.get("OPENSSL_CONF", "")).resolve() != expected_conf:
    raise SystemExit(f"runtime-relative OPENSSL_CONF default is missing: {os.environ.get('OPENSSL_CONF')!r}")
verify_paths = ssl.get_default_verify_paths()
if not verify_paths.cafile or pathlib.Path(verify_paths.cafile).resolve() != expected_cert:
    raise SystemExit(f"default verify paths escaped staged runtime: {verify_paths}")
if ssl.create_default_context().cert_store_stats().get("x509_ca", 0) <= 0:
    raise SystemExit("default TLS context did not load staged CA certificates")
with sqlite3.connect(":memory:") as connection:
    assert connection.execute("select 40 + 2").fetchone()[0] == 42
ctypes.CDLL(None)
assert zlib.decompress(zlib.compress(b"bridge")) == b"bridge"
assert bz2.decompress(bz2.compress(b"bridge")) == b"bridge"
assert lzma.decompress(lzma.compress(b"bridge")) == b"bridge"
assert multiprocessing.get_context("spawn").get_start_method() == "spawn"
assert zoneinfo.ZoneInfo("UTC").key == "UTC"
assert hashlib.sha256(b"bridge").hexdigest() == "17f29b073143d8cd97b5bbe492bdeffec1c5fee55cc1fe2112c8b9335f8b6121"
if encodings.__file__ is None or not under(encodings.__file__, stdlib):
    raise SystemExit("encodings escaped staged stdlib")

if str(_tkinter.TCL_VERSION) != "8.6" or str(_tkinter.TK_VERSION) != "8.6":
    raise SystemExit("unexpected _tkinter Tcl/Tk API version")
interp = tkinter.Tcl()
if interp.call("info", "patchlevel") != "8.6.18":
    raise SystemExit(f"unexpected Tcl patchlevel: {interp.call('info', 'patchlevel')}")
observed_tcl = pathlib.Path(interp.eval("info library")).resolve()
if observed_tcl != tcl_scripts:
    raise SystemExit(f"Tcl scripts escaped controlled runtime: {observed_tcl}")

providers = {}
for line in pathlib.Path("/proc/self/maps").read_text(encoding="utf-8", errors="replace").splitlines():
    fields = line.split(None, 5)
    if len(fields) < 6 or not fields[5].startswith("/"):
        continue
    path = pathlib.Path(fields[5]).resolve()
    if path.name.startswith("libtcl8.6"):
        providers["tcl"] = path
    elif path.name.startswith("libtk8.6"):
        providers["tk"] = path
    elif path.name == "libssl.so.3":
        providers["ssl"] = path
    elif path.name == "libcrypto.so.3":
        providers["crypto"] = path
if set(providers) != {"tcl", "tk", "ssl", "crypto"}:
    raise SystemExit(f"did not load complete staged Tcl/Tk/OpenSSL providers: {providers}")
if not all(under(providers[name], native) for name in ("tcl", "tk")):
    raise SystemExit(f"did not load staged Tcl/Tk providers: {providers}")
runtime_lib = (runtime / "lib").resolve()
expected_openssl = {
    "ssl": (runtime_lib / "libssl.so.3").resolve(),
    "crypto": (runtime_lib / "libcrypto.so.3").resolve(),
}
if any(providers[name] != expected_openssl[name] for name in expected_openssl):
    raise SystemExit(f"did not load staged OpenSSL providers: {providers}")

print("linux_glibc217_python_isolation=pass")
print("linux_glibc217_tcl=pass")
PY

relocated="$poison/relocated-runtime"
cp -a "$runtime" "$relocated"
env -u SSL_CERT_FILE -u SSL_CERT_DIR -u OPENSSL_CONF -u OPENSSL_MODULES \
PYTHONHOME="$poison/home" \
PYTHONPATH="$poison/python" \
PYTHONUSERBASE="$poison/user" \
LD_LIBRARY_PATH="$poison/native" \
TCL_LIBRARY="$relocated/tcl/tcl8.6" \
TK_LIBRARY="$relocated/tcl/tk8.6" \
"$relocated/bin/python3.14" -I - "$relocated" <<'PY'
import hashlib
import os
import pathlib
import ssl
import sys

runtime = pathlib.Path(sys.argv[1]).resolve()
if pathlib.Path(sys.prefix).resolve() != runtime:
    raise SystemExit(f"relocated Python prefix mismatch: {sys.prefix}")
expected_cert = (runtime / "ssl/cert.pem").resolve()
expected_conf = (runtime / "ssl/openssl.cnf").resolve()
if pathlib.Path(os.environ.get("SSL_CERT_FILE", "")).resolve() != expected_cert:
    raise SystemExit(f"relocated SSL_CERT_FILE default is wrong: {os.environ.get('SSL_CERT_FILE')!r}")
if pathlib.Path(os.environ.get("OPENSSL_CONF", "")).resolve() != expected_conf:
    raise SystemExit(f"relocated OPENSSL_CONF default is wrong: {os.environ.get('OPENSSL_CONF')!r}")
paths = ssl.get_default_verify_paths()
if not paths.cafile or pathlib.Path(paths.cafile).resolve() != expected_cert:
    raise SystemExit(f"relocated default verify paths escaped runtime: {paths}")
if ssl.create_default_context().cert_store_stats().get("x509_ca", 0) <= 0:
    raise SystemExit("relocated default TLS context did not load staged CA certificates")
if hashlib.sha256(b"relocated").digest() == b"":
    raise SystemExit("unreachable")
print("linux_glibc217_ssl_relocation=pass")
PY

[[ -n "${DISPLAY:-}" ]] || {
    echo "DISPLAY must be supplied by CI for the real Tk/Xvfb smoke" >&2
    exit 1
}

env -u SSL_CERT_FILE -u SSL_CERT_DIR -u OPENSSL_CONF -u OPENSSL_MODULES \
PYTHONHOME="$poison/home" \
PYTHONPATH="$poison/python" \
PYTHONUSERBASE="$poison/user" \
LD_LIBRARY_PATH="$poison/native" \
TCL_LIBRARY="$tcl_root" \
TK_LIBRARY="$tk_root" \
DISPLAY="$DISPLAY" \
"$python" -I - "$runtime" <<'PY'
import pathlib
import sys
import tkinter

runtime = pathlib.Path(sys.argv[1]).resolve()
native = (runtime / "lib/tcltk").resolve()
expected_tcl = (runtime / "tcl/tcl8.6").resolve()
expected_tk = (runtime / "tcl/tk8.6").resolve()

def under(path, root):
    path = pathlib.Path(path).resolve()
    root = pathlib.Path(root).resolve()
    return path == root or root in path.parents

root = tkinter.Tk()
try:
    root.withdraw()
    if root.tk.call("tk", "windowingsystem") != "x11":
        raise SystemExit(f"unexpected Tk windowing system: {root.tk.call('tk', 'windowingsystem')}")
    if root.tk.call("info", "patchlevel") != "8.6.18":
        raise SystemExit(f"unexpected Tcl patchlevel: {root.tk.call('info', 'patchlevel')}")
    if root.tk.call("set", "tk_patchLevel") != "8.6.18":
        raise SystemExit(f"unexpected Tk patchlevel: {root.tk.call('set', 'tk_patchLevel')}")
    if pathlib.Path(root.tk.eval("info library")).resolve() != expected_tcl:
        raise SystemExit("Tcl script provider escaped controlled runtime")
    if pathlib.Path(root.tk.eval("set tk_library")).resolve() != expected_tk:
        raise SystemExit("Tk script provider escaped controlled runtime")

    tcltk = {}
    bundled_x11 = []
    for line in pathlib.Path("/proc/self/maps").read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split(None, 5)
        if len(fields) < 6 or not fields[5].startswith("/"):
            continue
        path = pathlib.Path(fields[5]).resolve()
        if path.name.startswith("libtcl8.6"):
            tcltk["tcl"] = path
        elif path.name.startswith("libtk8.6"):
            tcltk["tk"] = path
        if path.name.startswith(("libX11.so", "libxcb.so", "libxcb-")) and under(path, runtime):
            bundled_x11.append(path)
    if set(tcltk) != {"tcl", "tk"} or not all(under(path, native) for path in tcltk.values()):
        raise SystemExit(f"Tk smoke did not use staged Tcl/Tk providers: {tcltk}")
    if bundled_x11:
        raise SystemExit(f"Tk smoke loaded bundled X11/XCB providers: {bundled_x11}")

    label = tkinter.Label(root, text="bridge")
    label.pack()
    root.update_idletasks()
finally:
    root.destroy()

print("linux_glibc217_tk_xvfb=pass")
PY

printf 'linux_glibc217_image=%s\n' "$expected_image"
printf 'linux_glibc217_glibc=%s\n' "$glibc_identity"
echo "linux_glibc217_runtime_test=pass"
