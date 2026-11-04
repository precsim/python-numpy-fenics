"""Public embedded-host boundary probe; run with the staged CPython -B -I.

Usage: <staged-python> -B -I tests/consumer/embedded_host_smoke.py tests/fenics/test_poisson.py
"""

from pathlib import Path
import sys

root = Path(sys.prefix).resolve()
import fenics_embed_runtime

assert fenics_embed_runtime.activate() == root, "runtime activation escaped staged prefix"

import numpy
import dolfinx
import petsc4py
from mpi4py import MPI
from dolfinx import mesh

assert numpy.__version__ == "2.5.2"
assert Path(numpy.__file__).resolve().is_relative_to(root), "NumPy imported outside staged runtime"
assert Path(dolfinx.__file__).resolve().is_relative_to(root), "DOLFINx imported outside staged runtime"
assert Path(petsc4py.__file__).resolve().is_relative_to(root), "petsc4py imported outside staged runtime"
mesh.create_unit_square(MPI.COMM_SELF, 4, 4)
print("consumer_embedded_import=pass", flush=True)

if len(sys.argv) != 2:
    raise SystemExit("provide the public Poisson/FFCx JIT test script")
poisson = Path(sys.argv[1]).resolve(strict=True)
exec(compile(poisson.read_text(encoding="utf-8"), str(poisson), "exec"),
     {"__name__": "__main__", "__file__": str(poisson)})
