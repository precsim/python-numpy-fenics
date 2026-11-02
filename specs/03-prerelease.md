---
status: draft
priority: P1
depends_on:
  - 02-core-numpy-profiles.md
  - windows/01-fenics-overlay.md
  - macos/01-fenics-overlay.md
  - linux/01-fenics-overlay.md
---

# Producer pre-release publication

## Goal

Publish qualified runtime artifacts as GitHub pre-releases that downstream
consumers can pin by tag, asset name and SHA-256.

## Contract

- GitHub Actions performs publication.
- The pre-release contains no downstream application/MEX artifact.
- Each asset has a whole-file SHA-256 and manifest SHA-256.
- Qualification evidence identifies producer revision and exact workflow run.
- All promoted qualification runs must be successful expected producer
  workflows from one producer revision, and qualification JSON must bind back
  to those exact run IDs and revision.
- Publication remains blocked until Linux FEnIcs has a successful
  `qualify-linux-fenics` producer run; partial-matrix producer pre-releases are
  not valid release candidates.
- Publication is immutable: changing bytes requires a new producer version or
  pre-release identifier.
