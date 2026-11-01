# Migration from the previous producer

This repository supersedes the producer role previously spread across
platform-specific CI and downstream runtime planning.

Initial migration sources are public producer assets only:

- Windows FEniCS relocatable overlay packaging and standalone qualification;
- Windows FEniCS package recipes/JIT work as needed for selective rebuilds;
- macOS exact package locks, solver-free replay, relocatable overlay packaging,
  Zig JIT bootstrap, Mach-O closure audit and deterministic archive production;
- shared fresh-FFCx/PETSc numerical probes.

Downstream private source trees are not migration inputs.

The old producer can remain available as historical evidence until this
repository has reproduced the corresponding GitHub Actions results. After
parity, the old producer should be marked superseded rather than maintained as a
second release authority.
