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
- Replay and verify the checked-in exact package transaction before invoking the
  overlay packager; the packager never performs dependency solving.
- Exclude producer CPython and NumPy ownership from the overlay.
- Prove whether stock PETSc/DOLFINx can coexist with the selected NumPy
  BLAS/LAPACK provider without generic-symbol interposition problems.
- Rebuild only the smallest incompatible native component set when executable
  evidence proves stock binaries cannot meet the contract.
- Bundle a private serial MPI runtime and fresh-JIT toolchain/sysroot.
- Audit the complete ELF closure and generated JIT module at the required
  glibc/C++/GCC symbol ceilings.
- Layer retained packages below `fenics-runtime/conda`, with the activation
  bootstrap installed into the public NumPy base `site-packages` only when the
  overlay is applied.
- Emit a deterministic `tar.gz`, FEniCS manifest, exact package/source
  provenance, native dependency census with measured GLIBCXX/CXXABI/GCC maxima,
  and license inventory. The stock closure and freshly generated JIT modules
  must not exceed GLIBC 2.17, GLIBCXX 3.4.19, CXXABI 1.3.7 (with nonnumeric
  `CXXABI_TM_1` allowed), or GCC 4.8.0. A stock-package failure at one of these
  executable gates is rebuild evidence; it is not a reason to weaken the
  baseline silently.

## Executable contract

`scripts/linux/package_fenics_overlay.py` consumes a prefix already verified by
`scripts/linux/package_lock.py verify-prefix`, its package cache, the checked-in
lock, and a qualified public Linux NumPy base runtime. It filters producer
CPython/NumPy ownership, retains the stock FEniCS/PETSc/MPICH closure and the
locked GCC/glibc-2.17 sysroot needed for fresh FFCx JIT, rewrites shipped ELF
search paths to `$ORIGIN`-relative RPATHs, retains the locked conda GCC specs as
a prefix-tokenized relocation template, and rejects unresolved producer prefix
references or forbidden MPI/GPU providers. The runtime wrapper supplies the
equivalent private include/lib/sysroot search explicitly so fresh JIT output
does not embed an absolute runtime path.

`scripts/linux/test-fenics-overlay.sh` qualifies the archive on glibc 2.17 after
relocation to a path containing spaces. It proves the inherited NumPy bytes are
unchanged, activation wins over hostile Python/compiler/native state, both
NumPy-first and PETSc-first import orders keep the private NumPy BLAS provider
separate from the stock FEniCS BLAS provider, exactly one MPICH provider is
loaded, and fresh FFCx JIT plus the serial PETSc Poisson solve pass with the
bundled GCC/sysroot.

## Readiness blockers

- Settle the collision-safe BLAS/LAPACK binding using stock binaries first.
- Execute the new packager/qualification path on the glibc-2.17 Linux producer
  and record whether the locked stock ELF/JIT closure satisfies the GLIBC 2.17
  floor without rebuild.
- Wire the qualified runtime/overlay slice into the Linux FEniCS GitHub Actions
  workflow in the follow-up workflow phase.
