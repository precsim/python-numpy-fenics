"""Runtime bootstrap for the relocatable macOS FEniCS overlay."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import platform
import shlex
import stat
import sys
import tempfile


_ACTIVATED_ROOT: Path | None = None
_JIT_WRAPPER: Path | None = None


def _runtime_root() -> Path:
    here = Path(__file__).resolve()
    framework = next((parent for parent in here.parents if parent.name == "Python.framework"), None)
    if framework is None:
        raise RuntimeError(f"cannot derive embedded runtime root from bootstrap location: {here}")
    return framework.parent.resolve()


def _prepend_path(name: str, value: Path) -> None:
    current = os.environ.get(name, "")
    parts = [item for item in current.split(os.pathsep) if item]
    normalized = {os.path.normcase(os.path.abspath(item)) for item in parts}
    candidate = os.path.normcase(os.path.abspath(str(value)))
    if candidate not in normalized:
        os.environ[name] = str(value) + (os.pathsep + current if current else "")


def _wrapper_root(runtime_root: Path) -> Path:
    candidates = []
    for raw in (os.environ.get("RUNNER_TEMP"), tempfile.gettempdir(), "/tmp"):
        if raw:
            path = Path(raw).resolve()
            if path not in candidates:
                candidates.append(path)
    for base in candidates:
        if any(character.isspace() for character in str(base)):
            continue
        token = hashlib.sha256(str(runtime_root).encode("utf-8")).hexdigest()[:16]
        return base / f"fenics-embed-clang-{token}-{platform.machine()}"
    raise RuntimeError("no whitespace-safe temporary directory is available for the FEniCS Clang compiler shim")


def _write_jit_wrapper(runtime_root: Path) -> Path:
    global _JIT_WRAPPER
    if _JIT_WRAPPER is not None and _JIT_WRAPPER.exists():
        return _JIT_WRAPPER

    machine = platform.machine()
    target_tools = {
        "arm64": (
            "arm64-apple-darwin20.0.0-clang",
            "arm64-apple-darwin20.0.0-ld",
        ),
        "x86_64": (
            "x86_64-apple-darwin13.4.0-clang",
            "x86_64-apple-darwin13.4.0-ld",
        ),
    }.get(machine)
    if target_tools is None:
        raise RuntimeError(f"unsupported macOS FEniCS runtime architecture: {machine}")

    private_prefix = runtime_root / "fenics-runtime" / "conda"
    clang = private_prefix / "bin" / target_tools[0]
    linker = private_prefix / "bin" / target_tools[1]
    if not clang.is_file():
        raise RuntimeError(f"bundled Clang executable is missing: {clang}")
    if not linker.is_file():
        raise RuntimeError(f"bundled cctools linker is missing: {linker}")

    root = _wrapper_root(runtime_root)
    root.mkdir(parents=True, exist_ok=True)
    wrapper = root / "fenics-clang-cc"
    python_include = runtime_root / "Python.framework" / "Versions" / "3.14" / "include" / "python3.14"
    private_include = private_prefix / "include"
    if not python_include.is_dir():
        raise RuntimeError(f"staged CPython headers are missing: {python_include}")
    if not private_include.is_dir():
        raise RuntimeError(f"private FEniCS/PETSc headers are missing: {private_include}")
    script = f"""#!/usr/bin/env bash
set -euo pipefail
clang={shlex.quote(str(clang))}
tool_bin={shlex.quote(str(private_prefix / "bin"))}
runtime_root={shlex.quote(str(runtime_root))}
python_include={shlex.quote(str(python_include))}
private_include={shlex.quote(str(private_include))}
args=("-I$python_include" "-I$private_include")
while [[ $# -gt 0 ]]; do
    case "$1" in
        -isysroot)
            shift 2
            ;;
        -isysroot*)
            shift
            ;;
        -arch)
            # The staged CPython framework carries universal-build arch flags
            # in sysconfig. The target-prefixed Clang driver already selects
            # the runtime architecture, so drop inherited universal flags.
            shift 2
            ;;
        -I)
            if [[ "${{2:-}}" == */include/python3.14 && "${{2:-}}" != "$runtime_root"/* ]]; then
                shift 2
            else
                args+=("$1" "${{2:-}}")
                shift 2
            fi
            ;;
        -I*/include/python3.14)
            if [[ "$1" == -I"$runtime_root"/* ]]; then
                args+=("$1")
            fi
            shift
            ;;
        -isystem)
            if [[ "${{2:-}}" == "$runtime_root"/* ]]; then
                args+=("$1" "${{2:-}}")
            fi
            shift 2
            ;;
        -isystem*)
            if [[ "$1" == -isystem"$runtime_root"/* ]]; then
                args+=("$1")
            fi
            shift
            ;;
        -mmacosx-version-min=*)
            shift
            ;;
        -Wl,-syslibroot,*|-Wl,-platform_version,macos,*)
            shift
            ;;
        *)
            args+=("$1")
            shift
            ;;
    esac
done
sdkroot="${{SDKROOT:-}}"
if [[ -z "$sdkroot" || ! -d "$sdkroot" ]]; then
    sdkroot="$(/usr/bin/xcrun --sdk macosx --show-sdk-path)"
fi
if [[ ! -d "$sdkroot" ]]; then
    echo "macOS SDK root is unavailable: $sdkroot" >&2
    exit 1
fi
exec "$clang" -B"$tool_bin" -isysroot "$sdkroot" -mmacosx-version-min=14.0 "${{args[@]}}"
"""
    wrapper.write_text(script, encoding="utf-8")
    wrapper.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    _JIT_WRAPPER = wrapper
    return wrapper


def activate() -> Path:
    """Activate the bundled macOS FEniCS runtime from its installed location."""

    global _ACTIVATED_ROOT

    if sys.platform != "darwin":
        raise RuntimeError("fenics_embed_runtime macOS overlay requires Darwin")
    if sys.version_info[:3] != (3, 14, 10):
        raise RuntimeError(
            f"FEniCS embedded runtime requires CPython 3.14.10, got {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )

    root = _runtime_root()
    if _ACTIVATED_ROOT is not None:
        if _ACTIVATED_ROOT != root:
            raise RuntimeError(
                f"FEniCS embedded runtime already activated from {_ACTIVATED_ROOT}; cannot switch to {root}"
            )
        return root

    private_prefix = root / "fenics-runtime" / "conda"
    private_site = private_prefix / "lib" / "python3.14" / "site-packages"
    public_site = (
        root
        / "Python.framework"
        / "Versions"
        / "3.14"
        / "lib"
        / "python3.14"
        / "site-packages"
    )
    for required, label in (
        (private_prefix / "lib", "private native-library root"),
        (private_site, "private FEniCS site-packages"),
        (public_site, "public NumPy site-packages"),
    ):
        if not required.exists():
            raise RuntimeError(f"{label} is missing from embedded runtime: {required}")

    # The consumer can start with an arbitrary PYTHONPATH. Activation must
    # make both producer-owned sites authoritative: FEniCS from the private
    # overlay and NumPy from the public base runtime. Reinsert them in a
    # deterministic order rather than merely adding the private site when
    # absent, otherwise ambient paths can remain ahead of the public NumPy
    # payload.
    authoritative_sites = [str(private_site), str(public_site)]
    authoritative_normalized = {
        os.path.normcase(os.path.abspath(path)) for path in authoritative_sites
    }
    sys.path[:] = [
        path
        for path in sys.path
        if os.path.normcase(os.path.abspath(path)) not in authoritative_normalized
    ]
    sys.path[:0] = authoritative_sites

    wrapper = _write_jit_wrapper(root)
    _prepend_path("PATH", private_prefix / "bin")
    _prepend_path("PATH", wrapper.parent)

    os.environ["PETSC_DIR"] = str(private_prefix)
    os.environ["PETSC_ARCH"] = ""
    os.environ["CC"] = str(wrapper)
    os.environ["CXX"] = str(wrapper)
    os.environ["CPP"] = f"{wrapper} -E"
    os.environ["LDSHARED"] = f"{wrapper} -bundle -undefined dynamic_lookup"
    os.environ["MACOSX_DEPLOYMENT_TARGET"] = "14.0"
    tool_names = {
        "arm64": ("arm64-apple-darwin20.0.0-clang", "arm64-apple-darwin20.0.0-ld"),
        "x86_64": ("x86_64-apple-darwin13.4.0-clang", "x86_64-apple-darwin13.4.0-ld"),
    }[platform.machine()]
    os.environ["FENICS_JIT_CLANG"] = str(private_prefix / "bin" / tool_names[0])
    os.environ["FENICS_JIT_LINKER"] = str(private_prefix / "bin" / tool_names[1])

    import petsc4py

    current_get_config = petsc4py.get_config
    if not getattr(current_get_config, "_fenics_embed_runtime", False):

        def relocated_get_config(_petsc_root=str(private_prefix)):
            return {"PETSC_DIR": _petsc_root, "PETSC_ARCH": ""}

        relocated_get_config._fenics_embed_runtime = True
        petsc4py.get_config = relocated_get_config

    _ACTIVATED_ROOT = root
    return root
