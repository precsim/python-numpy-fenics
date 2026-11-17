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

Public integration tests reproduce only the runtime boundary:

- isolated CPython startup;
- deterministic package and native-library activation;
- optional zero-argument fenics_embed_runtime.activate();
- NumPy import and numerical probe;
- FEniCS import, fresh FFCx JIT and serial PETSc solve;
- poisoned ambient package/compiler/native-library state.

Mocks must not reproduce private application APIs or copy private host source.

The public reference-bridge/ is the executable mock for this contract. It has
one intentionally small host API for scalar, runtime, NumPy and FEniCS probes.
It is independently authored and is not an API compatibility target for a
downstream bridge.

## Reusable public consumer kit

consumer-kit/ owns the generic mechanics required to consume a qualified
producer runtime:

- immutable release receipt and qualification verification;
- safe, transactional runtime staging and offline full-tree re-attestation;
- source-manifest validation and target-specific MEX compilation;
- MEX export/load-time dependency auditing and GNU Octave execution;
- exact candidate SHA-256 qualification records;
- reference package assembly, offline verification and local test launchers.

The kit never resolves Python dependencies, rebuilds a producer runtime, changes
producer-owned payloads, or reads a private repository. A downstream consumer
may vendor the kit at a recorded upstream revision/tree hash while keeping its
actual source manifest, bridge source, product tests and release policy private.

Passing the public reference bridge qualifies the generic kit path. A private
consumer must still compile and qualify its own binary against every profile and
target it claims to support.

## Artifact promotion

Producer artifacts may be published as GitHub pre-releases. A producer
pre-release is not a downstream product release and makes no claim that a
private consumer's final host qualification has passed.

The pre-release contract requires qualified `core` and `numpy` profiles on
all supported producer targets. FEniCS remains an explicitly opt-in profile:
it was included in `runtime-v0.1.0-rc2` following qualification and does not
otherwise gate core/NumPy-only promotion.
