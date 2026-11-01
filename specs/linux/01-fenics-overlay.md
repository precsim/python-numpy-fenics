---
status: draft
priority: P1
---

# Linux x86_64 glibc-2.17 FEniCS overlay

## Goal

Produce a deterministic CPython-3.12 FEniCS overlay from an exact locked
existing package transaction and qualify it for the glibc >= 2.17 runtime
contract.

## Design

- Reuse existing locked binary packages first.
- Exclude producer CPython and NumPy ownership from the overlay.
- Prove whether stock PETSc/DOLFINx can coexist with the selected NumPy
  BLAS/LAPACK provider without generic-symbol interposition problems.
- Rebuild only the smallest incompatible native component set when executable
  evidence proves stock binaries cannot meet the contract.
- Bundle a private serial MPI runtime and fresh-JIT toolchain/sysroot.
- Audit the complete ELF closure and generated JIT module at the required
  glibc/C++/GCC symbol ceilings.

## Readiness blockers

- Freeze the exact Linux package transaction and per-package hashes.
- Settle the collision-safe BLAS/LAPACK binding using stock binaries first.
- Pin the fresh-JIT compiler/sysroot and complete private native closure.
