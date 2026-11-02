# Producer / consumer boundary

## Objective

Keep dependency engineering and pre-release runtime qualification public without
exposing downstream consumer source.

The producer is trusted for runtime bytes, not for a downstream application's
implementation. The consumer is trusted for its own integration, not for
reconstructing the runtime dependency graph.

## Public producer responsibilities

The producer may acquire, build, filter, relocate and qualify CPython, NumPy,
DOLFINx, PETSc/petsc4py, mpi4py, Basix, FFCx, UFL, MPI runtime, JIT compiler
resources, third-party native libraries, and runtime bootstrap modules.

It must emit immutable artifacts, manifests, checksums and qualification
evidence.

## Private consumer responsibilities

A downstream private consumer should need only:

1. producer repository identity;
2. producer git revision or producer pre-release tag;
3. exact asset name;
4. asset SHA-256;
5. manifest SHA-256.

The consumer verifies these values, stages the artifact, executes its own real
host integration tests, and performs final product packaging.

The producer must never require a checkout or token for the private consumer.

## Public integration mocks

Public integration tests should reproduce only the runtime boundary:

- isolated CPython startup;
- deterministic package and native-library activation;
- optional zero-argument fenics_embed_runtime.activate();
- NumPy import and numerical probe;
- FEniCS import, fresh FFCx JIT and serial PETSc solve;
- poisoned ambient package/compiler/native-library state.

Mocks must not reproduce private application APIs or copy private host source.

## Artifact promotion

Producer artifacts may be published as GitHub pre-releases. A producer
pre-release is not a downstream product release and makes no claim that a
private consumer's final host qualification has passed.

The active pre-release contract requires the qualified `core` and `numpy`
profiles on all supported producer targets. FEniCS remains a retained,
explicitly opt-in profile and does not gate core/NumPy promotion while that work
is deferred.
