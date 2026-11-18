# Pull-request runtime qualification gating

The Windows, Linux and macOS core/NumPy qualification workflows are required
GitHub checks. Each workflow runs a lightweight change-detection job for pull
requests using `scripts/common/qualify_pr_changes.py`. The detector returns
whether its own platform needs an expensive runtime build.

| Pull-request changes | Windows | Linux | macOS arm64/x86_64 |
| --- | --- | --- | --- |
| Markdown or root license only | Skip | Skip | Skip |
| Windows-owned scripts, locks or tests | Build | Skip | Skip |
| Linux-owned scripts, locks or tests | Skip | Build | Skip |
| macOS-owned scripts, locks or tests | Skip | Skip | Build both |
| Consumer kit/reference bridge and associated tests/receipts only | Skip | Skip | Skip |
| Shared runtime code, core locks, contracts, configuration, or unknown files | Build | Build | Build both |
| Changes affecting multiple platforms | Build affected platforms | Build affected platforms | Build if affected |

Unknown paths and missing PR file lists deliberately select **all** platforms.
Renames are classified using both old and new paths; an old runtime path cannot
be hidden by moving it into a consumer-only directory. Changes to the gate
itself select all platforms. A new path must be explicitly reviewed before
being added to any narrowly classified category.

The macOS workflow always expands its two architecture jobs, preserving the
required `qualify-macos-core-numpy-arm64` and
`qualify-macos-core-numpy-x86_64` check names. When macOS is unaffected, both
jobs run a trivial no-op on Ubuntu rather than reserving costly `macos-15`
runners. Windows and Linux produce their existing skipped job checks. All
platforms still run the repository validation workflow on every PR.

Consumer-only paths are tested by the dedicated `qualify-reference-bridge`
workflow when its trigger paths match. They do not need a rebuild of immutable
producer runtimes.

**Manual dispatches and push-triggered qualification on `main` always perform
the actual build**, regardless of the PR classifier. This retains explicit
runtime promotion/release evidence rather than treating a lightweight PR check
as qualification.
