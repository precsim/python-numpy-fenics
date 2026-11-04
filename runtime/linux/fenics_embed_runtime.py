"""Runtime bootstrap for the relocatable Linux FEniCS overlay."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile


_ACTIVATED_ROOT: Path | None = None
_JIT_WRAPPER: Path | None = None
_GCC_SPECS_PREFIX_TOKEN = "@FENICS_PRIVATE_PREFIX@"


def _runtime_root() -> Path:
    here = Path(__file__).resolve()
    # <runtime>/lib/python3.14/site-packages/fenics_embed_runtime.py
    if len(here.parents) < 4 or here.parents[2].name != "lib":
        raise RuntimeError(f"cannot derive embedded runtime root from bootstrap location: {here}")
    root = here.parents[3]
    if not (root / "bin" / "python3.14").exists():
        raise RuntimeError(f"derived runtime root has no CPython executable: {root}")
    return root.resolve()


def _wrapper_root(runtime_root: Path) -> Path:
    import hashlib

    candidates: list[Path] = []
    for raw in (os.environ.get("RUNNER_TEMP"), tempfile.gettempdir(), "/tmp"):
        if not raw:
            continue
        candidate = Path(raw).resolve()
        if candidate not in candidates:
            candidates.append(candidate)
    for base in candidates:
        if any(character.isspace() for character in str(base)):
            continue
        token = hashlib.sha256(str(runtime_root).encode("utf-8")).hexdigest()[:16]
        return base / f"fenics-embed-gcc-{token}"
    raise RuntimeError("no whitespace-safe temporary directory is available for the FEniCS GCC shim")


def _private_compiler(private_prefix: Path) -> Path:
    candidates = (
        private_prefix / "bin" / "x86_64-conda-linux-gnu-gcc",
        private_prefix / "bin" / "gcc",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"bundled GCC driver is missing from private prefix: {private_prefix}")


def _private_binutils(private_prefix: Path) -> dict[str, Path]:
    tools: dict[str, Path] = {}
    for variable, basename in (
        ("LD", "x86_64-conda-linux-gnu-ld"),
        ("AR", "x86_64-conda-linux-gnu-ar"),
        ("RANLIB", "x86_64-conda-linux-gnu-ranlib"),
        ("AS", "x86_64-conda-linux-gnu-as"),
    ):
        candidate = private_prefix / "bin" / basename
        if not candidate.is_file():
            raise RuntimeError(f"bundled JIT binutils tool is missing: {candidate}")
        tools[variable] = candidate
    return tools


def _mapped_soname_paths(soname: str) -> set[Path]:
    maps = Path("/proc/self/maps")
    if not maps.is_file():
        raise RuntimeError("Linux FEniCS activation requires /proc/self/maps for provider isolation")
    result: set[Path] = set()
    for line in maps.read_text(errors="replace").splitlines():
        # /proc/self/maps has five fixed columns followed by an optional
        # pathname. The relocated runtime is intentionally tested in a path
        # containing spaces; unrestricted split() truncates that pathname.
        fields = line.split(maxsplit=5)
        if len(fields) < 6 or not fields[5].startswith("/"):
            continue
        path = Path(fields[5])
        if path.name != soname and not path.name.startswith(soname + "."):
            continue
        try:
            result.add(path.resolve())
        except OSError:
            continue
    return result


def _activate_toolchain_runtime(private_prefix: Path) -> None:
    # Conda-forge FEniCS packages are built with a newer libstdc++ than the
    # manylinux2014 CPython/NumPy base.  Bind the locked overlay provider before
    # importing NumPy or DOLFINx so one SONAME cannot silently resolve to two
    # incompatible C++ runtimes depending on import order.
    for soname in ("libgcc_s.so.1", "libstdc++.so.6"):
        provider = (private_prefix / "lib" / soname).resolve()
        if not provider.is_file():
            raise RuntimeError(f"private toolchain runtime provider is missing: {provider}")
        mapped = _mapped_soname_paths(soname)
        competing = mapped - {provider}
        if competing:
            raise RuntimeError(
                f"competing {soname} provider was loaded before FEniCS activation: {sorted(map(str, competing))}"
            )
        ctypes.CDLL(str(provider), mode=os.RTLD_NOW | os.RTLD_GLOBAL)
        mapped = _mapped_soname_paths(soname)
        if mapped != {provider}:
            raise RuntimeError(
                f"private {soname} provider did not become authoritative: {sorted(map(str, mapped))}"
            )


def _activate_openssl_runtime(private_prefix: Path) -> None:
    # Python's _hashlib loads the public base libcrypto.so.3 on first import.
    # FEniCS's locked libssl requires newer OPENSSL_3.6.0 symbols from the
    # matching private libcrypto. Bind that pair before importing hashlib or
    # anything else that could load the public SONAME into this process.
    for soname in ("libcrypto.so.3", "libssl.so.3"):
        provider = (private_prefix / "lib" / soname).resolve()
        if not provider.is_file():
            raise RuntimeError(f"private OpenSSL provider is missing: {provider}")
        mapped = _mapped_soname_paths(soname)
        competing = mapped - {provider}
        if competing:
            raise RuntimeError(
                f"competing {soname} provider was loaded before FEniCS activation: {sorted(map(str, competing))}"
            )
        ctypes.CDLL(str(provider), mode=os.RTLD_NOW | os.RTLD_GLOBAL)
        mapped = _mapped_soname_paths(soname)
        if mapped != {provider}:
            raise RuntimeError(
                f"private {soname} provider did not become authoritative: {sorted(map(str, mapped))}"
            )


def _write_jit_wrapper(runtime_root: Path) -> Path:
    global _JIT_WRAPPER
    if _JIT_WRAPPER is not None and _JIT_WRAPPER.exists():
        return _JIT_WRAPPER

    private_prefix = runtime_root / "fenics-runtime" / "conda"
    compiler = _private_compiler(private_prefix)
    sysroot = private_prefix / "x86_64-conda-linux-gnu" / "sysroot"
    python_include = runtime_root / "include" / "python3.14"
    private_include = private_prefix / "include"
    for required, label in (
        (sysroot, "bundled glibc-2.17 sysroot"),
        (python_include, "public CPython headers"),
        (private_include, "private FEniCS/PETSc headers"),
    ):
        if not required.exists():
            raise RuntimeError(f"{label} is missing from embedded runtime: {required}")

    wrapper_root = _wrapper_root(runtime_root)
    wrapper_root.mkdir(parents=True, exist_ok=True)
    wrapper = wrapper_root / "fenics-gcc"
    script = f"""#!/usr/bin/env bash
set -euo pipefail
compiler={shlex.quote(str(compiler))}
private_prefix={shlex.quote(str(private_prefix))}
sysroot={shlex.quote(str(sysroot))}
python_include={shlex.quote(str(python_include))}
private_include={shlex.quote(str(private_include))}

args=(
  "--sysroot=$sysroot"
  "-B$private_prefix/libexec/gcc/x86_64-conda-linux-gnu/16.2.0/"
  "-B$private_prefix/lib/gcc/x86_64-conda-linux-gnu/16.2.0/"
  "-B$private_prefix/x86_64-conda-linux-gnu/bin/"
  "-B$private_prefix/bin/"
  "-I$python_include"
  "-I$private_include"
  "-L$private_prefix/lib"
  "-Wl,-rpath-link,$private_prefix/lib"
)

# Ignore producer/ambient sysroot and compiler-search overrides.  The wrapper
# owns those inputs so a fresh FFCx JIT cannot drift to the host compiler SDK.
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sysroot|-isysroot|-B|-specs)
      shift 2
      ;;
    --sysroot=*|-isysroot*|-B*|-specs=*)
      shift
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done
exec "$compiler" "${{args[@]}}"
"""
    wrapper.write_text(script, encoding="utf-8")
    wrapper.chmod(
        stat.S_IRUSR
        | stat.S_IWUSR
        | stat.S_IXUSR
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH
    )
    _JIT_WRAPPER = wrapper
    return wrapper


def activate() -> Path:
    """Activate the bundled Linux FEnICS runtime from its installed location."""

    global _ACTIVATED_ROOT
    if not sys.platform.startswith("linux"):
        raise RuntimeError("fenics_embed_runtime Linux overlay requires Linux")
    if sys.version_info[:3] != (3, 14, 10):
        raise RuntimeError(
            "FEnICS embedded runtime requires CPython 3.14.10, "
            f"got {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )

    root = _runtime_root()
    if _ACTIVATED_ROOT is not None:
        if _ACTIVATED_ROOT != root:
            raise RuntimeError(
                f"FEnICS embedded runtime already activated from {_ACTIVATED_ROOT}; cannot switch to {root}"
            )
        return root

    private_prefix = root / "fenics-runtime" / "conda"
    private_site = private_prefix / "lib" / "python3.14" / "site-packages"
    public_site = root / "lib" / "python3.14" / "site-packages"
    gcc_specs_template = (
        private_prefix / "lib" / "gcc" / "x86_64-conda-linux-gnu" / "16.2.0" / "conda.specs"
    )
    for required, label in (
        (private_prefix / "lib", "private native-library root"),
        (private_site, "private FEnICS site-packages"),
        (public_site / "numpy", "public NumPy package"),
        (gcc_specs_template, "relocatable conda GCC specs template"),
    ):
        if not required.exists():
            raise RuntimeError(f"{label} is missing from embedded runtime: {required}")
    if _GCC_SPECS_PREFIX_TOKEN not in gcc_specs_template.read_text(encoding="utf-8"):
        raise RuntimeError("bundled conda GCC specs template lost its relocation token")

    authoritative_sites = [str(private_site), str(public_site)]
    normalized = {os.path.normcase(os.path.abspath(path)) for path in authoritative_sites}
    sys.path[:] = [
        path
        for path in sys.path
        if os.path.normcase(os.path.abspath(path)) not in normalized
    ]
    sys.path[:0] = authoritative_sites

    _activate_openssl_runtime(private_prefix)
    _activate_toolchain_runtime(private_prefix)
    wrapper = _write_jit_wrapper(root)
    private_bin = private_prefix / "bin"
    compiler = _private_compiler(private_prefix)
    binutils = _private_binutils(private_prefix)
    os.environ["PATH"] = os.pathsep.join(
        [str(wrapper.parent), str(private_bin), "/usr/bin", "/bin"]
    )
    for name in (
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "LIBRARY_PATH",
        "CPATH",
        "C_INCLUDE_PATH",
        "CPLUS_INCLUDE_PATH",
        "GCC_EXEC_PREFIX",
        "COMPILER_PATH",
    ):
        os.environ.pop(name, None)

    os.environ["PETSC_DIR"] = str(private_prefix)
    os.environ["PETSC_ARCH"] = ""
    os.environ["SLEPC_DIR"] = str(private_prefix)
    os.environ["CC"] = str(wrapper)
    os.environ["CXX"] = str(wrapper)
    os.environ["CPP"] = f"{shlex.quote(str(wrapper))} -E"
    os.environ["LDSHARED"] = f"{shlex.quote(str(wrapper))} -shared"
    os.environ["FENICS_JIT_COMPILER"] = str(wrapper)
    os.environ["FENICS_JIT_GCC"] = str(compiler)
    os.environ["FENICS_JIT_CONDA_SPECS_TEMPLATE"] = str(gcc_specs_template)
    os.environ["FENICS_JIT_SYSROOT"] = str(
        private_prefix / "x86_64-conda-linux-gnu" / "sysroot"
    )
    for variable, tool in binutils.items():
        os.environ[variable] = str(tool)
    os.environ["CFLAGS"] = "-O2 -fPIC"
    os.environ["CPPFLAGS"] = ""
    os.environ["LDFLAGS"] = "-Wl,-z,relro,-z,now"

    import petsc4py

    current_get_config = petsc4py.get_config
    if not getattr(current_get_config, "_fenics_embed_runtime", False):

        def relocated_get_config(_petsc_root=str(private_prefix)):
            return {"PETSC_DIR": _petsc_root, "PETSC_ARCH": ""}

        relocated_get_config._fenics_embed_runtime = True
        petsc4py.get_config = relocated_get_config

    import slepc4py

    current_slepc_get_config = slepc4py.get_config
    if not getattr(current_slepc_get_config, "_fenics_embed_runtime", False):

        def relocated_slepc_get_config(
            _get_config=current_slepc_get_config, _private_root=str(private_prefix)
        ):
            config = dict(_get_config())
            config.update(
                {
                    "SLEPC_DIR": _private_root,
                    "PETSC_DIR": _private_root,
                    "PETSC_ARCH": "",
                }
            )
            return config

        relocated_slepc_get_config._fenics_embed_runtime = True
        slepc4py.get_config = relocated_slepc_get_config

    _ACTIVATED_ROOT = root
    return root
