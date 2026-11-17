---
status: draft
priority: P1
depends_on:
  - 02-core-numpy-profiles.md
---

# Public reference bridge and reusable consumer kit

## Detailed mock bridge specification

The authoritative source/API/functionality and acceptance design for the public
mock is [04a-mock-bridge-specification.md](04a-mock-bridge-specification.md).
Implement the mock-facing parts of phases 03-05 against that spec. The present
plan owns the shared consumer kit, GitHub Actions orchestration, artifact
publication and private-consumer handoff; the linked spec owns the mock's
operations, C/Python integration, host behavior and exact smoke expectations.

## Goal

Make this repository the public, executable reference for consuming its immutable
CPython/NumPy/FEniCS runtimes from a minimal MATLAB/GNU Octave MEX bridge. Keep
application-specific source private. The private `python-matlab-octave-bridge`
repository should be able to vendor the same consumer build/stage/verify/package
tooling, provide its own sources and tests, and qualify a production MEX without
maintaining a parallel runtime integration toolchain.

**All build, CI qualification, and release packaging must run on GitHub Actions
runners.** The optionally published reference-bridge package must also support
download, integrity verification, extraction, and independent local execution on
a compatible host, without a compiler, source checkout, network connection after
download, or rebuilding any runtime or MEX files. Local execution is an
additional user acceptance path, not a required production build step.

## Boundaries and ownership

- The existing producer remains the only authority for CPython 3.14.10/cp314,
  Tcl/Tk 8.6.18, NumPy 2.5.2, FEniCS dependencies, native closure, and qualified
  immutable runtime bytes.
- `consumer-kit/` owns reusable consumer-side receipt verification, transactional
  staging, source-configured MEX builds, host execution, exact-artifact checks,
  package assembly, and manifests. It cannot solve dependencies, build producer
  runtime packages, or modify producer-owned files and links.
- `reference-bridge/` owns new, minimal public mock source, a mock Python backend,
  the Octave smoke test, and the source selection manifest. Its interface is
  deliberately minimal and does not reproduce private application APIs, private
  source files, or private release packaging.
- The private bridge owns its actual C/Python/MATLAB sources, richer functional
  tests, target capability claims, final product package, and releases. Public
  acceptance proves the *generic* consumer integration path; the private repo
  must still independently qualify its actual binary.
- All public CI uses GitHub Actions. Third-party actions are pinned to immutable
  commit SHAs. No CircleCI configuration, API, credentials, or gates are added.

## Proposed layout

```text
consumer-kit/
  README.md
  contracts/
    bridge-build-v1.schema.json
    consumer-runtime-receipt-v1.schema.json
    reference-package-v1.schema.json
  tools/
    stage-runtime.py           # immutable receipt, cache and transaction
    verify-runtime.py          # offline, full-tree producer re-attestation
    build.ps1                  # Windows MSVC MEX compile only
    build-linux.sh             # Linux GCC MEX compile only
    build-macos.sh             # macOS Clang MEX compile only
    test-octave.ps1            # Windows host invocation
    test-octave-linux.sh       # Linux host invocation
    test-octave-macos.sh       # macOS host invocation
    package-reference.py      # package exact qualified inputs
    verify-package.py         # offline manifest/SHA/inventory verification
reference-bridge/
  bridge-build.json
  src/mock_bridge.c
  python/mock_backend.py
  host/mock_smoke.m
  selftest/                    # tiny portable invocation fixtures
locks/consumer/               # immutable runtime inputs for reference CI
tests/reference-bridge/       # packaging and negative contract tests
.github/workflows/
  qualify-reference-bridge.yml
  publish-reference-bridge.yml
```

The listed paths are design targets, not a requirement to create empty files.
The platform scripts should share contract verification and data-handling code
where possible; workflows invoke scripts instead of duplicating logic in YAML.
Existing `scripts/consumer/stage_qualified_fenics.py`,
`tests/consumer/embedded_host_smoke.py`, and
`tests/integration/runtime_contract_probe.py` are starting references. Consolidate
overlapping logic when implementing the kit rather than retaining two divergent
consumer staging systems.

## Build and runtime contracts

1. **Source-driven MEX build.** A versioned `bridge-build.json` declares common
   sources, target-specific sources and exports, Python helper files, Octave
   fixtures, and the MEX output name. Resolve and validate the manifest before
   compiling; reject missing paths, path traversal, duplicate inputs, and unsafe
   output destinations. For the reference target it selects only independently
   authored mock source. The private consumer uses the same schema with its own
   implementation sources. Platform flags and toolchains remain in the kit.
2. **Immutable runtime input.** Select each `core`, `numpy`, or `fenics`
   profile by a source-controlled receipt containing producer repository,
   release tag/revision, archive asset, archive SHA-256, embedded manifest
   SHA-256, and matching qualification evidence. Verify the exact producer
   tree before use, including symlinks and permissions where applicable.
   Preserve separate rc1 NumPy-base binding for the existing rc2 FEniCS overlay;
   do not silently substitute the independent rc2 NumPy profile. Updating the
   producer family requires an explicit reviewed receipt change.
3. **Build once per target/revision.** Each target compiles one MEX from one
   source revision/configuration with the pinned producer cp314 headers, records
   its SHA-256, and uploads the exact binary. Its `core`, `numpy`, and optional
   `fenics` qualifications consume that same binary. If FEniCS initialization
   requires bridge support, make it runtime-selected within the common MEX so
   another profile build is unnecessary. Any change to the MEX source/config or
   toolchain identity creates a new candidate that must be requalified.
4. **No implicit staging or rebuilding.** Only an explicit acquisition/stage
   step can download verified producer artifacts. Build, test, and package
   commands use already-staged inputs and fail if absent or mutated. Packaging
   consumes the previously qualified MEX bytes without recompilation.
5. **MEX portability.** Export only the required host MEX entry points; load
   the host API and CPython dynamically. Do not link against MATLAB, Octave,
   libpython, NumPy, BLAS/LAPACK, MPI, or PETSc as load-time dependencies.
   Enforce target-specific PE/ELF/Mach-O symbol and native-provider audits,
   including Linux glibc >= 2.17 and existing symbol ceilings.
6. **Distinct identities.** Runtime artifact identity, mock MEX identity,
   complete reference package identity, and any private consumer artifact
   identity are separate. The reference mock must never be promoted or described
   as the actual private bridge.

## Minimal executable reference behavior

The public C mock should expose a real host-callable MEX entry point, resolve
Octave's MEX API dynamically, validate one scalar double input, start the staged
CPython in isolated mode, import `mock_backend.py`, call its scalar operation,
convert one scalar double result to a host value, and report useful errors.
Verify a second call in the same host process and a clean fresh process. This
exercises loading, CPython lifetime, paths, and return conversion with little
source. Do not copy or reconstruct the private bridge's conversion/callback/API
implementation.

Use the **same compiled mock MEX** for the following independent profiles:

| Profile | GitHub-runner execution checks |
| --- | --- |
| `core` | Octave loads the MEX; scalar round-trip; isolated CPython 3.14.10; controlled Python/module paths; negative NumPy import; relocation and hostile ambient Python state. |
| `numpy` | Core behavior plus NumPy import/version/location and a small numerical computation called through the MEX. |
| `fenics` | Core and NumPy behavior plus exact FEniCS-bound NumPy base, optional `fenics_embed_runtime.activate()`, fresh FFCx JIT and serial PETSc/Poisson execution through the embedded backend. |

Add native-provider collision probes and manifest/overlay ownership checks where
each target supports them. Record explicit `not-applicable` results with a
platform explanation where a required collision cannot be constructed. A direct
Python smoke test can help diagnose failures, but it must not replace the
Octave-to-MEX-to-CPython acceptance path.

## GitHub Actions build/qualification design

Use a dedicated `qualify-reference-bridge.yml` with matrix entries for Windows
x64, Linux x86_64 (glibc 2.17 compatibility), macOS arm64, and macOS x86_64
(Rosetta 2 when required). Use supported GitHub-hosted runner environments;
the Linux compatibility build/ABI test may run in a pinned manylinux container
on a GitHub-hosted Linux runner. Pin the compiler/container inputs and Octave
version, and record the effective runner/toolchain identity. Any macOS runner
limitations must be exposed as unqualified instead of silently passing.

For each target, orchestrate the following dependency chain:

```text
validate schema, scripts, source, receipt
                 |
                 v
download/verify pinned producer runtime archives (reuse immutable cache)
                 |
                 v
compile one reference MEX -> SHA-256 + source/toolchain provenance
                 |
           +-----+-----+
           |     |     |
         core  numpy  fenics (optional)
           |     |     |
           +-----+-----+
                 |
qualify identical MEX SHA-256 across selected profiles
                 |
package exact MEX + immutable runtimes + mock fixtures
                 |
extract package into a new path, verify, rerun Octave smoke
                 |
publish qualifying artifact for download / promotion
```

Within a runner, a matrix or sequential profile loop may be used depending on
artifact size and GitHub storage costs. If tests fan out across jobs, distribute
one previously built candidate using GitHub Actions artifacts; verify its SHA-256
in every job. Do not build separate binaries in profile jobs. Never rebuild the
producer runtime. Cache published immutable inputs by repository/tag/SHA;
qualification and packaging bind to asset hashes rather than trusting a cache
key or mutable branch. Use concurrency cancellation for superseded PR runs and
path filters to avoid costly qualification on documentation-only changes.

The normal PR gate runs repository validation, contract tests, and applicable
reference-bridge qualification. Explicit `workflow_dispatch` supports targeting
all four platforms and selecting `core`, `core+numpy`, or the full
`core+numpy+fenics` matrix, subject to existing producer FEniCS readiness.
Only evidence backed by successful qualification on the intended source revision
can be promoted as a downloadable release. This new bridge workflow must not
become a prerequisite for existing producer runtime release gates until its
required scope is complete and explicitly adopted.

## Optional downloadable, independently testable mock

`publish-reference-bridge.yml` is an explicit GitHub Actions workflow. It
collects completed target qualifications from the same intended source revision,
checks the source/build/producer hashes, and optionally publishes a GitHub
**pre-release** such as `reference-bridge-vX.Y.Z-rcN`. Publication is independent
from producer runtime pre-releases and is never automatic on a PR. The workflow
must reject an existing tag/asset instead of overwriting it.

Produce a standalone package **per platform and profile**, e.g.:

```text
reference-bridge-linux-x64-cp314-core-vX.Y.Z-rcN.tar.gz
reference-bridge-linux-x64-cp314-numpy-vX.Y.Z-rcN.tar.gz
reference-bridge-linux-x64-cp314-fenics-vX.Y.Z-rcN.tar.gz
reference-bridge-win-x64-cp314-core-vX.Y.Z-rcN.zip
reference-bridge-macos-arm64-cp314-core-vX.Y.Z-rcN.tar.gz
reference-bridge-macos-x64-cp314-core-vX.Y.Z-rcN.tar.gz
```

Additional profile packages for other platforms use the same naming rule.
Package each profile with the exact qualified mock MEX, producer runtime
contents (including the correct FEniCS base where applicable), mock Python
backend, Octave smoke fixture, offline package verifier, local test launcher,
licensing/provenance inventory, and human-readable `README-LOCAL-TEST.md`.
Package only permitted redistribution inputs and retain license notices.
The package carries both the canonical Octave filename `bridge.mex` and the
correct MATLAB filename alias for the target, with byte identity asserted.
The explicit host prerequisite is an installed compatible GNU Octave; do not
bundle GNU Octave or claim MATLAB qualification from Octave results.

Each downloadable package must include:

- `reference-manifest.json`: format version, producer and mock source revisions,
  profile/platform/ABI, producer receipt and archive hashes, qualification run
  identity, exact MEX SHA-256, file inventory and hashes, native closure summary,
  and supported local host/OS prerequisites;
- `SHA256SUMS.txt`: a complete verifiable archive/package inventory, with the
  release index additionally publishing each whole-archive SHA-256;
- `verify-package.py`: a Python standard-library-only, offline verifier for all
  packaged files and manifest identity (bootstrap Python only; no pip needed);
- `run-local-test.ps1` / `run-local-test.sh` as applicable: find or accept an
  explicit `octave-cli` executable, verify the package, invoke the bundled
  Octave smoke file with an isolated runtime and a relocated package directory,
  and print a stable result such as `reference_bridge_result=pass` or an error.

The local test is deliberately optional. A user can download one package plus
its published SHA-256, verify and extract it, and run its test on a compatible
machine. The test must not call GitHub, download Python, invoke a compiler,
resolve packages, or change producer-owned payloads. It must work from a path
different from the original runner/build path, including spaces in the path.
Validate both successful execution and deliberate corruption detection on
GitHub runners **before** making any package publishable. Local MATLAB testing
may be documented separately as experimental/manual, without a support claim.

An example local use should be documented for each OS. Linux may look like:

```sh
sha256sum -c reference-bridge-linux-x64-cp314-core-vX.Y.Z-rcN.tar.gz.sha256
tar -xzf reference-bridge-linux-x64-cp314-core-vX.Y.Z-rcN.tar.gz
cd reference-bridge-linux-x64-cp314-core
./run-local-test.sh --octave /path/to/octave-cli
```

The final archive layout and release index must make those commands accurate.
The launcher must accept a configurable Octave path and not depend on the shell
current working directory after determining its own package root.

## Private bridge adoption

The public consumer kit should be vendored into
`python-matlab-octave-bridge/vendor/python-runtime-consumer-kit/` with a checked-in
upstream revision, kit tree hash and format version. A sync/check script verifies
vendored-file identity and reports drift; bridge-specific source, smoke tests,
and release policy live outside the vendored directory. The private repo keeps
its source selection manifest and lock/receipt, then calls the same kit entry
points that the public mock uses.

Before changing the private repo, amend its existing
`specs/python-numpy-fenics-consumer-host-integration/EPIC.md` and phase 01a-01e
specs to reflect this ownership: public kit implements generic producer artifact
staging, network-free validation, compilation, and common package assembly;
private phases own the pinning decision, actual bridge adaptation, host parity,
exact artifact qualification and final release. Preserve the private specs'
explicit cp314 core-first migration and support evidence requirements. Any
CircleCI migration or changes to private publication must be separately scoped
there; this public project stays GitHub Actions-only.

Private CI should compile one production MEX per platform/revision, distribute
the hash-bound candidate to core/NumPy/FEniCS profile tests, and package those
exact bytes. A passing public mock qualifies the kit, not the private MEX.

## Implementation phases

| Phase | Public deliverable | Acceptance gate |
| --- | --- | --- |
| 01. Contract and ownership | Public source/receipt/package schemas, sample manifest, producer/private boundary update, kit interface documentation. | Schema fixtures and negative-path tests pass; no private source, API implementation, or secrets present. |
| 02. Immutable consumer kit | Explicit acquisition/cache/stage/verify operations across four targets, profile layering and full tree re-attestation. | Offline staging and corruption tests pass; runtime bytes unchanged; cross-release FEniCS NumPy binding preserved. |
| 03. Portable mock MEX | Implement the public API and C/Python/Octave fixtures defined in [04a](04a-mock-bridge-specification.md) using source-driven MSVC/GCC/Clang builds and native audits. | GitHub runners build exactly one MEX per target and validate the mock operations, exports, loader dependencies and provenance against 04a. |
| 04. End-to-end qualification | All-target GitHub Actions workflow with core/NumPy and opt-in FEniCS host integration using [04a's smoke tests](04a-mock-bridge-specification.md); build artifacts shared across profile checks. | Exact MEX SHA is unchanged across profile tests; Octave -> MEX -> embedded Python, isolation, relocation and JIT/PETSc gates pass per 04a. |
| 05. Downloadable reference package | Exact candidate packaging, published checksums/manifests and the offline verifier/launcher specified in [04a](04a-mock-bridge-specification.md). | On runners, fresh extraction passes the same locally downloadable smoke test and corruption rejection for each promoted target/profile; no compiler/source checkout needed. |
| 06. Consumer handoff | Vendoring instructions and automated kit identity check; private integration epic amended in the private repo. | Private candidate uses the same kit tools with private source manifest; each target builds once and tests the same MEX against all supported profiles. |

Implement phases as focused PRs. Do not publish a reference pre-release until
phase 05 passes for all included target/profile packages. Core/NumPy may be
published before FEniCS if the optional FEniCS matrix is not yet qualified;
record the exact supported set in the release manifest and documentation.

## Final completion criteria

- [ ] Only GitHub Actions runners build/qualify/package the public mock; no local
      build is required or claimed as qualification.
- [ ] Four supported target architectures have a successful exact-revision MEX
      build and a full core/NumPy Octave host path; FEniCS appears only for
      explicitly qualified target/profile combinations.
- [ ] Per target/revision, compilation happens at most once, with identical
      MEX SHA-256 proven across selected runtime profile tests and publication.
- [ ] No producer runtime is rebuilt by the consumer kit; staging verifies
      immutable artifacts and rejects any modified producer-owned entry.
- [ ] Public mock source is purpose-built, minimal and independent of the
      private bridge; public CI never checks out or authenticates against it.
- [ ] Optional GitHub pre-release contains independently downloadable packages,
      checksums, manifests, offline test launchers and complete prerequisites.
- [ ] Every published package passes fresh-path, offline extracted-artifact
      Octave smoke and negative tampering checks on its GitHub target runner.
- [ ] The user-facing local test instructions work with a compatible installed
      Octave, without compiling, fetching dependencies, or modifying the package.
- [ ] Private bridge integration can adopt the same versioned consumer kit with
      only its own sources, receipt, tests and release-specific configuration.
- [ ] Repository validation (`python scripts/common/validate_repo.py`), all
      Python script compilation, applicable shell/PowerShell syntax checks and
      changed-runtime platform qualification pass before implementation merge.
