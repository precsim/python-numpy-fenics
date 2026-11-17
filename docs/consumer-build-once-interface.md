# Consumer-kit handoff for downstream MEX bridges

## Current public interface

`consumer-kit/` is the reusable consumer tooling maintained by
`precsim/python-numpy-fenics`. The producer builds and qualifies immutable
CPython, NumPy and FEniCS runtime artifacts; the kit verifies and stages those
artifacts, builds a source-configured MEX, audits it and verifies a reference
package. See [consumer-kit/README.md](../consumer-kit/README.md) and
[producer-consumer-boundary.md](producer-consumer-boundary.md).

The public `reference-bridge/` is an executable contract probe, not the
implementation or qualification evidence of `python-matlab-octave-bridge`.
The downstream repository supplies its own MEX source-selection manifest,
private C/Python/MATLAB or Octave sources, host acceptance tests, capability
claims, packaging policy and release artifacts.

## Source-control pins and versions

Keep **two independent pins** in the downstream repository:

1. **Tooling pin:** immutable producer Git commit that includes `consumer-kit/`,
   the kit's deterministic `tree_sha256`, and the three `v1` schema formats
   used by that revision (`bridge-build`, `consumer-runtime-receipt`, and
   `reference-package`). The schemas have explicit format versions; there is
   no independent semantic-version release for `consumer-kit/`. Do not select
   tooling from a moving `main` checkout.
2. **Runtime pin:** immutable producer release tag and revision, exact asset
   names, archive and embedded-manifest SHA-256 values, and qualification
   identities recorded in the consumer-owned runtime receipt. Upgrading the
   kit does not silently change any runtime release or asset.

For example, the audited producer tree at commit
`2e27104f006a40c40094156d55f78e2e712898fd` (merge of PR #14) has
`consumer_kit_tree_sha256=5669aff3517d7e16227547db63e90ce4cc8c54d4161a333323fdc02194bec74a`
and `consumer_kit_files=28`. These values identify that **example revision**,
not a floating latest-kit version. Verify both after vendoring.

The currently published producer runtime family is
`runtime-v0.1.0-rc2` at producer revision
`88f36aaa09fb8a84ea528d5823d9b0be043cfa3e`, with
`prerelease-manifest.json` SHA-256
`dff3909366f2c57e3765226399fb867282a3e253ac3ece065f28e26713489e29`.
It supplies `cp314` core/NumPy and opt-in FEniCS inputs on Windows x64,
Linux x86_64, macOS arm64 and macOS x86_64. The FEniCS overlay's NumPy base
is **the separately receipt-bound rc1 NumPy runtime**, not the independent
rc2 NumPy profile; use `locks/consumer/runtime-v0.1.0-rc2.json` to bind it.
Never infer runtime input identities from an unpinned release listing.

## Initial adoption in python-matlab-octave-bridge

1. Select an exact reviewed producer commit with passing public
   `qualify-reference-bridge` checks. Copy the complete
   `consumer-kit/` directory from that commit into
   `vendor/python-runtime-consumer-kit/` using a clean checkout or Git
   archive. Preserve its relative file layout and do not mix revisions.
2. Record outside that vendored directory the producer repository, full
   commit SHA, kit tree SHA-256, and schema format versions. Run the vendored
   `tools/check-vendored-kit.py` with `--root` pointed at the vendored
   directory and `--expect` set to the recorded tree hash. Run this check in
   CI and before any downstream build; it must fail when the tree drifts.
3. Keep the downstream runtime receipt, `bridge-build.json`, real bridge
   source, backend/host fixtures and release configuration outside the
   vendored directory. Populate the consumer runtime receipt from a verified
   immutable producer release, following
   `consumer-kit/contracts/consumer-runtime-receipt-v1.schema.json`.
4. Delegate download/cache/transactional staging to
   `consumer-kit/tools/stage-runtime.py` and network-free full-tree
   verification to `consumer-kit/tools/verify-runtime.py`. Private command
   wrappers may supply arguments and paths but should not keep independent
   archive extraction, manifest verification or marker formats. Never modify
   producer-owned runtime entries.
5. Validate the private source manifest with the kit's build entry points,
   compile one MEX candidate per target/source/toolchain identity against
   verified core CPython headers, and reuse **exactly those bytes** for each
   selected profile. Audit the candidate before qualifying or packaging it.
   Keep build provenance, runtime receipt, native audit, qualification record
   and final package inventory tied by their recorded SHA-256 values.
6. Run the actual private GNU Octave → MEX → CPython tests on each supported
   target/profile, including relocation and hostile ambient state. NumPy and
   FEniCS need their own functional/native-provider tests. Passing public mock
   qualification does not validate the private MEX or authorize a support
   claim. Packaging uses only the previously qualified candidate.

The public kit is built and exercised by GitHub Actions. Its workflow files
are not a private CI or product release policy; the downstream repository may
orchestrate the vendored scripts in its existing CI system.

## Upgrading the vendored kit

1. Choose a new immutable producer commit and inspect its changes to
   `consumer-kit/`, contracts and tests. Confirm the public reference
   qualification for that commit; a runtime-only tag is not a kit version.
2. Replace the **entire** vendored kit with the new upstream tree, without
   modifying private source, receipts or product policy. Recompute the
   deterministic tree digest with `check-vendored-kit.py`; review and commit
   the new upstream SHA, digest and format versions together with the copy.
3. Adapt the private wrappers and source manifest to any changed v1 contract
   requirements (or deliberately migrate to a newly versioned contract).
   Run the kit identity check, staging verification and relevant negative
   tests, target MEX build/native audits, actual host qualification,
   relocation/offline package checks and final archive verification.
4. Treat a changed private source/build manifest/toolchain or ABI-sensitive
   kit behavior as a new MEX candidate that requires qualification.
   Compare the old/new candidate hashes; preserve old published packages and
   their evidence. Separately review changes to producer runtime receipts.

The helper reports a deterministic content-tree identity, not a Git tree object
ID. Compute it with (from either repository, using its own copy):

```sh
python vendor/python-runtime-consumer-kit/tools/check-vendored-kit.py --root vendor/python-runtime-consumer-kit
python vendor/python-runtime-consumer-kit/tools/check-vendored-kit.py --root vendor/python-runtime-consumer-kit --expect <recorded-sha256>
```

Future releases may add a per-target ABI-compatibility sidecar to simplify
cross-release caching. Such a sidecar would be advisory; it cannot replace
exact native input hashes or private host qualification, and is not required
for the current build-once contract.
