# python-numpy-fenics

Public producer and qualification repository for relocatable CPython 3.14.10 / cp314
runtime profiles containing NumPy and optional FEniCSx/DOLFINx/PETSc support.

This repository is deliberately independent of downstream application source.
It produces immutable, checksummed artifacts and machine-readable qualification
evidence. A private consumer can therefore verify and integrate a runtime
without giving public CI access to its repository.

## Boundary

This repository owns:

- controlled CPython runtime production;
- producer-owned Tcl/Tk 8.6.18 acquisition, staging and qualification;
- NumPy runtime production and BLAS/LAPACK provider qualification;
- FEniCS/DOLFINx/PETSc/MPI package locking and overlay construction;
- package-reuse-first qualification and narrowly scoped rebuild exceptions;
- JIT compiler/runtime packaging;
- ELF, Mach-O and PE/native dependency audits;
- relocation and hostile-environment tests;
- standalone fresh FFCx JIT and PETSc numerical tests;
- manifests, checksums, provenance, licenses and producer pre-releases.

Downstream consumers own:

- their application or bridge source;
- their real embedded-host integration;
- final consumer-specific acceptance;
- final product/release assembly and publication.

Public CI never checks out a private consumer repository.

## Profiles and targets

The intended producer matrix is:

| Platform | core | numpy | fenics |
| --- | --- | --- | --- |
| Windows x64 | qualified | qualified | deferred/manual |
| Linux x86_64 / glibc >= 2.17 | qualified | qualified | deferred/manual |
| macOS arm64 | qualified | qualified | deferred/manual |
| macOS x86_64 (Rosetta 2) | qualified | qualified | deferred/manual |

The core/NumPy migration was qualified on feature revision `95d56a5` and
merged into `main` by PR #3 (`f6e6e77`). The qualification run IDs and scope
are recorded in [the cp314 migration record](docs/cp314-core-numpy-qualification.md).
These results qualify the merged implementation; publication requires its own
matching `main` revision qualification evidence as described in
[the pre-release guide](docs/prerelease-guide.md).

FEniCS is cumulative on the exact matching NumPy profile. A FEniCS overlay must
not replace consumer-owned NumPy bytes unless its profile contract explicitly
says otherwise.

The active producer pre-release scope is `core` + `numpy`. FEniCS qualification
workflows and packaging code are retained for future use but are not required to
publish a producer pre-release. The manual pre-release workflow can explicitly
opt into the complete FEniCS matrix when that work resumes.
The retained FEniCS workflows now target the qualified cp314 / Tcl/Tk 8.6.18
core and NumPy inputs. Their retained FEniCS package transactions still contain
legacy cp312 native/Python packages, so these workflows remain manual and
unqualified. Native package locks must be refreshed and independently qualified
before FEniCS can join a pre-release.

## Producer artifact contract

A qualified producer artifact is accompanied by an archive, whole-file SHA-256,
machine-readable summary, embedded manifest, qualification record, and
license/provenance inventory.

The manifest binds the producer repository/revision, platform, Python ABI,
package identities, file hashes, native closure and JIT policy. See contracts/
and docs/producer-consumer-boundary.md.

## FEniCS package policy

The default path is exact package reuse:

1. replay a checked-in package lock without dependency solving;
2. filter producer CPython/NumPy ownership as required by the profile;
3. relocate and audit the existing binaries;
4. run fresh JIT/PETSc qualification;
5. rebuild only a native component that executable evidence proves cannot meet
   the release contract unchanged.

A blanket FEniCS stack rebuild is not an acceptable fallback.

The Linux workflow also exercises a hosted selective-rebuild prototype after
capturing the required stock ABI failure. The prototype binds that immutable
preflight evidence, fetches the pinned public Basix/DOLFINx dependency sources
by SHA-256, prepares the Python wheel builder from a fixed seven-wheel public
closure on the already qualified NumPy CPython 3.14 runtime, replays the separate
41-record build-only toolchain lock, prepares the libxml2 zlib/liblzma build-only
inputs from a two-record `@EXPLICIT` development-header transaction plus the
already verified runtime `libzlib`/`liblzma` packages, compiles a C++20 probe
offline in the pinned glibc 2.17 image, and records a fail-closed public-input
provenance skeleton.
The builder reuses the existing NumPy-profile `pip` and `packaging` pins, uses
only SHA-bound pure wheels, performs no dependency solve, and is validated with
networking disabled. The libxml2 build-dependency producer similarly uses exact
conda-forge package URLs and SHA-256 digests with no channel/index solve, hashes
the downloaded development archives, and copies only the pinned regular headers
and versioned shared libraries. That skeleton is not a qualified source rebuild.
Full execution still requires separately reproducible private C++ provider and
native-closure evidence inputs. ICU remains live in the stock native closure,
and the overlay packager does not yet accept rebuilt native outputs, so the
hosted release path fails closed after recording the prototype evidence.

## CI

CI is GitHub Actions only. Workflows live in .github/workflows; build and
qualification implementation lives under scripts.

Local repository validation:

    python scripts/common/validate_repo.py

## Migration provenance

The initial Windows and macOS FEniCS producer implementation is migrated from
the public precise-simulation/fenics-windows-ci producer. Historical
experiments are intentionally not copied; only the currently qualified
producer-facing overlay, package-lock, JIT, and numerical-test paths are carried
forward.
