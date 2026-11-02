---
status: in-progress
priority: P0
---

# Windows x64 FEniCS overlay

## Goal

Reproduce the already-qualified public Windows CPython-3.12 FEniCS overlay
producer in this repository, preserving the standalone fresh-JIT/PETSc test and
removing any dependency on a private consumer.

## Acceptance

- [ ] GitHub Actions replays a checked-in, SHA-256-bound explicit FEniCS
      package transaction without dependency solving, then packages the
      relocatable overlay from that exact prefix.
- [ ] The overlay excludes producer CPython ownership.
- [ ] fenics_embed_runtime.activate() is zero-argument and idempotent.
- [ ] Exact manifest file hashes verify after relocation.
- [ ] Ambient Python/compiler state is poisoned during testing.
- [ ] Fresh FFCx JIT uses the bundled micro-Clang backend.
- [ ] Serial PETSc Poisson solve passes.
- [ ] The workflow uploads overlay, checksums, transaction and qualification
      evidence without reading any private repository.
