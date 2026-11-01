# python-numpy-fenics

Public producer and qualification repository for relocatable CPython 3.12
runtime profiles containing NumPy and optional FEniCSx/DOLFINx/PETSc support.

This repository is deliberately independent of downstream application source.
It produces immutable, checksummed artifacts and machine-readable qualification
evidence. A private consumer can therefore verify and integrate a runtime
without giving public CI access to its repository.

## Boundary

This repository owns:

- controlled CPython runtime production;
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
| Windows x64 | producer migration | producer migration | proven overlay migration |
| Linux x86_64 / glibc >= 2.17 | producer migration | producer migration | reuse-first work queued |
| macOS arm64 | producer migration | producer migration | proven relocatable overlay migration |
| macOS x86_64 | producer migration | producer migration | proven relocatable overlay migration |

FEniCS is cumulative on the exact matching NumPy profile. A FEniCS overlay must
not replace consumer-owned NumPy bytes unless its profile contract explicitly
says otherwise.

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
