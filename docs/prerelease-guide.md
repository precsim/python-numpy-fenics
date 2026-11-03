# Producer pre-release and downstream artifact handoff

The active publication scope is the `core` and `numpy` profiles for Windows
x64, Linux x86_64, macOS arm64 and macOS x86_64. FEniCS is explicitly optional
and remains deferred. A producer pre-release is an immutable input for
downstream testing, not a downstream product release.

## Qualification inputs

1. Choose the exact `main` commit to publish. Run `validate` and the three
   `qualify-*-core-numpy` GitHub Actions workflows on that commit. Use successful
   **push** or **workflow_dispatch** runs with the same `headSha` and `main`
   branch identity. The macOS workflow must pass for both arm64 and x86_64.
2. Collect one run ID each from `qualify-windows-core-numpy`,
   `qualify-linux-core-numpy`, and `qualify-macos-core-numpy`. Verify their
   artifacts, manifests, SHA-256 files and qualification records exist.
3. In GitHub Actions, dispatch `producer-prerelease` **from that same `main`
   commit**. Supply a new tag in the format `runtime-vX.Y.Z-rcN`, the three
   qualification run IDs, and leave `include_fenics` false.
4. The workflow verifies run success, source workflow and event, shared
   `main` SHA, the exact publisher checkout SHA, archive hashes, manifest
   identities and the expected six core/NumPy profile records before creating
   the tag and GitHub pre-release. The tag must not already exist.

The qualification runs from PR #3 (`95d56a5`) were made on a feature branch;
they prove the migration but **cannot** be used as promotion inputs. New
qualifications from the chosen `main` SHA are required before publishing.
Publishing a later documentation commit also requires qualifications at that
exact later SHA.

When FEniCS work resumes, first migrate and qualify the retained cp312
workflows against the corresponding cp314 NumPy base. Only then opt in with
`include_fenics` and provide the three FEniCS workflow run IDs as well. That
mode requires the full additional FEniCS target matrix from the same `main`
revision and matching NumPy base identities.

## Downstream handoff

After publication, a consumer records the producer repository, tag, revision,
asset name, asset SHA-256 and embedded manifest SHA-256 for each required
platform/profile. Download artifacts by these immutable identities and verify
both hashes before staging the runtimes. Use the manifest and qualification
record to check Python ABI, package identities, target and native closure.

The consumer is responsible for final embedded-host integration and product
packaging. Neither consumer source nor private release assets belong in this
repository or its GitHub Actions workflows. See
[the producer/consumer boundary](producer-consumer-boundary.md).
