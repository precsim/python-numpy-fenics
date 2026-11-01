#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
build="$root/build/linux-glibc217-runtime"
runtime="${PY_RUNTIME_OUTPUT:-$root/out/linux-x64/core}"
src_root="$build/src"
tcltk_prefix="$build/tcltk-prefix"
openssl_prefix="$build/openssl-prefix"
openssl_perl_lib="$build/openssl-perl-lib"
openssl_perl_compat="$openssl_perl_lib/IPC/Cmd.pm"
openssl_time_piece_compat="$openssl_perl_lib/Time/Piece.pm"

image_repository="quay.io/pypa/manylinux2014_x86_64"
image_digest="sha256:296069bc05d85275248f168f6e3b34ba8aa06181b67e40cd7447c64d56d3a851"
image_ref="$image_repository@$image_digest"

python_name="Python-3.12.10.tar.xz"
python_url="https://www.python.org/ftp/python/3.12.10/$python_name"
python_sha="07ab697474595e06f06647417d3c7fa97ded07afc1a7e4454c5639919b46eaea"
openssl_name="openssl-3.5.9.tar.gz"
openssl_url="https://github.com/openssl/openssl/releases/download/openssl-3.5.9/$openssl_name"
openssl_sha="603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"
libxcrypt_name="libxcrypt-4.5.2.tar.xz"
libxcrypt_url="https://github.com/besser82/libxcrypt/releases/download/v4.5.2/$libxcrypt_name"
libxcrypt_sha="71513a31c01a428bccd5367a32fd95f115d6dac50fb5b60c779d5c7942aec071"
tcl_name="tcl8.6.12-src.tar.gz"
tcl_url="https://prdownloads.sourceforge.net/tcl/$tcl_name"
tcl_sha="26c995dd0f167e48b11961d891ee555f680c175f7173ff8cb829f4ebcde4c1a6"
tk_name="tk8.6.12-src.tar.gz"
tk_url="https://prdownloads.sourceforge.net/tcl/$tk_name"
tk_sha="12395c1f3fcb6bed2938689f797ea3cdf41ed5cb6c4766eec8ac949560310630"

# The final manylinux2014 image intentionally removes development packages after
# building its helper interpreters. These exact CentOS Vault RPMs restore only
# the matching headers/linker metadata needed to retain the helper CPython 3.12
# native-extension surface. Runtime provider bytes continue to come from the
# pinned image and are closed/staged separately below.
dev_rpm_names=(
    "bzip2-devel-1.0.6-13.el7.x86_64.rpm"
    "gdbm-devel-1.10-8.el7.x86_64.rpm"
    "libffi-devel-3.0.13-19.el7.x86_64.rpm"
    "libuuid-devel-2.23.2-65.el7_9.1.x86_64.rpm"
    "ncurses-devel-5.9-14.20130511.el7_4.x86_64.rpm"
    "readline-devel-6.2-11.el7.x86_64.rpm"
    "xz-devel-5.2.2-2.el7_9.x86_64.rpm"
)
dev_rpm_urls=(
    "https://vault.centos.org/7.9.2009/os/x86_64/Packages/bzip2-devel-1.0.6-13.el7.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/os/x86_64/Packages/gdbm-devel-1.10-8.el7.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/os/x86_64/Packages/libffi-devel-3.0.13-19.el7.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/updates/x86_64/Packages/libuuid-devel-2.23.2-65.el7_9.1.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/os/x86_64/Packages/ncurses-devel-5.9-14.20130511.el7_4.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/os/x86_64/Packages/readline-devel-6.2-11.el7.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/updates/x86_64/Packages/xz-devel-5.2.2-2.el7_9.x86_64.rpm"
)
dev_rpm_shas=(
    "28e0eef4dfa7e5a79bd4e46a5174091c87027a2b2e33e1e9e19958bcc1ac6700"
    "fc02a803f89c0d68de96bcc7a46d7cdf8768e62577b27aed9c531e6fb2818c30"
    "a31ba33f74252929d396e4d8557fe1c40be95d777aad0660771b14f1187b3b2e"
    "edc0594488f23ae7a46857065d3cd4d74e61a711b96dd5b99713419b033816a4"
    "1c93fc77242cf1129f1aa44a8e31fa118511681776d17f0ffb2c3e84bb811a6d"
    "dd4f93b315f8a2facdd3508ac5b7fe070d663d4ebf70cdfa2781ffb6abd25160"
    "8c62263350421e7c3081b4a531f8e15ef5bfb5e410a34687ca4a799ff23362ef"
)

# These exact runtime RPMs are legal/provenance fallbacks only. Their matching
# provider bytes always come from the pinned manylinux image. The fallback is
# used only when the image's RPM database names a legal file whose bytes were
# stripped as %doc from the final image.
license_rpm_packages=(
    "bzip2-libs"
    "gdbm"
    "libffi"
    "libuuid"
    "ncurses-libs"
    "readline"
    "xz-libs"
    "zlib"
)
license_rpm_archive_packages=(
    "bzip2-libs"
    "gdbm"
    "libffi"
    "libuuid"
    "ncurses"
    "readline"
    "xz-libs"
    "zlib"
)
license_rpm_version_releases=(
    "1.0.6-13.el7"
    "1.10-8.el7"
    "3.0.13-19.el7"
    "2.23.2-65.el7_9.1"
    "5.9-14.20130511.el7_4"
    "6.2-11.el7"
    "5.2.2-2.el7_9"
    "1.2.7-21.el7_9"
)
license_rpm_licenses=(
    "BSD"
    "GPLv3+"
    "MIT and Public Domain"
    "BSD"
    "MIT"
    "GPLv3+"
    "LGPLv2+"
    "zlib and Boost"
)
license_rpm_names=(
    "bzip2-libs-1.0.6-13.el7.x86_64.rpm"
    "gdbm-1.10-8.el7.x86_64.rpm"
    "libffi-3.0.13-19.el7.x86_64.rpm"
    "libuuid-2.23.2-65.el7_9.1.x86_64.rpm"
    "ncurses-5.9-14.20130511.el7_4.x86_64.rpm"
    "readline-6.2-11.el7.x86_64.rpm"
    "xz-libs-5.2.2-2.el7_9.x86_64.rpm"
    "zlib-1.2.7-21.el7_9.x86_64.rpm"
)
license_rpm_urls=(
    "https://vault.centos.org/7.9.2009/os/x86_64/Packages/bzip2-libs-1.0.6-13.el7.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/os/x86_64/Packages/gdbm-1.10-8.el7.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/os/x86_64/Packages/libffi-3.0.13-19.el7.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/updates/x86_64/Packages/libuuid-2.23.2-65.el7_9.1.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/os/x86_64/Packages/ncurses-5.9-14.20130511.el7_4.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/os/x86_64/Packages/readline-6.2-11.el7.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/updates/x86_64/Packages/xz-libs-5.2.2-2.el7_9.x86_64.rpm"
    "https://vault.centos.org/7.9.2009/updates/x86_64/Packages/zlib-1.2.7-21.el7_9.x86_64.rpm"
)
license_rpm_shas=(
    "1fd32866a2a82f5abc19142b59dce8fd961bc1d32e3bedfa0de7a950066fdaa5"
    "65f28fdefa291bfe53e9c8646ce0f7510f312a4fee232af36e3d4fe678b60591"
    "670463ed744b7243cfe32f0c4aa6676657a6078a6e2009a51fdb7b907b98ef71"
    "f530a65c0efca53f618f63e02801129283b12266fbf21ac221e35617a45b0d4b"
    "59773dff8c5aa8f084fd2edc88b5dc34f574f254ec98280fc2b0adafe07f85a2"
    "d9e52b61beb1ac9c3006604d9ebd4f930f667b64478a61fd78de7ed74922f52e"
    "4b698de5fd7e0a64306106f3018e9d00dedc1f7a46d354339f012c97d004bd0c"
    "2bee86d12902723756eafa82ba6cc322136c4a67a78e0104ac988e14eb7fb5a1"
)
license_rpm_members=(
    "usr/share/doc/bzip2-libs-1.0.6/LICENSE"
    "usr/share/doc/gdbm-1.10/COPYING"
    "usr/share/doc/libffi-3.0.13/LICENSE"
    "usr/share/doc/libuuid-2.23.2/COPYING"
    "usr/share/doc/ncurses-5.9/README"
    "usr/share/doc/readline-6.2/COPYING"
    "usr/share/doc/xz-libs-5.2.2/COPYING.LGPLv2.1"
    "usr/share/doc/zlib-1.2.7/README"
)
license_rpm_member_shas=(
    "4919cfb14a73cd64fcef67b107613970cf1659a09aa675dba31314f373bc7204"
    "690d762f2e8e149ab1e2d6a409a3853b6151a2533b2382fae549a176d6bedecf"
    "414f460aed519486beb78e92ea712c9ccf87c1acfb735a82d301f4cdf06f48e3"
    "18a4a3931b5b703202793f7ec14b07639dd6b502162049f66250aa6445032ae3"
    "37e56186af1edbc4b0c41b85e224295fe2ef114399a488651ebc658f57bf80c7"
    "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903"
    "dc626520dcd53a22f727af3ee42c770e56c97a64fe3adb063799d8ab032fe551"
    "a4dfc7f4225a68315763aafb536428852aff6dd3135ab4e0cdc9e3e6a65fd796"
)
helper_python="/opt/python/cp312-cp312/bin/python"
jobs="${PY_RUNTIME_BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)}"

usage() {
    cat >&2 <<'EOF'
usage: scripts/linux/build-core-runtime.sh \
  --python-source <Python-3.12.10.tar.xz> \
  --openssl-source <openssl-3.5.9.tar.gz> \
  --libxcrypt-source <libxcrypt-4.5.2.tar.xz> \
  --dev-rpm-dir <directory-containing-exact-development-and-license-rpms> \
  --tcl-source <tcl8.6.12-src.tar.gz> \
  --tk-source <tk8.6.12-src.tar.gz>
EOF
    exit 2
}

die() {
    echo "error: $*" >&2
    exit 1
}

for field_count in \
    "${#license_rpm_archive_packages[@]}" \
    "${#license_rpm_version_releases[@]}" \
    "${#license_rpm_licenses[@]}" \
    "${#license_rpm_names[@]}" \
    "${#license_rpm_urls[@]}" \
    "${#license_rpm_shas[@]}" \
    "${#license_rpm_members[@]}" \
    "${#license_rpm_member_shas[@]}"; do
    [[ "$field_count" -eq "${#license_rpm_packages[@]}" ]] || \
        die "license/provenance RPM pin arrays are inconsistent"
done

python_source=""
openssl_source=""
libxcrypt_source=""
dev_rpm_dir=""
tcl_source=""
tk_source=""
while (($#)); do
    case "$1" in
        --python-source)
            (($# >= 2)) || usage
            python_source="$2"
            shift 2
            ;;
        --openssl-source)
            (($# >= 2)) || usage
            openssl_source="$2"
            shift 2
            ;;
        --libxcrypt-source)
            (($# >= 2)) || usage
            libxcrypt_source="$2"
            shift 2
            ;;
        --dev-rpm-dir)
            (($# >= 2)) || usage
            dev_rpm_dir="$2"
            shift 2
            ;;
        --tcl-source)
            (($# >= 2)) || usage
            tcl_source="$2"
            shift 2
            ;;
        --tk-source)
            (($# >= 2)) || usage
            tk_source="$2"
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

[[ -n "$python_source" && -n "$openssl_source" && -n "$libxcrypt_source" && -n "$dev_rpm_dir" && -n "$tcl_source" && -n "$tk_source" ]] || usage
[[ -f "$python_source" ]] || die "CPython source archive is missing: $python_source"
[[ -f "$openssl_source" ]] || die "OpenSSL source archive is missing: $openssl_source"
[[ -f "$libxcrypt_source" ]] || die "libxcrypt source archive is missing: $libxcrypt_source"
[[ -d "$dev_rpm_dir" ]] || die "development RPM input directory is missing: $dev_rpm_dir"
[[ -f "$tcl_source" ]] || die "Tcl source archive is missing: $tcl_source"
[[ -f "$tk_source" ]] || die "Tk source archive is missing: $tk_source"
[[ "$(basename "$python_source")" == "$python_name" ]] || die "expected CPython source filename $python_name"
[[ "$(basename "$openssl_source")" == "$openssl_name" ]] || die "expected OpenSSL source filename $openssl_name"
[[ "$(basename "$libxcrypt_source")" == "$libxcrypt_name" ]] || die "expected libxcrypt source filename $libxcrypt_name"
[[ "$(basename "$tcl_source")" == "$tcl_name" ]] || die "expected Tcl source filename $tcl_name"
[[ "$(basename "$tk_source")" == "$tk_name" ]] || die "expected Tk source filename $tk_name"

for tool in bash perl gcc ld as ar ranlib strip make pkg-config readelf ldd patchelf sha256sum tar rpm rpm2cpio cpio find grep sed awk; do
    command -v "$tool" >/dev/null 2>&1 || die "required pinned-image tool is missing: $tool"
done
[[ -x "$helper_python" ]] || die "pinned-image helper Python is missing: $helper_python"
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || die "PY_RUNTIME_BUILD_JOBS must be a positive integer"

machine="$(uname -m)"
[[ "$machine" == "x86_64" ]] || die "expected x86_64 build image, got $machine"
glibc_identity="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
[[ "$glibc_identity" == "glibc 2.17" ]] || die "expected glibc 2.17 build image, got ${glibc_identity:-<unknown>}"
if [[ -n "${AUDITWHEEL_PLAT:-}" ]]; then
    case "$AUDITWHEEL_PLAT" in
        manylinux2014_x86_64|manylinux_2_17_x86_64) ;;
        *) die "unexpected AUDITWHEEL_PLAT in pinned build image: $AUDITWHEEL_PLAT" ;;
    esac
fi
[[ -n "${PY_RUNTIME_BUILD_IMAGE_REF:-}" ]] || \
    die "PY_RUNTIME_BUILD_IMAGE_REF is required and must identify the pinned image: $image_ref"
[[ "$PY_RUNTIME_BUILD_IMAGE_REF" == "$image_ref" ]] || \
    die "PY_RUNTIME_BUILD_IMAGE_REF must identify the pinned image: $image_ref"
[[ "${PKG_CONFIG_PATH:-}" == "/usr/local/lib/pkgconfig" ]] || \
    die "pinned image PKG_CONFIG_PATH must be exactly /usr/local/lib/pkgconfig; got ${PKG_CONFIG_PATH:-<unset>}"
[[ -d /usr/local/lib/pkgconfig ]] || die "pinned image package metadata root is missing: /usr/local/lib/pkgconfig"
[[ -e /usr/local/lib/pkgconfig/sqlite3.pc ]] || die "pinned image SQLite pkg-config metadata is missing"

# The final manylinux image keeps the runtime libraries used by its helper
# CPython but removes the corresponding development packages. Emit their exact
# installed identities so hosted qualification can bind any admitted header
# overlay to the same provider versions instead of guessing from CentOS 7.
rpm -q --qf 'runtime_package=%{NAME} version=%{VERSION}-%{RELEASE} arch=%{ARCH}\n' \
    bzip2-libs libffi ncurses-libs readline gdbm xz-libs libuuid

actual_python_sha="$(sha256sum "$python_source" | awk '{print $1}')"
actual_openssl_sha="$(sha256sum "$openssl_source" | awk '{print $1}')"
actual_libxcrypt_sha="$(sha256sum "$libxcrypt_source" | awk '{print $1}')"
actual_tcl_sha="$(sha256sum "$tcl_source" | awk '{print $1}')"
actual_tk_sha="$(sha256sum "$tk_source" | awk '{print $1}')"
[[ "$actual_python_sha" == "$python_sha" ]] || die "CPython source hash mismatch: expected $python_sha, got $actual_python_sha"
[[ "$actual_openssl_sha" == "$openssl_sha" ]] || die "OpenSSL source hash mismatch: expected $openssl_sha, got $actual_openssl_sha"
[[ "$actual_libxcrypt_sha" == "$libxcrypt_sha" ]] || die "libxcrypt source hash mismatch: expected $libxcrypt_sha, got $actual_libxcrypt_sha"
[[ "$actual_tcl_sha" == "$tcl_sha" ]] || die "Tcl source hash mismatch: expected $tcl_sha, got $actual_tcl_sha"
[[ "$actual_tk_sha" == "$tk_sha" ]] || die "Tk source hash mismatch: expected $tk_sha, got $actual_tk_sha"

dev_rpm_paths=()
for index in "${!dev_rpm_names[@]}"; do
    rpm_path="$dev_rpm_dir/${dev_rpm_names[$index]}"
    [[ -f "$rpm_path" ]] || die "development RPM input is missing: $rpm_path"
    actual_rpm_sha="$(sha256sum "$rpm_path" | awk '{print $1}')"
    [[ "$actual_rpm_sha" == "${dev_rpm_shas[$index]}" ]] || \
        die "development RPM hash mismatch for ${dev_rpm_names[$index]}: expected ${dev_rpm_shas[$index]}, got $actual_rpm_sha"
    dev_rpm_paths+=("$rpm_path")
done
license_rpm_paths=()
for index in "${!license_rpm_names[@]}"; do
    rpm_path="$dev_rpm_dir/${license_rpm_names[$index]}"
    [[ -f "$rpm_path" ]] || die "license/provenance RPM input is missing: $rpm_path"
    actual_rpm_sha="$(sha256sum "$rpm_path" | awk '{print $1}')"
    [[ "$actual_rpm_sha" == "${license_rpm_shas[$index]}" ]] || \
        die "license/provenance RPM hash mismatch for ${license_rpm_names[$index]}: expected ${license_rpm_shas[$index]}, got $actual_rpm_sha"
    license_rpm_paths+=("$rpm_path")
done

rm -rf "$build" "$runtime"
mkdir -p "$src_root" "$runtime"

# Copy and extract every exact legal-evidence RPM under generated build state so
# qualification can independently re-hash the archives and members after the
# original /inputs mount is gone. None of these runtime RPMs is installed.
license_rpm_input_root="$build/license-inputs"
license_rpm_manifest_tsv="$build/license-rpm-inputs.tsv"
license_rpm_provenance_json="$build/license-rpm-provenance.json"
mkdir -p "$license_rpm_input_root"
: > "$license_rpm_manifest_tsv"
for index in "${!license_rpm_names[@]}"; do
    package="${license_rpm_packages[$index]}"
    archive_package="${license_rpm_archive_packages[$index]}"
    version_release="${license_rpm_version_releases[$index]}"
    license_id="${license_rpm_licenses[$index]}"
    rpm_name="${license_rpm_names[$index]}"
    rpm_url="${license_rpm_urls[$index]}"
    rpm_sha="${license_rpm_shas[$index]}"
    member="${license_rpm_members[$index]}"
    member_sha="${license_rpm_member_shas[$index]}"
    rpm_copy="$license_rpm_input_root/$rpm_name"
    extract_root="$license_rpm_input_root/${package}-extracted"
    cp -p "${license_rpm_paths[$index]}" "$rpm_copy"
    [[ "$(sha256sum "$rpm_copy" | awk '{print $1}')" == "$rpm_sha" ]] || \
        die "copied license/provenance RPM hash mismatch for $rpm_name"
    observed_identity="$(rpm -qp --qf '%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\t%{LICENSE}' "$rpm_copy")"
    expected_identity="${archive_package}"$'\t'"${version_release}"$'\t'"x86_64"$'\t'"${license_id}"
    [[ "$observed_identity" == "$expected_identity" ]] || \
        die "unexpected license/provenance RPM identity for $rpm_name: $observed_identity"
    mkdir -p "$extract_root"
    (
        cd "$extract_root"
        rpm2cpio "$rpm_copy" | cpio -idm --quiet "./$member"
    )
    source_path="$extract_root/$member"
    [[ -f "$source_path" ]] || die "license/provenance RPM $rpm_name lacks $member"
    [[ "$(sha256sum "$source_path" | awk '{print $1}')" == "$member_sha" ]] || \
        die "license/provenance member hash mismatch for $rpm_name: $member"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$package" "$archive_package" "$version_release" "$license_id" "$rpm_name" "$rpm_url" \
        "$rpm_sha" "$rpm_copy" "$member" "$member_sha" "$source_path" "x86_64" \
        >> "$license_rpm_manifest_tsv"
done
"$helper_python" - "$license_rpm_manifest_tsv" "$license_rpm_provenance_json" <<'PY'
import hashlib
import json
import pathlib
import sys

manifest = pathlib.Path(sys.argv[1])
output = pathlib.Path(sys.argv[2])

def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

records = []
for line in manifest.read_text(encoding="utf-8").splitlines():
    fields = line.split("\t")
    if len(fields) != 12:
        raise SystemExit(f"invalid license/provenance RPM manifest line: {line!r}")
    package, archive_package, version_release, license_id, filename, url, expected_sha, archive_arg, member, member_sha, source_arg, arch = fields
    archive = pathlib.Path(archive_arg).resolve()
    source = pathlib.Path(source_arg).resolve()
    actual_archive_sha = sha(archive)
    actual_member_sha = sha(source)
    if actual_archive_sha != expected_sha or actual_member_sha != member_sha:
        raise SystemExit(f"license/provenance RPM evidence changed after shell verification: {filename}")
    records.append({
        "package": package,
        "archive_package": archive_package,
        "version_release": version_release,
        "arch": arch,
        "license": license_id,
        "filename": filename,
        "url": url,
        "sha256": expected_sha,
        "verified_archive_path": str(archive),
        "verified_archive_sha256": actual_archive_sha,
        "member": member,
        "member_sha256": member_sha,
        "source_path": str(source),
        "source_sha256": actual_member_sha,
        "usage": "license-provenance-only",
    })

output.write_text(json.dumps({"packages": records}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
rm -f "$license_rpm_manifest_tsv"

# Install the exact matching development overlay into this ephemeral offline
# build container. Hashes above are the trust boundary; no package-manager
# resolution or network access is permitted here. The transaction is allowed to
# add headers, unversioned linker symlinks and pkg-config metadata only; final
# runtime closure later resolves provider bytes from the original pinned image.
rpm -ivh --nosignature "${dev_rpm_paths[@]}"
dev_rpm_provenance_json="$build/development-rpm-provenance.json"
"$helper_python" - "$dev_rpm_dir" "$dev_rpm_provenance_json" <<'PY'
import hashlib
import json
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1]).resolve()
output = pathlib.Path(sys.argv[2])
records = [
    ("bzip2-devel", "1.0.6-13.el7", "bzip2-libs", "1.0.6-13.el7", "bzip2-devel-1.0.6-13.el7.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/bzip2-devel-1.0.6-13.el7.x86_64.rpm", "28e0eef4dfa7e5a79bd4e46a5174091c87027a2b2e33e1e9e19958bcc1ac6700"),
    ("gdbm-devel", "1.10-8.el7", "gdbm", "1.10-8.el7", "gdbm-devel-1.10-8.el7.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/gdbm-devel-1.10-8.el7.x86_64.rpm", "fc02a803f89c0d68de96bcc7a46d7cdf8768e62577b27aed9c531e6fb2818c30"),
    ("libffi-devel", "3.0.13-19.el7", "libffi", "3.0.13-19.el7", "libffi-devel-3.0.13-19.el7.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/libffi-devel-3.0.13-19.el7.x86_64.rpm", "a31ba33f74252929d396e4d8557fe1c40be95d777aad0660771b14f1187b3b2e"),
    ("libuuid-devel", "2.23.2-65.el7_9.1", "libuuid", "2.23.2-65.el7_9.1", "libuuid-devel-2.23.2-65.el7_9.1.x86_64.rpm", "https://vault.centos.org/7.9.2009/updates/x86_64/Packages/libuuid-devel-2.23.2-65.el7_9.1.x86_64.rpm", "edc0594488f23ae7a46857065d3cd4d74e61a711b96dd5b99713419b033816a4"),
    ("ncurses-devel", "5.9-14.20130511.el7_4", "ncurses-libs", "5.9-14.20130511.el7_4", "ncurses-devel-5.9-14.20130511.el7_4.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/ncurses-devel-5.9-14.20130511.el7_4.x86_64.rpm", "1c93fc77242cf1129f1aa44a8e31fa118511681776d17f0ffb2c3e84bb811a6d"),
    ("readline-devel", "6.2-11.el7", "readline", "6.2-11.el7", "readline-devel-6.2-11.el7.x86_64.rpm", "https://vault.centos.org/7.9.2009/os/x86_64/Packages/readline-devel-6.2-11.el7.x86_64.rpm", "dd4f93b315f8a2facdd3508ac5b7fe070d663d4ebf70cdfa2781ffb6abd25160"),
    ("xz-devel", "5.2.2-2.el7_9", "xz-libs", "5.2.2-2.el7_9", "xz-devel-5.2.2-2.el7_9.x86_64.rpm", "https://vault.centos.org/7.9.2009/updates/x86_64/Packages/xz-devel-5.2.2-2.el7_9.x86_64.rpm", "8c62263350421e7c3081b4a531f8e15ef5bfb5e410a34687ca4a799ff23362ef"),
]

def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

result = []
for package, version_release, runtime_package, runtime_version_release, filename, url, expected_sha in records:
    path = root / filename
    actual_sha = sha256(path)
    if actual_sha != expected_sha:
        raise SystemExit(f"development RPM changed after verification: {filename}")
    installed = subprocess.check_output(
        ["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}\t%{ARCH}", package], text=True
    ).strip()
    if installed != f"{version_release}\tx86_64":
        raise SystemExit(f"unexpected installed development RPM identity for {package}: {installed!r}")
    runtime_installed = subprocess.check_output(
        ["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}\t%{ARCH}", runtime_package], text=True
    ).strip()
    if runtime_installed != f"{runtime_version_release}\tx86_64":
        raise SystemExit(
            f"development RPM {package} does not match pinned-image runtime package "
            f"{runtime_package}: {runtime_installed!r}"
        )
    result.append({
        "package": package,
        "version_release": version_release,
        "runtime_package": runtime_package,
        "runtime_version_release": runtime_version_release,
        "arch": "x86_64",
        "filename": filename,
        "url": url,
        "sha256": expected_sha,
        "verified_archive_sha256": actual_sha,
        "usage": "build-only-development-overlay",
    })

output.write_text(json.dumps({"packages": result}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

# Capture the complete native-extension module inventory from the pinned-image
# helper CPython. This is the capability baseline the staged interpreter must
# preserve, independent of the smaller functional smoke-import list below.
helper_dynamic_extension_baseline_json="$build/helper-dynamic-extension-baseline.json"
"$helper_python" - "$helper_dynamic_extension_baseline_json" <<'PY'
import importlib.machinery
import json
import pathlib
import sys
import sysconfig

output = pathlib.Path(sys.argv[1])
destshared = sysconfig.get_config_var("DESTSHARED")
lib_dynload = pathlib.Path(destshared) if destshared else pathlib.Path(sysconfig.get_path("stdlib")) / "lib-dynload"
if not lib_dynload.is_dir():
    raise SystemExit(f"pinned-image helper lib-dynload is missing: {lib_dynload}")

suffixes = sorted(set(importlib.machinery.EXTENSION_SUFFIXES), key=len, reverse=True)
modules = set()
for path in sorted(lib_dynload.iterdir()):
    if not path.is_file():
        continue
    for suffix in suffixes:
        if path.name.endswith(suffix):
            module = path.name[:-len(suffix)]
            if not module:
                raise SystemExit(f"empty module name from helper extension filename: {path.name}")
            modules.add(module)
            break

if not modules:
    raise SystemExit(f"pinned-image helper lib-dynload has no extension modules: {lib_dynload}")

data = {
    "helper_python": str(pathlib.Path(sys.executable).resolve()),
    "helper_version": sys.version.split()[0],
    "lib_dynload": str(lib_dynload.resolve()),
    "extension_suffixes": suffixes,
    "modules": sorted(modules),
}
output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

# Record the OpenSSL runtime identity used by the helper CPython in the pinned
# image. The final manylinux image deliberately removes its OpenSSL development
# headers, so the controlled runtime rebuilds the same pinned OpenSSL release
# from source below rather than borrowing an ambient development provider.
openssl_provider_json="$build/openssl-provider.json"
"$helper_python" - "$openssl_provider_json" <<'PY'
import hashlib
import json
import pathlib
import re
import ssl
import subprocess
import sys
import _ssl

output = pathlib.Path(sys.argv[1])
if not ssl.OPENSSL_VERSION.startswith("OpenSSL 3.5.9 "):
    raise SystemExit(
        "pinned-image helper CPython OpenSSL version does not match the controlled "
        f"OpenSSL 3.5.9 source input: {ssl.OPENSSL_VERSION}"
    )

extension = pathlib.Path(_ssl.__file__).resolve()
ldd = subprocess.check_output(["ldd", str(extension)], text=True)
resolved = {}
for line in ldd.splitlines():
    match = re.match(r"\s*(\S+)\s+=>\s+(\S+)\s+\(", line)
    if match:
        resolved[match.group(1)] = pathlib.Path(match.group(2)).resolve()

ssl_candidates = [(name, path) for name, path in resolved.items() if name.startswith("libssl.so")]
crypto_candidates = [(name, path) for name, path in resolved.items() if name.startswith("libcrypto.so")]
if len(ssl_candidates) != 1 or len(crypto_candidates) != 1:
    raise SystemExit(
        f"unable to derive unique helper OpenSSL providers from {_ssl.__file__}: "
        f"ssl={ssl_candidates}, crypto={crypto_candidates}"
    )
ssl_name, ssl_path = ssl_candidates[0]
crypto_name, crypto_path = crypto_candidates[0]
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

data = {
    "version": ssl.OPENSSL_VERSION,
    "version_info": list(ssl.OPENSSL_VERSION_INFO),
    "helper_ssl_extension": str(extension),
    "helper_ssl_extension_sha256": sha(extension),
    "ssl_needed_name": ssl_name,
    "ssl_provider": str(ssl_path),
    "ssl_provider_sha256": sha(ssl_path),
    "crypto_needed_name": crypto_name,
    "crypto_provider": str(crypto_path),
    "crypto_provider_sha256": sha(crypto_path),
}
output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

tar -xJf "$python_source" -C "$src_root"
tar -xzf "$openssl_source" -C "$src_root"
tar -xJf "$libxcrypt_source" -C "$src_root"
tar -xzf "$tcl_source" -C "$src_root"
tar -xzf "$tk_source" -C "$src_root"

python_src="$src_root/Python-3.12.10"
openssl_src="$src_root/openssl-3.5.9"
libxcrypt_src="$src_root/libxcrypt-4.5.2"
tcl_src="$src_root/tcl8.6.12"
tk_src="$src_root/tk8.6.12"
[[ -f "$python_src/configure" && -f "$python_src/LICENSE" ]] || die "CPython source archive layout is unexpected"
[[ -f "$openssl_src/Configure" && -f "$openssl_src/LICENSE.txt" ]] || die "OpenSSL source archive layout is unexpected"
[[ -f "$libxcrypt_src/LICENSING" && -f "$libxcrypt_src/COPYING.LIB" && -f "$libxcrypt_src/AUTHORS" ]] || \
    die "libxcrypt source archive layout is unexpected"
[[ -f "$tcl_src/unix/configure" && -f "$tcl_src/license.terms" ]] || die "Tcl source archive layout is unexpected"
[[ -f "$tk_src/unix/configure" && -f "$tk_src/license.terms" ]] || die "Tk source archive layout is unexpected"

certifi_root="/opt/_internal/vendor/certifi"
certifi_meta="/opt/_internal/vendor/certifi-2026.10.23.dist-info"
certifi_bundle="$certifi_root/cacert.pem"
certifi_license="$certifi_meta/licenses/LICENSE"
certifi_bundle_sha="2ef52ad56b4803e264045c915e55c236cbfa045b84b81efefc218a19eb44bd2b"
certifi_license_sha="e93716da6b9c0d5a4a1df60fe695b370f0695603d21f6f83f053e42cfc10caf7"
[[ -f "$certifi_bundle" && -f "$certifi_license" ]] || die "pinned-image certifi 2026.10.23 trust assets are missing"
[[ "$(sha256sum "$certifi_bundle" | awk '{print $1}')" == "$certifi_bundle_sha" ]] || die "pinned-image certifi CA bundle hash mismatch"
[[ "$(sha256sum "$certifi_license" | awk '{print $1}')" == "$certifi_license_sha" ]] || die "pinned-image certifi license hash mismatch"

# Make Python's default TLS trust/config paths runtime-relative without relying
# on sitecustomize (the bridge embeds Python with site import disabled). User
# overrides remain authoritative because setdefault is used.
"$helper_python" - "$python_src/Lib/ssl.py" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
anchor = "import sys\nimport os\nfrom collections import namedtuple\n"
replacement = (
    "import sys\nimport os\n"
    "os.environ.setdefault(\"SSL_CERT_FILE\", os.path.join(sys.prefix, \"ssl\", \"cert.pem\"))\n"
    "os.environ.setdefault(\"OPENSSL_CONF\", os.path.join(sys.prefix, \"ssl\", \"openssl.cnf\"))\n"
    "from collections import namedtuple\n"
)
if text.count(anchor) != 1:
    raise SystemExit("CPython 3.12.10 ssl.py import anchor is not unique")
path.write_text(text.replace(anchor, replacement, 1), encoding="utf-8")
PY

common_cflags="-O2 -g0 -fPIC"
common_ldflags="-Wl,-z,relro -Wl,-z,now"

# The final pinned manylinux image retains Perl but not the split IPC::Cmd and
# Time::Piece modules used by OpenSSL's build templates. OpenSSL 3.5.9 uses
# IPC::Cmd only for can_run() executable discovery, and Time::Piece only to
# format its release date in the generated Unix Makefile. Provide those exact
# narrow behaviors from repository-controlled build logic rather than adding a
# mutable package-manager/CPAN input to the closed build set.
mkdir -p "$(dirname "$openssl_perl_compat")"
cat > "$openssl_perl_compat" <<'PERL'
package IPC::Cmd;
use strict;
use warnings;

sub import { return; }

sub can_run {
    my ($cmd) = @_;
    return undef if !defined($cmd) || $cmd eq '';

    if ($cmd =~ m{/}) {
        return (-f $cmd && -x $cmd) ? $cmd : undef;
    }

    for my $dir (split(/:/, $ENV{PATH} // '')) {
        $dir = '.' if $dir eq '';
        my $candidate = "$dir/$cmd";
        return $candidate if -f $candidate && -x $candidate;
    }
    return undef;
}

1;
PERL

mkdir -p "$(dirname "$openssl_time_piece_compat")"
cat > "$openssl_time_piece_compat" <<'PERL'
package Time::Piece;
use strict;
use warnings;

my %MONTH = (
    Jan => 1, Feb => 2, Mar => 3, Apr => 4, May => 5, Jun => 6,
    Jul => 7, Aug => 8, Sep => 9, Oct => 10, Nov => 11, Dec => 12,
);

sub import {
    my $caller = caller;
    no strict 'refs';
    *{"${caller}::localtime"} = \&localtime;
    return;
}

sub localtime {
    my $epoch = @_ ? $_[0] : CORE::time();
    my @parts = CORE::localtime($epoch);
    return bless {
        year => $parts[5] + 1900,
        month => $parts[4] + 1,
        day => $parts[3],
    }, __PACKAGE__;
}

sub strptime {
    my ($class, $value, $format) = @_;
    die "unsupported Time::Piece strptime format: $format\n"
        if !defined($format) || $format ne '%d %b %Y';
    my ($day, $month_name, $year) =
        defined($value) ? ($value =~ /\A(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})\z/) : ();
    my $month = defined($month_name) ? $MONTH{$month_name} : undef;
    die "unsupported Time::Piece strptime value: " . (defined($value) ? $value : '<undef>') . "\n"
        if !defined($day) || !defined($month) || $day < 1 || $day > 31;
    return bless {year => 0 + $year, month => $month, day => 0 + $day}, $class;
}

sub strftime {
    my ($self, $format) = @_;
    die "unsupported Time::Piece strftime format: $format\n"
        if !defined($format) || $format ne '%Y-%m-%d';
    return sprintf('%04d-%02d-%02d', $self->{year}, $self->{month}, $self->{day});
}

1;
PERL

mkdir -p "$openssl_prefix"
(
    cd "$openssl_src"
    export PERL5LIB="$openssl_perl_lib${PERL5LIB:+:$PERL5LIB}"
    perl -MIPC::Cmd -MTime::Piece -e '
        IPC::Cmd::can_run("gcc") or die "IPC::Cmd compatibility module cannot resolve gcc\n";
        my $date = Time::Piece->strptime("29 Sep 2026", "%d %b %Y");
        $date->strftime("%Y-%m-%d") eq "2026-09-29"
            or die "Time::Piece compatibility module failed release-date formatting\n";
    '
    CFLAGS="$common_cflags" LDFLAGS="$common_ldflags" \
        ./Configure \
            "--prefix=$openssl_prefix" \
            "--openssldir=$openssl_prefix" \
            --libdir=lib \
            no-afalgeng
    make -j"$jobs"
    make install_sw
)
openssl_libdir="$openssl_prefix/lib"
openssl_include="$openssl_prefix/include"
[[ -f "$openssl_libdir/libssl.so.3" ]] || die "OpenSSL build did not produce libssl.so.3"
[[ -f "$openssl_libdir/libcrypto.so.3" ]] || die "OpenSSL build did not produce libcrypto.so.3"
[[ -f "$openssl_include/openssl/ssl.h" ]] || die "OpenSSL build did not install development headers"

mkdir -p "$tcltk_prefix"
(
    cd "$tcl_src/unix"
    CFLAGS="$common_cflags" LDFLAGS="$common_ldflags" \
        ./configure \
            --prefix="$tcltk_prefix" \
            --enable-shared \
            --enable-threads \
            --enable-64bit
    make -j"$jobs"
    make install
)

(
    cd "$tk_src/unix"
    CFLAGS="$common_cflags" LDFLAGS="$common_ldflags" \
        ./configure \
            --prefix="$tcltk_prefix" \
            --with-tcl="$tcl_src/unix" \
            --enable-shared \
            --enable-threads \
            --enable-64bit
    make -j"$jobs"
    make install
)

[[ -f "$tcltk_prefix/lib/libtcl8.6.so" ]] || die "Tcl shared library was not built"
[[ -f "$tcltk_prefix/lib/libtk8.6.so" ]] || die "Tk shared library was not built"
[[ -d "$tcltk_prefix/lib/tcl8.6" ]] || die "Tcl script library was not installed"
[[ -d "$tcltk_prefix/lib/tk8.6" ]] || die "Tk script library was not installed"

python_configure=(
    "--prefix=$runtime"
    --enable-shared
    --without-static-libpython
    --with-ensurepip=no
    "--with-openssl=$openssl_prefix"
    --with-openssl-rpath=no
)
cpython_pkg_config_path="$openssl_prefix/lib/pkgconfig:$openssl_prefix/lib64/pkgconfig:$tcltk_prefix/lib/pkgconfig:/usr/local/lib/pkgconfig"
cpython_pkg_config_libdir="/usr/lib64/pkgconfig:/usr/lib/pkgconfig:/usr/share/pkgconfig"
(
    cd "$python_src"
    export LD_LIBRARY_PATH="$openssl_libdir:$tcltk_prefix/lib"
    PKG_CONFIG_PATH="$cpython_pkg_config_path" \
    PKG_CONFIG_LIBDIR="$cpython_pkg_config_libdir" \
    TCLTK_CFLAGS="-I$tcltk_prefix/include" \
    TCLTK_LIBS="-L$tcltk_prefix/lib -ltk8.6 -ltcl8.6" \
    CPPFLAGS="-I$openssl_include" \
    CFLAGS="$common_cflags" \
    LDFLAGS="$common_ldflags -L$openssl_libdir" \
        ./configure "${python_configure[@]}"
    make -j"$jobs"
    make install
)

[[ -x "$runtime/bin/python3.12" ]] || die "CPython install did not produce bin/python3.12"
[[ -f "$runtime/lib/libpython3.12.so.1.0" ]] || die "CPython install did not produce shared libpython3.12.so.1.0"
[[ -d "$runtime/lib/python3.12/lib-dynload" ]] || die "CPython install did not produce lib-dynload"

rm -rf "$runtime/lib/python3.12/site-packages"
mkdir -p "$runtime/lib/python3.12/site-packages"
if find "$runtime" -iname 'numpy*' -print -quit | grep -q .; then
    die "NumPy payload unexpectedly appeared in controlled core runtime"
fi

mkdir -p "$runtime/lib/tcltk" "$runtime/lib/native" "$runtime/tcl" "$runtime/ssl" \
    "$runtime/LICENSES/TclTk" "$runtime/LICENSES/Libxcrypt"
cp -p "$tcltk_prefix/lib/libtcl8.6.so" "$runtime/lib/tcltk/libtcl8.6.so"
cp -p "$tcltk_prefix/lib/libtk8.6.so" "$runtime/lib/tcltk/libtk8.6.so"
cp -p "$openssl_libdir/libssl.so.3" "$runtime/lib/libssl.so.3"
cp -p "$openssl_libdir/libcrypto.so.3" "$runtime/lib/libcrypto.so.3"
cp -a "$tcltk_prefix/lib/tcl8.6" "$runtime/tcl/tcl8.6"
cp -a "$tcltk_prefix/lib/tk8.6" "$runtime/tcl/tk8.6"
cp -p "$certifi_bundle" "$runtime/ssl/cert.pem"
cp -p "$openssl_src/apps/openssl.cnf" "$runtime/ssl/openssl.cnf"
cp -p "$python_src/LICENSE" "$runtime/LICENSES/CPython.txt"
cp -p "$openssl_src/LICENSE.txt" "$runtime/LICENSES/OpenSSL.txt"
cp -p "$certifi_license" "$runtime/LICENSES/Certifi.txt"
cp -p "$libxcrypt_src/AUTHORS" "$runtime/LICENSES/Libxcrypt/AUTHORS.txt"
cp -p "$libxcrypt_src/COPYING.LIB" "$runtime/LICENSES/Libxcrypt/COPYING.LIB.txt"
cp -p "$libxcrypt_src/LICENSING" "$runtime/LICENSES/Libxcrypt/LICENSING.txt"
cp -p "$tcl_src/license.terms" "$runtime/LICENSES/TclTk/Tcl.txt"
cp -p "$tk_src/license.terms" "$runtime/LICENSES/TclTk/Tk.txt"

# Close every direct non-PEP599 dependency of the staged ELF set by copying the
# exact provider bytes from the pinned image. Iterate because copied providers
# can themselves introduce non-baseline dependencies. License files are copied
# from the image when present, otherwise from an exact fallback RPM in the
# closed input set, and recorded with each provider package.
"$helper_python" - "$runtime" "$build/native-provider-provenance.json" "$image_ref" \
    "$libxcrypt_src" "$libxcrypt_name" "$libxcrypt_url" "$libxcrypt_sha" \
    "$license_rpm_provenance_json" <<'PY'
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

runtime = pathlib.Path(sys.argv[1]).resolve()
output = pathlib.Path(sys.argv[2])
image_ref = sys.argv[3]
libxcrypt_src = pathlib.Path(sys.argv[4]).resolve()
libxcrypt_name = sys.argv[5]
libxcrypt_url = sys.argv[6]
libxcrypt_sha = sys.argv[7]
license_rpm_manifest = json.loads(pathlib.Path(sys.argv[8]).read_text(encoding="utf-8"))
license_rpm_index = {}
for record in license_rpm_manifest.get("packages", []):
    key = (record.get("package"), record.get("version_release"), record.get("arch"))
    if not all(isinstance(item, str) and item for item in key) or key in license_rpm_index:
        raise SystemExit(f"invalid or duplicate license/provenance RPM record: {record!r}")
    license_rpm_index[key] = record
native_root = runtime / "lib/native"
license_root = runtime / "LICENSES/ImageRPMs"
native_root.mkdir(parents=True, exist_ok=True)
license_root.mkdir(parents=True, exist_ok=True)

allowlist = {
    "libgcc_s.so.1", "libstdc++.so.6", "libm.so.6", "libdl.so.2",
    "librt.so.1", "libc.so.6", "libnsl.so.1", "libutil.so.1",
    "libpthread.so.0", "libresolv.so.2", "libX11.so.6", "libXext.so.6",
    "libXrender.so.1", "libICE.so.6", "libSM.so.6", "libGL.so.1",
    "libgobject-2.0.so.0", "libgthread-2.0.so.0", "libglib-2.0.so.0",
}

def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def is_elf(path):
    try:
        return (
            path.is_file()
            and not path.is_symlink()
            and path.open("rb").read(4) == b"\x7fELF"
        )
    except OSError:
        return False

def dynamic(path):
    return subprocess.check_output(["readelf", "-dW", str(path)], text=True)

def needed(path):
    return re.findall(r"Shared library: \[([^\]]+)\]", dynamic(path))

def soname(path):
    match = re.search(r"Library soname: \[([^\]]+)\]", dynamic(path))
    return match.group(1) if match else None

def staged_provider_index():
    providers = {}
    for path in sorted(runtime.rglob("*")):
        if not is_elf(path):
            continue
        providers.setdefault(path.name, path)
        name = soname(path)
        if name:
            providers.setdefault(name, path)
    return providers

def resolve_with_ldd(path):
    env = os.environ.copy()
    search = [runtime / "lib", runtime / "lib/native", runtime / "lib/tcltk"]
    env["LD_LIBRARY_PATH"] = ":".join(map(str, search))
    proc = subprocess.run(["ldd", str(path)], text=True, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"ldd failed for {path}:\n{proc.stdout}")
    resolved = {}
    for line in proc.stdout.splitlines():
        match = re.match(r"\s*(\S+)\s+=>\s+(\S+)\s+\(", line)
        if match and match.group(2) != "not":
            resolved[match.group(1)] = pathlib.Path(match.group(2)).resolve()
    return resolved

def rpm_owner(path):
    proc = subprocess.run(
        ["rpm", "-qf", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\t%{LICENSE}\n", str(path)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode != 0:
        return None
    fields = proc.stdout.rstrip("\n").split("\t")
    if len(fields) != 4:
        raise SystemExit(f"unexpected RPM provider metadata for {path}: {proc.stdout!r}")
    return {"name": fields[0], "version_release": fields[1], "arch": fields[2], "license": fields[3]}

packages = {}
providers = []
internal_root = pathlib.Path("/opt/_internal").resolve()
sqlite_root = pathlib.Path("/opt/_internal/sqlite3").resolve()
libxcrypt_root = pathlib.Path("/usr/local/lib").resolve()

def package_licenses(package):
    key = (package["name"], package["version_release"], package["arch"])
    if key in packages:
        return packages[key]
    name = package["name"]
    package_query = f"{name}-{package['version_release']}.{package['arch']}"
    proc = subprocess.run(["rpm", "-qL", package_query], text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, check=False)
    candidates = [pathlib.Path(line) for line in proc.stdout.splitlines() if line.startswith("/")]
    if not candidates:
        listing = subprocess.check_output(["rpm", "-ql", package_query], text=True)
        candidates = [
            pathlib.Path(line) for line in listing.splitlines()
            if line.startswith("/") and re.search(r"/(?:LICENSE|LICENCE|COPYING|COPYRIGHT|copyright|license)", line, re.I)
        ]
    candidates = [path for path in candidates if path.is_file()]
    evidence_archive = None
    if not candidates:
        archive_record = license_rpm_index.get(key)
        if archive_record is not None:
            archive_path = pathlib.Path(str(archive_record.get("verified_archive_path", ""))).resolve()
            source_path = pathlib.Path(str(archive_record.get("source_path", ""))).resolve()
            expected_archive_identity = [
                archive_record["archive_package"], archive_record["version_release"],
                archive_record["arch"], archive_record["license"],
            ]
            observed_archive_identity = subprocess.check_output(
                ["rpm", "-qp", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\t%{LICENSE}", str(archive_path)],
                text=True,
            ).strip().split("\t")
            if observed_archive_identity != expected_archive_identity:
                raise SystemExit(
                    f"unexpected legal-evidence RPM identity for {package_query}: "
                    f"observed={observed_archive_identity!r}, expected={expected_archive_identity!r}"
                )
            if sha(archive_path) != archive_record.get("sha256"):
                raise SystemExit(f"legal-evidence RPM hash changed after input verification: {package_query}")
            if (
                not source_path.is_file()
                or sha(source_path) != archive_record.get("member_sha256")
                or archive_record.get("source_sha256") != archive_record.get("member_sha256")
            ):
                raise SystemExit(f"legal-evidence member is missing or has an unexpected hash: {package_query}")
            candidates = [source_path]
            evidence_archive = dict(archive_record)
    candidates = sorted({p.resolve() for p in candidates if p.is_file()})
    if not candidates:
        raise SystemExit(f"no redistributed license file found in pinned-image RPM: {name}")
    package_dir = license_root / re.sub(r"[^A-Za-z0-9_.+-]+", "_", f"{name}-{package['version_release']}")
    package_dir.mkdir(parents=True, exist_ok=True)
    license_entries = []
    for index, source in enumerate(candidates, start=1):
        destination = package_dir / f"{index:02d}-{source.name}"
        shutil.copy2(source, destination)
        license_entries.append({
            "source_path": str(source),
            "source_sha256": sha(source),
            "staged_path": destination.relative_to(runtime).as_posix(),
            "staged_sha256": sha(destination),
        })
    record = dict(package)
    record["provider"] = "pinned-manylinux-image-rpm"
    record["image"] = image_ref
    record["licenses"] = license_entries
    if evidence_archive is not None:
        record["license_evidence_archive"] = evidence_archive
    packages[key] = record
    return record

def source_license_evidence(component_root, paths):
    entries = []
    for source_rel, staged_rel in paths:
        source = component_root / source_rel
        staged = runtime / staged_rel
        if not source.is_file() or not staged.is_file():
            raise SystemExit(f"missing source/staged license evidence: {source} -> {staged}")
        if sha(source) != sha(staged):
            raise SystemExit(f"source/staged license evidence differs: {source} -> {staged}")
        entries.append({
            "source_archive_path": source_rel,
            "source_sha256": sha(source),
            "staged_path": staged_rel,
            "staged_sha256": sha(staged),
        })
    return entries

libxcrypt_licenses = source_license_evidence(libxcrypt_src, [
    ("AUTHORS", "LICENSES/Libxcrypt/AUTHORS.txt"),
    ("COPYING.LIB", "LICENSES/Libxcrypt/COPYING.LIB.txt"),
    ("LICENSING", "LICENSES/Libxcrypt/LICENSING.txt"),
])

sqlite_pc = sqlite_root / "lib/pkgconfig/sqlite3.pc"
if not sqlite_pc.is_file():
    raise SystemExit(f"pinned-image SQLite metadata is missing: {sqlite_pc}")
sqlite_text = sqlite_pc.read_text(encoding="utf-8", errors="strict")
sqlite_match = re.search(r"^Version:\s*(\S+)\s*$", sqlite_text, re.M)
if sqlite_match is None or sqlite_match.group(1) != "3.54.1":
    raise SystemExit(f"unexpected pinned-image SQLite version metadata: {sqlite_match.group(1) if sqlite_match else None!r}")
libxcrypt_pc = pathlib.Path("/usr/local/lib/pkgconfig/libxcrypt.pc")
if not libxcrypt_pc.is_file():
    raise SystemExit(f"pinned-image libxcrypt metadata is missing: {libxcrypt_pc}")
libxcrypt_text = libxcrypt_pc.read_text(encoding="utf-8", errors="strict")
libxcrypt_match = re.search(r"^Version:\s*(\S+)\s*$", libxcrypt_text, re.M)
if libxcrypt_match is None or libxcrypt_match.group(1) != "4.5.2":
    raise SystemExit(
        f"unexpected pinned-image libxcrypt version metadata: "
        f"{libxcrypt_match.group(1) if libxcrypt_match else None!r}"
    )

def non_rpm_provider_identity(needed_name, source):
    try:
        source.relative_to(sqlite_root)
    except ValueError:
        pass
    else:
        if needed_name != "libsqlite3.so":
            raise SystemExit(f"unexpected SQLite DT_NEEDED name for pinned-image provider: {needed_name}")
        expected_sqlite_source = pathlib.Path("/opt/_internal/sqlite3/lib/libsqlite3.so.3.54.1")
        if source != expected_sqlite_source:
            raise SystemExit(
                f"unexpected SQLite provider path for {needed_name}: {source}; "
                f"expected {expected_sqlite_source}"
            )
        return {
            "kind": "pinned-manylinux-image-sqlite",
            "image": image_ref,
            "component": "sqlite",
            "version": "3.54.1",
            "needed_name": needed_name,
            "source_path": str(source),
            "source_sha256": sha(source),
            "metadata_path": str(sqlite_pc),
            "metadata_sha256": sha(sqlite_pc),
            "legal_status": "public-domain",
        }

    try:
        source.relative_to(libxcrypt_root)
    except ValueError:
        pass
    else:
        expected_libxcrypt_sources = {
            "libcrypt.so.2": pathlib.Path("/usr/local/lib/libcrypt.so.2"),
        }
        if needed_name not in expected_libxcrypt_sources:
            raise SystemExit(f"unexpected libxcrypt DT_NEEDED name: {needed_name}")
        if source != expected_libxcrypt_sources[needed_name]:
            raise SystemExit(
                f"pinned-image libxcrypt source path is unexpected: "
                f"{needed_name} -> {source}; expected {expected_libxcrypt_sources[needed_name]}"
            )
        return {
            "kind": "pinned-manylinux-image-libxcrypt",
            "image": image_ref,
            "component": "libxcrypt",
            "version": "4.5.2",
            "needed_name": needed_name,
            "license": (
                "LGPL-2.1-or-later AND BSD-3-Clause AND BSD-2-Clause AND "
                "BSD-2-Clause-FreeBSD AND 0BSD AND CC0-1.0 AND "
                "LicenseRef-Fedora-Public-Domain"
            ),
            "source_path": str(source),
            "source_sha256": sha(source),
            "metadata_path": str(libxcrypt_pc),
            "metadata_sha256": sha(libxcrypt_pc),
            "license_source": {
                "filename": libxcrypt_name,
                "url": libxcrypt_url,
                "sha256": libxcrypt_sha,
                "usage": "license-provenance-only",
            },
            "licenses": libxcrypt_licenses,
        }

    raise SystemExit(
        f"non-PEP599 provider is neither RPM-owned nor an explicitly modeled pinned-image component: {source}"
    )

def interpreter_path(path):
    text = subprocess.check_output(["readelf", "-lW", str(path)], text=True)
    match = re.search(r"Requesting program interpreter: ([^\]]+)\]", text)
    return match.group(1) if match else None

python_interpreter = interpreter_path(runtime / "bin/python3.12")
if python_interpreter != "/lib64/ld-linux-x86-64.so.2":
    raise SystemExit(f"unexpected staged Python PT_INTERP: {python_interpreter!r}")
loader_source = pathlib.Path(python_interpreter)
loader_resolved = loader_source.resolve()
if not loader_source.exists() or not loader_resolved.is_file():
    raise SystemExit(f"pinned-image glibc loader is missing: {loader_source} -> {loader_resolved}")
loader_package = rpm_owner(loader_resolved)
if loader_package is None or loader_package.get("name") != "glibc":
    raise SystemExit(f"pinned-image glibc loader lacks expected glibc RPM identity: {loader_resolved}: {loader_package!r}")
glibc_loader = {
    "kind": "glibc-loader-external",
    "image": image_ref,
    "needed_name": "ld-linux-x86-64.so.2",
    "interpreter_path": str(loader_source),
    "resolved_path": str(loader_resolved),
    "resolved_sha256": sha(loader_resolved),
    "package": loader_package,
    "consumers": [],
}

while True:
    staged = staged_provider_index()
    additions = []
    for elf in sorted(p for p in runtime.rglob("*") if is_elf(p)):
        elf_relative = elf.relative_to(runtime).as_posix()
        resolved = None
        for library in needed(elf):
            if library == glibc_loader["needed_name"]:
                if elf_relative not in glibc_loader["consumers"]:
                    glibc_loader["consumers"].append(elf_relative)
                continue
            if library in allowlist or library in staged:
                continue
            if resolved is None:
                resolved = resolve_with_ldd(elf)
            source = resolved.get(library)
            if source is None or not source.is_file():
                raise SystemExit(f"unresolved non-PEP599 dependency {library} needed by {elf}")
            if runtime == source or runtime in source.parents:
                raise SystemExit(f"staged provider index missed runtime dependency {library}: {source}")
            destination = native_root / library
            if destination.exists():
                if sha(destination) != sha(source):
                    raise SystemExit(f"conflicting providers for {library}: {source} vs {destination}")
                continue
            package = rpm_owner(source)
            if package is not None:
                package_record = package_licenses(package)
                provider_identity = {
                    "kind": "pinned-manylinux-image-rpm",
                    "image": image_ref,
                    "name": package_record["name"],
                    "version_release": package_record["version_release"],
                    "arch": package_record["arch"],
                    "license": package_record["license"],
                    "licenses": package_record["licenses"],
                }
            else:
                provider_identity = non_rpm_provider_identity(library, source)
            shutil.copy2(source, destination)
            if sha(destination) != sha(source):
                raise SystemExit(f"provider copy hash mismatch: {library}")
            staged.setdefault(destination.name, destination)
            destination_soname = soname(destination)
            if destination_soname:
                staged.setdefault(destination_soname, destination)
            additions.append(destination)
            providers.append({
                "needed_name": library,
                "source_path": str(source),
                "source_sha256": sha(source),
                "staged_path": destination.relative_to(runtime).as_posix(),
                "staged_sha256": sha(destination),
                "provider": provider_identity,
            })
    if not additions:
        break

glibc_loader["consumers"].sort()
data = {
    "pep599_external_allowlist": sorted(allowlist),
    "glibc_loader": glibc_loader,
    "image_providers": sorted(providers, key=lambda item: item["staged_path"]),
    "image_packages": sorted(packages.values(), key=lambda item: (item["name"], item["version_release"])),
}
output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

# Give every staged ELF a package-relative RUNPATH that can resolve libpython,
# the private dependency closure, and Tcl/Tk providers without LD_LIBRARY_PATH.
"$helper_python" - "$runtime" "$build/native-provider-provenance.json" <<'PY'
import hashlib
import json
import os
import pathlib
import subprocess
import sys

runtime = pathlib.Path(sys.argv[1]).resolve()
native_provenance_path = pathlib.Path(sys.argv[2])
targets = [runtime / "lib", runtime / "lib/native", runtime / "lib/tcltk"]

def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def is_elf(path):
    try:
        return (
            path.is_file()
            and not path.is_symlink()
            and path.open("rb").read(4) == b"\x7fELF"
        )
    except OSError:
        return False

count = 0
for path in sorted(runtime.rglob("*")):
    if not is_elf(path):
        continue
    entries = []
    for target in targets:
        relative = os.path.relpath(target, path.parent.resolve())
        entry = "$ORIGIN" if relative == "." else f"$ORIGIN/{relative}"
        if entry not in entries:
            entries.append(entry)
    subprocess.check_call(["patchelf", "--set-rpath", ":".join(entries), str(path)])
    count += 1
if count == 0:
    raise SystemExit("no staged ELF files found for RUNPATH relocation")

# Source hashes describe the immutable image bytes copied into the runtime.
# patchelf intentionally mutates the staged copies, so refresh staged hashes
# only after all relocation edits and retain source hashes unchanged.
native = json.loads(native_provenance_path.read_text(encoding="utf-8"))
for provider in native.get("image_providers", []):
    staged_path = runtime / provider["staged_path"]
    if not staged_path.is_file():
        raise SystemExit(f"staged image provider disappeared after relocation: {staged_path}")
    provider["staged_sha256"] = sha(staged_path)
native_provenance_path.write_text(
    json.dumps(native, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(f"relocated_elf_runpaths={count}")
PY

# Prove the retained core interpreter surface from the relocated bytes with all
# ambient Python/native search variables removed. A missing retained module is
# a build failure; the ABI rebaseline must not solve compatibility by pruning it.
env -u PYTHONHOME -u PYTHONPATH -u PYTHONUSERBASE -u LD_LIBRARY_PATH -u LD_PRELOAD \
    -u SSL_CERT_FILE -u SSL_CERT_DIR -u OPENSSL_CONF -u OPENSSL_MODULES \
    TCL_LIBRARY="$runtime/tcl/tcl8.6" \
    TK_LIBRARY="$runtime/tcl/tk8.6" \
    "$runtime/bin/python3.12" -I -S - "$runtime" "$helper_dynamic_extension_baseline_json" <<'PY'
import importlib
import importlib.util
import json
import os
import pathlib
import platform
import sys
import tkinter
import _tkinter
import ssl

runtime = pathlib.Path(sys.argv[1]).resolve()
dynamic_extension_baseline = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
if platform.python_implementation() != "CPython" or platform.python_version() != "3.12.10":
    raise SystemExit(f"unexpected staged Python: {platform.python_implementation()} {platform.python_version()}")
if pathlib.Path(sys.prefix).resolve() != runtime:
    raise SystemExit(f"staged Python prefix is not relocatable runtime root: {sys.prefix}")

baseline_modules = dynamic_extension_baseline.get("modules")
if not isinstance(baseline_modules, list) or not baseline_modules or baseline_modules != sorted(set(baseline_modules)):
    raise SystemExit("invalid pinned-image helper dynamic-extension baseline inventory")
for name in baseline_modules:
    spec = importlib.util.find_spec(name)
    if spec is None:
        raise SystemExit(f"staged Python lost helper native-extension capability: {name}")
    if spec.origin in {"built-in", "frozen"}:
        continue
    if not spec.origin:
        raise SystemExit(f"staged Python resolved baseline module without an origin: {name}")
    origin = pathlib.Path(spec.origin).resolve()
    if origin != runtime and runtime not in origin.parents:
        raise SystemExit(f"staged Python resolved baseline module outside runtime: {name}: {origin}")

required = [
    "ssl", "_ssl", "sqlite3", "_sqlite3", "ctypes", "_ctypes", "bz2", "_bz2",
    "lzma", "_lzma", "zlib", "readline", "curses", "_curses", "curses.panel",
    "_curses_panel", "multiprocessing", "encodings", "zoneinfo", "hashlib", "decimal", "dbm",
    "_dbm", "_gdbm", "uuid", "_uuid", "pyexpat", "xml.parsers.expat", "tkinter", "_tkinter",
]
for name in required:
    importlib.import_module(name)

expected_cert = (runtime / "ssl/cert.pem").resolve()
expected_conf = (runtime / "ssl/openssl.cnf").resolve()
if pathlib.Path(os.environ.get("SSL_CERT_FILE", "")).resolve() != expected_cert:
    raise SystemExit(f"ssl.py did not install runtime-relative SSL_CERT_FILE: {os.environ.get('SSL_CERT_FILE')!r}")
if pathlib.Path(os.environ.get("OPENSSL_CONF", "")).resolve() != expected_conf:
    raise SystemExit(f"ssl.py did not install runtime-relative OPENSSL_CONF: {os.environ.get('OPENSSL_CONF')!r}")
verify_paths = ssl.get_default_verify_paths()
if not verify_paths.cafile or pathlib.Path(verify_paths.cafile).resolve() != expected_cert:
    raise SystemExit(f"unexpected default TLS CA file: {verify_paths}")
if ssl.create_default_context().cert_store_stats().get("x509_ca", 0) <= 0:
    raise SystemExit("default TLS context did not load staged CA certificates")

site = runtime / "lib/python3.12/site-packages"
if not site.is_dir() or any(site.iterdir()):
    raise SystemExit("controlled site-packages is not empty")
try:
    importlib.import_module("numpy")
except ModuleNotFoundError:
    pass
else:
    raise SystemExit("NumPy unexpectedly imports in the core runtime")

if str(_tkinter.TCL_VERSION) != "8.6" or str(_tkinter.TK_VERSION) != "8.6":
    raise SystemExit(f"unexpected Tcl/Tk API: {_tkinter.TCL_VERSION}/{_tkinter.TK_VERSION}")
interp = tkinter.Tcl()
if interp.eval("info patchlevel") != "8.6.12":
    raise SystemExit(f"unexpected Tcl patchlevel: {interp.eval('info patchlevel')}")
print("controlled_runtime_python_probe=pass")
PY

# Generate the final, self-contained machine-readable build/runtime record. The
# ELF census covers every staged ELF byte after all relocation edits and records
# direct dependencies, loader paths, version needs, architecture and SHA-256.
"$helper_python" - \
    "$runtime" \
    "$build/native-provider-provenance.json" \
    "$openssl_provider_json" \
    "$helper_dynamic_extension_baseline_json" \
    "$dev_rpm_provenance_json" \
    "$license_rpm_provenance_json" \
    "$runtime/build-provenance-linux.json" \
    "$image_repository" "$image_digest" "$image_ref" \
    "$python_source" "$python_name" "$python_url" "$python_sha" \
    "$openssl_source" "$openssl_name" "$openssl_url" "$openssl_sha" \
    "$libxcrypt_source" "$libxcrypt_name" "$libxcrypt_url" "$libxcrypt_sha" \
    "$tcl_source" "$tcl_name" "$tcl_url" "$tcl_sha" \
    "$tk_source" "$tk_name" "$tk_url" "$tk_sha" \
    "$openssl_perl_compat" "$openssl_time_piece_compat" \
    "$cpython_pkg_config_path" "$cpython_pkg_config_libdir" "$certifi_bundle" "$certifi_license" \
    "$python_src/Lib/ssl.py" "$openssl_src/apps/openssl.cnf" \
    "$common_cflags" "$common_ldflags" "$jobs" <<'PY'
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

(
    runtime_arg, native_json_arg, openssl_json_arg, dynamic_extension_baseline_json_arg,
    dev_rpm_json_arg, license_rpm_json_arg, output_arg,
    image_repository, image_digest, image_ref,
    python_archive_arg, python_name, python_url, python_sha,
    openssl_archive_arg, openssl_name, openssl_url, openssl_sha,
    libxcrypt_archive_arg, libxcrypt_name, libxcrypt_url, libxcrypt_sha,
    tcl_archive_arg, tcl_name, tcl_url, tcl_sha,
    tk_archive_arg, tk_name, tk_url, tk_sha,
    openssl_perl_compat_arg, openssl_time_piece_compat_arg,
    cpython_pkg_config_path_arg, cpython_pkg_config_libdir_arg, certifi_bundle_arg, certifi_license_arg,
    patched_ssl_py_arg, openssl_conf_source_arg,
    common_cflags, common_ldflags, jobs,
) = sys.argv[1:]

runtime = pathlib.Path(runtime_arg).resolve()
native = json.loads(pathlib.Path(native_json_arg).read_text(encoding="utf-8"))
openssl_provider = json.loads(pathlib.Path(openssl_json_arg).read_text(encoding="utf-8"))
dynamic_extension_baseline = json.loads(
    pathlib.Path(dynamic_extension_baseline_json_arg).read_text(encoding="utf-8")
)
development_rpms = json.loads(pathlib.Path(dev_rpm_json_arg).read_text(encoding="utf-8"))
license_rpms = json.loads(pathlib.Path(license_rpm_json_arg).read_text(encoding="utf-8"))
output = pathlib.Path(output_arg)

expected_pkg_config_path = ":".join([
    str(runtime.parent / "build/linux-glibc217-runtime/openssl-prefix/lib/pkgconfig"),
    str(runtime.parent / "build/linux-glibc217-runtime/openssl-prefix/lib64/pkgconfig"),
    str(runtime.parent / "build/linux-glibc217-runtime/tcltk-prefix/lib/pkgconfig"),
    "/usr/local/lib/pkgconfig",
])
if cpython_pkg_config_path_arg != expected_pkg_config_path:
    raise SystemExit(
        "effective CPython PKG_CONFIG_PATH differs from the controlled search path: "
        f"{cpython_pkg_config_path_arg!r} != {expected_pkg_config_path!r}"
    )
expected_pkg_config_libdir = ":".join([
    "/usr/lib64/pkgconfig",
    "/usr/lib/pkgconfig",
    "/usr/share/pkgconfig",
])
if cpython_pkg_config_libdir_arg != expected_pkg_config_libdir:
    raise SystemExit(
        "effective CPython PKG_CONFIG_LIBDIR differs from the controlled search path: "
        f"{cpython_pkg_config_libdir_arg!r} != {expected_pkg_config_libdir!r}"
    )

baseline_modules = dynamic_extension_baseline.get("modules")
if not isinstance(baseline_modules, list) or not baseline_modules or baseline_modules != sorted(set(baseline_modules)):
    raise SystemExit("invalid pinned-image helper dynamic-extension baseline inventory")

def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def is_elf(path):
    try:
        return (
            path.is_file()
            and not path.is_symlink()
            and path.open("rb").read(4) == b"\x7fELF"
        )
    except OSError:
        return False

def readelf(*args):
    return subprocess.check_output(["readelf", *args], text=True)

def dynamic(path):
    return readelf("-dW", str(path))

def elf_soname(path, text=None):
    text = dynamic(path) if text is None else text
    match = re.search(r"Library soname: \[([^\]]+)\]", text)
    return match.group(1) if match else None

def version_needs(path):
    text = readelf("--version-info", "-W", str(path))
    result = {}
    current = None
    in_needs = False
    for line in text.splitlines():
        if line.startswith("Version needs section"):
            in_needs = True
            current = None
            continue
        if in_needs and line.startswith("Version definition section"):
            break
        if not in_needs:
            continue
        file_match = re.search(r"\bFile: (\S+)", line)
        if file_match:
            current = file_match.group(1)
            result.setdefault(current, [])
            continue
        name_match = re.search(r"\bName: (\S+)", line)
        if current and name_match:
            name = name_match.group(1)
            if name not in result[current]:
                result[current].append(name)
    return {key: sorted(value) for key, value in sorted(result.items())}

elf_paths = sorted(path for path in runtime.rglob("*") if is_elf(path))
if not elf_paths:
    raise SystemExit("final runtime contains no ELF files")

provider_map = {}
for path in elf_paths:
    text = dynamic(path)
    provider_map.setdefault(path.name, path)
    soname = elf_soname(path, text)
    if soname:
        provider_map.setdefault(soname, path)

allowlist = set(native["pep599_external_allowlist"])
glibc_loader = native.get("glibc_loader", {})
if glibc_loader.get("kind") != "glibc-loader-external":
    raise SystemExit(f"missing glibc loader provenance: {glibc_loader!r}")
if glibc_loader.get("needed_name") != "ld-linux-x86-64.so.2":
    raise SystemExit(f"unexpected glibc loader soname: {glibc_loader!r}")
glibc_loader_consumers = set(glibc_loader.get("consumers", []))
if not glibc_loader_consumers:
    raise SystemExit("glibc loader consumer set is empty")
observed_glibc_loader_consumers = {
    path.relative_to(runtime).as_posix()
    for path in elf_paths
    if glibc_loader["needed_name"] in re.findall(
        r"Shared library: \[([^\]]+)\]", dynamic(path)
    )
}
if glibc_loader_consumers != observed_glibc_loader_consumers:
    raise SystemExit(
        "recorded glibc loader consumers differ from final staged ELF closure: "
        f"recorded={sorted(glibc_loader_consumers)}, "
        f"observed={sorted(observed_glibc_loader_consumers)}"
    )
census = []
all_versions = set()
for path in elf_paths:
    rel = path.relative_to(runtime).as_posix()
    header = readelf("-hW", str(path))
    dynamic_text = dynamic(path)
    machine_match = re.search(r"^\s*Machine:\s*(.+)$", header, re.M)
    class_match = re.search(r"^\s*Class:\s*(.+)$", header, re.M)
    interpreter = None
    program_headers = readelf("-lW", str(path))
    interp_match = re.search(r"Requesting program interpreter: ([^\]]+)\]", program_headers)
    if interp_match:
        interpreter = interp_match.group(1)
    needed_names = re.findall(r"Shared library: \[([^\]]+)\]", dynamic_text)
    runpaths = re.findall(r"\((?:RUNPATH|RPATH)\).*?\[([^\]]+)\]", dynamic_text)
    if not runpaths or any(not entry.startswith("$ORIGIN") for value in runpaths for entry in value.split(":")):
        raise SystemExit(f"non-runtime-relative or missing staged RUNPATH: {rel}: {runpaths}")
    dependencies = []
    for library in needed_names:
        if library == glibc_loader["needed_name"]:
            if rel not in glibc_loader_consumers:
                raise SystemExit(f"glibc loader dependency appeared on unapproved consumer: {rel}")
            dependencies.append({
                "name": library,
                "resolution": "glibc-loader-external",
                "provider": glibc_loader["resolved_path"],
                "provider_sha256": glibc_loader["resolved_sha256"],
                "package": glibc_loader["package"],
            })
        elif library in allowlist:
            dependencies.append({"name": library, "resolution": "pep599-external"})
        elif library in provider_map:
            provider = provider_map[library]
            dependencies.append({
                "name": library,
                "resolution": "bundled",
                "provider": provider.relative_to(runtime).as_posix(),
                "provider_sha256": sha(provider),
            })
        else:
            raise SystemExit(f"unbundled non-PEP599 dependency in final runtime: {rel}: {library}")
    needs = version_needs(path)
    for versions in needs.values():
        all_versions.update(versions)
    census.append({
        "path": rel,
        "size": path.stat().st_size,
        "sha256": sha(path),
        "elf_class": class_match.group(1).strip() if class_match else None,
        "machine": machine_match.group(1).strip() if machine_match else None,
        "soname": elf_soname(path, dynamic_text),
        "interpreter": interpreter,
        "needed": needed_names,
        "dependencies": dependencies,
        "runpath": runpaths,
        "version_needs": needs,
    })

def max_version(prefix):
    candidates = []
    for item in all_versions:
        match = re.fullmatch(re.escape(prefix) + r"_([0-9]+(?:\.[0-9]+)*)", item)
        if match:
            value = match.group(1)
            candidates.append((tuple(int(part) for part in value.split(".")), value))
    return max(candidates)[1] if candidates else None

ceilings = {
    "GLIBC": (2, 17),
    "CXXABI": (1, 3, 7),
    "GLIBCXX": (3, 4, 19),
    "GCC": (4, 8, 0),
}
for item in sorted(all_versions):
    if item == "CXXABI_TM_1":
        continue
    checked = False
    for prefix, ceiling in ceilings.items():
        marker = prefix + "_"
        if not item.startswith(marker):
            continue
        checked = True
        suffix = item[len(marker):]
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", suffix):
            raise SystemExit(f"unsupported {prefix} version requirement in staged ELF closure: {item}")
        value = tuple(int(part) for part in suffix.split("."))
        padded = value + (0,) * (len(ceiling) - len(value))
        if padded > ceiling:
            raise SystemExit(f"staged ELF closure exceeds {prefix}_{'.'.join(map(str, ceiling))}: {item}")
        break
    if checked:
        continue

for entry in census:
    if entry["elf_class"] != "ELF64" or entry["machine"] != "Advanced Micro Devices X86-64":
        raise SystemExit(
            f"unexpected staged ELF architecture: {entry['path']}: "
            f"{entry['elf_class']} / {entry['machine']}"
        )

tkinter_candidates = [
    path for path in elf_paths
    if path.parent == runtime / "lib/python3.12/lib-dynload" and path.name.startswith("_tkinter")
]
if len(tkinter_candidates) != 1:
    raise SystemExit(f"expected exactly one staged _tkinter ELF, found: {tkinter_candidates}")
tkinter_needed = set(re.findall(r"Shared library: \[([^\]]+)\]", dynamic(tkinter_candidates[0])))
if not {"libtcl8.6.so", "libtk8.6.so"}.issubset(tkinter_needed):
    raise SystemExit(f"_tkinter is not dynamically linked to staged Tcl/Tk providers: {sorted(tkinter_needed)}")
tk_needed = set(re.findall(r"Shared library: \[([^\]]+)\]", dynamic(runtime / "lib/tcltk/libtk8.6.so")))
if "libX11.so.6" not in tk_needed:
    raise SystemExit(f"Tk is not dynamically linked to the host X11 baseline: {sorted(tk_needed)}")
for path in elf_paths:
    if path.name.startswith("libX11.so") or path.name.startswith("libxcb.so"):
        raise SystemExit(f"runtime unexpectedly bundles competing X11/XCB provider: {path.relative_to(runtime)}")

def command_identity(command, args):
    path = pathlib.Path(subprocess.check_output(["bash", "-lc", f"command -v {command}"], text=True).strip()).resolve()
    text = subprocess.run([str(path), *args], text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=False).stdout.strip()
    return {"path": str(path), "sha256": sha(path), "version_output": text}

def tree_hash(path):
    path = pathlib.Path(path)
    h = hashlib.sha256()
    files = []
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = item.relative_to(path).as_posix()
        digest = sha(item)
        h.update(rel.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\n")
        files.append({"path": rel, "sha256": digest, "size": item.stat().st_size})
    return {"sha256": h.hexdigest(), "file_count": len(files), "files": files}

source_licenses = {
    "cpython": {
        "path": "LICENSES/CPython.txt",
        "sha256": sha(runtime / "LICENSES/CPython.txt"),
        "source": "Python-3.12.10/LICENSE",
    },
    "openssl": {
        "path": "LICENSES/OpenSSL.txt",
        "sha256": sha(runtime / "LICENSES/OpenSSL.txt"),
        "source": "openssl-3.5.9/LICENSE.txt",
    },
    "libxcrypt": {
        "path": "LICENSES/Libxcrypt/LICENSING.txt",
        "sha256": sha(runtime / "LICENSES/Libxcrypt/LICENSING.txt"),
        "source": "libxcrypt-4.5.2/LICENSING",
        "usage": "license-provenance-only",
        "additional_paths": [
            {
                "path": "LICENSES/Libxcrypt/COPYING.LIB.txt",
                "sha256": sha(runtime / "LICENSES/Libxcrypt/COPYING.LIB.txt"),
                "source": "libxcrypt-4.5.2/COPYING.LIB",
            },
            {
                "path": "LICENSES/Libxcrypt/AUTHORS.txt",
                "sha256": sha(runtime / "LICENSES/Libxcrypt/AUTHORS.txt"),
                "source": "libxcrypt-4.5.2/AUTHORS",
            },
        ],
    },
    "tcl": {
        "path": "LICENSES/TclTk/Tcl.txt",
        "sha256": sha(runtime / "LICENSES/TclTk/Tcl.txt"),
        "source": "tcl8.6.12/license.terms",
    },
    "tk": {
        "path": "LICENSES/TclTk/Tk.txt",
        "sha256": sha(runtime / "LICENSES/TclTk/Tk.txt"),
        "source": "tk8.6.12/license.terms",
    },
}

symlinks = []
for path in sorted(runtime.rglob("*")):
    if path.is_symlink():
        symlinks.append({"path": path.relative_to(runtime).as_posix(), "target": os.readlink(path)})

python_exe = runtime / "bin/python3.12"
libpython = runtime / "lib/libpython3.12.so.1.0"
tcl_provider = runtime / "lib/tcltk/libtcl8.6.so"
tk_provider = runtime / "lib/tcltk/libtk8.6.so"

data = {
    "format_version": 2,
    "baseline": {
        "image": image_ref,
        "image_repository": image_repository,
        "image_digest": image_digest,
        "architecture": "x86_64",
        "glibc": "2.17",
        "policy": "manylinux2014_x86_64 / manylinux_2_17_x86_64",
        "pep599_external_allowlist": sorted(allowlist),
        "symbol_ceilings": {
            "GLIBC": "2.17",
            "CXXABI": "1.3.7",
            "CXXABI_additional_allowed": ["CXXABI_TM_1"],
            "GLIBCXX": "3.4.19",
            "GCC": "4.8.0",
        },
    },
    "sources": {
        "cpython": {
            "version": "3.12.10", "filename": python_name, "url": python_url,
            "sha256": python_sha, "verified_archive_sha256": sha(pathlib.Path(python_archive_arg)),
        },
        "openssl": {
            "version": "3.5.9", "filename": openssl_name, "url": openssl_url,
            "sha256": openssl_sha, "verified_archive_sha256": sha(pathlib.Path(openssl_archive_arg)),
        },
        "libxcrypt": {
            "version": "4.5.2", "filename": libxcrypt_name, "url": libxcrypt_url,
            "sha256": libxcrypt_sha,
            "verified_archive_sha256": sha(pathlib.Path(libxcrypt_archive_arg)),
            "usage": "license-provenance-only",
        },
        "license_rpms": license_rpms.get("packages", []),
        "tcl": {
            "version": "8.6.12", "filename": tcl_name, "url": tcl_url,
            "sha256": tcl_sha, "verified_archive_sha256": sha(pathlib.Path(tcl_archive_arg)),
        },
        "tk": {
            "version": "8.6.12", "filename": tk_name, "url": tk_url,
            "sha256": tk_sha, "verified_archive_sha256": sha(pathlib.Path(tk_archive_arg)),
        },
        "development_rpms": development_rpms.get("packages", []),
    },
    "toolchain": {
        "gcc": command_identity("gcc", ["--version"]),
        "ld": command_identity("ld", ["--version"]),
        "as": command_identity("as", ["--version"]),
        "ar": command_identity("ar", ["--version"]),
        "ranlib": command_identity("ranlib", ["--version"]),
        "strip": command_identity("strip", ["--version"]),
        "pkg_config": command_identity("pkg-config", ["--version"]),
        "make": command_identity("make", ["--version"]),
        "patchelf": command_identity("patchelf", ["--version"]),
        "perl": command_identity("perl", ["-v"]),
    },
    "build": {
        "jobs": int(jobs),
        "cflags": common_cflags,
        "ldflags": common_ldflags,
        "tcl_configure": ["--enable-shared", "--enable-threads", "--enable-64bit"],
        "tk_configure": ["--with-tcl=<source>/tcl8.6.12/unix", "--enable-shared", "--enable-threads", "--enable-64bit"],
        "openssl_configure": [
            "--prefix=<build>/openssl-prefix", "--openssldir=<build>/openssl-prefix",
            "--libdir=lib", "no-afalgeng",
        ],
        "openssl_perl_compat": {
            "scope": "OpenSSL Configure executable discovery and Unix Makefile release-date formatting only",
            "modules": [
                {
                    "module": "IPC::Cmd",
                    "source": "scripts/linux/build-core-runtime.sh:inline",
                    "sha256": sha(pathlib.Path(openssl_perl_compat_arg)),
                },
                {
                    "module": "Time::Piece",
                    "source": "scripts/linux/build-core-runtime.sh:inline",
                    "sha256": sha(pathlib.Path(openssl_time_piece_compat_arg)),
                },
            ],
        },
        "cpython_configure": [
            "--enable-shared", "--without-static-libpython", "--with-ensurepip=no",
            "--with-openssl=<source-built-openssl-prefix>", "--with-openssl-rpath=no",
        ],
        "cpython_pkg_config_path": [
            "<build>/openssl-prefix/lib/pkgconfig",
            "<build>/openssl-prefix/lib64/pkgconfig",
            "<build>/tcltk-prefix/lib/pkgconfig",
            "/usr/local/lib/pkgconfig",
        ],
        "cpython_pkg_config_libdir": [
            "/usr/lib64/pkgconfig",
            "/usr/lib/pkgconfig",
            "/usr/share/pkgconfig",
        ],
        "openssl_image_baseline": openssl_provider,
        "development_header_overlay": development_rpms.get("packages", []),
        "closed_input_policy": (
            "pinned image plus four build source archives and one hash-verified "
            "libxcrypt license/provenance archive plus seven hash-verified matching CentOS 7 "
            "development RPMs installed offline plus eight hash-verified CentOS 7 runtime RPMs used only "
            "for fallback license/provenance evidence; no package-manager resolution, CPAN, or pip downloads"
        ),
    },
    "python": {
        "implementation": "CPython",
        "version": "3.12.10",
        "executable": "bin/python3.12",
        "executable_sha256": sha(python_exe),
        "libpython": "lib/libpython3.12.so.1.0",
        "libpython_sha256": sha(libpython),
        "site_packages": "lib/python3.12/site-packages",
        "site_packages_empty": not any((runtime / "lib/python3.12/site-packages").iterdir()),
        "numpy_bundled": False,
        "dynamic_extension_baseline": baseline_modules,
        "dynamic_extension_baseline_source": {
            "helper_python": dynamic_extension_baseline["helper_python"],
            "helper_version": dynamic_extension_baseline["helper_version"],
            "lib_dynload": dynamic_extension_baseline["lib_dynload"],
            "extension_suffixes": dynamic_extension_baseline["extension_suffixes"],
        },
        "retained_module_probe": [
            "ssl", "_ssl", "sqlite3", "_sqlite3", "ctypes", "_ctypes", "bz2", "_bz2",
            "lzma", "_lzma", "zlib", "readline", "curses", "_curses", "curses.panel",
            "_curses_panel", "multiprocessing", "encodings", "zoneinfo", "hashlib", "decimal", "dbm",
            "_dbm", "_gdbm", "uuid", "_uuid", "pyexpat", "xml.parsers.expat", "tkinter", "_tkinter",
        ],
    },
    "tcl_tk": {
        "tcl_version": "8.6.12",
        "tk_version": "8.6.12",
        "dynamic_provider_model": True,
        "native_loader_strategy": "runtime-relative-dt-runpath",
        "tcl_provider": {"path": "lib/tcltk/libtcl8.6.so", "sha256": sha(tcl_provider)},
        "tk_provider": {"path": "lib/tcltk/libtk8.6.so", "sha256": sha(tk_provider)},
        "tcl_scripts": {"path": "tcl/tcl8.6", **tree_hash(runtime / "tcl/tcl8.6")},
        "tk_scripts": {"path": "tcl/tk8.6", **tree_hash(runtime / "tcl/tk8.6")},
    },
    "licenses": {
        "source_components": source_licenses,
        "image_packages": native["image_packages"],
    },
    "ssl_runtime": {
        "policy": "runtime-relative-pinned-image-certifi",
        "certifi": {
            "version": "2026.10.23",
            "license": "MPL-2.0",
            "image": image_ref,
            "source_path": str(pathlib.Path(certifi_bundle_arg).resolve()),
            "source_sha256": sha(pathlib.Path(certifi_bundle_arg)),
            "staged_path": "ssl/cert.pem",
            "staged_sha256": sha(runtime / "ssl/cert.pem"),
            "license_source_path": str(pathlib.Path(certifi_license_arg).resolve()),
            "license_source_sha256": sha(pathlib.Path(certifi_license_arg)),
            "license_staged_path": "LICENSES/Certifi.txt",
            "license_staged_sha256": sha(runtime / "LICENSES/Certifi.txt"),
        },
        "openssl_config": {
            "source": "openssl-3.5.9/apps/openssl.cnf",
            "source_sha256": sha(pathlib.Path(openssl_conf_source_arg)),
            "staged_path": "ssl/openssl.cnf",
            "staged_sha256": sha(runtime / "ssl/openssl.cnf"),
        },
        "python_ssl_patch": {
            "policy": "setdefault sys.prefix-relative SSL_CERT_FILE and OPENSSL_CONF before importing _ssl",
            "source": "Python-3.12.10/Lib/ssl.py",
            "patched_source_sha256": sha(pathlib.Path(patched_ssl_py_arg)),
            "staged_path": "lib/python3.12/ssl.py",
            "staged_sha256": sha(runtime / "lib/python3.12/ssl.py"),
        },
    },
    "native_providers": {
        "source_built": [
            {"component": "cpython", "path": "lib/libpython3.12.so.1.0", "sha256": sha(libpython)},
            {"component": "openssl", "path": "lib/libssl.so.3", "sha256": sha(runtime / "lib/libssl.so.3")},
            {"component": "openssl", "path": "lib/libcrypto.so.3", "sha256": sha(runtime / "lib/libcrypto.so.3")},
            {"component": "tcl", "path": "lib/tcltk/libtcl8.6.so", "sha256": sha(tcl_provider)},
            {"component": "tk", "path": "lib/tcltk/libtk8.6.so", "sha256": sha(tk_provider)},
        ],
        "pinned_image": native["image_providers"],
        "glibc_loader_external": glibc_loader,
    },
    "elf_census": census,
    "elf_summary": {
        "count": len(census),
        "measured_max_glibc": max_version("GLIBC"),
        "measured_max_cxxabi": max_version("CXXABI"),
        "measured_max_glibcxx": max_version("GLIBCXX"),
        "measured_max_gcc": max_version("GCC"),
        "all_version_needs": sorted(all_versions),
    },
    "symlinks": symlinks,
}

if not data["python"]["site_packages_empty"]:
    raise SystemExit("site-packages ceased to be empty before provenance generation")
if any("numpy" in p.relative_to(runtime).as_posix().lower() for p in runtime.rglob("*")):
    raise SystemExit("NumPy-named payload detected before provenance generation")

output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"runtime_provenance={output}")
print(f"staged_elf_count={len(census)}")
PY

echo "controlled_runtime=$runtime"
echo "build_image=$image_ref"
echo "python_source_sha256=$python_sha"
echo "openssl_source_sha256=$openssl_sha"
echo "libxcrypt_source_sha256=$libxcrypt_sha"
echo "tcl_source_sha256=$tcl_sha"
echo "tk_source_sha256=$tk_sha"
echo "controlled_runtime_build=pass"
