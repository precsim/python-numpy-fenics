---
status: in-progress
priority: P1
depends_on: []
---

# Public core and NumPy runtime profiles

## Goal

Move controlled CPython core and cumulative NumPy runtime production from
downstream integration repositories into this producer.

## Requirements

- Produce Windows x64, Linux x86_64, macOS arm64 and macOS x86_64 CPython 3.12
  runtime artifacts without application/MEX source.
- Preserve platform runtime floors and native-provider policies.
- NumPy 2.5.2 is layered on the exact matching core runtime.
- Linux retains a collision-safe private BLAS/LAPACK policy.
- macOS retains the qualified Apple Accelerate provider contract.
- Every profile emits a manifest, file hashes, native closure and standalone
  qualification evidence.
- Public mocks exercise embedding-relevant loader conflicts without copying a
  downstream consumer implementation.
