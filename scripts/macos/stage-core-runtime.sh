#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: scripts/macos/stage-core-runtime.sh --python <python3.14> --python-archive <archive> --python-hashes <hashes.sha256> --python-source <Python-3.14.10.tar.xz> --tcl-source <tcl8.6.18-src.tar.gz> --tk-source <tk8.6.18-src.tar.gz> [--arch arm64|x86_64]" >&2
    exit 2
}

python=""
python_archive=""
python_hashes=""
python_source=""
tcl_source=""
tk_source=""
target_arch="arm64"
while (($#)); do
    case "$1" in
        --python) python="$2"; shift 2 ;;
        --python-archive) python_archive="$2"; shift 2 ;;
        --python-hashes) python_hashes="$2"; shift 2 ;;
        --python-source) python_source="$2"; shift 2 ;;
        --tcl-source) tcl_source="$2"; shift 2 ;;
        --tk-source) tk_source="$2"; shift 2 ;;
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
[[ -f "$python_source" ]] || { echo "CPython source archive is missing: $python_source" >&2; exit 1; }
[[ -f "$tcl_source" ]] || { echo "locked Tcl source is missing: $tcl_source" >&2; exit 1; }
[[ -f "$tk_source" ]] || { echo "locked Tk source is missing: $tk_source" >&2; exit 1; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
build="$root/build/macos-core-$target_arch"
runtime="$root/out/macos-$target_arch/core"
hash_asset_id=678419306
case "$target_arch" in
    arm64)
        archive_name="python-3.14.10-darwin-arm64.tar.gz"
        archive_asset_id=678419297
        ;;
    x86_64)
        archive_name="python-3.14.10-darwin-x64.tar.gz"
        archive_asset_id=678419298
        ;;
esac

[[ "$(basename "$python_archive")" == "$archive_name" ]] || {
    echo "unexpected CPython archive filename: $(basename "$python_archive")" >&2
    exit 1
}
for tool in clang codesign ditto install_name_tool lipo make otool pkgutil shasum tar unzip; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "required macOS staging tool is missing: $tool" >&2
        exit 1
    }
done

python_source_name="Python-3.14.10.tar.xz"
python_source_expected_sha="de414a57119abef52784d8993e341d53aed635fc2b05c2607d9f98a2c5b72032"
[[ "$(basename "$python_source")" == "$python_source_name" ]] || {
    echo "unexpected CPython source filename: $(basename "$python_source")" >&2
    exit 1
}
python_source_sha="$(shasum -a 256 "$python_source" | awk '{print $1}')"
[[ "$python_source_sha" == "$python_source_expected_sha" ]] || {
    echo "CPython source SHA-256 mismatch: $python_source_sha" >&2
    exit 1
}

tcl_name="tcl8.6.18-src.tar.gz"
tk_name="tk8.6.18-src.tar.gz"
tcl_expected_sha="14f9af32b1767ff718477a8f974ad03c34341097e6b43f4ce54644ee974e268e"
tk_expected_sha="95cd528a80f5e4bdb557af9b14a7197d6860793a3894e25e7c9fad2ed05d4c3c"
[[ "$(basename "$tcl_source")" == "$tcl_name" ]] || { echo "unexpected Tcl source filename" >&2; exit 1; }
[[ "$(basename "$tk_source")" == "$tk_name" ]] || { echo "unexpected Tk source filename" >&2; exit 1; }
tcl_source_sha="$(shasum -a 256 "$tcl_source" | awk '{print $1}')"
tk_source_sha="$(shasum -a 256 "$tk_source" | awk '{print $1}')"
[[ "$tcl_source_sha" == "$tcl_expected_sha" ]] || { echo "Tcl source SHA-256 mismatch" >&2; exit 1; }
[[ "$tk_source_sha" == "$tk_expected_sha" ]] || { echo "Tk source SHA-256 mismatch" >&2; exit 1; }

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
[[ "${pyinfo[0]}" == CPython && "${pyinfo[1]}" == 3.14.10 && "${pyinfo[2]}" == 64 && "${pyinfo[3]}" == "$target_arch" ]] || {
    echo "expected $target_arch CPython 3.14.10, got ${pyinfo[*]}" >&2
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
import sys
import xml.etree.ElementTree as ET

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

package_python = payload_file("Versions/3.14/Python")
package_exe = payload_file("Versions/3.14/bin/python3.14")
selected_python = selected / "Python"
selected_exe = selected / "bin/python3.14"
for package_file, selected_file, label in (
    (package_python, selected_python, "framework"),
    (package_exe, selected_exe, "executable"),
):
    if sha(package_file) != sha(selected_file):
        raise SystemExit(f"selected {label} does not byte-match verified package payload")

tkinter_matches = list((payload / "Versions/3.14/lib/python3.14/lib-dynload").glob("_tkinter*.so"))
if len(tkinter_matches) != 1:
    raise SystemExit(f"expected one packaged _tkinter, found {tkinter_matches}")
selected_tkinter = next((selected / "lib/python3.14/lib-dynload").glob("_tkinter*.so"))
if sha(tkinter_matches[0]) != sha(selected_tkinter):
    raise SystemExit("selected _tkinter does not byte-match verified package payload")
data = {
    "package_python": str(package_python),
    "package_executable": str(package_exe),
    "package_tkinter": str(tkinter_matches[0].resolve()),
    "package_prefix": str(package_python.parent),
    "package_install_location": install_location,
    "source_hashes": {
        "python": sha(package_python),
        "executable": sha(package_exe),
        "tkinter": sha(tkinter_matches[0]),
    },
}
output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

tcltk_src_root="$build/tcltk-src"
tcl_build="$build/tcl-build"
tk_build="$build/tk-build"
tcltk_prefix="$build/tcltk-prefix"
mkdir -p "$tcltk_src_root" "$tcl_build" "$tk_build" "$tcltk_prefix"
tar -xzf "$tcl_source" -C "$tcltk_src_root"
tar -xzf "$tk_source" -C "$tcltk_src_root"
tcl_src="$tcltk_src_root/tcl8.6.18"
tk_src="$tcltk_src_root/tk8.6.18"
[[ -x "$tcl_src/unix/configure" && -x "$tk_src/unix/configure" ]] || {
    echo "locked Tcl/Tk source archives have unexpected layout" >&2
    exit 1
}

common_cflags="-O2 -arch $target_arch -mmacosx-version-min=14.0"
common_ldflags="-arch $target_arch -mmacosx-version-min=14.0"
(
    cd "$tcl_build"
    env \
        MACOSX_DEPLOYMENT_TARGET=14.0 \
        CC=clang \
        CFLAGS="$common_cflags" \
        LDFLAGS="$common_ldflags" \
        "$tcl_src/unix/configure" \
            --prefix="$tcltk_prefix" \
            --enable-shared \
            --enable-threads \
            --enable-64bit
    python3 - <<'PY'
from pathlib import Path

path = Path("Makefile")
text = path.read_text(encoding="utf-8")
lines = []
for line in text.splitlines(keepends=True):
    if line.startswith("COMPAT_OBJS"):
        for name in ("strstr.o", "strtoul.o", " strtod.o"):
            line = line.replace(name, "")
    lines.append(line)
path.write_text("".join(lines), encoding="utf-8")
PY
    make -j2
    make install
)
(
    cd "$tk_build"
    env \
        MACOSX_DEPLOYMENT_TARGET=14.0 \
        CC=clang \
        CFLAGS="$common_cflags" \
        LDFLAGS="$common_ldflags" \
        "$tk_src/unix/configure" \
            --prefix="$tcltk_prefix" \
            --with-tcl="$tcl_build" \
            --enable-shared \
            --enable-threads \
            --enable-64bit \
            --enable-aqua
    make -j2
    make install
)

built_tcl="$tcltk_prefix/lib/libtcl8.6.dylib"
built_tk="$tcltk_prefix/lib/libtk8.6.dylib"
built_tcl_scripts="$tcltk_prefix/lib/tcl8.6"
built_tk_scripts="$tcltk_prefix/lib/tk8.6"
[[ -f "$built_tcl" && -f "$built_tk" && -d "$built_tcl_scripts" && -d "$built_tk_scripts" ]] || {
    echo "Tcl/Tk 8.6.18 install is incomplete" >&2
    exit 1
}
for provider in "$built_tcl" "$built_tk"; do
    [[ "$(lipo -archs "$provider")" == "$target_arch" ]] || {
        echo "Tcl/Tk provider does not contain exactly target architecture $target_arch: $provider" >&2
        exit 1
    }
done
grep -Eq '^package[[:space:]]+require[[:space:]]+-exact[[:space:]]+Tcl[[:space:]]+8\.6\.18[[:space:]]*$' \
    "$built_tcl_scripts/init.tcl" || { echo "Tcl script patchlevel is not 8.6.18" >&2; exit 1; }
grep -Eq '^package[[:space:]]+(require[[:space:]]+-exact|provide)[[:space:]]+Tk[[:space:]]+8\.6\.18[[:space:]]*$' \
    "$built_tk_scripts/tk.tcl" || { echo "Tk script patchlevel is not 8.6.18" >&2; exit 1; }

cpython_src_root="$build/cpython-src"
mkdir -p "$cpython_src_root"
tar -xf "$python_source" -C "$cpython_src_root"
cpython_src="$cpython_src_root/Python-3.14.10"
[[ -f "$cpython_src/Modules/_tkinter.c" && -f "$cpython_src/Modules/tkappinit.c" ]] || {
    echo "CPython 3.14.10 source archive has unexpected layout" >&2
    exit 1
}
package_tkinter="$("$python" - "$package_paths" <<'PY'
import json
import pathlib
import sys
print(pathlib.Path(json.load(open(sys.argv[1], encoding="utf-8"))["package_tkinter"]).name)
PY
)"
python_include="$selected_prefix/include/python3.14"
[[ -f "$python_include/Python.h" && -d "$cpython_src/Include/internal" ]] || {
    echo "selected CPython development headers are incomplete: $python_include" >&2
    exit 1
}
rebuilt_tkinter="$build/$package_tkinter"
clang \
    -bundle \
    -undefined dynamic_lookup \
    -O2 \
    -fPIC \
    -arch "$target_arch" \
    -mmacosx-version-min=14.0 \
    -DPy_BUILD_CORE_MODULE \
    -DWITH_APPINIT=1 \
    -I"$python_include" \
    -I"$cpython_src/Include/internal" \
    -I"$cpython_src/Include/internal/mimalloc" \
    -I"$cpython_src/Modules" \
    -I"$tcltk_prefix/include" \
    "$cpython_src/Modules/_tkinter.c" \
    "$cpython_src/Modules/tkappinit.c" \
    -L"$tcltk_prefix/lib" \
    -ltcl8.6 \
    -ltk8.6 \
    -o "$rebuilt_tkinter"
[[ -f "$rebuilt_tkinter" ]] || { echo "rebuilt _tkinter extension is missing" >&2; exit 1; }
[[ "$(lipo -archs "$rebuilt_tkinter")" == "$target_arch" ]] || {
    echo "rebuilt _tkinter does not contain exactly target architecture $target_arch" >&2
    exit 1
}
rebuilt_tkinter_sha="$(shasum -a 256 "$rebuilt_tkinter" | awk '{print $1}')"

rm -rf "$runtime"
mkdir -p "$runtime/Python.framework/Versions" "$runtime/tcl"

package_prefix="$("$python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["package_prefix"])' "$package_paths")"
ditto "$package_prefix" "$runtime/Python.framework/Versions/3.14"

staged_tkinter=("$runtime"/Python.framework/Versions/3.14/lib/python3.14/lib-dynload/_tkinter*.so)
[[ ${#staged_tkinter[@]} -eq 1 && -f "${staged_tkinter[0]}" ]] || {
    echo "staged _tkinter is missing or ambiguous" >&2
    exit 1
}
tkinter_so="${staged_tkinter[0]}"
before_tkinter_sha="$(shasum -a 256 "$tkinter_so" | awk '{print $1}')"
cp -p "$rebuilt_tkinter" "$tkinter_so"
rebuilt_staged_tkinter_sha="$(shasum -a 256 "$tkinter_so" | awk '{print $1}')"
[[ "$rebuilt_staged_tkinter_sha" == "$rebuilt_tkinter_sha" ]] || {
    echo "staged rebuilt _tkinter bytes changed during copy" >&2
    exit 1
}
install_changes="$build/tcltk-install-name-changes.tsv"
: > "$install_changes"
framework_version="$runtime/Python.framework/Versions/3.14"
staged_tcl_binary="$framework_version/lib/libtcl8.6.dylib"
staged_tk_binary="$framework_version/lib/libtk8.6.dylib"
staged_tcl_scripts="$framework_version/lib/tcl8.6"
staged_tk_scripts="$framework_version/lib/tk8.6"
for packaged_provider in "$framework_version"/lib/libtcl*.dylib "$framework_version"/lib/libtk*.dylib; do
    [[ -f "$packaged_provider" ]] || continue
    rm -f "$packaged_provider"
done
for packaged_scripts in "$framework_version"/lib/tcl[0-9]* "$framework_version"/lib/tk[0-9]*; do
    [[ -d "$packaged_scripts" ]] || continue
    rm -rf "$packaged_scripts"
done
cp -p "$built_tcl" "$staged_tcl_binary"
cp -p "$built_tk" "$staged_tk_binary"
ditto "$built_tcl_scripts" "$staged_tcl_scripts"
ditto "$built_tk_scripts" "$staged_tk_scripts"
unexpected_tcltk_provider="$(find "$framework_version/lib" -maxdepth 1 \
    \( -type f -o -type l \) \
    \( -name 'libtcl*.dylib' -o -name 'libtk*.dylib' \) \
    ! -name 'libtcl8.6.dylib' ! -name 'libtk8.6.dylib' -print)"
[[ -z "$unexpected_tcltk_provider" ]] || {
    echo "unexpected packaged Tcl/Tk provider remains: $unexpected_tcltk_provider" >&2
    exit 1
}
if find "$framework_version/lib" -maxdepth 1 -type d \
    \( -name 'tcl9*' -o -name 'tk9*' \) -print -quit | grep -q .; then
    echo "packaged Tcl/Tk 9 script roots remain after replacement" >&2
    exit 1
fi

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
# Tk 8.6 on macOS is built against Tcl's stub interface, so libtk8.6.dylib
# intentionally has no direct libtcl8.6.dylib load command to rewrite. _tkinter
# carries the direct runtime bindings to both producer-owned providers above.
install_name_tool -id '@rpath/libtcl8.6.dylib' "$staged_tcl_binary"
install_name_tool -id '@rpath/libtk8.6.dylib' "$staged_tk_binary"
staged_python_executable="$runtime/Python.framework/Versions/3.14/bin/python3.14"
rewrite_dylib_dep "$staged_python_executable" Python '@loader_path/../Python'
staged_app_executable="$runtime/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python"
[[ -f "$staged_app_executable" ]] || {
    echo "staged Python.app executable is missing" >&2
    exit 1
}
rewrite_dylib_dep "$staged_app_executable" Python '@loader_path/../../../../Python'
# Relocate absolute references between libraries supplied by the pinned
# CPython framework. The source distribution also contains libraries such as
# libform and libncurses whose install names point at /Library/Frameworks.
while IFS= read -r -d '' binary; do
    if ! file -b "$binary" | grep -q 'Mach-O'; then
        continue
    fi
    framework_dependencies="$(otool -L "$binary" | awk 'NR > 1 && $1 ~ /^\/Library\/Frameworks\/Python\.framework\/Versions\/3\.14\// {print $1}' | sort -u)"
    [[ -n "$framework_dependencies" ]] || continue
    while IFS= read -r original; do
        [[ -n "$original" ]] || continue
        relative_to_framework="${original#/Library/Frameworks/Python.framework/Versions/3.14/}"
        provider="$framework_version/$relative_to_framework"
        [[ -f "$provider" ]] || {
            echo "pinned Python framework dependency is absent: $binary -> $original" >&2
            exit 1
        }
        relative_provider="$(python3 - "$provider" "$binary" <<'PY'
import os
import sys
print(os.path.relpath(sys.argv[1], os.path.dirname(sys.argv[2])))
PY
)"
        replacement="@loader_path/$relative_provider"
        printf '%s\t%s\t%s\n' "$binary" "$original" "$replacement" >> "$install_changes"
        install_name_tool -change "$original" "$replacement" "$binary"
    done <<< "$framework_dependencies"
    codesign --force --sign - "$binary" >/dev/null
    codesign --verify --strict "$binary"
done < <(find "$framework_version" -type f -print0)
# The pinned universal distribution includes optional architecture-specific
# launchers. Keep only helpers usable by this output profile, and relocate
# their framework imports before the full-tree Mach-O closure audit.
for helper in "$runtime"/Python.framework/Versions/3.14/bin/python3.14-*; do
    [[ -f "$helper" && ! -L "$helper" ]] || continue
    if ! file -b "$helper" | grep -q 'Mach-O'; then
        continue
    fi
    if ! lipo -archs "$helper" | tr ' ' '\n' | grep -Fxq "$target_arch"; then
        rm "$helper"
        continue
    fi
    rewrite_dylib_dep "$helper" Python '@loader_path/../Python'
done
codesign --force --sign - "$tkinter_so" >/dev/null
codesign --force --sign - "$staged_tcl_binary" >/dev/null
codesign --force --sign - "$staged_tk_binary" >/dev/null
codesign --force --sign - "$staged_python_executable" >/dev/null
codesign --force --sign - "$staged_app_executable" >/dev/null
for helper in "$runtime"/Python.framework/Versions/3.14/bin/python3.14-*; do
    [[ -f "$helper" && ! -L "$helper" ]] || continue
    codesign --force --sign - "$helper" >/dev/null
done
codesign --verify --strict "$tkinter_so"
codesign --verify --strict "$staged_tcl_binary"
codesign --verify --strict "$staged_tk_binary"
codesign --verify --strict "$staged_python_executable"
codesign --verify --strict "$staged_app_executable"
for helper in "$runtime"/Python.framework/Versions/3.14/bin/python3.14-*; do
    [[ -f "$helper" && ! -L "$helper" ]] || continue
    codesign --verify --strict "$helper"
done

for binary in "$tkinter_so" "$staged_python_executable" "$staged_app_executable"; do
    if otool -L "$binary" | grep -Eq '/Library/Frameworks/Python\.framework/.*/lib/lib(tcl|tk)8\.6\.dylib|/Library/Frameworks/(Tcl|Tk)\.framework/'; then
        echo "ambient Tcl/Tk dependency remains after relocation: $binary" >&2
        otool -L "$binary" >&2
        exit 1
    fi
done
for binary in "$staged_python_executable" "$staged_app_executable" "$runtime"/Python.framework/Versions/3.14/bin/python3.14-*; do
    [[ -f "$binary" && ! -L "$binary" ]] || continue
    if otool -L "$binary" | awk 'NR > 1 { print $1 }' | grep -Fq '/Library/Frameworks/Python.framework/'; then
        echo "ambient Python framework dependency remains after launcher relocation: $binary" >&2
        otool -L "$binary" >&2
        exit 1
    fi
done

tcl_scripts="$runtime/Python.framework/Versions/3.14/lib/tcl8.6"
tk_scripts="$runtime/Python.framework/Versions/3.14/lib/tk8.6"
[[ -d "$tcl_scripts" && -d "$tk_scripts" ]] || { echo "private Tcl/Tk script roots are missing" >&2; exit 1; }
ditto "$tcl_scripts" "$runtime/tcl/tcl8.6"
ditto "$tk_scripts" "$runtime/tcl/tk8.6"

site_packages="$runtime/Python.framework/Versions/3.14/lib/python3.14/site-packages"
rm -rf "$site_packages"
mkdir -p "$site_packages"
printf '%s\n' core > "$runtime/RUNTIME_PROFILE"

after_tkinter_sha="$(shasum -a 256 "$tkinter_so" | awk '{print $1}')"
tkinter_relative="${tkinter_so#"$runtime"/}"
staged_python_sha="$(shasum -a 256 "$runtime/Python.framework/Versions/3.14/Python" | awk '{print $1}')"
staged_executable_sha="$(shasum -a 256 "$runtime/Python.framework/Versions/3.14/bin/python3.14" | awk '{print $1}')"
staged_tcl_sha="$(shasum -a 256 "$staged_tcl_binary" | awk '{print $1}')"
staged_tk_sha="$(shasum -a 256 "$staged_tk_binary" | awk '{print $1}')"

"$python" - "$package_paths" "$install_changes" "$runtime/build-provenance-macos.json" \
    "$archive_sha" "$before_tkinter_sha" "$after_tkinter_sha" \
    "$staged_python_sha" "$staged_executable_sha" "$staged_tcl_sha" "$staged_tk_sha" \
    "$selected_prefix" "$selected_executable" "$target_arch" "$archive_asset_id" \
    "$archive_name" "$tcl_source_sha" "$tk_source_sha" "$tcl_name" "$tk_name" \
    "$python_source_sha" "$python_source_name" "$rebuilt_tkinter_sha" "$tkinter_relative" <<'PY'
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
        "version": "3.14.10",
        "architecture": target_arch,
        "release": "3.14.10-40336797752",
        "archive_asset_id": archive_asset_id,
        "checksum_asset_id": 678419306,
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
        "version": "8.6.18",
        "ownership": "producer",
        "tcl_api_version": "8.6",
        "tk_api_version": "8.6",
        "tcl_runtime_version": "8.6.18",
        "tk_runtime_version": "8.6.18",
        "source_lock": "locks/core/tcltk-8.6.18.json",
        "sources": {
            "tcl": {
                "filename": sys.argv[18],
                "url": "https://prdownloads.sourceforge.net/tcl/tcl8.6.18-src.tar.gz",
                "sha256": sys.argv[16],
            },
            "tk": {
                "filename": sys.argv[19],
                "url": "https://prdownloads.sourceforge.net/tcl/tk8.6.18-src.tar.gz",
                "sha256": sys.argv[17],
            },
        },
        "source_hashes": {
            "tcl": sys.argv[16],
            "tk": sys.argv[17],
            "cpython": sys.argv[20],
            "tkinter_package": package["source_hashes"]["tkinter"],
        },
        "staged_hashes": {
            "tcl": sys.argv[9],
            "tk": sys.argv[10],
            "tkinter_package_original": sys.argv[5],
            "tkinter_rebuilt_before_rewrite": sys.argv[22],
            "tkinter_after_rewrite": sys.argv[6],
        },
        "tkinter_rebuild": {
            "reason": "actions/python-versions CPython 3.14.10 _tkinter targets Tcl/Tk 9; producer contract requires Tcl/Tk 8.6.18",
            "scope": ["Modules/_tkinter.c", "Modules/tkappinit.c"],
            "cpython_source": {
                "filename": sys.argv[21],
                "url": "https://www.python.org/ftp/python/3.14.10/Python-3.14.10.tar.xz",
                "sha256": sys.argv[20],
            },
            "output_path": sys.argv[23],
            "compiled_sha256": sys.argv[22],
            "staged_sha256": sys.argv[6],
            "package_original_sha256": sys.argv[5],
            "architecture": target_arch,
            "macos_floor": "14.0",
            "compiler": "clang",
            "defines": ["Py_BUILD_CORE_MODULE", "WITH_APPINIT=1"],
        },
        "install_name_changes": changes,
        "build": {
            "macos_floor": "14.0",
            "architecture": target_arch,
            "tcl_configure": ["--enable-shared", "--enable-threads", "--enable-64bit"],
            "tk_configure": [
                "--enable-shared",
                "--enable-threads",
                "--enable-64bit",
                "--enable-aqua",
                "--with-tcl=<build>/tcl-build",
            ],
        },
        "loader_strategy": "runtime-relative-loader-path-dylibs",
    },
}
output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

staged_python_home="$runtime/Python.framework/Versions/3.14"
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
if not (home / "lib/python3.14/encodings").is_dir():
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
echo "tcl_tk_8_6_18=pass"
echo "native_closure=pass"
echo "macos_floor=pass"
echo "relocated_tcl_tk=pass"
echo "macos_runtime_staging=pass"
