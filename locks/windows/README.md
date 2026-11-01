# Windows FEnICS package transaction

The Windows x64 FEnICS producer replays a checked-in package transaction. Release
qualification must not ask a dependency solver to reconstruct this environment.

The initial transaction was frozen on 2026-11-01 with micromamba 2.9.0 from a
strict-priority dry-run solve using, in order:

1. `precise-simulation`
2. `conda-forge`

The requested specs were:

- `python=3.12.10`
- `numpy=2.5.2`
- `libblas=*=*openblas`
- `fenics-dolfinx`

`windows-x64-fenics-explicit.txt` preserves the resolved URL order.
`windows-x64-fenics-package-lock.json` records each package URL, version, build,
subdir, MD5 and SHA-256. `scripts/windows/package_lock.py` binds the source
explicit file by SHA-256 and renders a CEP-23-style `@EXPLICIT` transaction with
per-package SHA-256 fragments.

The source explicit file SHA-256 is:

`e33a118540b9c5d1c954c4d4cd86a2f7343530a9852eb203ea16ee7aa84d9afa`

Freezing the transaction is not itself qualification. GitHub Actions must replay
the exact transaction, verify installed package metadata, package the overlay,
and pass relocation, fresh FFCx JIT and PETSc numerical tests before the
transaction is treated as producer-qualified.
