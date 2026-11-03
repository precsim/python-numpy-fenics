---
status: draft
priority: P1
depends_on:
  - 02-core-numpy-profiles.md
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
- By default, promotion requires successful `core` and `numpy` qualifications
  for all four targets from one `main` producer revision. The three core/NumPy
  workflows supply six profile identities in total.
- FEniCS is deferred and optional. Explicitly selecting `include_fenics`
  requires successful qualifications for all four matching FEniCS targets
  from the same producer revision, with matching NumPy base identities.
- Feature-branch qualification results alone cannot be promoted. All selected
  qualification runs must target `main` and bind to one revision.
- Publication is immutable: changing bytes requires a new producer version or
  pre-release identifier.

See [the publication and downstream handoff guide](../docs/prerelease-guide.md).
