#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat >&2 <<'EOF'
usage: scripts/linux/test-numpy-runtime.sh \
  --core-runtime <qualified-core-runtime-root> \
  --runtime <numpy-runtime-root> \
  --sources-manifest <linux-glibc217-numpy-sources.json> \
  --input-root <verified-input-directory> \
  --image <manylinux2014@sha256:digest>
EOF
    exit 2
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
runtime=""
core_runtime=""
sources_manifest=""
input_root=""
expected_image="quay.io/pypa/manylinux2014_x86_64@sha256:296069bc05d85275248f168f6e3b34ba8aa06181b67e40cd7447c64d56d3a851"
image=""

while (($#)); do
    case "$1" in
        --runtime)
            (($# >= 2)) || usage
            runtime="$2"
            shift 2
            ;;
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
        --input-root)
            (($# >= 2)) || usage
            input_root="$2"
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

[[ -n "$runtime" && -n "$core_runtime" && -n "$sources_manifest" && -n "$input_root" && -n "$image" ]] || usage
[[ "$image" == "$expected_image" ]] || {
    echo "NumPy runtime qualification must use the pinned manylinux2014 image: $expected_image" >&2
    exit 1
}
[[ "${PY_RUNTIME_BUILD_IMAGE_REF:-}" == "$expected_image" ]] || {
    echo "PY_RUNTIME_BUILD_IMAGE_REF must identify the executing pinned image: $expected_image" >&2
    exit 1
}

for tool_name in getconf uname readelf ldd nm sha256sum find grep sed rpm; do
    command -v "$tool_name" >/dev/null 2>&1 || {
        echo "required NumPy runtime qualification tool is missing: $tool_name" >&2
        exit 1
    }
done

[[ "$(uname -m)" == "x86_64" ]] || {
    echo "NumPy runtime qualification host must be x86_64; observed $(uname -m)" >&2
    exit 1
}
glibc_identity="$(getconf GNU_LIBC_VERSION)"
[[ "$glibc_identity" == "glibc 2.17" ]] || {
    echo "NumPy runtime qualification must execute on glibc 2.17; observed $glibc_identity" >&2
    exit 1
}

[[ -d "$runtime" ]] || {
    echo "NumPy runtime root is missing: $runtime" >&2
    exit 1
}
[[ -d "$core_runtime" ]] || {
    echo "qualified core runtime root is missing: $core_runtime" >&2
    exit 1
}
[[ -f "$sources_manifest" ]] || {
    echo "NumPy source manifest is missing: $sources_manifest" >&2
    exit 1
}
[[ -d "$input_root" ]] || {
    echo "verified NumPy input root is missing: $input_root" >&2
    exit 1
}
runtime="$(cd "$runtime" && pwd -P)"
core_runtime="$(cd "$core_runtime" && pwd -P)"
sources_manifest="$(cd "$(dirname "$sources_manifest")" && pwd -P)/$(basename "$sources_manifest")"
input_root="$(cd "$input_root" && pwd -P)"
[[ "$runtime" != "$core_runtime" ]] || {
    echo "NumPy runtime must be a separate overlay tree, not the core runtime itself" >&2
    exit 1
}

provenance="$runtime/build-provenance-linux-numpy.json"
[[ -f "$provenance" ]] || {
    echo "NumPy runtime provenance is missing: $provenance" >&2
    exit 1
}
provenance="$(cd "$(dirname "$provenance")" && pwd -P)/$(basename "$provenance")"

core_provenance="$core_runtime/build-provenance-linux.json"
copied_core_provenance="$runtime/build-provenance-linux.json"
audit_python="$core_runtime/bin/python3.14"
python="$runtime/bin/python3.14"
site_packages="$runtime/lib/python3.14/site-packages"

required_paths=("$core_provenance" "$copied_core_provenance" "$audit_python" "$python" "$site_packages")
for required_path in "${required_paths[@]}"; do
    [[ -e "$required_path" ]] || {
        echo "NumPy runtime qualification input is missing: $required_path" >&2
        exit 1
    }
done

if find "$runtime" -mindepth 1 -type f \( -iname '*.mex' -o -iname '*.mexa64' -o -iname '*.mexw64' -o -iname '*.mexmaci64' -o -iname '*.mex*' \) -print -quit | grep -q .; then
    echo "phase-01 NumPy runtime must not contain any MEX binary" >&2
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
for provider_name in libopenblas.so libopenblas.so.0 libblas.so libblas.so.3 liblapack.so liblapack.so.3; do
    printf 'ambient native-provider poison: %s\n' "$provider_name" > "$poison/native/$provider_name"
done

audit_output="$poison/audit.txt"
"$audit_python" -I -S - "$runtime" "$core_runtime" "$provenance" "$sources_manifest" "$input_root" "$expected_image" <<'PY' | tee "$audit_output"
import csv
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import zipfile

runtime = pathlib.Path(sys.argv[1]).resolve()
core = pathlib.Path(sys.argv[2]).resolve()
provenance_path = pathlib.Path(sys.argv[3]).resolve()
sources_manifest_path = pathlib.Path(sys.argv[4]).resolve()
input_root = pathlib.Path(sys.argv[5]).resolve()
expected_image = sys.argv[6]
core_provenance_path = core / "build-provenance-linux.json"
copied_core_provenance_path = runtime / "build-provenance-linux.json"
payload_manifest_path = runtime / "numpy-payload-manifest-linux.json"

PEP599_EXTERNAL = {
    "libgcc_s.so.1", "libstdc++.so.6", "libm.so.6", "libdl.so.2",
    "librt.so.1", "libc.so.6", "libnsl.so.1", "libutil.so.1",
    "libpthread.so.0", "libresolv.so.2", "libX11.so.6", "libXext.so.6",
    "libXrender.so.1", "libICE.so.6", "libSM.so.6", "libGL.so.1",
    "libgobject-2.0.so.0", "libgthread-2.0.so.0", "libglib-2.0.so.0",
}
EXPECTED_CEILINGS = {
    "GLIBC": "2.17",
    "CXXABI": "1.3.7",
    "CXXABI_additional_allowed": ["CXXABI_TM_1"],
    "GLIBCXX": "3.4.19",
    "GCC": "4.8.0",
}
EXPECTED_PIN_HASHES = {
    "318b9a4c845dbea06708a29c84ee429cc3065048db34cdb799047643492050ee",
    "d482d171c406ae88c5b19cad3b6a1c4c5209f886ab74bc44c2c865c23f52d860",
    "6761af1d9f5d353ab4f0b7497be2643313b36c8f31caec0144bfef198e71e6ab",
    "9655943313a94722b7774661c21049070f6bbb0a1516bf02f7c8d5d9201514cd",
    "3b0fe051551cc238f5febb873247c0949cd60ded556efa130aa57021804868e2",
    "ef02b806ce0c5b6becd5bb5dc9fa67662320b29b337e7ace73e4354500590233",
    "d13025b34f72f77bf7f65c1cd628914763e6c285f4deb934314c922b91e6be5a",
    "29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484",
    "ee5efde548c3ed9b75a354fc319d5afd25e9585fa918a34f62f904cc731973ad",
    "096487995473320de7f65d622c3f1d16c3ad174797602218ca8c967f51ec38a0",
    "ac350205a9316c777f842fa3451dc786ba9b058ca997776b73e4aa221908c84e",
    "dde2f4b967e4a379caf1c38f756029799d610d8911f0c13b1f4f94a5d7a93561",
    "7049c0fdcee4412cf0e2570e2ed90e4879019aa58b9c7d1112e85260ead0ec30",
    "adbf4b0743fa1227da7d93ef8236e20aa7ea5bda67965baadf782095bfee540f",
    "b42d3c7ff9f4da0f7fedd354051b002fe68dbb56dc59c713c61bdf92773b3d9c",
}
PRIVATE_SUFFIX = "bridge_np252_"
EXPECTED_SOURCE_MANIFEST_SHA256 = "81ebc2c98319683a8e523889db8275069bfeab9c8d0012e3fa3c01ca886bf687"


def fail(message):
    raise SystemExit(message)


def sha256(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
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
        return (
            path.is_file()
            and not path.is_symlink()
            and path.open("rb").read(4) == b"\x7fELF"
        )
    except OSError:
        return False


def tree_identity(root):
    result = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_symlink():
            result[rel] = {
                "path": rel,
                "kind": "symlink",
                "target": os.readlink(path),
                "mode": mode,
            }
        elif path.is_file():
            result[rel] = {
                "path": rel,
                "kind": "file",
                "sha256": sha256(path),
                "size": info.st_size,
                "mode": mode,
            }
        elif not path.is_dir():
            fail(f"unsupported non-file runtime entry: {path}")
    return result


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)


def dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from dicts(item)


def normalize_manifest_record(record):
    if not isinstance(record, dict):
        return None
    rel = record.get("path")
    if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in pathlib.PurePosixPath(rel).parts:
        return None
    digest = record.get("sha256")
    target = record.get("target", record.get("symlink_target"))
    kind = record.get("kind", record.get("type"))
    if isinstance(target, str) or kind == "symlink":
        if not isinstance(target, str):
            return None
        normalized = {"path": rel, "kind": "symlink", "target": target}
        if isinstance(record.get("mode"), int):
            normalized["mode"] = record["mode"]
        return normalized
    if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
        normalized = {"path": rel, "kind": "file", "sha256": digest}
        if isinstance(record.get("size"), int):
            normalized["size"] = record["size"]
        if isinstance(record.get("mode"), int):
            normalized["mode"] = record["mode"]
        return normalized
    return None


def collect_manifest_candidates(value, trail=()):
    candidates = []
    if isinstance(value, dict):
        combined = []
        for key in ("files", "symlinks", "entries", "manifest"):
            item = value.get(key)
            if isinstance(item, list):
                normalized = [normalize_manifest_record(entry) for entry in item]
                normalized = [entry for entry in normalized if entry is not None]
                if normalized:
                    combined.extend(normalized)
        if combined:
            candidates.append((trail, combined))
        for key, item in value.items():
            candidates.extend(collect_manifest_candidates(item, trail + (str(key),)))
    elif isinstance(value, list):
        normalized = [normalize_manifest_record(entry) for entry in value]
        if normalized and all(entry is not None for entry in normalized):
            candidates.append((trail, normalized))
        for index, item in enumerate(value):
            candidates.extend(collect_manifest_candidates(item, trail + (str(index),)))
    return candidates


def manifest_map(records):
    result = {}
    for record in records:
        if record is None:
            return None
        rel = record["path"]
        if rel in result:
            return None
        result[rel] = record
    return result


def record_matches(actual, recorded):
    if actual["kind"] != recorded["kind"]:
        return False
    if "mode" in recorded and actual["mode"] != recorded["mode"]:
        return False
    if actual["kind"] == "symlink":
        return actual["target"] == recorded.get("target")
    if actual["sha256"] != recorded.get("sha256"):
        return False
    if "size" in recorded and actual["size"] != recorded["size"]:
        return False
    return True


def find_manifest_covering(data, expected, trail_words, label, exact=False):
    candidates = collect_manifest_candidates(data)
    preferred = []
    fallback = []
    for trail, records in candidates:
        mapping = manifest_map(records)
        if mapping is None:
            continue
        if exact and set(mapping) != set(expected):
            continue
        if not set(expected).issubset(mapping):
            continue
        if not all(record_matches(record, mapping[rel]) for rel, record in expected.items()):
            continue
        lowered = "/".join(trail).lower()
        target = preferred if any(word in lowered for word in trail_words) else fallback
        target.append((trail, mapping))
    matches = preferred or fallback
    if not matches:
        fail(f"provenance does not contain an exact {label} manifest")
    return matches[0]


def dynamic(path):
    text = run("readelf", "-dW", str(path))
    needed = re.findall(r"Shared library: \[([^\]]+)\]", text)
    soname_match = re.search(r"Library soname: \[([^\]]+)\]", text)
    soname = soname_match.group(1) if soname_match else None
    runpaths = re.findall(r"\((?:RUNPATH|RPATH)\).*?\[([^\]]*)\]", text)
    return needed, soname, runpaths


def version_needs(path):
    text = run("readelf", "--version-info", "-W", str(path))
    names = set(re.findall(r"\bName: ((?:GLIBC|CXXABI|GLIBCXX|GCC)_[^\s]+)", text))
    return names


def check_symbol_ceilings(path):
    ceilings = {
        "GLIBC": (2, 17),
        "CXXABI": (1, 3, 7),
        "GLIBCXX": (3, 4, 19),
        "GCC": (4, 8, 0),
    }
    for item in sorted(version_needs(path)):
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
                fail(f"unsupported {prefix} version requirement in {path.relative_to(runtime)}: {item}")
            value = tuple(int(part) for part in suffix.split("."))
            padded = value + (0,) * max(0, len(ceiling) - len(value))
            if padded > ceiling:
                fail(
                    f"{path.relative_to(runtime)} exceeds "
                    f"{prefix}_{'.'.join(map(str, ceiling))}: {item}"
                )
            break
        if not matched:
            fail(f"unrecognized version namespace in {path.relative_to(runtime)}: {item}")


def ldd_map(path):
    clean_env = os.environ.copy()
    clean_env.pop("LD_LIBRARY_PATH", None)
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
    return resolved


data = json.loads(provenance_path.read_text(encoding="utf-8"))
core_provenance = json.loads(core_provenance_path.read_text(encoding="utf-8"))
sources_manifest = json.loads(sources_manifest_path.read_text(encoding="utf-8"))
if not payload_manifest_path.is_file() or payload_manifest_path.is_symlink():
    fail(f"named NumPy payload manifest is missing or not a regular file: {payload_manifest_path}")
payload_manifest = json.loads(payload_manifest_path.read_text(encoding="utf-8"))
if not isinstance(data, dict):
    fail("NumPy runtime provenance root must be an object")
if core_provenance.get("format_version") != 2:
    fail("qualified core provenance format is unsupported")
if (
    not isinstance(payload_manifest, dict)
    or payload_manifest.get("format_version") != 1
    or payload_manifest.get("runtime_profile") != "numpy"
):
    fail("named NumPy payload manifest schema/profile is unsupported")

sources_manifest_sha = sha256(sources_manifest_path)
if sources_manifest_sha != EXPECTED_SOURCE_MANIFEST_SHA256:
    fail(
        "supplied NumPy source manifest differs from committed phase-01 manifest: "
        f"{sources_manifest_sha}"
    )
if sources_manifest.get("schema_version") != 1:
    fail("NumPy source manifest schema is unsupported")
target = sources_manifest.get("target", {})
if target != {
    "architecture": "x86_64",
    "python_version": "3.14.10",
    "python_abi": "cp314",
    "python_wheel_abi": "cp314-cp314",
    "platform_policy": "manylinux2014_x86_64 / manylinux_2_17_x86_64",
    "glibc_floor": "2.17",
}:
    fail(f"NumPy source manifest target policy is invalid: {target!r}")
build_image = sources_manifest.get("build_image", {})
if (
    build_image.get("repository") != "quay.io/pypa/manylinux2014_x86_64"
    or build_image.get("digest") != expected_image.rsplit("@", 1)[1]
    or build_image.get("ref") != expected_image
):
    fail(f"NumPy source manifest build-image pin is invalid: {build_image!r}")
expected_binary_tags = [
    "cp314-cp314-manylinux_2_27_x86_64",
    "cp314-cp314-manylinux_2_28_x86_64",
]
expected_binary_policy = {
    "reason": "declared-manylinux-glibc-floor-above-target",
    "target_glibc_floor": "2.17",
    "declared_glibc_floors": ["2.27", "2.28"],
}
binary_candidate = sources_manifest.get("binary_candidate", {})
if binary_candidate != {
    "name": "numpy",
    "version": "2.5.2",
    "filename": "numpy-2.5.2-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
    "url": "https://files.pythonhosted.org/packages/c7/99/461bd36dbdfac6c1c53efa370bd55a83227542d0d118f1677dbf1a3dacd5/numpy-2.5.2-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
    "sha256": "318b9a4c845dbea06708a29c84ee429cc3065048db34cdb799047643492050ee",
    "expected_wheel_tags": expected_binary_tags,
    "rejection_policy": expected_binary_policy,
}:
    fail(f"NumPy binary candidate lock differs from the required cp314/glibc evidence: {binary_candidate!r}")
candidate_path = input_root / binary_candidate["filename"]
if not candidate_path.is_file() or candidate_path.is_symlink():
    fail(f"NumPy binary candidate is missing or invalid: {candidate_path}")
if sha256(candidate_path) != binary_candidate["sha256"]:
    fail("NumPy binary candidate SHA-256 differs from the committed lock")
with zipfile.ZipFile(candidate_path) as archive:
    wheel_metadata = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
    if len(wheel_metadata) != 1:
        fail(f"NumPy binary candidate has unexpected WHEEL metadata paths: {wheel_metadata!r}")
    wheel_text = archive.read(wheel_metadata[0]).decode("utf-8")
observed_binary_tags = sorted(
    line.split(":", 1)[1].strip()
    for line in wheel_text.splitlines()
    if line.startswith("Tag:")
)
if observed_binary_tags != expected_binary_tags:
    fail(f"NumPy binary candidate WHEEL tags differ from the committed lock: {observed_binary_tags!r}")
observed_binary_floors = []
for tag in observed_binary_tags:
    match = re.search(r"manylinux_2_([0-9]+)_x86_64$", tag)
    if match is None:
        fail(f"NumPy binary candidate has unsupported platform tag: {tag}")
    observed_binary_floors.append(f"2.{int(match.group(1))}")
if observed_binary_floors != ["2.27", "2.28"] or any(
    int(value.split(".", 1)[1]) <= 17 for value in observed_binary_floors
):
    fail(f"NumPy binary candidate no longer proves incompatibility with glibc 2.17: {observed_binary_floors!r}")
if sources_manifest.get("build_policy") != {
    "network_disabled_after_input_verification": True,
    "pep517_build_isolation": False,
    "dependency_resolution": False,
    "index_access": False,
    "stage_build_tools_into_runtime": False,
    "host_native_tuning": "forbidden",
    "compiler": "devtoolset-11 GCC/G++ 11.2.1",
    "runtime_symbol_ceilings": {
        "GLIBC": "2.17",
        "CXXABI": "1.3.7",
        "CXXABI_extra_allowed": ["CXXABI_TM_1"],
        "GLIBCXX": "3.4.19",
        "GCC": "4.8.0",
    },
}:
    fail("NumPy source manifest build policy differs from the settled phase-01 contract")

numpy_build = sources_manifest.get("numpy_build", {})
required_meson_args = {
    "-Dblas=bridge-openblas",
    "-Dlapack=bridge-openblas",
    "-Dallow-noblas=false",
    "-Duse-ilp64=false",
    "-Dblas-symbol-suffix=bridge_np252_",
    "-Dcpu-baseline=none",
    "-Dcpu-baseline-detect=disabled",
    "-Dcpu-dispatch=X86_V2,X86_V3",
    "-Ddisable-highway=true",
    "-Ddisable-intel-sort=true",
    "-Ddisable-svml=true",
}
if (
    numpy_build.get("backend") != "mesonpy"
    or set(numpy_build.get("meson_setup_args", [])) != required_meson_args
    or numpy_build.get("extra_compile_defines") != ["OPENBLAS_ILP64_NAMING_SCHEME"]
    or numpy_build.get("openblas_suffix_placement") != "fortran-underscore-before-private-symbol-suffix"
    or numpy_build.get("cpu_policy") != {
        "baseline": "none",
        "baseline_detect": "disabled",
        "dispatch": ["X86_V2", "X86_V3"],
        "disabled_components": ["highway", "intel-x86-sort", "svml"],
    }
):
    fail("NumPy source manifest CPU/BLAS build policy differs from the settled contract")

openblas_build = sources_manifest.get("openblas_build", {})
expected_openblas_options = {
    "BINARY": "64",
    "TARGET": "OPTERON",
    "DYNAMIC_ARCH": "1",
    "DYNAMIC_OLDER": "1",
    "NO_AVX512": "1",
    "NOFORTRAN": "1",
    "USE_THREAD": "1",
    "USE_OPENMP": "0",
    "NO_AFFINITY": "1",
    "NUM_THREADS": "64",
    "CONSISTENT_FPCSR": "1",
    "INTERFACE64": "0",
    "LIBNAMESUFFIX": PRIVATE_SUFFIX,
    "SYMBOLSUFFIX": PRIVATE_SUFFIX,
}
if (
    openblas_build.get("interface") != "LP64"
    or openblas_build.get("make_options") != expected_openblas_options
    or openblas_build.get("runtime_identity", {}).get("symbol_suffix") != PRIVATE_SUFFIX
    or openblas_build.get("runtime_identity", {}).get("library_name_suffix") != PRIVATE_SUFFIX
    or openblas_build.get("baseline_probe_environment") != {
        "NPY_DISABLE_CPU_FEATURES": "X86_V2,X86_V3",
        "OPENBLAS_CORETYPE": "unset",
    }
):
    fail("OpenBLAS source manifest policy differs from the settled private-provider contract")

provider_discovery = sources_manifest.get("provider_discovery", {})
if (
    provider_discovery.get("provider") != "OpenBLAS 0.3.33"
    or provider_discovery.get("method") != "pkg-config-only"
    or provider_discovery.get("pkg_config_name") != "bridge-openblas"
    or provider_discovery.get("PKG_CONFIG_LIBDIR") != "<openblas-prefix>/lib/pkgconfig"
    or set(provider_discovery.get("cleared_environment", []))
       != {"PKG_CONFIG_PATH", "CMAKE_PREFIX_PATH", "BLAS", "LAPACK", "LD_LIBRARY_PATH", "LD_PRELOAD"}
    or provider_discovery.get("cmake_fallback") != "forbidden"
    or provider_discovery.get("ambient_blas_lapack") != "forbidden"
):
    fail("NumPy source manifest provider-discovery policy differs from the settled contract")
if sources_manifest.get("cpu_validation") != {
    "numpy_mask_newer_features": True,
    "openblas_runtime_core_selection": "autodetect",
    "reject_openblas_coretype_override": True,
}:
    fail("NumPy source manifest CPU validation policy differs from the settled contract")

build_record = data.get("build", {})
if build_record.get("provider_discovery") != provider_discovery:
    fail("NumPy provenance provider-discovery policy differs from the committed source manifest")
if build_record.get("numpy_extra_compile_defines") != ["OPENBLAS_ILP64_NAMING_SCHEME"]:
    fail("NumPy provenance is missing the OpenBLAS suffix-placement compile define")
if build_record.get("openblas_suffix_placement") != "fortran-underscore-before-private-symbol-suffix":
    fail("NumPy provenance records the wrong OpenBLAS suffix-placement policy")
if build_record.get("numpy_cflags") != "-O2 -fPIC -DOPENBLAS_ILP64_NAMING_SCHEME":
    fail("NumPy provenance records unexpected CFLAGS")
if build_record.get("numpy_cxxflags") != "-O2 -fPIC -D_GLIBCXX_USE_CXX11_ABI=0 -DOPENBLAS_ILP64_NAMING_SCHEME":
    fail("NumPy provenance records unexpected CXXFLAGS")
if build_record.get("provider_discovery_guard") != {
    "cmake_fallback_poisoned": True,
    "cmake_fallback_attempted": False,
}:
    fail("NumPy provenance does not prove the forbidden CMake discovery fallback stayed unused")
if build_record.get("installed_metadata_normalization") != {
    "direct_url_json": "removed",
    "record_direct_url_entry": "removed",
    "reason": "exclude pip local-wheel path/hash metadata from deterministic runtime payload",
}:
    fail("NumPy provenance does not record the required deterministic install-metadata normalization")
binary_rejection = data.get("binary_candidate_rejection", {})
if binary_rejection != {
    "filename": binary_candidate["filename"],
    "sha256": binary_candidate["sha256"],
    "observed_wheel_tags": observed_binary_tags,
    "target_glibc_floor": "2.17",
    "declared_glibc_floors": observed_binary_floors,
    "reason": "declared-manylinux-glibc-floor-above-target",
    "qualification_host": {
        "python_version": "3.14.10",
        "python_abi": "cp314",
        "architecture": "x86_64",
        "glibc": "2.17",
    },
    "decision": "reject-binary-and-authorize-source-rebuild",
}:
    fail(f"NumPy provenance does not bind binary-first rebuild authorization: {binary_rejection!r}")
closed_candidate = data.get("closed_inputs", {}).get("binary_candidate", {})
if (
    closed_candidate.get("filename") != binary_candidate["filename"]
    or closed_candidate.get("verified_sha256") != binary_candidate["sha256"]
    or closed_candidate.get("observed_wheel_tags") != observed_binary_tags
    or closed_candidate.get("rejection_policy") != expected_binary_policy
    or closed_candidate.get("qualification_host") != binary_rejection["qualification_host"]
    or closed_candidate.get("decision") != "reject-binary-and-authorize-source-rebuild"
):
    fail(f"NumPy closed inputs do not retain binary-candidate rejection evidence: {closed_candidate!r}")

site_packages_root = runtime / "lib/python3.14/site-packages"
dist_info_matches = sorted(site_packages_root.glob("numpy-2.5.2.dist-info"))
if len(dist_info_matches) != 1:
    fail(f"expected exactly one staged NumPy 2.5.2 dist-info directory: {dist_info_matches}")
dist_info = dist_info_matches[0]
direct_url = dist_info / "direct_url.json"
record = dist_info / "RECORD"
if direct_url.exists() or direct_url.is_symlink():
    fail("staged NumPy payload retains pip build-local direct_url.json metadata")
if not record.is_file() or record.is_symlink():
    fail(f"staged NumPy RECORD is missing or invalid: {record}")
with record.open("r", encoding="utf-8", newline="") as stream:
    record_rows = list(csv.reader(stream))
if not record_rows or any(len(row) != 3 for row in record_rows):
    fail("staged NumPy RECORD contains malformed rows")
direct_rel = f"{dist_info.name}/direct_url.json"
record_rel = f"{dist_info.name}/RECORD"
if any(row[0] == direct_rel for row in record_rows):
    fail("staged NumPy RECORD retains a direct_url.json entry")
self_rows = [row for row in record_rows if row[0] == record_rel]
if len(self_rows) != 1 or self_rows[0][1:] != ["", ""]:
    fail(f"staged NumPy RECORD self row is invalid: {self_rows!r}")

download_inputs = {}
for item in dicts(sources_manifest):
    filename = item.get("filename")
    url = item.get("url")
    digest = item.get("sha256")
    if not (isinstance(filename, str) and isinstance(url, str) and isinstance(digest, str)):
        continue
    if pathlib.PurePath(filename).name != filename:
        fail(f"source manifest contains unsafe input filename: {filename!r}")
    if not url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", digest):
        fail(f"source manifest contains invalid pinned input: {filename!r}")
    if filename in download_inputs:
        fail(f"source manifest contains duplicate pinned input filename: {filename}")
    download_inputs[filename] = digest
if len(download_inputs) != 15:
    fail(f"expected exactly 15 closed downloadable inputs, found {len(download_inputs)}")
if set(download_inputs.values()) != EXPECTED_PIN_HASHES:
    fail("source manifest closed-input hashes differ from the settled phase-01 pins")
for filename, expected_sha in sorted(download_inputs.items()):
    path = input_root / filename
    if not path.is_file() or path.is_symlink():
        fail(f"verified input is missing or not a regular file: {filename}")
    if sha256(path) != expected_sha:
        fail(f"verified input SHA-256 differs from source manifest: {filename}")

manifest_copy = input_root / "linux-glibc217-numpy-sources.json"
if not manifest_copy.is_file() or sha256(manifest_copy) != sha256(sources_manifest_path):
    fail("verified input root does not contain the exact committed source manifest")
expected_input_files = set(download_inputs) | {manifest_copy.name}
observed_input_files = {
    path.relative_to(input_root).as_posix()
    for path in input_root.rglob("*")
    if path.is_file() and not path.is_symlink()
}
input_symlinks = sorted(
    path.relative_to(input_root).as_posix()
    for path in input_root.rglob("*")
    if path.is_symlink()
)
if input_symlinks:
    fail(f"verified input root contains symlink inputs: {input_symlinks}")
if observed_input_files != expected_input_files:
    fail(
        "verified input root contains unpinned or missing files: "
        f"missing={sorted(expected_input_files - observed_input_files)}, "
        f"extra={sorted(observed_input_files - expected_input_files)}"
    )

core_baseline = core_provenance.get("baseline", {})
if (
    core_baseline.get("image") != expected_image
    or core_baseline.get("architecture") != "x86_64"
    or core_baseline.get("glibc") != "2.17"
    or set(core_baseline.get("pep599_external_allowlist", [])) != PEP599_EXTERNAL
    or core_baseline.get("symbol_ceilings") != EXPECTED_CEILINGS
):
    fail("qualified core provenance does not describe the pinned manylinux2014 baseline")
core_loader = core_provenance.get("native_providers", {}).get("glibc_loader_external", {})
if (
    core_loader.get("kind") != "glibc-loader-external"
    or core_loader.get("image") != core_baseline.get("image")
    or core_loader.get("needed_name") != "ld-linux-x86-64.so.2"
    or core_loader.get("interpreter_path") != "/lib64/ld-linux-x86-64.so.2"
):
    fail(f"qualified core glibc-loader provenance is invalid: {core_loader!r}")
loader_resolved = pathlib.Path(core_loader.get("resolved_path", "")).resolve()
if not loader_resolved.is_file() or sha256(loader_resolved) != core_loader.get("resolved_sha256"):
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
if core_loader.get("package") != loader_package:
    fail(f"qualified core glibc-loader RPM identity differs from pinned image: {core_loader.get('package')!r}")

all_dicts = list(dicts(data))
baseline_matches = [
    item for item in all_dicts
    if item.get("image") == expected_image
    and item.get("architecture") == "x86_64"
    and str(item.get("glibc")) == "2.17"
    and set(item.get("pep599_external_allowlist", [])) == PEP599_EXTERNAL
    and item.get("symbol_ceilings") == EXPECTED_CEILINGS
]
if not baseline_matches:
    fail("NumPy provenance does not bind to the exact core manylinux2014/ABI policy")

runtime_profiles = {
    item.get("runtime_profile")
    for item in all_dicts
    if isinstance(item.get("runtime_profile"), str)
}
if "numpy" not in runtime_profiles:
    fail(f"NumPy provenance does not identify runtime_profile=numpy: {sorted(runtime_profiles)!r}")

flat_strings = list(strings(data))
flat_set = set(flat_strings)
flat_text = "\n".join(flat_strings)
flat_lower = flat_text.lower()
missing_hashes = sorted(EXPECTED_PIN_HASHES - flat_set)
if missing_hashes:
    fail(f"NumPy provenance is missing exact closed-input SHA-256 pins: {missing_hashes}")
if sources_manifest_sha not in flat_set:
    fail("NumPy provenance does not bind to the exact committed source-manifest SHA-256")
for required_identity in (
    "numpy-2.5.2.tar.gz",
    "OpenBLAS-0.3.33.tar.gz",
    "2.5.2",
    "0.3.33",
):
    if required_identity not in flat_set:
        fail(f"NumPy provenance is missing pinned source identity: {required_identity}")

if "-march=native" in flat_lower or "-mtune=native" in flat_lower:
    fail("NumPy provenance contains forbidden host-native compiler tuning")

core_provenance_sha = sha256(core_provenance_path)
if sha256(copied_core_provenance_path) != core_provenance_sha:
    fail("NumPy overlay changed the copied qualified-core provenance")
if core_provenance_sha not in flat_set:
    fail("NumPy provenance does not bind to the exact qualified-core provenance SHA-256")

core_identity = tree_identity(core)
runtime_identity = tree_identity(runtime)
for rel, expected in core_identity.items():
    actual = runtime_identity.get(rel)
    if actual is None:
        fail(f"NumPy overlay removed inherited core path: {rel}")
    if actual != expected:
        fail(f"NumPy overlay modified inherited core identity: {rel}")

find_manifest_covering(
    data,
    core_identity,
    ("core", "inherited", "parent"),
    "inherited core file/symlink identity",
)

added_identity = {
    rel: record for rel, record in runtime_identity.items()
    if rel not in core_identity
}
if not added_identity:
    fail("NumPy overlay did not add any staged payload")
try:
    provenance_rel = provenance_path.relative_to(runtime).as_posix()
except ValueError:
    provenance_rel = None
payload_manifest_rel = payload_manifest_path.relative_to(runtime).as_posix()

named_payload_expected = {
    rel: record for rel, record in added_identity.items()
    if rel not in {provenance_rel, payload_manifest_rel}
}
named_records = []
for key in ("files", "symlinks"):
    records = payload_manifest.get(key)
    if not isinstance(records, list):
        fail(f"named NumPy payload manifest is missing {key}")
    named_records.extend(records)
named_payload_map = manifest_map(
    [record for record in (normalize_manifest_record(item) for item in named_records) if record is not None]
)
if named_payload_map is None or set(named_payload_map) != set(named_payload_expected):
    fail(
        "named NumPy payload manifest path set differs from staged payload: "
        f"expected={sorted(named_payload_expected)}, observed={sorted(named_payload_map or {})}"
    )
if not all(record_matches(record, named_payload_map[rel]) for rel, record in named_payload_expected.items()):
    fail("named NumPy payload manifest file/symlink identities differ from staged payload")

expected_file_count = sum(1 for record in named_payload_expected.values() if record["kind"] == "file")
expected_symlink_count = sum(1 for record in named_payload_expected.values() if record["kind"] == "symlink")
if payload_manifest.get("file_count") != expected_file_count or payload_manifest.get("symlink_count") != expected_symlink_count:
    fail("named NumPy payload manifest entry counts differ from staged payload")

def canonical_tree_sha(identity):
    aggregate = hashlib.sha256()
    for rel in sorted(identity, key=pathlib.PurePosixPath):
        record = identity[rel]
        mode = record["mode"]
        if record["kind"] == "symlink":
            aggregate.update(
                b"L\0" + rel.encode() + b"\0" + oct(mode).encode() + b"\0" +
                record["target"].encode() + b"\n"
            )
        else:
            aggregate.update(
                b"F\0" + rel.encode() + b"\0" + oct(mode).encode() + b"\0" +
                str(record["size"]).encode() + b"\0" + record["sha256"].encode() + b"\n"
            )
    return aggregate.hexdigest()

named_tree_sha = canonical_tree_sha(named_payload_expected)
if payload_manifest.get("tree_sha256") != named_tree_sha:
    fail("named NumPy payload manifest tree SHA-256 differs from staged payload")

payload_binding = data.get("payload")
if not isinstance(payload_binding, dict):
    fail("NumPy provenance is missing the named payload binding")
if payload_binding.get("manifest_path") != payload_manifest_rel:
    fail("NumPy provenance names the wrong payload-manifest path")
if payload_binding.get("manifest_sha256") != sha256(payload_manifest_path):
    fail("NumPy provenance payload-manifest SHA-256 does not match the named file")
if payload_binding.get("manifest_tree_sha256") != named_tree_sha:
    fail("NumPy provenance payload-manifest tree SHA-256 does not match the named handoff")

manifest_expected = {
    rel: record for rel, record in added_identity.items()
    if rel != provenance_rel
}
find_manifest_covering(
    data,
    manifest_expected,
    ("manifest", "payload", "added", "numpy"),
    "added-payload file/symlink hash",
)
if payload_binding.get("tree_sha256") != canonical_tree_sha(manifest_expected):
    fail("NumPy provenance added-payload tree SHA-256 differs from the staged runtime")

for rel in added_identity:
    lower = rel.lower()
    if rel.startswith("bin/"):
        fail(f"NumPy overlay added a command wrapper outside selective site-packages staging: {rel}")
    if lower.endswith((".mex", ".mexa64", ".mexw64", ".mexmaci64")) or ".mex" in pathlib.PurePosixPath(rel).name.lower():
        fail(f"MEX binary leaked into phase-01 NumPy runtime: {rel}")
    if (
        lower.endswith(".whl")
        and "numpy-2.5.2" in lower
        and ("manylinux_2_27" in lower or "manylinux_2_28" in lower)
    ):
        fail(f"retained official NumPy wheel leaked into staged runtime: {rel}")

added_elfs = []
all_runtime_elfs = []
for path in sorted(runtime.rglob("*")):
    if not is_elf(path):
        continue
    all_runtime_elfs.append(path)
    rel = path.relative_to(runtime).as_posix()
    if rel in added_identity:
        added_elfs.append(path)
if not added_elfs:
    fail("NumPy overlay contains no added ELF objects")

provider_index = {}
elf_dynamic = {}
for path in all_runtime_elfs:
    needed, soname, runpaths = dynamic(path)
    elf_dynamic[path] = (needed, soname, runpaths)
    provider_index.setdefault(path.name, []).append(path)
    if soname:
        provider_index.setdefault(soname, []).append(path)

openblas_candidates = []
for path in added_elfs:
    _, soname, _ = elf_dynamic[path]
    names = " ".join(filter(None, (path.name, soname))).lower()
    if "openblas" in names:
        openblas_candidates.append(path)
if len(openblas_candidates) != 1:
    fail(f"expected exactly one added OpenBLAS ELF provider, found: {[p.name for p in openblas_candidates]}")
provider = openblas_candidates[0]
provider_rel = provider.relative_to(runtime).as_posix()
provider_sha = sha256(provider)
_, provider_soname, _ = elf_dynamic[provider]
if (
    not provider_soname
    or "openblas" not in provider_soname.lower()
    or PRIVATE_SUFFIX not in provider_soname
    or PRIVATE_SUFFIX not in provider.name
):
    fail(
        "staged OpenBLAS provider lacks the private library/SONAME suffix: "
        f"path={provider_rel!r}, soname={provider_soname!r}"
    )
if provider_rel not in flat_set or provider_sha not in flat_set or provider_soname not in flat_set:
    fail("NumPy provenance does not bind the exact private OpenBLAS path/hash/SONAME")

nm_text = run("nm", "-D", "--defined-only", str(provider))
defined_symbols = set()
for line in nm_text.splitlines():
    fields = line.split()
    if fields:
        defined_symbols.add(fields[-1].split("@", 1)[0])
private_exports = {name for name in defined_symbols if PRIVATE_SUFFIX in name}
if len(private_exports) < 8:
    fail(f"private OpenBLAS symbol suffix is not present on enough exports: count={len(private_exports)}")
for required_provider_symbol in (
    "dgemm_bridge_np252_",
    "dgesv_bridge_np252_",
    "cblas_dgemmbridge_np252_",
):
    if required_provider_symbol not in defined_symbols:
        fail(f"private OpenBLAS lacks exact collision-safe symbol: {required_provider_symbol}")
for public_symbol in (
    "dgemm_", "sgemm_", "dgesv_", "sgesv_", "dgetrf_", "dgetrs_",
    "cblas_dgemm", "cblas_sgemm",
):
    if public_symbol in defined_symbols:
        fail(f"private OpenBLAS provider leaks unsuffixed public symbol: {public_symbol}")

corename_symbols = sorted(name for name in defined_symbols if "openblas_get_corename" in name)
private_corename_symbols = [name for name in corename_symbols if PRIVATE_SUFFIX in name]
if len(private_corename_symbols) == 1:
    corename_symbol = private_corename_symbols[0]
elif len(corename_symbols) == 1:
    corename_symbol = corename_symbols[0]
else:
    fail(f"expected one usable openblas_get_corename symbol, found: {corename_symbols}")

numpy_root = runtime / "lib/python3.14/site-packages/numpy"
if not numpy_root.is_dir():
    fail("staged NumPy package directory is missing")
numpy_elfs = [path for path in added_elfs if under(path, numpy_root)]
if not numpy_elfs:
    fail("staged NumPy package contains no native extensions")

private_needed_consumers = []
private_undefined = set()
public_undefined = set()
blas_roots = ("dgemm", "sgemm", "dgesv", "sgesv", "dgetrf", "dgetrs", "cblas_", "lapacke_")
for path in numpy_elfs:
    needed, _, _ = elf_dynamic[path]
    blas_needed = [name for name in needed if "blas" in name.lower() or "lapack" in name.lower()]
    for name in blas_needed:
        if name != provider_soname:
            fail(f"NumPy extension depends on a non-private BLAS/LAPACK SONAME: {path.relative_to(runtime)} -> {name}")
    if provider_soname in needed:
        private_needed_consumers.append(path)
    symbol_text = run("readelf", "-Ws", str(path))
    for line in symbol_text.splitlines():
        if " UND " not in f" {line} ":
            continue
        fields = line.split()
        if not fields:
            continue
        name = fields[-1].split("@", 1)[0]
        lower = name.lower()
        if any(root in lower for root in blas_roots):
            if PRIVATE_SUFFIX in name:
                private_undefined.add(name)
            else:
                public_undefined.add(name)
if not private_needed_consumers:
    fail("no NumPy native extension records a DT_NEEDED edge to the private OpenBLAS provider")
if not private_undefined:
    fail("NumPy native extensions do not reference private-suffixed BLAS/LAPACK symbols")
if public_undefined:
    fail(f"NumPy native extensions reference unsuffixed BLAS/LAPACK symbols: {sorted(public_undefined)}")
if "dgesv_bridge_np252_" not in private_undefined:
    fail("NumPy native extensions do not request exact LP64 LAPACK symbol dgesv_bridge_np252_")
if "dgesvbridge_np252__" in private_undefined:
    fail("NumPy native extensions retain incompatible LP64 LAPACK symbol dgesvbridge_np252__")
recorded_undefined = build_record.get("numpy_undefined_blas_symbols")
if not isinstance(recorded_undefined, list) or set(recorded_undefined) != private_undefined | public_undefined:
    fail("NumPy provenance undefined BLAS/LAPACK symbol set differs from independent runtime inspection")

def find_elf_census(data):
    candidates = []
    expected_paths = {path.relative_to(runtime).as_posix() for path in added_elfs}
    for item in all_dicts:
        for key in ("elf_census", "added_elf_census", "native_census"):
            records = item.get(key)
            if not isinstance(records, list):
                continue
            mapping = {}
            valid = True
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                    valid = False
                    break
                if record["path"] in mapping:
                    valid = False
                    break
                mapping[record["path"]] = record
            if valid and expected_paths.issubset(mapping):
                candidates.append(mapping)
    if not candidates:
        fail("NumPy provenance has no added-ELF census covering the staged closure")
    return candidates[0]


elf_census = find_elf_census(data)
recorded_loader = data.get("abi", {}).get("glibc_loader_external", {})
if (
    recorded_loader.get("needed_name") != core_loader["needed_name"]
    or recorded_loader.get("interpreter_path") != core_loader["interpreter_path"]
    or pathlib.Path(recorded_loader.get("resolved_path", "")).resolve() != loader_resolved
    or recorded_loader.get("resolved_sha256") != core_loader["resolved_sha256"]
    or recorded_loader.get("package") != loader_package
):
    fail("NumPy provenance glibc-loader identity differs from the qualified core")
observed_loader_consumers = []

for path in added_elfs:
    rel = path.relative_to(runtime).as_posix()
    header = run("readelf", "-hW", str(path))
    machine = re.search(r"^\s*Machine:\s*(.+)$", header, re.MULTILINE)
    elf_class = re.search(r"^\s*Class:\s*(.+)$", header, re.MULTILINE)
    if (
        machine is None
        or machine.group(1).strip() != "Advanced Micro Devices X86-64"
        or elf_class is None
        or elf_class.group(1).strip() != "ELF64"
    ):
        fail(f"added payload contains non-x86_64 ELF: {rel}")

    needed, soname, runpaths = elf_dynamic[path]
    for needed_name in needed:
        if "/" in needed_name:
            fail(f"non-relocatable DT_NEEDED entry in added ELF: {rel} -> {needed_name}")
    for packed in runpaths:
        for entry in packed.split(":"):
            if not entry or not entry.startswith("$ORIGIN"):
                fail(f"non-runtime-relative RPATH/RUNPATH in added ELF: {rel}: {entry!r}")
            suffix = entry[len("$ORIGIN"):]
            if suffix and not suffix.startswith("/"):
                fail(f"invalid $ORIGIN RPATH/RUNPATH in added ELF: {rel}: {entry}")
            resolved_dir = (path.parent / suffix.lstrip("/")).resolve()
            if not under(resolved_dir, runtime):
                fail(f"RPATH/RUNPATH escapes staged runtime: {rel}: {entry}")

    check_symbol_ceilings(path)
    resolved = ldd_map(path)
    internal_needed = []
    observed_dependencies = []
    for needed_name in needed:
        actual = resolved.get(needed_name)
        if needed_name == core_loader["needed_name"]:
            if actual is None:
                fail(f"glibc loader dependency did not resolve: {rel} -> {needed_name}")
            actual_path = pathlib.Path(actual).resolve()
            if actual_path != loader_resolved or under(actual_path, runtime):
                fail(f"glibc loader dependency resolved unexpectedly: {rel} -> {needed_name} -> {actual_path}")
            observed_dependencies.append({
                "name": needed_name,
                "resolution": "glibc-loader-external",
                "provider": str(loader_resolved),
                "provider_sha256": core_loader["resolved_sha256"],
                "package": loader_package,
            })
            observed_loader_consumers.append(rel)
            continue
        if needed_name in PEP599_EXTERNAL:
            if actual is None:
                fail(f"PEP 599 external dependency did not resolve: {rel} -> {needed_name}")
            if under(actual, runtime):
                fail(f"PEP 599 dependency unexpectedly resolved from staged runtime: {rel} -> {needed_name} -> {actual}")
            observed_dependencies.append({
                "name": needed_name,
                "resolution": "pep599-external",
            })
            continue

        providers = provider_index.get(needed_name, [])
        if not providers:
            fail(f"added ELF has unbundled non-PEP599 dependency: {rel} -> {needed_name}")
        if len({provider.resolve() for provider in providers}) != 1:
            fail(f"ambiguous bundled provider for added ELF: {rel} -> {needed_name}: {providers}")
        bundled = providers[0].resolve()
        if actual is None or pathlib.Path(actual).resolve() != bundled:
            fail(f"bundled dependency resolved to wrong provider: {rel} -> {needed_name} -> {actual}")
        bundled_rel = bundled.relative_to(runtime).as_posix()
        if bundled_rel in added_identity and bundled.resolve() != provider.resolve():
            fail(f"unexpected added native provider beyond private OpenBLAS: {rel} -> {needed_name} -> {bundled_rel}")
        internal_needed.append(bundled)
        observed_dependencies.append({
            "name": needed_name,
            "resolution": "bundled",
            "provider": bundled_rel,
            "provider_sha256": sha256(bundled),
        })

    if internal_needed:
        if not runpaths:
            fail(f"added ELF with bundled dependencies lacks runtime-relative RPATH/RUNPATH: {rel}")
        search_dirs = {
            (path.parent / entry[len("$ORIGIN"):].lstrip("/")).resolve()
            for packed in runpaths
            for entry in packed.split(":")
        }
        for bundled in internal_needed:
            if bundled.parent.resolve() not in search_dirs:
                fail(f"RUNPATH does not cover bundled provider directory: {rel} -> {bundled.relative_to(runtime)}")

    census = elf_census[rel]
    if census.get("sha256") != sha256(path):
        fail(f"added-ELF provenance hash mismatch: {rel}")
    if census.get("needed") != needed:
        fail(f"added-ELF provenance DT_NEEDED mismatch: {rel}")
    recorded_runpath = census.get("runpath", census.get("runpaths"))
    if recorded_runpath != runpaths:
        fail(f"added-ELF provenance RUNPATH mismatch: {rel}")
    if census.get("soname") != soname:
        fail(f"added-ELF provenance SONAME mismatch: {rel}")
    if "machine" in census and census.get("machine") != "Advanced Micro Devices X86-64":
        fail(f"added-ELF provenance machine mismatch: {rel}")
    if "elf_class" in census and census.get("elf_class") != "ELF64":
        fail(f"added-ELF provenance class mismatch: {rel}")
    recorded_dependencies = census.get("dependencies")
    if not isinstance(recorded_dependencies, list) or recorded_dependencies != observed_dependencies:
        fail(
            f"added-ELF dependency provenance mismatch: {rel}: "
            f"recorded={recorded_dependencies!r}, observed={observed_dependencies!r}"
        )

if recorded_loader.get("added_consumers") != sorted(set(observed_loader_consumers)):
    fail(
        "NumPy provenance glibc-loader consumer set differs from independent inspection: "
        f"recorded={recorded_loader.get('added_consumers')!r}, "
        f"observed={sorted(set(observed_loader_consumers))!r}"
    )

provider_census = elf_census.get(provider_rel)
if not provider_census or provider_census.get("sha256") != provider_sha:
    fail("private OpenBLAS provider is not bound by the added-ELF census")

print(f"numpy_runtime_core_provenance_sha256={core_provenance_sha}")
print(f"numpy_runtime_sources_manifest_sha256={sources_manifest_sha}")
print(f"numpy_runtime_inherited_core_paths={len(core_identity)}")
print(f"numpy_runtime_added_paths={len(added_identity)}")
print(f"numpy_runtime_added_elf_count={len(added_elfs)}")
print(f"numpy_private_provider={provider_rel}")
print(f"numpy_private_provider_sha256={provider_sha}")
print(f"numpy_private_provider_soname={provider_soname}")
print(f"openblas_corename_symbol={corename_symbol}")
print("numpy_runtime_provenance_audit=pass")
PY

provider_rel="$(sed -n 's/^numpy_private_provider=//p' "$audit_output")"
provider_soname="$(sed -n 's/^numpy_private_provider_soname=//p' "$audit_output")"
corename_symbol="$(sed -n 's/^openblas_corename_symbol=//p' "$audit_output")"
[[ -n "$provider_rel" && -n "$provider_soname" && -n "$corename_symbol" ]] || {
    echo "NumPy runtime audit did not report the private OpenBLAS identity" >&2
    exit 1
}
provider="$runtime/$provider_rel"
[[ -f "$provider" ]] || {
    echo "private OpenBLAS provider reported by audit is missing: $provider" >&2
    exit 1
}

run_numpy_probe() {
    local mode="$1"
    shift
    env -u OPENBLAS_CORETYPE -u NPY_DISABLE_CPU_FEATURES PYTHONHOME="$poison/home" PYTHONPATH="$poison/python" PYTHONUSERBASE="$poison/user" PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 HOME="$poison/home" LD_LIBRARY_PATH="$poison/native" "$@" "$python" -I -S - "$runtime" "$provider" "$provider_soname" "$corename_symbol" "$mode" <<'PY'
import ctypes
import importlib
import os
import pathlib
import sys

runtime = pathlib.Path(sys.argv[1]).resolve()
provider = pathlib.Path(sys.argv[2]).resolve()
provider_soname = sys.argv[3]
corename_symbol = sys.argv[4]
mode = sys.argv[5]
site_packages = (runtime / "lib/python3.14/site-packages").resolve()

if "OPENBLAS_CORETYPE" in os.environ:
    raise SystemExit("qualification must not override OpenBLAS runtime core selection")


def under(path, root):
    path = pathlib.Path(path).resolve()
    root = pathlib.Path(root).resolve()
    return path == root or root in path.parents


if pathlib.Path(sys.executable).resolve() != (runtime / "bin/python3.14").resolve():
    raise SystemExit(f"unexpected staged Python executable: {sys.executable}")
sys.path.insert(0, str(site_packages))
for entry in sys.path:
    if entry and not under(entry, runtime):
        raise SystemExit(f"ambient Python path leaked into isolated interpreter: {entry}")

import numpy as np

if np.__version__ != "2.5.2":
    raise SystemExit(f"unexpected staged NumPy version: {np.__version__}")
if not under(np.__file__, site_packages):
    raise SystemExit(f"NumPy imported outside staged site-packages: {np.__file__}")

dist_info = sorted(site_packages.glob("numpy-2.5.2.dist-info"))
if len(dist_info) != 1:
    raise SystemExit(f"expected exactly one NumPy 2.5.2 dist-info directory: {dist_info}")

config_text = repr(np.show_config(mode="dicts")).lower()
if "openblas" not in config_text:
    raise SystemExit(f"NumPy configuration does not report OpenBLAS: {config_text}")

a = np.array([[1.0, 2.0], [3.0, 5.0]], dtype=np.float64)
b = np.array([[2.0, 0.0], [1.0, 4.0]], dtype=np.float64)
product = a @ b
expected_product = np.array([[4.0, 8.0], [11.0, 20.0]], dtype=np.float64)
if not np.allclose(product, expected_product, rtol=0.0, atol=1e-12):
    raise SystemExit(f"matrix multiply failed: {product}")

rhs = np.array([5.0, 13.0], dtype=np.float64)
solution = np.linalg.solve(a, rhs)
if not np.allclose(solution, np.array([1.0, 2.0]), rtol=0.0, atol=1e-12):
    raise SystemExit(f"numpy.linalg.solve failed: {solution}")

linalg_native = importlib.import_module("numpy.linalg._umath_linalg")
if not under(linalg_native.__file__, site_packages):
    raise SystemExit(f"NumPy linalg extension escaped staged site-packages: {linalg_native.__file__}")

mapped = set()
for line in pathlib.Path("/proc/self/maps").read_text(encoding="utf-8", errors="replace").splitlines():
    fields = line.split(None, 5)
    if len(fields) < 6 or not fields[5].startswith("/"):
        continue
    path = pathlib.Path(fields[5]).resolve()
    lower = path.name.lower()
    if "openblas" in lower or lower.startswith(("libblas.", "liblapack.")):
        mapped.add(path)
if provider not in mapped:
    raise SystemExit(f"private OpenBLAS provider was not loaded: expected={provider}, mapped={sorted(map(str, mapped))}")
ambient = sorted(path for path in mapped if not under(path, runtime))
if ambient:
    raise SystemExit(f"ambient BLAS/LAPACK provider leaked into staged execution: {ambient}")
if any(path != provider for path in mapped):
    raise SystemExit(f"unexpected competing staged BLAS/LAPACK provider loaded: {sorted(map(str, mapped))}")

library = ctypes.CDLL(str(provider))
try:
    get_corename = getattr(library, corename_symbol)
except AttributeError as exc:
    raise SystemExit(f"private OpenBLAS corename symbol is missing at runtime: {corename_symbol}") from exc
get_corename.restype = ctypes.c_char_p
core_name_raw = get_corename()
if not core_name_raw:
    raise SystemExit("OpenBLAS did not report a runtime core name")
core_name = core_name_raw.decode("ascii", errors="replace")

if mode == "masked":
    required_mask = {"X86_V2", "X86_V3"}
    configured_mask = {
        item.strip().upper()
        for item in os.environ.get("NPY_DISABLE_CPU_FEATURES", "").split(",")
        if item.strip()
    }
    if not required_mask.issubset(configured_mask):
        raise SystemExit(f"masked probe did not receive the required NumPy CPU mask: {configured_mask}")
    features = getattr(np._core._multiarray_umath, "__cpu_features__", None)
    if not isinstance(features, dict):
        raise SystemExit("NumPy CPU feature inventory is unavailable")
    missing_features = sorted(required_mask - set(features))
    if missing_features:
        raise SystemExit(f"NumPy CPU feature inventory lacks masked features: {missing_features}")
    enabled = sorted(name for name in required_mask if features.get(name))
    if enabled:
        raise SystemExit(f"masked NumPy CPU features remained enabled: {enabled}")
elif mode != "native":
    raise SystemExit(f"unknown functional probe mode: {mode}")

print(f"numpy_probe_mode={mode}")
print(f"numpy_probe_version={np.__version__}")
print(f"numpy_probe_location={pathlib.Path(np.__file__).resolve()}")
print(f"numpy_probe_linalg={pathlib.Path(linalg_native.__file__).resolve()}")
print(f"numpy_probe_provider={provider}")
print(f"numpy_probe_provider_soname={provider_soname}")
print(f"numpy_probe_openblas_core={core_name}")
print(f"numpy_probe_{mode}=pass")
PY
}

run_numpy_probe native

numpy_cpu_mask="X86_V2,X86_V3"
run_numpy_probe masked NPY_DISABLE_CPU_FEATURES="$numpy_cpu_mask"

relocated_runtime="$poison/relocated-numpy-runtime"
cp -a "$runtime" "$relocated_runtime"
original_runtime="$runtime"
original_python="$python"
original_provider="$provider"
runtime="$relocated_runtime"
python="$runtime/bin/python3.14"
provider="$runtime/$provider_rel"
run_numpy_probe native
runtime="$original_runtime"
python="$original_python"
provider="$original_provider"
echo "linux_glibc217_numpy_relocation=pass"

printf 'linux_glibc217_numpy_image=%s\n' "$expected_image"
printf 'linux_glibc217_numpy_glibc=%s\n' "$glibc_identity"
echo "linux_glibc217_numpy_runtime_test=pass"
