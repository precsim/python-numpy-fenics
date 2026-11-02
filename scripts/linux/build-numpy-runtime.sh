#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
build="$root/build/linux-glibc217-numpy-runtime"
default_manifest="$root/locks/linux/linux-glibc217-numpy-sources.json"
default_output="$root/out/linux-x64/numpy"
helper_python="/opt/python/cp314-cp314/bin/python"
image_repository="quay.io/pypa/manylinux2014_x86_64"
image_digest="sha256:296069bc05d85275248f168f6e3b34ba8aa06181b67e40cd7447c64d56d3a851"
image_ref="$image_repository@$image_digest"
jobs="${PY_RUNTIME_BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)}"

usage() {
    cat >&2 <<'EOF'
usage: scripts/linux/build-numpy-runtime.sh \
  --core-runtime <qualified-core-runtime> \
  --input-root <directory-containing-all-pinned-inputs> \
  --sources-manifest <locks/linux/linux-glibc217-numpy-sources.json> \
  --output-runtime <numpy-runtime-root>
EOF
    exit 2
}

die() {
    echo "error: $*" >&2
    exit 1
}

core_runtime=""
sources_manifest="$default_manifest"
input_dir=""
numpy_source=""
openblas_source=""
wheelhouse=""
devtoolset_rpm_dir=""
output_runtime="$default_output"

while (($#)); do
    case "$1" in
        --core-runtime)
            (($# >= 2)) || usage
            core_runtime="$2"
            shift 2
            ;;
        --sources-manifest)
            (($# >= 2)) || usage
            sources_manifest="$2"
            shift 2
            ;;
        --input-root|--input-dir)
            (($# >= 2)) || usage
            input_dir="$2"
            shift 2
            ;;
        --output|--output-runtime)
            (($# >= 2)) || usage
            output_runtime="$2"
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

[[ -n "$core_runtime" && -n "$input_dir" ]] || usage
[[ -f "$sources_manifest" ]] || die "NumPy source manifest is missing: $sources_manifest"
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || die "PY_RUNTIME_BUILD_JOBS must be a positive integer"
[[ -d "$input_dir" ]] || die "input directory is missing: $input_dir"
wheelhouse="$input_dir"
devtoolset_rpm_dir="$input_dir"
[[ -d "$core_runtime" ]] || die "qualified core runtime is missing: $core_runtime"
[[ -x "$helper_python" ]] || die "pinned-image helper Python is missing: $helper_python"

for tool in bash cp find getconf grep ldd ln make nm patchelf pkg-config readelf readlink rpm sha256sum tar uname; do
    command -v "$tool" >/dev/null 2>&1 || die "required pinned-image tool is missing: $tool"
done
[[ "$(uname -m)" == "x86_64" ]] || die "expected x86_64 build image"
[[ "$(getconf GNU_LIBC_VERSION 2>/dev/null || true)" == "glibc 2.17" ]] || die "expected glibc 2.17 build image"
[[ "${PY_RUNTIME_BUILD_IMAGE_REF:-}" == "$image_ref" ]] || die "PY_RUNTIME_BUILD_IMAGE_REF must identify the pinned image: $image_ref"

canonical() {
    "$helper_python" - "$1" <<'PY'
import pathlib, sys
print(pathlib.Path(sys.argv[1]).resolve())
PY
}

core_runtime="$(canonical "$core_runtime")"
sources_manifest="$(canonical "$sources_manifest")"
wheelhouse="$(canonical "$wheelhouse")"
devtoolset_rpm_dir="$(canonical "$devtoolset_rpm_dir")"
[[ -z "$input_dir" ]] || input_dir="$(canonical "$input_dir")"
output_runtime="$(canonical "$output_runtime")"

[[ "$output_runtime" != "/" && "$output_runtime" != "$root" && "$output_runtime" != "$core_runtime" ]] || \
    die "NumPy output must be a separate safe path"
case "$output_runtime/" in "$core_runtime/"*) die "NumPy output must not be nested inside core runtime" ;; esac
case "$core_runtime/" in "$output_runtime/"*) die "core runtime must not be nested inside NumPy output" ;; esac

core_provenance="$core_runtime/build-provenance-linux.json"
core_python="$core_runtime/bin/python3.14"
[[ -f "$core_provenance" ]] || die "core provenance is missing: $core_provenance"
[[ -x "$core_python" ]] || die "core CPython is missing: $core_python"

rm -rf "$build"
mkdir -p "$build"
core_identity="$build/core-tree-identity.json"
verified_inputs="$build/verified-inputs.json"

"$helper_python" - "$core_runtime" "$core_provenance" "$core_identity" "$image_ref" <<'PY'
import hashlib, json, os, pathlib, shutil, stat, subprocess, sys
runtime = pathlib.Path(sys.argv[1]).resolve()
provenance_path = pathlib.Path(sys.argv[2]).resolve()
identity_path = pathlib.Path(sys.argv[3])
image_ref = sys.argv[4]

def fail(message): raise SystemExit(message)
def sha(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()
def under(path, root):
    path, root = pathlib.Path(path).resolve(), pathlib.Path(root).resolve()
    return path == root or root in path.parents

data = json.loads(provenance_path.read_text(encoding="utf-8"))
baseline = data.get("baseline", {})
ceilings = {
    "GLIBC": "2.17", "CXXABI": "1.3.7",
    "CXXABI_additional_allowed": ["CXXABI_TM_1"],
    "GLIBCXX": "3.4.19", "GCC": "4.8.0",
}
if data.get("format_version") != 2 or baseline.get("image") != image_ref or \
   baseline.get("architecture") != "x86_64" or baseline.get("glibc") != "2.17" or \
   baseline.get("symbol_ceilings") != ceilings:
    fail("qualified core provenance does not match the glibc-2.17 baseline")
allowlist = baseline.get("pep599_external_allowlist")
if not isinstance(allowlist, list) or not allowlist or allowlist != sorted(set(allowlist)):
    fail("qualified core PEP 599 allowlist is invalid")
python = data.get("python", {})
if python.get("implementation") != "CPython" or python.get("version") != "3.14.10" or \
   python.get("site_packages") != "lib/python3.14/site-packages" or \
   python.get("site_packages_empty") is not True or python.get("numpy_bundled") is not False:
    fail("qualified core Python provenance is not the NumPy-free baseline")
for rel, field in (("bin/python3.14", "executable_sha256"), ("lib/libpython3.14.so.1.0", "libpython_sha256")):
    path = runtime / rel
    if not path.is_file() or sha(path) != python.get(field): fail(f"core byte identity differs: {rel}")
site = runtime / "lib/python3.14/site-packages"
if not site.is_dir() or any(site.iterdir()): fail("qualified core site-packages is not empty")

toolchain = data.get("toolchain", {})
for key, command in (
    ("gcc","gcc"), ("ld","ld"), ("as","as"), ("ar","ar"), ("ranlib","ranlib"),
    ("strip","strip"), ("pkg_config","pkg-config"), ("make","make"),
    ("patchelf","patchelf"), ("perl","perl"),
):
    record = toolchain.get(key)
    executable = shutil.which(command)
    if not isinstance(record, dict) or executable is None: fail(f"missing image tool identity: {command}")
    executable = str(pathlib.Path(executable).resolve())
    if executable != record.get("path") or sha(executable) != record.get("sha256"):
        fail(f"pinned-image tool bytes changed since core qualification: {command}")
    args = ["-v"] if command == "perl" else ["--version"]
    observed = subprocess.run([executable,*args], text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, check=False).stdout.strip()
    if observed != record.get("version_output"): fail(f"pinned-image tool version changed: {command}")

files, symlinks, aggregate = [], [], hashlib.sha256()
for path in sorted(runtime.rglob("*")):
    rel = path.relative_to(runtime).as_posix()
    if path.is_symlink():
        target = os.readlink(path)
        try: resolved = path.resolve(strict=True)
        except OSError as exc: fail(f"core runtime contains broken symlink: {rel}: {exc}")
        if not under(resolved, runtime): fail(f"core symlink escapes runtime: {rel}")
        symlinks.append({"path":rel,"target":target,"mode":stat.S_IMODE(path.lstat().st_mode)})
        mode = stat.S_IMODE(path.lstat().st_mode)
        aggregate.update(b"L\0"+rel.encode()+b"\0"+oct(mode).encode()+b"\0"+target.encode()+b"\n")
    elif path.is_file():
        digest, size = sha(path), path.stat().st_size
        files.append({"path":rel,"sha256":digest,"size":size,"mode":stat.S_IMODE(path.stat().st_mode)})
        mode = stat.S_IMODE(path.stat().st_mode)
        aggregate.update(b"F\0"+rel.encode()+b"\0"+oct(mode).encode()+b"\0"+str(size).encode()+b"\0"+digest.encode()+b"\n")
identity = {
    "sha256": aggregate.hexdigest(), "file_count": len(files), "symlink_count": len(symlinks),
    "files": files, "symlinks": symlinks, "root": str(runtime),
    "provenance_path": "build-provenance-linux.json", "provenance_sha256": sha(provenance_path),
}
identity_path.write_text(json.dumps(identity, indent=2, sort_keys=True)+"\n", encoding="utf-8")
print(f"core_tree_sha256={identity['sha256']}")
PY

"$helper_python" - "$sources_manifest" "$verified_inputs" "$image_ref" \
    "$input_dir" "$numpy_source" "$openblas_source" "$wheelhouse" "$devtoolset_rpm_dir" <<'PY'
import hashlib, json, pathlib, re, sys
manifest_path = pathlib.Path(sys.argv[1]).resolve()
output_path = pathlib.Path(sys.argv[2])
image_ref, input_dir_arg, numpy_arg, openblas_arg = sys.argv[3:7]
wheelhouse, rpm_dir = pathlib.Path(sys.argv[7]).resolve(), pathlib.Path(sys.argv[8]).resolve()
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
def fail(message): raise SystemExit(message)
def sha(path):
    h=hashlib.sha256()
    with pathlib.Path(path).open("rb") as s:
        for b in iter(lambda:s.read(1024*1024),b""): h.update(b)
    return h.hexdigest()
EXPECTED_MANIFEST_SHA = "47c5864635ea7b5a2890245f544065fcdf2ed90cc640644260f41b3e7ae98559"
if sha(manifest_path) != EXPECTED_MANIFEST_SHA:
    fail("supplied NumPy source manifest differs from the committed phase-01 manifest")
SOURCE_PINS = [
 ("numpy","2.5.2","d482d171c406ae88c5b19cad3b6a1c4c5209f886ab74bc44c2c865c23f52d860"),
 ("openblas","0.3.33","6761af1d9f5d353ab4f0b7497be2643313b36c8f31caec0144bfef198e71e6ab"),
]
WHEEL_PINS = [
 ("pip","25.3","9655943313a94722b7774661c21049070f6bbb0a1516bf02f7c8d5d9201514cd"),
 ("meson-python","0.18.0","3b0fe051551cc238f5febb873247c0949cd60ded556efa130aa57021804868e2"),
 ("meson","1.8.3","ef02b806ce0c5b6becd5bb5dc9fa67662320b29b337e7ace73e4354500590233"),
 ("cython","3.1.3","d13025b34f72f77bf7f65c1cd628914763e6c285f4deb934314c922b91e6be5a"),
 ("packaging","25.0","29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484"),
 ("pyproject-metadata","0.9.1","ee5efde548c3ed9b75a354fc319d5afd25e9585fa918a34f62f904cc731973ad"),
 ("ninja","1.11.1.4","096487995473320de7f65d622c3f1d16c3ad174797602218ca8c967f51ec38a0"),
]
RPM_PINS = [
 ("devtoolset-11-runtime","11.1-2.el7","ac350205a9316c777f842fa3451dc786ba9b058ca997776b73e4aa221908c84e"),
 ("devtoolset-11-binutils","2.36.1-1.el7.2","dde2f4b967e4a379caf1c38f756029799d610d8911f0c13b1f4f94a5d7a93561"),
 ("devtoolset-11-gcc","11.2.1-9.1.el7","7049c0fdcee4412cf0e2570e2ed90e4879019aa58b9c7d1112e85260ead0ec30"),
 ("devtoolset-11-gcc-c++","11.2.1-9.1.el7","adbf4b0743fa1227da7d93ef8236e20aa7ea5bda67965baadf782095bfee540f"),
 ("devtoolset-11-libstdc++-devel","11.2.1-9.1.el7","b42d3c7ff9f4da0f7fedd354051b002fe68dbb56dc59c713c61bdf92773b3d9c"),
]
def dicts(value):
    if isinstance(value,dict):
        yield value
        for item in value.values(): yield from dicts(item)
    elif isinstance(value,list):
        for item in value: yield from dicts(item)
def strings(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if isinstance(key,str): yield key
            yield from strings(item)
    elif isinstance(value,list):
        for item in value: yield from strings(item)
    elif isinstance(value,str): yield value
records, all_text = list(dicts(manifest)), set(strings(manifest))
if image_ref not in all_text: fail("source manifest does not bind the pinned image digest")
if "x86_64" not in all_text or not ({"cp314","3.14"} & all_text): fail("source manifest lacks x86_64/cp314 identity")
for required in ("bridge_np252_","OPTERON","X86_V2","X86_V3"):
    if not any(required == v or required in v for v in all_text): fail(f"source manifest lacks policy: {required}")
def record(label,digest):
    matches=[dict(r) for r in records if str(r.get("sha256","")).lower()==digest]
    if len(matches)!=1: fail(f"source manifest needs exactly one {label} record with SHA {digest}; found {len(matches)}")
    r=matches[0]
    if not isinstance(r.get("filename"),str) or pathlib.PurePath(r["filename"]).name!=r["filename"]: fail(f"bad filename for {label}")
    if not isinstance(r.get("url"),str) or not r["url"].startswith("https://"): fail(f"missing HTTPS URL for {label}")
    return r
def version(r):
    for key in ("version","version_release","version-release"):
        if isinstance(r.get(key),str): return r[key]
    return None
sources={}
for label,ver,digest in SOURCE_PINS:
    r=record(label,digest)
    if version(r)!=ver: fail(f"{label} manifest version mismatch")
    explicit=numpy_arg if label=="numpy" else openblas_arg
    path=pathlib.Path(explicit).resolve() if explicit else (pathlib.Path(input_dir_arg).resolve()/r["filename"] if input_dir_arg else None)
    if path is None or path.name!=r["filename"] or not path.is_file(): fail(f"missing pinned {label} source: {path}")
    if sha(path)!=digest: fail(f"{label} source hash mismatch")
    r.update({"verified_path":str(path),"verified_sha256":digest}); sources[label]=r
wheels=[]
for name,ver,digest in WHEEL_PINS:
    r=record(f"{name} wheel",digest)
    if version(r)!=ver or not r["filename"].endswith(".whl"): fail(f"{name} wheel manifest mismatch")
    path=wheelhouse/r["filename"]
    if not path.is_file() or sha(path)!=digest: fail(f"{name} wheel missing or hash mismatch: {path}")
    r.update({"name":name,"version":ver,"verified_path":str(path),"verified_sha256":digest,"usage":"build-only-pep517"}); wheels.append(r)
rpms=[]
for package,ver,digest in RPM_PINS:
    r=record(f"{package} RPM",digest)
    if version(r)!=ver or not r["filename"].endswith(".rpm"): fail(f"{package} RPM manifest mismatch")
    if r.get("package",r.get("name")) not in (None,package): fail(f"{package} package identity mismatch")
    path=rpm_dir/r["filename"]
    if not path.is_file() or sha(path)!=digest: fail(f"{package} RPM missing or hash mismatch: {path}")
    r.update({"package":package,"version_release":ver,"verified_path":str(path),"verified_sha256":digest,"usage":"build-only-devtoolset-11"}); rpms.append(r)
input_root = pathlib.Path(input_dir_arg).resolve()
manifest_copy = input_root / "linux-glibc217-numpy-sources.json"
if not manifest_copy.is_file() or manifest_copy.is_symlink() or sha(manifest_copy) != sha(manifest_path):
    fail("verified input root does not contain the exact committed source manifest")
expected_files = {
    manifest_copy.name,
    *(record["filename"] for record in sources.values()),
    *(record["filename"] for record in wheels),
    *(record["filename"] for record in rpms),
}
observed_files = {
    path.relative_to(input_root).as_posix()
    for path in input_root.rglob("*")
    if path.is_file() and not path.is_symlink()
}
if observed_files != expected_files:
    fail(
        "verified input root contains unpinned or missing files: "
        f"missing={sorted(expected_files-observed_files)}, extra={sorted(observed_files-expected_files)}"
    )
if any(path.is_symlink() for path in input_root.rglob("*")):
    fail("verified input root must not contain symlinks")
result={
 "format_version":1, "manifest":{"path":str(manifest_path),"sha256":sha(manifest_path)},
 "build_image":image_ref, "sources":sources, "pep517_wheels":wheels, "devtoolset_rpms":rpms,
}
output_path.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",encoding="utf-8")
print("closed_input_verification=pass")
PY

numpy_source="$("$helper_python" - "$verified_inputs" <<'PY'
import json,pathlib,sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text())["sources"]["numpy"]["verified_path"])
PY
)"
openblas_source="$("$helper_python" - "$verified_inputs" <<'PY'
import json,pathlib,sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text())["sources"]["openblas"]["verified_path"])
PY
)"
mapfile -t pep517_wheels < <("$helper_python" - "$verified_inputs" <<'PY'
import json,pathlib,sys
for r in json.loads(pathlib.Path(sys.argv[1]).read_text())["pep517_wheels"]: print(r["verified_path"])
PY
)
mapfile -t devtoolset_rpms < <("$helper_python" - "$verified_inputs" <<'PY'
import json,pathlib,sys
for r in json.loads(pathlib.Path(sys.argv[1]).read_text())["devtoolset_rpms"]: print(r["verified_path"])
PY
)
pip_wheel="$("$helper_python" - "$verified_inputs" <<'PY'
import json,pathlib,sys
for r in json.loads(pathlib.Path(sys.argv[1]).read_text())["pep517_wheels"]:
    if r["name"]=="pip": print(r["verified_path"]); break
else: raise SystemExit("verified pip wheel is missing")
PY
)"

"$helper_python" - "$sources_manifest" "$image_ref" <<'PY'
import json, pathlib, sys
manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
image_ref = sys.argv[2]
expected_target = {
    "architecture": "x86_64", "python_version": "3.14.10", "python_abi": "cp314",
    "python_wheel_abi": "cp314-cp314",
    "platform_policy": "manylinux2014_x86_64 / manylinux_2_17_x86_64", "glibc_floor": "2.17",
}
expected_numpy_args = [
    "-Dblas=bridge-openblas", "-Dlapack=bridge-openblas", "-Dallow-noblas=false",
    "-Duse-ilp64=false", "-Dblas-symbol-suffix=bridge_np252_",
    "-Dcpu-baseline=none", "-Dcpu-baseline-detect=disabled",
    "-Dcpu-dispatch=X86_V2,X86_V3", "-Ddisable-highway=true",
    "-Ddisable-intel-sort=true", "-Ddisable-svml=true",
]
expected_openblas = {
    "BINARY": "64", "TARGET": "OPTERON", "DYNAMIC_ARCH": "1", "DYNAMIC_OLDER": "1",
    "NO_AVX512": "1", "NOFORTRAN": "1", "USE_THREAD": "1", "USE_OPENMP": "0",
    "NO_AFFINITY": "1", "NUM_THREADS": "64", "CONSISTENT_FPCSR": "1", "INTERFACE64": "0",
    "LIBNAMESUFFIX": "bridge_np252_", "SYMBOLSUFFIX": "bridge_np252_",
}
expected_baseline_probe_environment = {
    "NPY_DISABLE_CPU_FEATURES": "X86_V2,X86_V3",
    "OPENBLAS_CORETYPE": "unset",
}
expected_cpu_validation = {
    "numpy_mask_newer_features": True,
    "openblas_runtime_core_selection": "autodetect",
    "reject_openblas_coretype_override": True,
}
if manifest.get("schema_version") != 1 or manifest.get("target") != expected_target:
    raise SystemExit("NumPy source manifest target/schema differs from phase-01 contract")
if manifest.get("build_image", {}).get("ref") != image_ref:
    raise SystemExit("NumPy source manifest build-image identity differs from pinned image")
policy = manifest.get("build_policy", {})
if (
    policy.get("network_disabled_after_input_verification") is not True
    or policy.get("pep517_build_isolation") is not False
    or policy.get("dependency_resolution") is not False
    or policy.get("index_access") is not False
    or policy.get("stage_build_tools_into_runtime") is not False
    or policy.get("host_native_tuning") != "forbidden"
    or policy.get("compiler") != "devtoolset-11 GCC/G++ 11.2.1"
):
    raise SystemExit("NumPy source manifest closed-build policy differs from phase-01 contract")
numpy_build = manifest.get("numpy_build", {})
if (
    numpy_build.get("backend") != "mesonpy"
    or numpy_build.get("meson_setup_args") != expected_numpy_args
    or numpy_build.get("extra_compile_defines") != ["OPENBLAS_ILP64_NAMING_SCHEME"]
    or numpy_build.get("openblas_suffix_placement") != "fortran-underscore-before-private-symbol-suffix"
):
    raise SystemExit("NumPy source manifest Meson configuration differs from phase-01 contract")
openblas = manifest.get("openblas_build", {})
if (
    openblas.get("interface") != "LP64"
    or openblas.get("make_options") != expected_openblas
    or openblas.get("baseline_probe_environment") != expected_baseline_probe_environment
):
    raise SystemExit("NumPy source manifest OpenBLAS configuration differs from phase-01 contract")
if manifest.get("cpu_validation") != expected_cpu_validation:
    raise SystemExit("NumPy source manifest CPU validation policy differs from phase-01 contract")
discovery = manifest.get("provider_discovery", {})
if (
    discovery.get("provider") != "OpenBLAS 0.3.33"
    or discovery.get("method") != "pkg-config-only"
    or discovery.get("pkg_config_name") != "bridge-openblas"
    or discovery.get("cmake_fallback") != "forbidden"
    or discovery.get("ambient_blas_lapack") != "forbidden"
):
    raise SystemExit("NumPy source manifest provider-discovery policy differs from phase-01 contract")
print("source_manifest_policy=pass")
PY

rm -rf "$output_runtime"
mkdir -p "$output_runtime"
cp -a "$core_runtime/." "$output_runtime/"
"$helper_python" - "$output_runtime" "$core_identity" <<'PY'
import hashlib,json,os,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve(); identity=json.loads(pathlib.Path(sys.argv[2]).read_text())
def sha(p):
    h=hashlib.sha256()
    with p.open("rb") as s:
        for b in iter(lambda:s.read(1024*1024),b""): h.update(b)
    return h.hexdigest()
for r in identity["files"]:
    p=root/r["path"]
    if p.is_symlink() or not p.is_file() or p.stat().st_size!=r["size"] or sha(p)!=r["sha256"] or (p.stat().st_mode & 0o7777)!=r["mode"]:
        raise SystemExit(f"core copy changed file: {r['path']}")
for r in identity["symlinks"]:
    p=root/r["path"]
    if not p.is_symlink() or os.readlink(p)!=r["target"] or (p.lstat().st_mode & 0o7777)!=r["mode"]: raise SystemExit(f"core copy changed symlink: {r['path']}")
print("core_copy_identity=pass")
PY

[[ ! -e /opt/rh/devtoolset-11 ]] || die "ambient /opt/rh/devtoolset-11 exists before controlled installation"
rpm -ivh --nosignature --nodeps --noscripts --notriggers "${devtoolset_rpms[@]}"
devtoolset_root="/opt/rh/devtoolset-11/root/usr"
dev_cc="$devtoolset_root/bin/gcc"
dev_cxx="$devtoolset_root/bin/g++"
dev_ld="$devtoolset_root/bin/ld.bfd"
dev_ld_link="$devtoolset_root/bin/ld"
[[ -x "$dev_cc" && -x "$dev_cxx" && -x "$dev_ld" ]] || die "devtoolset-11 toolchain payload was not installed"
[[ "$(rpm -qf --qf '%{NAME}' "$dev_cc")" == "devtoolset-11-gcc" ]] || die "controlled GCC is not owned by the pinned devtoolset GCC RPM"
[[ "$(rpm -qf --qf '%{NAME}' "$dev_cxx")" == "devtoolset-11-gcc-c++" ]] || die "controlled G++ is not owned by the pinned devtoolset G++ RPM"
[[ "$(rpm -qf --qf '%{NAME}' "$dev_ld")" == "devtoolset-11-binutils" ]] || die "controlled ld.bfd is not owned by the pinned devtoolset binutils RPM"
# The pinned binutils RPM ships ld.bfd/ld.gold and normally creates `ld` from
# its alternatives %post script. Scriptlets are intentionally disabled for the
# closed-input build, so reproduce the package's default (ld.bfd priority 50)
# deterministically rather than executing ambient alternatives machinery.
[[ ! -e "$dev_ld_link" && ! -L "$dev_ld_link" ]] || die "unexpected devtoolset linker alias exists before controlled creation"
ln -s ld.bfd "$dev_ld_link"
[[ "$(readlink "$dev_ld_link")" == "ld.bfd" && "$(readlink -f "$dev_ld_link")" == "$dev_ld" ]] || \
    die "controlled devtoolset linker alias does not resolve to ld.bfd"
"$dev_cc" --version | grep -q '11\.2\.1' || die "devtoolset GCC is not 11.2.1"
"$dev_cxx" --version | grep -q '11\.2\.1' || die "devtoolset G++ is not 11.2.1"
dev_gcc_ld="$(PATH="$devtoolset_root/bin:/usr/local/bin:/usr/bin:/bin" "$dev_cc" -print-prog-name=ld)"
if [[ -n "$dev_gcc_ld" && "$dev_gcc_ld" != */* ]]; then
    dev_gcc_ld="$(PATH="$devtoolset_root/bin:/usr/local/bin:/usr/bin:/bin" command -v "$dev_gcc_ld" || true)"
fi
[[ -n "$dev_gcc_ld" && -e "$dev_gcc_ld" ]] || die "devtoolset GCC does not resolve a linker"
[[ "$(readlink -f "$dev_gcc_ld")" == "$dev_ld" ]] || \
    die "devtoolset GCC resolves a linker outside the controlled ld.bfd payload: $dev_gcc_ld"
"$helper_python" - "$verified_inputs" <<'PY'
import json,pathlib,subprocess,sys
data=json.loads(pathlib.Path(sys.argv[1]).read_text())
for r in data["devtoolset_rpms"]:
    p=subprocess.run(["rpm","-q","--qf","%{VERSION}-%{RELEASE}\t%{ARCH}",r["package"]],
                     text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,check=False)
    if p.returncode or p.stdout.strip().split("\t")[0]!=r["version_release"]:
        raise SystemExit(f"installed devtoolset identity mismatch for {r['package']}: {p.stdout!r}")
print("devtoolset_rpm_identity=pass")
PY

src_root="$build/src"
numpy_src="$src_root/numpy-2.5.2"
openblas_src="$src_root/OpenBLAS-0.3.33"
openblas_prefix="$build/openblas-prefix"
private_pc_root="$openblas_prefix/lib/pkgconfig"
pep517_venv="$build/pep517-venv"
wheel_output="$build/numpy-wheel"
numpy_install="$build/numpy-install"
openblas_log="$build/openblas-build.log"
numpy_log="$build/numpy-build.log"
discovery_guard_bin="$build/provider-discovery-guard/bin"
cmake_guard_log="$build/cmake-fallback-attempts.log"
mkdir -p "$src_root" "$openblas_prefix" "$private_pc_root" "$wheel_output" "$numpy_install" "$discovery_guard_bin"
tar -xzf "$numpy_source" -C "$src_root"
tar -xzf "$openblas_source" -C "$src_root"
[[ -d "$numpy_src" && -f "$numpy_src/LICENSE.txt" ]] || die "NumPy source archive layout is invalid"
[[ -d "$openblas_src" && -f "$openblas_src/LICENSE" ]] || die "OpenBLAS source archive layout is invalid"

openblas_make_args=(
    "CC=$dev_cc" "HOSTCC=$dev_cc" "FC="
    "BINARY=64" "TARGET=OPTERON" "DYNAMIC_ARCH=1" "DYNAMIC_OLDER=1"
    "NO_AVX512=1" "NOFORTRAN=1" "USE_THREAD=1" "USE_OPENMP=0"
    "NO_AFFINITY=1" "NUM_THREADS=64" "CONSISTENT_FPCSR=1" "INTERFACE64=0"
    "LIBNAMESUFFIX=bridge_np252_" "SYMBOLSUFFIX=bridge_np252_"
)
for arg in "${openblas_make_args[@]}"; do
    [[ "$arg" != *"-march=native"* && "$arg" != *"-mtune=native"* ]] || die "host-native OpenBLAS tuning is forbidden"
done
(
    set -o pipefail
    env -u CFLAGS -u CXXFLAGS -u CPPFLAGS -u LDFLAGS -u LIBRARY_PATH \
        -u CPATH -u C_INCLUDE_PATH -u CPLUS_INCLUDE_PATH -u PKG_CONFIG_PATH \
        -u PKG_CONFIG_LIBDIR -u CMAKE_PREFIX_PATH -u LD_LIBRARY_PATH -u LD_PRELOAD \
        PATH="$devtoolset_root/bin:/usr/local/bin:/usr/bin:/bin" \
        make -C "$openblas_src" -j "$jobs" "${openblas_make_args[@]}" 2>&1 | tee "$openblas_log"
    env -u CFLAGS -u CXXFLAGS -u CPPFLAGS -u LDFLAGS -u LIBRARY_PATH \
        -u CPATH -u C_INCLUDE_PATH -u CPLUS_INCLUDE_PATH -u PKG_CONFIG_PATH \
        -u PKG_CONFIG_LIBDIR -u CMAKE_PREFIX_PATH -u LD_LIBRARY_PATH -u LD_PRELOAD \
        PATH="$devtoolset_root/bin:/usr/local/bin:/usr/bin:/bin" \
        make -C "$openblas_src" "${openblas_make_args[@]}" PREFIX="$openblas_prefix" install
)

openblas_provider="$("$helper_python" - "$openblas_prefix" <<'PY'
import pathlib,re,subprocess,sys
root=pathlib.Path(sys.argv[1]).resolve(); candidates=[]
for p in sorted((root/"lib").glob("libopenblas*.so*")):
    if p.is_symlink() or not p.is_file(): continue
    try:
        if p.open("rb").read(4)!=b"\x7fELF": continue
    except OSError: continue
    text=subprocess.check_output(["readelf","-dW",str(p)],text=True)
    m=re.search(r"Library soname: \[([^\]]+)\]",text); soname=m.group(1) if m else ""
    if "bridge_np252_" in p.name and "bridge_np252_" in soname: candidates.append(p.resolve())
if len(candidates)!=1: raise SystemExit(f"expected one private OpenBLAS provider, found {candidates}")
print(candidates[0])
PY
)"
openblas_soname="$(readelf -dW "$openblas_provider" | sed -n 's/.*Library soname: \[\([^]]*\)\].*/\1/p')"
[[ -n "$openblas_soname" && "$openblas_soname" == *bridge_np252_* ]] || die "private OpenBLAS SONAME lacks suffix"
nm -D --defined-only "$openblas_provider" > "$build/openblas-defined-symbols.txt"
grep -Eq '(^|[[:space:]])dgemm_bridge_np252_$' "$build/openblas-defined-symbols.txt" || die "private OpenBLAS lacks exact suffixed dgemm"
grep -Eq '(^|[[:space:]])dgesv_bridge_np252_$' "$build/openblas-defined-symbols.txt" || die "C-translated LAPACK lacks exact suffixed dgesv"
grep -Eq '(^|[[:space:]])cblas_dgemmbridge_np252_$' "$build/openblas-defined-symbols.txt" || die "private OpenBLAS lacks exact suffixed cblas_dgemm"
grep -Eq '(^|[[:space:]])dgemm_$|(^|[[:space:]])dgesv_$' "$build/openblas-defined-symbols.txt" && die "private OpenBLAS leaked unsuffixed symbols"
openblas_needed="$(readelf -dW "$openblas_provider" | sed -n 's/.*(NEEDED).*Shared library: \[\([^]]*\)\].*/\1/p')"
grep -Eiq 'libgfortran|libquadmath' <<<"$openblas_needed" && die "NOFORTRAN OpenBLAS gained Fortran runtime dependency"

openblas_link_name="$("$helper_python" - "$openblas_prefix" "$openblas_provider" <<'PY'
import pathlib,sys
prefix=pathlib.Path(sys.argv[1]).resolve(); provider=pathlib.Path(sys.argv[2]).resolve(); matches=[]
for p in sorted((prefix/"lib").glob("libopenblas*.so")):
    try:
        if p.is_symlink() and p.resolve()==provider: matches.append(p.name)
    except OSError: pass
if len(matches)!=1: raise SystemExit(f"expected one private OpenBLAS linker symlink, found {matches}")
print(matches[0][3:-3])
PY
)"
[[ "$openblas_link_name" == *bridge_np252_* ]] || die "OpenBLAS linker name lacks private suffix"
cat > "$private_pc_root/bridge-openblas.pc" <<EOF_PC
prefix=$openblas_prefix
libdir=\${prefix}/lib
includedir=\${prefix}/include
Name: OpenBLAS
Description: bridge-private OpenBLAS 0.3.33
Version: 0.3.33
Libs: -L\${libdir} -l$openblas_link_name
Libs.private: -lm -lpthread
Cflags: -I\${includedir}
EOF_PC
PKG_CONFIG_PATH= PKG_CONFIG_LIBDIR="$private_pc_root" pkg-config --exact-version=0.3.33 bridge-openblas
private_pkg_libs="$(PKG_CONFIG_PATH= PKG_CONFIG_LIBDIR="$private_pc_root" pkg-config --libs bridge-openblas)"
[[ "$private_pkg_libs" == *"-l$openblas_link_name"* ]] || die "private pkg-config metadata selects wrong provider"

cat > "$discovery_guard_bin/cmake" <<'EOF_CMAKE_GUARD'
#!/usr/bin/env bash
set -euo pipefail
: "${PY_RUNTIME_CMAKE_GUARD_LOG:?missing CMake fallback guard log}"
printf '%s\n' "$*" >> "$PY_RUNTIME_CMAKE_GUARD_LOG"
echo "CMake dependency discovery is forbidden for the controlled NumPy/OpenBLAS build" >&2
exit 97
EOF_CMAKE_GUARD
chmod 0755 "$discovery_guard_bin/cmake"
: > "$cmake_guard_log"

PYTHONDONTWRITEBYTECODE=1 "$core_python" -I -m venv --without-pip "$pep517_venv"
venv_python="$pep517_venv/bin/python"
env -u PYTHONHOME -u PYTHONUSERBASE -u LD_LIBRARY_PATH -u LD_PRELOAD \
    PYTHONPATH="$pip_wheel" PIP_CONFIG_FILE=/dev/null PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    "$venv_python" -m pip install --no-index --no-deps "${pep517_wheels[@]}"
"$venv_python" -I - "$verified_inputs" "$build/pep517-tool-versions.json" <<'PY'
import importlib.metadata,json,pathlib,sys
inputs=json.loads(pathlib.Path(sys.argv[1]).read_text()); output=pathlib.Path(sys.argv[2])
names={"pip":"pip","meson-python":"meson-python","meson":"meson","cython":"Cython",
       "packaging":"packaging","pyproject-metadata":"pyproject-metadata","ninja":"ninja"}
records=[]
for r in inputs["pep517_wheels"]:
    observed=importlib.metadata.version(names[r["name"]])
    if observed!=r["version"]: raise SystemExit(f"PEP517 tool version mismatch: {r['name']} {observed}")
    records.append({"name":r["name"],"version":observed})
output.write_text(json.dumps(records,indent=2,sort_keys=True)+"\n",encoding="utf-8")
print("pep517_tool_closure=pass")
PY

numpy_setup_args=(
    "-Dblas=bridge-openblas" "-Dlapack=bridge-openblas" "-Dallow-noblas=false"
    "-Duse-ilp64=false" "-Dblas-symbol-suffix=bridge_np252_"
    "-Dcpu-baseline=none" "-Dcpu-baseline-detect=disabled"
    "-Dcpu-dispatch=X86_V2,X86_V3" "-Ddisable-highway=true"
    "-Ddisable-intel-sort=true" "-Ddisable-svml=true"
)
numpy_cflags="-O2 -fPIC -DOPENBLAS_ILP64_NAMING_SCHEME"
numpy_cxxflags="-O2 -fPIC -D_GLIBCXX_USE_CXX11_ABI=0 -DOPENBLAS_ILP64_NAMING_SCHEME"
numpy_ldflags="-Wl,-z,relro,-z,now"
pip_config_args=()
for arg in "${numpy_setup_args[@]}"; do
    [[ "$arg" != *native* ]] || die "host-native NumPy tuning is forbidden"
    pip_config_args+=("-Csetup-args=$arg")
done
(
    set -o pipefail
    env -u PYTHONHOME -u PYTHONPATH -u PYTHONUSERBASE -u LD_PRELOAD \
        -u CMAKE_PREFIX_PATH -u BLAS -u LAPACK -u BLAS_DIR -u LAPACK_DIR \
        -u MKLROOT -u OPENBLAS -u OPENBLAS_ROOT -u ATLAS \
        PATH="$discovery_guard_bin:$pep517_venv/bin:$devtoolset_root/bin:/usr/local/bin:/usr/bin:/bin" \
        CMAKE="$discovery_guard_bin/cmake" PY_RUNTIME_CMAKE_GUARD_LOG="$cmake_guard_log" \
        CC="$dev_cc" CXX="$dev_cxx" CFLAGS="$numpy_cflags" CXXFLAGS="$numpy_cxxflags" LDFLAGS="$numpy_ldflags" \
        CPATH= C_INCLUDE_PATH= CPLUS_INCLUDE_PATH= PKG_CONFIG_PATH= \
        PKG_CONFIG_LIBDIR="$private_pc_root" LIBRARY_PATH= LD_LIBRARY_PATH= \
        PIP_CONFIG_FILE=/dev/null PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
        "$venv_python" -I -m pip wheel --no-index --no-deps --no-build-isolation \
            --wheel-dir "$wheel_output" "${pip_config_args[@]}" "$numpy_src" 2>&1 | tee "$numpy_log"
)
if [[ -s "$cmake_guard_log" ]]; then
    cat "$cmake_guard_log" >&2
    die "NumPy/OpenBLAS discovery attempted forbidden CMake fallback"
fi
mapfile -t built_wheels < <(find "$wheel_output" -maxdepth 1 -type f -name 'numpy-2.5.2-*.whl' -print | LC_ALL=C sort)
[[ ${#built_wheels[@]} -eq 1 ]] || die "expected exactly one source-built NumPy wheel, found ${#built_wheels[@]}"
built_numpy_wheel="${built_wheels[0]}"
case "$(basename "$built_numpy_wheel")" in *manylinux_2_27*|*manylinux_2_28*) die "retained official NumPy wheel is forbidden" ;; esac

env -u PYTHONHOME -u PYTHONPATH -u PYTHONUSERBASE -u LD_LIBRARY_PATH -u LD_PRELOAD \
    PIP_CONFIG_FILE=/dev/null PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    "$venv_python" -I -m pip install --no-index --no-deps --no-compile --target "$numpy_install" "$built_numpy_wheel"
"$helper_python" - "$numpy_install" <<'PY'
import csv
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1]).resolve()
matches = sorted(path for path in root.iterdir() if re.fullmatch(r"numpy-2\.5\.2\.dist-info", path.name))
if len(matches) != 1:
    raise SystemExit(f"expected exactly one NumPy dist-info directory before metadata normalization: {matches}")
dist_info = matches[0]
direct_url = dist_info / "direct_url.json"
record = dist_info / "RECORD"
if not direct_url.is_file() or direct_url.is_symlink():
    raise SystemExit(f"pip local-wheel metadata is missing or invalid: {direct_url}")
if not record.is_file() or record.is_symlink():
    raise SystemExit(f"NumPy RECORD is missing or invalid: {record}")

direct_rel = f"{dist_info.name}/direct_url.json"
record_rel = f"{dist_info.name}/RECORD"
with record.open("r", encoding="utf-8", newline="") as stream:
    rows = list(csv.reader(stream))
if not rows or any(len(row) != 3 for row in rows):
    raise SystemExit("NumPy RECORD contains malformed rows")
direct_rows = [row for row in rows if row[0] == direct_rel]
if len(direct_rows) != 1:
    raise SystemExit(f"NumPy RECORD must contain exactly one direct_url.json row: {direct_rows!r}")
self_rows = [row for row in rows if row[0] == record_rel]
if len(self_rows) != 1 or self_rows[0][1:] != ["", ""]:
    raise SystemExit(f"NumPy RECORD self row is invalid: {self_rows!r}")

direct_url.unlink()
normalized = [row for row in rows if row[0] != direct_rel]
with record.open("w", encoding="utf-8", newline="") as stream:
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(normalized)
print("numpy_install_metadata_normalization=pass")
PY
"$helper_python" - "$numpy_install" <<'PY'
import configparser,pathlib,re,sys
root=pathlib.Path(sys.argv[1]).resolve(); top=sorted(p.name for p in root.iterdir())
dist=[n for n in top if re.fullmatch(r"numpy-2\.5\.2\.dist-info",n)]
if len(dist)!=1 or set(top)!={"bin","numpy",dist[0]}:
    raise SystemExit(f"unexpected NumPy temporary install surface: {top}")
bin_root=root/"bin"
if not bin_root.is_dir() or bin_root.is_symlink():
    raise SystemExit("NumPy temporary console-script directory is invalid")
scripts=sorted(p.name for p in bin_root.iterdir())
if scripts != ["f2py","numpy-config"]:
    raise SystemExit(f"unexpected NumPy temporary console scripts: {scripts}")
for name in scripts:
    path=bin_root/name
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"NumPy temporary console script is not a regular file: {name}")
entry_points_path=root/dist[0]/"entry_points.txt"
parser=configparser.ConfigParser(interpolation=None)
parser.optionxform=str
parser.read(entry_points_path,encoding="utf-8")
observed={section:dict(parser.items(section)) for section in parser.sections()}
expected={
    "array_api":{"numpy":"numpy"},
    "console_scripts":{
        "f2py":"numpy.f2py.f2py2e:main",
        "numpy-config":"numpy._configtool:main",
    },
    "pkg_config":{"numpy":"numpy._core.lib.pkgconfig"},
    "pyinstaller40":{"hook-dirs":"numpy:_pyinstaller_hooks_dir"},
}
if observed != expected:
    raise SystemExit(f"unexpected NumPy entry-point metadata: {observed!r}")
print("numpy_temporary_console_scripts_excluded=pass")
PY

site_packages="$output_runtime/lib/python3.14/site-packages"
[[ -d "$site_packages" && -z "$(find "$site_packages" -mindepth 1 -maxdepth 1 -print -quit)" ]] || die "core site-packages is not empty"
cp -a "$numpy_install/numpy" "$site_packages/numpy"
numpy_dist_info="$(find "$numpy_install" -mindepth 1 -maxdepth 1 -type d -name 'numpy-2.5.2.dist-info' -print -quit)"
cp -a "$numpy_dist_info" "$site_packages/$(basename "$numpy_dist_info")"
[[ ! -e "$output_runtime/bin/f2py" && ! -e "$output_runtime/bin/numpy-config" ]] || \
    die "NumPy console scripts leaked into selectively staged runtime"
private_lib_root="$output_runtime/lib/numpy-private"
mkdir -p "$private_lib_root"
cp -p "$openblas_provider" "$private_lib_root/$openblas_soname"
staged_provider="$private_lib_root/$openblas_soname"
mkdir -p "$output_runtime/LICENSES"
cp -p "$numpy_src/LICENSE.txt" "$output_runtime/LICENSES/NumPy.txt"
cp -p "$openblas_src/LICENSE" "$output_runtime/LICENSES/OpenBLAS.txt"

added_paths="$build/added-paths.txt"
"$helper_python" - "$output_runtime" "$core_identity" "$added_paths" <<'PY'
import json,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve(); core=json.loads(pathlib.Path(sys.argv[2]).read_text())
core_paths={r["path"] for r in core["files"]}|{r["path"] for r in core["symlinks"]}
added=[]
for p in sorted(root.rglob("*")):
    rel=p.relative_to(root).as_posix()
    if rel not in core_paths and (p.is_file() or p.is_symlink()): added.append(rel)
pathlib.Path(sys.argv[3]).write_text("\n".join(added)+"\n",encoding="utf-8")
PY
"$helper_python" - "$output_runtime" "$added_paths" "$private_lib_root" <<'PY'
import os,pathlib,subprocess,sys
root=pathlib.Path(sys.argv[1]).resolve(); private=pathlib.Path(sys.argv[3]).resolve()
added=[x for x in pathlib.Path(sys.argv[2]).read_text().splitlines() if x]
def elf(p):
    try:return p.is_file() and not p.is_symlink() and p.open("rb").read(4)==b"\x7fELF"
    except OSError:return False
count=0
for rel in added:
    p=root/rel
    if not elf(p):continue
    relative=os.path.relpath(private,p.parent.resolve())
    entries=["$ORIGIN"]; provider="$ORIGIN" if relative=="." else f"$ORIGIN/{relative}"
    if provider not in entries:entries.append(provider)
    subprocess.check_call(["patchelf","--set-rpath",":".join(entries),str(p)]); count+=1
if not count:raise SystemExit("NumPy overlay contains no ELF objects")
print(f"numpy_relocated_elf_count={count}")
PY

"$helper_python" - "$output_runtime" "$core_identity" <<'PY'
import hashlib,json,os,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve(); core=json.loads(pathlib.Path(sys.argv[2]).read_text())
def sha(p):
    h=hashlib.sha256()
    with p.open("rb") as s:
        for b in iter(lambda:s.read(1024*1024),b""):h.update(b)
    return h.hexdigest()
for r in core["files"]:
    p=root/r["path"]
    if p.is_symlink() or not p.is_file() or p.stat().st_size!=r["size"] or sha(p)!=r["sha256"] or (p.stat().st_mode & 0o7777)!=r["mode"]:raise SystemExit(f"inherited core file changed: {r['path']}")
for r in core["symlinks"]:
    p=root/r["path"]
    if not p.is_symlink() or os.readlink(p)!=r["target"] or (p.lstat().st_mode & 0o7777)!=r["mode"]:raise SystemExit(f"inherited core symlink changed: {r['path']}")
print("inherited_core_identity=pass")
PY

mkdir -p "$build/isolated-home"
env -i HOME="$build/isolated-home" PATH="/usr/bin:/bin" PYTHONDONTWRITEBYTECODE=1 \
    "$output_runtime/bin/python3.14" -I - "$output_runtime" "$staged_provider" <<'PY'
import pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve(); provider=pathlib.Path(sys.argv[2]).resolve()
import numpy as np
if np.__version__!="2.5.2":raise SystemExit(f"unexpected NumPy version: {np.__version__}")
if root not in pathlib.Path(np.__file__).resolve().parents:raise SystemExit("NumPy escaped runtime")
a=np.array([[1.,2.],[3.,4.]]); b=np.array([[5.,6.],[7.,8.]])
if not np.allclose(a@b,np.array([[19.,22.],[43.,50.]])):raise SystemExit("matrix multiply failed")
if not np.allclose(np.linalg.solve(np.array([[3.,1.],[1.,2.]]),np.array([9.,8.])),np.array([2.,3.])):raise SystemExit("linalg solve failed")
mapped=set()
for line in pathlib.Path("/proc/self/maps").read_text(errors="replace").splitlines():
    fields=line.split()
    if fields and "/" in fields[-1]:
        try:mapped.add(pathlib.Path(fields[-1]).resolve())
        except OSError:pass
if provider not in mapped:raise SystemExit("private OpenBLAS was not mapped")
for p in mapped:
    if "openblas" in p.name.lower() and p!=provider:raise SystemExit(f"competing OpenBLAS mapped: {p}")
print("numpy_blas_lapack_smoke=pass")
PY

# Keep the staged payload deterministic after import smoke. The core profile had
# an empty site-packages directory, so every cache below would be optional-layer
# state rather than an inherited core path.
find "$site_packages" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$site_packages" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

payload_manifest="$output_runtime/numpy-payload-manifest-linux.json"
provenance="$output_runtime/build-provenance-linux-numpy.json"
rm -f "$payload_manifest" "$provenance"

"$helper_python" - "$output_runtime" "$core_identity" "$payload_manifest" <<'PY'
import hashlib
import json
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1]).resolve()
core = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
output = pathlib.Path(sys.argv[3]).resolve()
core_paths = {item["path"] for item in core["files"]} | {item["path"] for item in core["symlinks"]}

def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

files = []
symlinks = []
aggregate = hashlib.sha256()
for path in sorted(root.rglob("*")):
    rel = path.relative_to(root).as_posix()
    if rel in core_paths or path == output:
        continue
    if path.is_symlink():
        target = os.readlink(path)
        mode = stat.S_IMODE(path.lstat().st_mode)
        symlinks.append({"path": rel, "kind": "symlink", "target": target, "mode": mode})
        aggregate.update(b"L\0" + rel.encode() + b"\0" + oct(mode).encode() + b"\0" + target.encode() + b"\n")
    elif path.is_file():
        digest = sha256(path)
        size = path.stat().st_size
        mode = stat.S_IMODE(path.stat().st_mode)
        files.append({"path": rel, "kind": "file", "sha256": digest, "size": size, "mode": mode})
        aggregate.update(
            b"F\0" + rel.encode() + b"\0" + oct(mode).encode() + b"\0" +
            str(size).encode() + b"\0" + digest.encode() + b"\n"
        )
    elif not path.is_dir():
        raise SystemExit(f"unsupported NumPy payload entry: {rel}")

if not files:
    raise SystemExit("NumPy payload manifest would be empty")
data = {
    "format_version": 1,
    "runtime_profile": "numpy",
    "tree_sha256": aggregate.hexdigest(),
    "file_count": len(files),
    "symlink_count": len(symlinks),
    "files": files,
    "symlinks": symlinks,
}
output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"numpy_payload_tree_sha256={data['tree_sha256']}")
PY

"$helper_python" - \
    "$output_runtime" "$core_identity" "$core_provenance" "$verified_inputs" \
    "$sources_manifest" "$payload_manifest" "$staged_provider" "$openblas_soname" \
    "$dev_cc" "$dev_cxx" "$dev_ld" "$build/pep517-tool-versions.json" \
    "$built_numpy_wheel" "$provenance" <<'PY'
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys

(
    runtime_arg, core_identity_arg, core_provenance_arg, verified_inputs_arg,
    sources_manifest_arg, payload_manifest_arg, provider_arg, provider_soname,
    cc_arg, cxx_arg, ld_arg, pep517_versions_arg, built_wheel_arg, output_arg,
) = sys.argv[1:]
runtime = pathlib.Path(runtime_arg).resolve()
core_identity_path = pathlib.Path(core_identity_arg).resolve()
core_provenance_path = pathlib.Path(core_provenance_arg).resolve()
verified_inputs_path = pathlib.Path(verified_inputs_arg).resolve()
sources_manifest_path = pathlib.Path(sources_manifest_arg).resolve()
payload_manifest_path = pathlib.Path(payload_manifest_arg).resolve()
provider = pathlib.Path(provider_arg).resolve()
cc = pathlib.Path(cc_arg).resolve()
cxx = pathlib.Path(cxx_arg).resolve()
ld = pathlib.Path(ld_arg).resolve()
pep517_versions_path = pathlib.Path(pep517_versions_arg).resolve()
built_wheel = pathlib.Path(built_wheel_arg).resolve()
output = pathlib.Path(output_arg).resolve()

def fail(message):
    raise SystemExit(message)

def sha256(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def under(path, root):
    path = pathlib.Path(path).resolve()
    root = pathlib.Path(root).resolve()
    return path == root or root in path.parents

def run(*args, env=None):
    proc = subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        env=env,
    )
    if proc.returncode:
        fail(f"command failed ({proc.returncode}): {' '.join(map(str, args))}\n{proc.stdout}")
    return proc.stdout

def is_elf(path):
    try:
        return path.is_file() and not path.is_symlink() and path.open("rb").read(4) == b"\x7fELF"
    except OSError:
        return False

def dynamic(path):
    text = run("readelf", "-dW", str(path))
    needed = re.findall(r"Shared library: \[([^\]]+)\]", text)
    soname_match = re.search(r"Library soname: \[([^\]]+)\]", text)
    soname = soname_match.group(1) if soname_match else None
    runpaths = re.findall(r"\((?:RUNPATH|RPATH)\).*?\[([^\]]*)\]", text)
    return needed, soname, runpaths

def version_needs(path):
    text = run("readelf", "--version-info", "-W", str(path))
    return sorted(set(re.findall(r"\bName: ((?:GLIBC|CXXABI|GLIBCXX|GCC)_[^\s]+)", text)))

def check_symbol_ceilings(path, names):
    ceilings = {
        "GLIBC": (2, 17),
        "CXXABI": (1, 3, 7),
        "GLIBCXX": (3, 4, 19),
        "GCC": (4, 8, 0),
    }
    for item in names:
        if item == "CXXABI_TM_1":
            continue
        matched = False
        for prefix, ceiling in ceilings.items():
            marker = prefix + "_"
            if not item.startswith(marker):
                continue
            matched = True
            suffix = item[len(marker):]
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", suffix):
                fail(f"unsupported ABI version requirement in {path.relative_to(runtime)}: {item}")
            value = tuple(int(part) for part in suffix.split("."))
            padded = value + (0,) * max(0, len(ceiling) - len(value))
            if padded > ceiling:
                fail(
                    f"{path.relative_to(runtime)} exceeds {prefix}_{'.'.join(map(str, ceiling))}: {item}"
                )
            break
        if not matched:
            fail(f"unrecognized ABI version namespace in {path.relative_to(runtime)}: {item}")

def ldd_map(path):
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    text = run("ldd", str(path), env=env)
    if "not found" in text:
        fail(f"unresolved dependency for {path.relative_to(runtime)}:\n{text}")
    result = {}
    for line in text.splitlines():
        match = re.match(r"\s*(\S+)\s+=>\s+(\S+)\s+\(", line)
        if match:
            result[match.group(1)] = match.group(2)
            continue
        match = re.match(r"\s*(/\S+)\s+\(", line)
        if match:
            result[pathlib.Path(match.group(1)).name] = match.group(1)
    return result

core_identity = json.loads(core_identity_path.read_text(encoding="utf-8"))
core_provenance = json.loads(core_provenance_path.read_text(encoding="utf-8"))
verified_inputs = json.loads(verified_inputs_path.read_text(encoding="utf-8"))
sources_manifest = json.loads(sources_manifest_path.read_text(encoding="utf-8"))
payload_manifest = json.loads(payload_manifest_path.read_text(encoding="utf-8"))
pep517_versions = json.loads(pep517_versions_path.read_text(encoding="utf-8"))
baseline = core_provenance.get("baseline", {})
pep599 = set(baseline.get("pep599_external_allowlist", []))
if not pep599:
    fail("core provenance has no PEP 599 allowlist")
glibc_loader = core_provenance.get("native_providers", {}).get("glibc_loader_external", {})
if (
    glibc_loader.get("kind") != "glibc-loader-external"
    or glibc_loader.get("image") != baseline.get("image")
    or glibc_loader.get("needed_name") != "ld-linux-x86-64.so.2"
    or glibc_loader.get("interpreter_path") != "/lib64/ld-linux-x86-64.so.2"
):
    fail(f"qualified core glibc-loader provenance is invalid: {glibc_loader!r}")
loader_resolved = pathlib.Path(glibc_loader.get("resolved_path", "")).resolve()
if not loader_resolved.is_file() or sha256(loader_resolved) != glibc_loader.get("resolved_sha256"):
    fail("qualified core glibc-loader provider is missing or has changed")
loader_owner = subprocess.run(
    ["rpm", "-qf", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\t%{LICENSE}\n", str(loader_resolved)],
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
if loader_owner.returncode != 0:
    fail(f"unable to re-derive glibc loader RPM identity: {loader_resolved}")
loader_owner_fields = loader_owner.stdout.rstrip("\n").split("\t")
if len(loader_owner_fields) != 4 or loader_owner_fields[0] != "glibc":
    fail(f"unexpected glibc loader RPM ownership: {loader_owner.stdout!r}")
loader_package = {
    "name": loader_owner_fields[0],
    "version_release": loader_owner_fields[1],
    "arch": loader_owner_fields[2],
    "license": loader_owner_fields[3],
}
if glibc_loader.get("package") != loader_package:
    fail(f"qualified core glibc-loader RPM identity differs from pinned image: {glibc_loader.get('package')!r}")

core_paths = {item["path"] for item in core_identity["files"]} | {item["path"] for item in core_identity["symlinks"]}
files = []
symlinks = []
aggregate = hashlib.sha256()
for path in sorted(runtime.rglob("*")):
    rel = path.relative_to(runtime).as_posix()
    if rel in core_paths or path == output:
        continue
    if path.is_symlink():
        target = os.readlink(path)
        mode = stat.S_IMODE(path.lstat().st_mode)
        symlinks.append({"path": rel, "kind": "symlink", "target": target, "mode": mode})
        aggregate.update(b"L\0" + rel.encode() + b"\0" + oct(mode).encode() + b"\0" + target.encode() + b"\n")
    elif path.is_file():
        digest = sha256(path)
        size = path.stat().st_size
        mode = stat.S_IMODE(path.stat().st_mode)
        files.append({"path": rel, "kind": "file", "sha256": digest, "size": size, "mode": mode})
        aggregate.update(
            b"F\0" + rel.encode() + b"\0" + oct(mode).encode() + b"\0" +
            str(size).encode() + b"\0" + digest.encode() + b"\n"
        )
    elif not path.is_dir():
        fail(f"unsupported staged NumPy entry: {rel}")
added_paths = {item["path"] for item in files} | {item["path"] for item in symlinks}
if payload_manifest_path.relative_to(runtime).as_posix() not in added_paths:
    fail("payload manifest is not part of the staged optional payload")

all_elfs = []
added_elfs = []
dynamic_map = {}
provider_index = {}
for path in sorted(runtime.rglob("*")):
    if not is_elf(path):
        continue
    all_elfs.append(path)
    rel = path.relative_to(runtime).as_posix()
    needed, soname, runpaths = dynamic(path)
    dynamic_map[path] = (needed, soname, runpaths)
    provider_index.setdefault(path.name, []).append(path)
    if soname:
        provider_index.setdefault(soname, []).append(path)
    if rel in added_paths:
        added_elfs.append(path)
if not added_elfs:
    fail("NumPy optional payload has no native ELF objects")

census = []
measured_versions = set()
added_glibc_loader_consumers = []
for path in added_elfs:
    rel = path.relative_to(runtime).as_posix()
    header = run("readelf", "-hW", str(path))
    machine_match = re.search(r"^\s*Machine:\s*(.+)$", header, re.MULTILINE)
    class_match = re.search(r"^\s*Class:\s*(.+)$", header, re.MULTILINE)
    machine = machine_match.group(1).strip() if machine_match else ""
    elf_class = class_match.group(1).strip() if class_match else ""
    if machine != "Advanced Micro Devices X86-64" or elf_class != "ELF64":
        fail(f"added payload contains non-x86_64 ELF: {rel}")
    needed, soname, runpaths = dynamic_map[path]
    for packed in runpaths:
        for entry in packed.split(":"):
            if not entry.startswith("$ORIGIN"):
                fail(f"added ELF has non-relative RUNPATH: {rel}: {entry!r}")
            suffix = entry[len("$ORIGIN"):]
            if suffix and not suffix.startswith("/"):
                fail(f"added ELF has malformed RUNPATH: {rel}: {entry!r}")
            if not under(path.parent / suffix.lstrip("/"), runtime):
                fail(f"added ELF RUNPATH escapes runtime: {rel}: {entry!r}")
    required_versions = version_needs(path)
    check_symbol_ceilings(path, required_versions)
    measured_versions.update(required_versions)
    resolved = ldd_map(path)
    dependencies = []
    bundled_dirs = set()
    for name in needed:
        actual = resolved.get(name)
        if name == glibc_loader["needed_name"]:
            if actual is None:
                fail(f"glibc loader dependency did not resolve: {rel} -> {name}")
            actual_path = pathlib.Path(actual).resolve()
            if actual_path != loader_resolved or under(actual_path, runtime):
                fail(f"glibc loader dependency resolved incorrectly: {rel} -> {name} -> {actual_path}")
            dependencies.append(
                {
                    "name": name,
                    "resolution": "glibc-loader-external",
                    "provider": str(loader_resolved),
                    "provider_sha256": glibc_loader["resolved_sha256"],
                    "package": loader_package,
                }
            )
            added_glibc_loader_consumers.append(rel)
            continue
        if name in pep599:
            if actual is None or under(actual, runtime):
                fail(f"PEP 599 dependency resolved incorrectly: {rel} -> {name} -> {actual}")
            dependencies.append({"name": name, "resolution": "pep599-external"})
            continue
        providers = provider_index.get(name, [])
        unique = {candidate.resolve() for candidate in providers}
        if len(unique) != 1:
            fail(f"non-PEP599 dependency has no unique bundled provider: {rel} -> {name}: {providers}")
        bundled = next(iter(unique))
        if actual is None or pathlib.Path(actual).resolve() != bundled:
            fail(f"bundled dependency resolved incorrectly: {rel} -> {name} -> {actual}")
        bundled_rel = bundled.relative_to(runtime).as_posix()
        if bundled_rel in added_paths and bundled != provider:
            fail(f"unexpected added native provider: {rel} -> {name} -> {bundled_rel}")
        bundled_dirs.add(bundled.parent.resolve())
        dependencies.append(
            {
                "name": name,
                "resolution": "bundled",
                "provider": bundled_rel,
                "provider_sha256": sha256(bundled),
            }
        )
    if bundled_dirs:
        search_dirs = {
            (path.parent / entry[len("$ORIGIN"):].lstrip("/")).resolve()
            for packed in runpaths
            for entry in packed.split(":")
        }
        missing = bundled_dirs - search_dirs
        if missing:
            fail(f"RUNPATH does not cover bundled dependencies for {rel}: {sorted(map(str, missing))}")
    census.append(
        {
            "path": rel,
            "sha256": sha256(path),
            "machine": machine,
            "elf_class": elf_class,
            "needed": needed,
            "soname": soname,
            "runpath": runpaths,
            "dependencies": dependencies,
            "required_versions": required_versions,
        }
    )

provider_rel = provider.relative_to(runtime).as_posix()
provider_record = next((item for item in census if item["path"] == provider_rel), None)
if provider_record is None or provider_record["soname"] != provider_soname:
    fail("private OpenBLAS provider is missing from final ELF census")
symbols = run("nm", "-D", "--defined-only", str(provider))
defined = {line.split()[-1].split("@", 1)[0] for line in symbols.splitlines() if line.split()}
for required_provider_symbol in (
    "dgemm_bridge_np252_",
    "dgesv_bridge_np252_",
    "cblas_dgemmbridge_np252_",
):
    if required_provider_symbol not in defined:
        fail(f"private OpenBLAS is missing exact collision-safe symbol: {required_provider_symbol}")
corenames = sorted(name for name in defined if "openblas_get_corename" in name)
private_corenames = [name for name in corenames if "bridge_np252_" in name]
if len(private_corenames) == 1:
    verification_symbol = private_corenames[0]
elif len(corenames) == 1:
    verification_symbol = corenames[0]
else:
    fail(f"private OpenBLAS has no unique verification symbol: {corenames}")

numpy_root = runtime / "lib/python3.14/site-packages/numpy"
numpy_undefined_blas = set()
blas_symbol_roots = ("dgemm", "sgemm", "dgesv", "sgesv", "dgetrf", "dgetrs", "cblas_", "lapacke_")
for record in census:
    path = runtime / record["path"]
    if not under(path, numpy_root):
        continue
    symbol_text = run("readelf", "-Ws", str(path))
    for line in symbol_text.splitlines():
        if " UND " not in f" {line} ":
            continue
        fields = line.split()
        if not fields:
            continue
        name = fields[-1].split("@", 1)[0]
        lower = name.lower()
        if any(root in lower for root in blas_symbol_roots):
            numpy_undefined_blas.add(name)
if "dgesv_bridge_np252_" not in numpy_undefined_blas:
    fail(
        "NumPy native extensions do not request the exact OpenBLAS LP64 LAPACK symbol "
        "dgesv_bridge_np252_"
    )
if "dgesvbridge_np252__" in numpy_undefined_blas:
    fail("NumPy native extensions retain the incompatible LP64 LAPACK suffix spelling")

def tool_record(path):
    version = run(str(path), "--version").strip()
    return {"path": str(path), "sha256": sha256(path), "version_output": version}

data = {
    "format_version": 1,
    "runtime_profile": "numpy",
    "baseline": baseline,
    "source_manifest": {
        "path": "locks/linux/linux-glibc217-numpy-sources.json",
        "sha256": sha256(sources_manifest_path),
        "contract": sources_manifest,
    },
    "closed_inputs": verified_inputs,
    "core": {
        "provenance_path": "build-provenance-linux.json",
        "provenance_sha256": sha256(core_provenance_path),
        "tree_identity": core_identity,
    },
    "numpy": {
        "version": "2.5.2",
        "source_filename": "numpy-2.5.2.tar.gz",
        "source_sha256": verified_inputs["sources"]["numpy"]["verified_sha256"],
        "built_wheel_filename": built_wheel.name,
        "built_wheel_sha256": sha256(built_wheel),
    },
    "provider": {
        "name": "OpenBLAS",
        "version": "0.3.33",
        "source_filename": "OpenBLAS-0.3.33.tar.gz",
        "source_sha256": verified_inputs["sources"]["openblas"]["verified_sha256"],
        "strategy": "private SONAME/library identity plus private BLAS/LAPACK symbol suffix",
        "path": provider_rel,
        "sha256": sha256(provider),
        "soname": provider_soname,
        "symbol_suffix": "bridge_np252_",
        "library_name_suffix": "bridge_np252_",
        "verification_symbol": verification_symbol,
        "collision_surface": ["dgemm_", "dgesv_", "cblas_dgemm", "openblas_get_corename"],
    },
    "build": {
        "network_after_input_verification": "disabled-by-container",
        "pep517_build_isolation": False,
        "dependency_resolution": False,
        "numpy_setup_args": sources_manifest["numpy_build"]["meson_setup_args"],
        "numpy_extra_compile_defines": sources_manifest["numpy_build"]["extra_compile_defines"],
        "openblas_suffix_placement": sources_manifest["numpy_build"]["openblas_suffix_placement"],
        "numpy_cflags": "-O2 -fPIC -DOPENBLAS_ILP64_NAMING_SCHEME",
        "numpy_cxxflags": "-O2 -fPIC -D_GLIBCXX_USE_CXX11_ABI=0 -DOPENBLAS_ILP64_NAMING_SCHEME",
        "numpy_ldflags": "-Wl,-z,relro,-z,now",
        "numpy_undefined_blas_symbols": sorted(numpy_undefined_blas),
        "openblas_make_options": [
            f"{key}={value}" for key, value in sources_manifest["openblas_build"]["make_options"].items()
        ],
        "provider_discovery": sources_manifest["provider_discovery"],
        "provider_discovery_guard": {
            "cmake_fallback_poisoned": True,
            "cmake_fallback_attempted": False,
        },
        "installed_metadata_normalization": {
            "direct_url_json": "removed",
            "record_direct_url_entry": "removed",
            "reason": "exclude pip local-wheel path/hash metadata from deterministic runtime payload",
        },
        "pep517_tools": pep517_versions,
        "devtoolset_11": {
            "gcc": tool_record(cc),
            "gxx": tool_record(cxx),
            "ld": tool_record(ld),
            "rpms": verified_inputs["devtoolset_rpms"],
        },
    },
    "payload": {
        "manifest_path": "numpy-payload-manifest-linux.json",
        "manifest_sha256": sha256(payload_manifest_path),
        "manifest_tree_sha256": payload_manifest["tree_sha256"],
        "tree_sha256": aggregate.hexdigest(),
        "file_count": len(files),
        "symlink_count": len(symlinks),
        "files": files,
        "symlinks": symlinks,
    },
    "elf_census": census,
    "abi": {
        "symbol_ceilings": baseline["symbol_ceilings"],
        "measured_required_versions": sorted(measured_versions),
        "added_elf_count": len(census),
        "glibc_loader_external": {
            "needed_name": glibc_loader["needed_name"],
            "interpreter_path": glibc_loader["interpreter_path"],
            "resolved_path": str(loader_resolved),
            "resolved_sha256": glibc_loader["resolved_sha256"],
            "package": loader_package,
            "added_consumers": sorted(set(added_glibc_loader_consumers)),
        },
    },
    "licenses": [
        {"component": "NumPy 2.5.2", "path": "LICENSES/NumPy.txt", "sha256": sha256(runtime / "LICENSES/NumPy.txt")},
        {"component": "OpenBLAS 0.3.33", "path": "LICENSES/OpenBLAS.txt", "sha256": sha256(runtime / "LICENSES/OpenBLAS.txt")},
    ],
}
output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"numpy_provenance_payload_tree_sha256={data['payload']['tree_sha256']}")
print(f"numpy_provenance_added_elf_count={len(census)}")
PY

# The core input itself is a release artifact handoff, not a scratch build tree.
# Recompute its complete identity after every use of its CPython so accidental
# bytecode/header/library mutations fail this phase rather than contaminating the
# optional overlay lineage.
"$helper_python" - "$core_runtime" "$core_identity" <<'PY'
import hashlib
import json
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1]).resolve()
expected = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))

def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

observed = set()
for item in expected["files"]:
    path = root / item["path"]
    observed.add(item["path"])
    if (
        path.is_symlink() or not path.is_file() or path.stat().st_size != item["size"]
        or sha256(path) != item["sha256"] or stat.S_IMODE(path.stat().st_mode) != item["mode"]
    ):
        raise SystemExit(f"original core input changed: {item['path']}")
for item in expected["symlinks"]:
    path = root / item["path"]
    observed.add(item["path"])
    if (
        not path.is_symlink() or os.readlink(path) != item["target"]
        or stat.S_IMODE(path.lstat().st_mode) != item["mode"]
    ):
        raise SystemExit(f"original core symlink changed: {item['path']}")
actual = {
    path.relative_to(root).as_posix()
    for path in root.rglob("*")
    if path.is_file() or path.is_symlink()
}
if actual != observed:
    raise SystemExit(
        "original core path set changed: "
        f"missing={sorted(observed-actual)}, extra={sorted(actual-observed)}"
    )
print("original_core_identity_final=pass")
PY

printf 'linux_glibc217_numpy_runtime=%s\n' "$output_runtime"
printf 'linux_glibc217_numpy_payload_manifest=%s\n' "$payload_manifest"
printf 'linux_glibc217_numpy_provenance=%s\n' "$provenance"
echo "linux_glibc217_numpy_runtime_build=pass"
