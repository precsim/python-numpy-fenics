# Producer support for a build-once embedded consumer

## Goal

The public producer publishes immutable runtime components that downstream
embedded CPython applications can reuse without rebuilding CPython, Tcl/Tk,
NumPy, FEniCS or their native dependencies. A downstream MATLAB/GNU Octave
bridge can compile its own native module once per platform and exact native
build inputs, then independently qualify and package the same module with the
producer's profiles.

The producer **does not build, cache, or distribute any private consumer MEX
implementation**. The bridge's source, MEX compiler, qualification, artifact
registry, and release remain downstream responsibilities.

## Existing producer handoff

The producer already exposes the essential public runtime inputs:

1. A fixed release tag, producer commit and platform/profile asset identity.
2. SHA-256 of the archive and embedded runtime manifest, plus qualification
   evidence for the supported platform/profile.
3. Native dependency/ABI and relocation evidence, with core, NumPy and FEniCS
   profile ownership and overlay rules.

Downstream consumers pin these identities, verify archive and per-file
manifest hashes, and stage the runtime offline for build/test/package steps.
The published rc2 artifact and reference staging integration are documented
in [consumer-integration-rc2.md](consumer-integration-rc2.md) and
[producer-consumer-boundary.md](producer-consumer-boundary.md).

## Improvement contract for future producer releases

To make reuse across consumer CI runs easier, publish a small machine-readable
**embedded build compatibility record** per platform, independently of each
runtime archive, either in the existing release manifest or as a versioned
sidecar with its own SHA-256. The record should have:

- schema version, target OS/architecture, CPython version/ABI and minimum
  supported host OS/deployment target;
- exact core archive name/SHA-256 and runtime-manifest SHA-256;
- ABI-relevant CPython header/library/framework file names and SHA-256,
  including any required include/link/search path metadata expressed relative
  to the staged producer root;
- Tcl/Tk runtime/ABI version and native-provider constraints;
- per-profile inheritance contract: whether NumPy/FEniCS changes leave the
  core interpreter/library/headers unchanged, and the immutable shared-file
  manifest to prove it;
- provider and native-closure metadata needed for a consumer's independent
  ELF/PE/Mach-O audit;
- a `build_compatibility_sha256` computed from canonical ABI-relevant
  content so unchanged compatible core bytes yield an unchanged compatibility
  identity even when a release tag changes.

This sidecar is **advisory input identity**, not authorization to skip
qualification. Every consumer must still independently verify all downloaded
runtime bytes and run its own real embedded-host acceptance tests.
An ABI-compatible core cannot promise compatibility with arbitrary consumer
source, compiler flags, or Octave/MATLAB ABI settings.

The producer's compatibility record must not include a downstream bridge
source digest, compiler selection, MEX artifacts, private token, private
repository URL, or consumer publication state. Consumer build keys combine
the producer identity with the exact consumer C source, toolchain and host ABI.

## Suggested independent producer tests

- Render the sidecar twice from the same pinned runtime manifest and assert
  byte-for-byte deterministic canonical JSON and stable SHA-256.
- Change a CPython development header/library, target ABI or native-provider
  constraint in a mock producer manifest: the compatibility identity changes.
- Change only release notes, qualification log metadata, or an optional
  profile-local payload while preserving core runtime bytes: the core
  compatibility identity stays stable.
- Compare the proposed shared-file membership against the already published
  per-platform runtime and overlay ownership manifests.
- Verify the public mock consumer can stage a core runtime by immutable
  identity without access to any private consumer code or credentials.

## Consumer-side requirement

The producer cannot guarantee that a consumer builds only once; that requires
the consumer CI to separate native compilation from qualification,
repackaging and publication, and to preserve the compiled binary in an
immutable content-addressed store. Its cache key must include the consumer
native inputs in addition to this producer compatibility record. A release
may reuse a previously compiled MEX while retaining independent package
revision and exact final-artifact provenance.

No existing producer release needs rebuilding just to implement the
consumer-side separation. The current pinned runtime manifest and hashes are
sufficient as an interim build key until the sidecar contract is implemented.
