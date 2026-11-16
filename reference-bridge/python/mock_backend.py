"""Fixed public backend for the reference MEX bridge.

The C bridge imports this module from a package-relative path under isolated
CPython.  Operations are deliberately narrow: one scalar transform, one NumPy
sum, one fresh serial FEniCS/FFCx/PETSc probe, and controlled runtime identity.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import platform
import sys
import tempfile


EXPECTED_PYTHON = "3.14.10"
EXPECTED_ABI = "cp314"
EXPECTED_NUMPY = "2.5.2"
MAX_NUMPY_N = 1000
FENICS_MAX_L2_ERROR = 5.0e-2
_PRODUCER = "precsim/python-numpy-fenics"

_PACKAGE_ROOT: Path | None = None
_RUNTIME_ROOT: Path | None = None
_PROFILE: str | None = None
_PYTHON_ABI: str | None = None


class ProfileUnavailableError(RuntimeError):
    """Raised when an operation is unavailable in the selected package profile."""


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read verified runtime identity {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"verified runtime identity {path.name} is not a JSON object")
    return value


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _validate_manifest(manifest: dict[str, object], *, expected_profile: str) -> None:
    if manifest.get("format_version") != 1:
        raise RuntimeError("runtime-manifest.json format_version is not 1")
    if manifest.get("python_abi") != EXPECTED_ABI:
        raise RuntimeError("runtime-manifest.json does not identify cp314")
    if manifest.get("profile") != expected_profile:
        raise RuntimeError(
            "runtime-manifest.json profile differs from the selected package: "
            f"{manifest.get('profile')!r} != {expected_profile!r}"
        )
    producer = manifest.get("producer")
    if not isinstance(producer, dict) or producer.get("repository") != _PRODUCER:
        raise RuntimeError("runtime-manifest.json does not identify the public producer")


def _validate_fenics_manifest(manifest: dict[str, object]) -> None:
    if manifest.get("format_version") != 1:
        raise RuntimeError("fenics-embed-manifest.json format_version is not 1")
    if manifest.get("profile") != "fenics" or manifest.get("python_abi") != EXPECTED_ABI:
        raise RuntimeError("fenics-embed-manifest.json does not identify the cp314 FEniCS overlay")

    producer = manifest.get("producer")
    if not isinstance(producer, dict) or producer.get("repository") != _PRODUCER:
        raise RuntimeError("fenics-embed-manifest.json does not identify the public producer")

    # Linux/macOS record the consumer patch directly.  The Windows overlay
    # predates that field and records the same authoritative identity under
    # python.version.  Accept both producer-owned manifest shapes.
    consumer_python = manifest.get("consumer_python")
    if consumer_python is None:
        python_identity = manifest.get("python")
        if isinstance(python_identity, dict):
            consumer_python = python_identity.get("version")
    if consumer_python != EXPECTED_PYTHON:
        raise RuntimeError(
            "fenics-embed-manifest.json does not bind the required CPython "
            f"{EXPECTED_PYTHON} consumer"
        )

    numpy_identity = manifest.get("numpy")
    if not isinstance(numpy_identity, dict) or numpy_identity.get("version") != EXPECTED_NUMPY:
        raise RuntimeError(
            "fenics-embed-manifest.json does not bind the required NumPy "
            f"{EXPECTED_NUMPY} base"
        )


def _validate_isolation(package_root: Path, runtime_root: Path) -> None:
    flags = sys.flags
    required_flags = {
        "isolated": flags.isolated,
        "ignore_environment": flags.ignore_environment,
        "no_site": flags.no_site,
        "no_user_site": flags.no_user_site,
        "safe_path": flags.safe_path,
    }
    missing = [name for name, value in required_flags.items() if not value]
    if missing:
        raise RuntimeError(f"embedded CPython isolation flags are disabled: {', '.join(missing)}")
    if not sys.dont_write_bytecode:
        raise RuntimeError("embedded CPython bytecode writing is not disabled")

    backend_root = (package_root / "python").resolve(strict=True)
    for raw in sys.path:
        if not raw:
            raise RuntimeError("embedded CPython sys.path contains an ambient empty entry")
        candidate = Path(raw).resolve(strict=False)
        if not (_inside(candidate, runtime_root) or _inside(candidate, backend_root)):
            raise RuntimeError(f"embedded CPython sys.path escaped the package: {candidate}")


def _configure(package_root_text: str, runtime_root_text: str, expected_profile: str) -> str:
    """Bind the backend once to the package and validate local runtime identity."""

    global _PACKAGE_ROOT, _RUNTIME_ROOT, _PROFILE, _PYTHON_ABI

    if expected_profile not in {"core", "numpy", "fenics"}:
        raise RuntimeError(f"unsupported package profile marker: {expected_profile!r}")
    package_root = Path(package_root_text).resolve(strict=True)
    runtime_root = Path(runtime_root_text).resolve(strict=True)
    if not runtime_root.is_dir() or runtime_root.parent != package_root or runtime_root.name != "runtime":
        raise RuntimeError("runtime root is not the canonical package-relative runtime directory")

    backend_path = Path(__file__).resolve(strict=True)
    expected_backend = (package_root / "python" / "mock_backend.py").resolve(strict=True)
    if backend_path != expected_backend:
        raise RuntimeError(f"mock_backend imported from unexpected location: {backend_path}")

    try:
        profile = (package_root / "profile.txt").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"cannot read package profile marker: {exc}") from exc
    if profile != expected_profile:
        raise RuntimeError(
            f"package profile marker differs from bridge selection: {profile!r} != {expected_profile!r}"
        )

    if sys.implementation.name != "cpython" or platform.python_version() != EXPECTED_PYTHON:
        raise RuntimeError(
            f"reference bridge requires CPython {EXPECTED_PYTHON}, got "
            f"{sys.implementation.name} {platform.python_version()}"
        )
    if sys.version_info[:3] != (3, 14, 10):
        raise RuntimeError(f"running interpreter does not have the required {EXPECTED_ABI} patch baseline")

    base_manifest = _read_json(runtime_root / "runtime-manifest.json")
    base_profile = "numpy" if profile == "fenics" else profile
    _validate_manifest(base_manifest, expected_profile=base_profile)
    python_abi = base_manifest.get("python_abi")
    if not isinstance(python_abi, str):
        raise RuntimeError("runtime-manifest.json python_abi is not text")

    marker = runtime_root / "RUNTIME_PROFILE"
    if marker.is_file():
        marker_profile = marker.read_text(encoding="utf-8").strip()
        if marker_profile != base_profile:
            raise RuntimeError(
                f"runtime profile marker differs from runtime-manifest.json: {marker_profile!r}"
            )

    if profile == "fenics":
        overlay = _read_json(runtime_root / "fenics-embed-manifest.json")
        _validate_fenics_manifest(overlay)

    _validate_isolation(package_root, runtime_root)

    if _PACKAGE_ROOT is not None:
        if (_PACKAGE_ROOT, _RUNTIME_ROOT, _PROFILE, _PYTHON_ABI) != (
            package_root,
            runtime_root,
            profile,
            python_abi,
        ):
            raise RuntimeError("mock backend cannot switch package identity within one interpreter")
        return profile

    _PACKAGE_ROOT = package_root
    _RUNTIME_ROOT = runtime_root
    _PROFILE = profile
    _PYTHON_ABI = python_abi
    return profile


def _configured() -> tuple[Path, str]:
    if _RUNTIME_ROOT is None or _PROFILE is None:
        raise RuntimeError("mock backend was called before runtime configuration")
    return _RUNTIME_ROOT, _PROFILE


def _require_profile(operation: str, allowed: set[str]) -> tuple[Path, str]:
    runtime_root, profile = _configured()
    if profile not in allowed:
        allowed_text = "/".join(sorted(allowed))
        raise ProfileUnavailableError(
            f"operation {operation!r} is unavailable in profile {profile!r}; requires {allowed_text}"
        )
    return runtime_root, profile


def _require_runtime_module(module: object, name: str, runtime_root: Path) -> None:
    raw = getattr(module, "__file__", None)
    if not isinstance(raw, str):
        raise RuntimeError(f"{name} has no package file identity")
    location = Path(raw).resolve(strict=True)
    if not _inside(location, runtime_root):
        raise RuntimeError(f"{name} imported outside the verified runtime root: {location}")


def scalar(value: float) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise TypeError("scalar requires one finite Python float")
    result = 2.0 * value + 1.0
    if not math.isfinite(result):
        raise ValueError("scalar result is non-finite")
    return result


def numpy_sum_squares(n: int) -> float:
    runtime_root, _ = _require_profile("numpy", {"numpy", "fenics"})
    if type(n) is not int or not 1 <= n <= MAX_NUMPY_N:
        raise ValueError(f"numpy requires an integer in [1, {MAX_NUMPY_N}]")

    import numpy as np

    if np.__version__ != EXPECTED_NUMPY:
        raise RuntimeError(f"NumPy version mismatch: {np.__version__} != {EXPECTED_NUMPY}")
    _require_runtime_module(np, "numpy", runtime_root)
    values = np.arange(1, n + 1, dtype=np.float64)
    result = float(np.sum(values * values, dtype=np.float64))
    if not math.isfinite(result):
        raise RuntimeError("NumPy sum-of-squares result is non-finite")
    return result


def fenics_poisson() -> float:
    runtime_root, _ = _require_profile("fenics", {"fenics"})

    import fenics_embed_runtime

    activated = Path(fenics_embed_runtime.activate()).resolve(strict=True)
    if activated != runtime_root:
        raise RuntimeError(f"FEniCS activation escaped runtime root: {activated} != {runtime_root}")
    if _PACKAGE_ROOT is None:
        raise RuntimeError("mock backend lost its configured package identity")
    _validate_isolation(_PACKAGE_ROOT, runtime_root)

    import basix
    import basix.ufl
    import numpy as np
    import petsc4py
    import ufl
    from dolfinx import fem, mesh
    import dolfinx
    from dolfinx.fem.petsc import LinearProblem
    import mpi4py
    from mpi4py import MPI

    if np.__version__ != EXPECTED_NUMPY:
        raise RuntimeError(f"NumPy version mismatch: {np.__version__} != {EXPECTED_NUMPY}")
    for module, name in (
        (np, "numpy"),
        (basix, "basix"),
        (dolfinx, "dolfinx"),
        (mpi4py, "mpi4py"),
        (petsc4py, "petsc4py"),
        (ufl, "ufl"),
    ):
        _require_runtime_module(module, name, runtime_root)

    # Match tests/fenics/test_poisson.py, but use COMM_SELF and a fresh cache
    # directory so every bridge request exercises new FFCx compilation/JIT.
    nr, nt = 16, 32
    angles = np.linspace(0.0, 2.0 * np.pi, nt, endpoint=False)
    points = [(0.0, 0.0)] + [
        (radius * np.cos(angle), radius * np.sin(angle))
        for radius in np.linspace(0.0, 1.0, nr + 1)[1:]
        for angle in angles
    ]
    cells = [(0, 1 + index, 1 + (index + 1) % nt) for index in range(nt)]
    for ring in range(nr - 1):
        inner = 1 + ring * nt
        outer = 1 + (ring + 1) * nt
        for index in range(nt):
            a1 = inner + index
            a2 = inner + (index + 1) % nt
            b1 = outer + index
            b2 = outer + (index + 1) % nt
            cells.extend(((a1, a2, b2), (a1, b2, b1)))

    domain = mesh.create_mesh(
        MPI.COMM_SELF,
        np.asarray(cells, dtype=np.int64),
        ufl.Mesh(basix.ufl.element("Lagrange", "triangle", 1, shape=(2,))),
        np.asarray(points, dtype=np.float64),
    )
    space = fem.functionspace(domain, ("Lagrange", 1))
    domain.topology.create_connectivity(domain.topology.dim - 1, domain.topology.dim)
    boundary = fem.dirichletbc(
        0.0,
        fem.locate_dofs_topological(
            space,
            domain.topology.dim - 1,
            mesh.exterior_facet_indices(domain.topology),
        ),
        space,
    )
    trial = ufl.TrialFunction(space)
    test = ufl.TestFunction(space)
    cache_dir = tempfile.mkdtemp(prefix="reference-bridge-ffcx-")
    problem = LinearProblem(
        ufl.dot(ufl.grad(trial), ufl.grad(test)) * ufl.dx,
        fem.Constant(domain, 4.0) * test * ufl.dx,
        bcs=[boundary],
        petsc_options={"ksp_type": "preonly", "pc_type": "lu"},
        petsc_options_prefix="reference_bridge_poisson",
        jit_options={"cache_dir": cache_dir},
    )
    solution = problem.solve()

    exact = fem.Function(space)
    exact.interpolate(lambda x: 1.0 - x[0] ** 2 - x[1] ** 2)
    error = float(
        np.sqrt(
            domain.comm.allreduce(
                fem.assemble_scalar(fem.form((solution - exact) ** 2 * ufl.dx)),
                op=MPI.SUM,
            )
        )
    )
    if not math.isfinite(error) or error > FENICS_MAX_L2_ERROR:
        raise RuntimeError(
            f"Poisson solve failed numerical accuracy check: L2 error {error!r} > {FENICS_MAX_L2_ERROR}"
        )
    return error


def runtime_info() -> tuple[str, str, str, str]:
    runtime_root, profile = _configured()
    if _PYTHON_ABI is None:
        raise RuntimeError("mock backend lost its verified Python ABI identity")
    version = platform.python_version()
    if version != EXPECTED_PYTHON or sys.version_info[:3] != (3, 14, 10):
        raise RuntimeError(f"running interpreter identity changed: {version}")
    return version, _PYTHON_ABI, str(runtime_root), profile
