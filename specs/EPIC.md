---
status: in-progress
priority: P0
---

# Epic: public cross-platform runtime producer

## Goal

Move dependency engineering, runtime production, standalone qualification and
producer pre-release work into this public repository while keeping downstream
consumer source private.

## Invariants

- GitHub Actions only.
- No private consumer checkout or source copy.
- Runtime scripts are authoritative; workflows orchestrate them.
- Existing locked packages are qualified before any source rebuild.
- Rebuild exceptions are minimal, evidence-backed and hash-complete.
- Producer artifacts are immutable inputs to downstream integration.

## Phases

| Phase | Spec | Status |
| --- | --- | --- |
| Windows FEniCS overlay migration | windows/01-fenics-overlay.md | in-progress |
| macOS FEniCS overlay migration | macos/01-fenics-overlay.md | in-progress |
| Linux FEniCS overlay producer | linux/01-fenics-overlay.md | draft |
| Core/NumPy producer migration | 02-core-numpy-profiles.md | in-progress |
| Producer pre-release publication | 03-prerelease.md | draft |
