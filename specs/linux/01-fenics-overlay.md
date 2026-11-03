---
status: draft
priority: P1
---

# Linux x86_64 glibc-2.17 FEniCS overlay

## Goal

Produce a deterministic CPython-3.14.10 / cp314 FEniCS overlay from the exact
124-record Linux x86_64 MPICH transaction and qualify it for the glibc >= 2.17
runtime contract.

The earlier feasibility work used a separate 128-record cp312 transaction. Its
stock-ABI, native-rebuild and provider evidence is retained as historical
provenance only; it is not the current package identity contract and must not be
used as cp314 qualification evidence.

## Design

- Reuse existing locked binary packages first.
- Replay and verify the checked-in exact 124-record cp314 package transaction
  before invoking the overlay packager; the packager never performs dependency
  solving.
- Keep the locked MPICH 5.0.1 provider as the sole MPI runtime provider.
- Preserve reviewed older Python build labels only where the cp314 lock records
  conda-forge stable-ABI/native companion packages; immutable identities are
  never relabeled to claim cp314 compatibility.
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
124-record cp314 lock, and a qualified public Linux NumPy base runtime. It
filters producer CPython/NumPy ownership, retains the stock FEniCS/PETSc/MPICH
closure and the locked GCC/glibc-2.17 sysroot needed for fresh FFCx JIT,
rewrites shipped ELF search paths to `$ORIGIN`-relative RPATHs, retains the
locked conda GCC specs as a prefix-tokenized relocation template, and rejects
unresolved producer prefix references or forbidden MPI/GPU providers. The
runtime wrapper supplies the equivalent private include/lib/sysroot search
explicitly so fresh JIT output does not embed an absolute runtime path.

`scripts/linux/test-fenics-overlay.sh` is the standalone qualification gate for
the archive on glibc 2.17 after relocation to a path containing spaces. It must
prove the inherited NumPy bytes are unchanged, activation wins over hostile
Python/compiler/native state, both NumPy-first and PETSc-first import orders
keep the private NumPy BLAS provider separate from the FEniCS BLAS provider,
exactly one MPICH provider is loaded, and fresh FFCx JIT plus the serial PETSc
Poisson solve pass with the bundled GCC/sysroot. That independent qualification
has not yet promoted the current cp314 candidate to release eligibility.

## Readiness blockers

- Independently audit the exact selective-rebuild candidate and bind the
  post-rebuild native-closure report to its candidate output identity.
- Prove the KaHIP/ADIOS2/ZeroMQ omission boundary and complete ICU resolution,
  including the independent MPICH → hwloc → libxml2 branch.
- Finalize and package only the closure-bound candidate, then pass standalone
  glibc-2.17 ABI, relocation, fresh-FFCx-JIT, PETSc and Poisson qualification.
- Promote source-rebuild provenance and release eligibility only from those
  positive artifact-bound qualification results.
