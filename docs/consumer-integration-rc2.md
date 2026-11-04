# Reference consumer handoff: CPython 3.14, NumPy and FEniCS

The public producer release [`runtime-v0.1.0-rc2`](https://github.com/precsim/python-numpy-fenics/releases/tag/runtime-v0.1.0-rc2) is the qualified artifact source. This is a **reference consumer runtime integration**, not certification of a private application's host or product packaging.

## Immutable identities

- Producer: `precsim/python-numpy-fenics`
- FEniCS overlay tag: `runtime-v0.1.0-rc2`
- Producer revision: `88f36aaa09fb8a84ea528d5823d9b0be043cfa3e`
- Release manifest SHA-256: `dff3909366f2c57e3765226399fb867282a3e253ac3ece065f28e26713489e29`
- **Important:** Every rc2 FEniCS overlay uses the immutable **rc1 NumPy base** (`runtime-v0.1.0-rc1`, producer revision `95a8b43b674da0d1a1955b8dec002c9f2ab1c925`), *not* the independent rc2 NumPy profile. Use the cross-profile bindings in the published prerelease manifest.

All four platform asset SHA-256 values, embedded manifest SHA-256 values, rc1 NumPy qualification record SHA-256 values, and pinned NumPy tree identities are recorded in [`locks/consumer/runtime-v0.1.0-rc2.json`](../locks/consumer/runtime-v0.1.0-rc2.json). This file is a **consumer-side receipt** and should be copied into the downstream application's source control without modifications.

## Acquire, verify, and stage

Prerequisites: Python 3.12+ for the staging tool and the GitHub CLI (`gh`) to fetch public release assets. A GitHub account token is not required for access to the public repository when the GitHub CLI is able to download the release. The staging tool can also use previously downloaded assets in its cache without network access.

From this repository, run one of the following commands, selecting the platform matching the **consumer host**:

```powershell
# Windows x86_64
python scripts/consumer/stage_qualified_fenics.py --platform win-x64 --cache build/consumer-release-cache --output 'build/consumer host with spaces/runtime'
```

```sh
# Linux x86_64 (run on the Linux target)
python3 scripts/consumer/stage_qualified_fenics.py --platform linux-x64 --cache build/consumer-release-cache --output 'build/consumer host with spaces/runtime'

# macOS arm64 (use macos-x64 on Intel macOS)
python3 scripts/consumer/stage_qualified_fenics.py --platform macos-arm64 --cache build/consumer-release-cache --output 'build/consumer host with spaces/runtime'
```

The script downloads the **rc2 release manifest**, the **rc1 NumPy base archive and qualification record**, and the **rc2 FEniCS overlay**. Before installing it verifies the pinned release manifest SHA-256, release producer revision, FEniCS archive and embedded manifest SHA-256, rc1 archive, embedded manifest and qualification SHA-256, the qualification's producer/artifact identity and successful checks, and each platform's cross-release NumPy tree binding. It rejects changed archives or qualification evidence, unsafe file paths, symlink escapes, and overlapping overlay files; it does not overwrite a pre-existing output runtime. The archive extraction is staged in a temporary directory and renamed into place only after both layers pass.

The tool makes **no modifications to the upstream producer release**, does not rebuild binary dependencies, and does not require any private consumer source or token. The cache and extracted runtime live under the ignored `build/` directory.

## Embedded runtime probe

Run the public runtime smoke test **with the staged interpreter**, never the system Python. It checks isolated FEniCS activation, NumPy 2.5.2, DOLFINx/petsc4py/mpi4py imports, a mesh creation, and the public Poisson/FFCx JIT/PETSc numerical solve:

```powershell
& 'build/consumer host with spaces/runtime/python.exe' -B -I tests/consumer/embedded_host_smoke.py tests/fenics/test_poisson.py
```

```sh
# Linux/macOS: select the Python 3.14 executable inside your staged runtime.
'build/consumer host with spaces/runtime/bin/python3.14' -B -I tests/consumer/embedded_host_smoke.py tests/fenics/test_poisson.py
```

For an isolation test set a hostile `PYTHONPATH`, `PYTHONUSERBASE` and a fresh `XDG_CACHE_HOME` path containing spaces before starting the staged interpreter. The `-I` interpreter flag prevents ambient Python package injection, and `-B` suppresses incidental bytecode writes. The FFCx JIT cache must be writable and initially empty to prove a fresh compile.

**Verified on Windows and in an Ubuntu 24.04 WSL 1 Linux environment** using the actual public release bytes: rc1 NumPy and rc2 FEniCS artifacts staged in relocated paths containing spaces; Python isolation, imports, mesh creation and fresh JIT/Poisson solve passed. The Linux x86_64 probe used poisoned ambient Python state and an initially empty XDG JIT cache; it populated the fresh cache and reported L2 error `0.009053553714814033` against tolerance `0.05`. This establishes Linux user-space runtime execution under WSL 1; it is not a substitute for final qualification on the downstream product's native Linux deployment environment. The staging regression tests also exercise mock macOS identities and GNU tar behavior; actual macOS *consumer-host* runtime execution is not yet established by this reference handoff.

## Downstream handoff boundary

The private consuming application must still perform real host bootstrapping, packaging/install/uninstall, relocation after product installation, native loader closure, fresh JIT and representative application simulations on each shipping target. Those results cannot be inferred from producer qualification or a public integration mock. Keep private application source, tokens, product bundles and release credentials outside the public producer repository. See [producer/consumer boundary](producer-consumer-boundary.md).
