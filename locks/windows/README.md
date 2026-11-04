# Windows FEnICS package transaction

The Windows x64 FEnICS producer replays a checked-in package transaction. Release
qualification must not ask a dependency solver to reconstruct this environment.

The cp314 transaction was refreshed on 2026-11-03 with micromamba 2.9.0 from a
strict-priority dry-run solve using, in order:

1. `precise-simulation`
2. `conda-forge`

The requested specs were:

- `python=3.14.10`
- `numpy=2.5.2`
- `libblas=*=*openblas`
- `fenics-dolfinx`

The solved environment has a CPython 3.14.10 interpreter, `python_abi=3.14`
with a `cp314` build, and cp314 NumPy, mpi4py and CFFI packages. Six FEniCS
and PETSc packages remain published with `py312` build labels:
`fenics-basix`, `fenics-basix-nanobind-abi`, `fenics-dolfinx`,
`fenics-libbasix`, `fenics-libdolfinx`, and `petsc4py`. The Python
extension packages `fenics-basix`, `fenics-dolfinx`, and `petsc4py`
declare conda-forge's `_python_abi3_support` stable-ABI contract with a
CPython >=3.12 floor; the other py312-labelled entries are the exact native/ABI
companions selected by that solve. The checked-in lock preserves all six
package identities exactly; it does not relabel them as py314. Qualification
must execute the unchanged payloads under CPython 3.14.10.

`windows-x64-fenics-explicit.txt` preserves the resolved URL order.
`windows-x64-fenics-package-lock.json` records each package URL, version, build,
subdir, MD5 and SHA-256. `scripts/windows/package_lock.py` binds the source
explicit file by SHA-256 and renders a CEP-23-style `@EXPLICIT` transaction with
per-package SHA-256 fragments.

The source explicit file SHA-256 is:

`f39c66f7739b1154b7c930c085924e73df89578da260e714c293bb923a869d00`

Freezing the transaction is not itself qualification. GitHub Actions must replay
the exact transaction, verify installed package metadata, package the overlay,
and pass relocation, fresh FFCx JIT and PETSc numerical tests before the
transaction is treated as producer-qualified.
