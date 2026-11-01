---
status: in-progress
priority: P0
---

# macOS arm64/x86_64 FEniCS overlays

## Goal

Reproduce the qualified relocatable macOS overlay producer using the exact
checked-in package locks, solver-free replay, bundled Zig 0.17.0 JIT and a
macOS 14.0 deployment floor.

## Acceptance

- [ ] arm64 and x86_64 package locks replay without dependency solving.
- [ ] Producer CPython and NumPy files are excluded.
- [ ] Existing locked packages are retained unless executable evidence requires
      an explicitly reviewed selective rebuild.
- [ ] Complete Mach-O closure is relocated and ad-hoc signed.
- [ ] Overlay archive reproduction is deterministic.
- [ ] Fresh FFCx JIT and serial PETSc solve pass with ambient tool discovery
      poisoned.
- [ ] GitHub Actions uploads immutable overlay/manifests/checksums/evidence.
