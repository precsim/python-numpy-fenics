# Repository agent guide

## Purpose

This is the public producer for relocatable CPython runtime profiles used by
downstream embedded-Python consumers.

The producer owns dependency acquisition, immutable package locks, selective
native rebuilds when required, runtime relocation, JIT tooling, native closure
audits, standalone qualification, checksums, provenance, and pre-release
artifacts.

It does not own any downstream MATLAB/Octave bridge implementation.

## CI

- GitHub Actions is the only CI service for this repository.
- Do not add .circleci, CircleCI orbs, CircleCI API dependencies, or release
  gates that require CircleCI.
- Keep build and qualification logic in scripts; workflows should primarily
  orchestrate those scripts.
- Pin third-party GitHub Actions by immutable commit SHA.

## Privacy boundary

- Never copy a private consumer source tree into this repository.
- Never require CI credentials that read a private consumer repository.
- Do not embed private MEX source, private backend source, or private release
  packaging logic.
- Integration fixtures must be purpose-built public mocks or contract probes.
- Producer artifacts are consumed downstream only by immutable artifact
  identity, manifest, and SHA-256.

## Runtime policy

- CPython ABI baseline: CPython 3.14, cp314 (exact patch baseline 3.14.10).
- Tcl/Tk 8.6.18 is an explicit producer-owned core dependency on every target.
- NumPy baseline: 2.5.2 unless a spec explicitly changes it.
- FEniCS is cumulative on the matching NumPy profile.
- Existing locked binary packages are always tried first.
- Source rebuilds are exceptions, not the default. A rebuild requires executable
  evidence that the locked package violates an ABI, relocation, native-provider,
  or JIT invariant, and must be limited to the smallest incompatible native
  component set.
- Producer-owned CPython and NumPy files must not leak into FEniCS overlays that
  are designed to layer onto a consumer-owned CPython/NumPy runtime.

## Supported producer targets

- Windows x64.
- Linux x86_64 with a glibc >= 2.17 compatibility floor.
- macOS arm64.
- macOS x86_64, qualified under Rosetta 2 where required.

## Validation

Before committing:

1. run python scripts/common/validate_repo.py;
2. compile all Python scripts;
3. syntax-check shell and PowerShell scripts on an appropriate host;
4. run the platform qualification workflow for changed runtime behavior;
5. verify generated artifacts contain no private-consumer source.
