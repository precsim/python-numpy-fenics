# python-numpy-fenics

Public producer and qualification repository for relocatable CPython runtime
profiles containing NumPy and optional FEniCSx/DOLFINx/PETSc support.

The repository is intentionally independent of any private MATLAB/Octave bridge
implementation. It produces immutable, checksummed runtime artifacts and
machine-readable qualification evidence that downstream consumers can verify
without exposing their source trees.

The initial migration is tracked on a feature branch. CI in this repository is
GitHub Actions only.
