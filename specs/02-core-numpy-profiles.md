---
status: completed
priority: P1
depends_on: []
---

# Public core and NumPy runtime profiles

## Goal

Move controlled CPython core and cumulative NumPy runtime production from
downstream integration repositories into this producer.

## Requirements

- Produce Windows x64, Linux x86_64, macOS arm64 and macOS x86_64 CPython
  3.14.10 / cp314 runtime artifacts without application/MEX source.
- Make Tcl/Tk 8.6.18 an explicit producer-owned core dependency on every target.
- Preserve platform runtime floors and native-provider policies.
- NumPy 2.5.2 is layered on the exact matching core runtime.
- Linux retains a collision-safe private BLAS/LAPACK policy.
- macOS retains the qualified Apple Accelerate provider contract.
- Every profile emits a manifest, file hashes, native closure and standalone
  qualification evidence.
- Public mocks exercise embedding-relevant loader conflicts without copying a
  downstream consumer implementation.

## Qualification result

The core and NumPy profiles passed standalone qualification on Windows x64,
Linux x86_64 (glibc 2.17), macOS arm64 and macOS x86_64 under Rosetta 2 at
revision `95d56a5`. PR #3 merged that revision into `main` as `f6e6e77`.
See [the recorded jobs and promotion constraints](../docs/cp314-core-numpy-qualification.md).
Pre-release publication is a separate phase.
