---
status: draft
priority: P1
depends_on:
  - 04-public-reference-bridge-consumer-kit.md
---

# Public reference mock MEX bridge: source, API and behavior

## Purpose and scope

Define the small, independently authored public mock used by the reference
consumer kit in [the parent implementation plan](04-public-reference-bridge-consumer-kit.md).
It is an executable test of the **GNU Octave -> MEX -> isolated producer CPython
-> Python backend -> MEX result** path, with optional NumPy and FEniCS execution.
It is not a copy, reimplementation, or compatibility promise for the private
`python-matlab-octave-bridge` API.

All mock compilation, automated tests and exact-package qualification run on
GitHub Actions runners. An optionally published prebuilt mock is independently
downloadable and testable on compatible local hosts with GNU Octave; local use
must not require source, a compiler, package resolution or network access after
download.

## Deliverables and file ownership

The following paths are proposed under `reference-bridge/`:

| Path | Ownership |
| --- | --- |
| `bridge-build.json` | Versioned source/target/export/helper/test manifest consumed by the public consumer kit. |
| `src/mock_bridge.c` | Exported MEX entry point, argument/result dispatch and host-facing errors. |
| `src/mock_host.c`, `src/mock_host.h` | Minimal dynamically resolved MEX host functions needed for strings, scalars, structure diagnostics and host errors. |
| `src/mock_python.c`, `src/mock_python.h` | Isolated embedded CPython setup, dynamic API loading, Python calls, error cleanup and lifecycle. |
| `src/mock_platform.h` | Minimal platform abstraction declarations; reusable native loader/path implementation belongs in `consumer-kit/`. |
| `python/mock_backend.py` | Tiny deterministic scalar, NumPy and FEniCS probes with lazy imports. |
| `host/mock_smoke.m` | Profile-selected Octave acceptance test. |

Do not require the exact file split if a simpler implementation remains clear.
No private C, Python, host, MEX, packaging source, or copied private tests may be
introduced. The existing public Poisson/JIT test under `tests/fenics/` can be
used as the FEniCS numerical reference rather than duplicating a solver.

## Public host API (mock only)

One MEX entry point named `bridge`:

```matlab
y = bridge("scalar", x);       % scalar double -> 2*x + 1 (core)
y = bridge("numpy", n);        % sum((1:n).^2) as double (numpy/fenics)
y = bridge("fenics");          % deterministic Poisson/PETSc result (fenics)
info = bridge("runtime");      % controlled runtime diagnostics (all)
```

Define precise argument behavior:

- Exactly one output for each query. `scalar` requires one real, finite,
  non-complex 1x1 double; its deterministic result is `2*x + 1`. Test `x=5`
  gives 11 and `x=10` gives 21. Reject invalid arity, type and shape.
- `numpy` requires one positive integral scalar double `n` with a small
  fixed maximum (e.g. 1000) to bound work and avoid overflow surprises;
  `n=5` returns 55 via actual `numpy.arange` and `numpy.sum`. Import NumPy
  only when requested. In `core`, failure must clearly indicate unavailable
  profile capability without loading ambient NumPy.
- `fenics` takes no additional arguments, lazily invokes the runtime's
  `fenics_embed_runtime.activate()` when present, and runs a **fresh** public
  FFCx JIT + serial PETSc Poisson numerical probe through embedded Python.
  The probe returns one documented finite numerical value, with a numerical
  tolerance fixed by the existing public test. In `core`/`numpy`, report
  unsupported profile capability; no implicit downloads or upgrades.
- `runtime` returns a small host struct with `python_version`,
  `python_abi`, `runtime_root`, and `profile`. Obtain identity from the
  running interpreter and verified local receipt/marker, not a hard-coded
  success string. `python_version` must identify 3.14.10; `python_abi`
  must be `cp314`. Return only controlled diagnostic data, never credentials
  or environment secrets.
- Reject unknown operation names, extra inputs/outputs, unsupported types,
  failed Python calls, invalid numerical results, and malformed runtime roots
  with stable, actionable host error identifiers/messages. Ensure a handled
  invalid-input call does not poison the next valid call.
- The baseline mock does **not** expose a `reset` operation. Repeated
  calls in one Octave process and a fresh second Octave process validate normal
  lifecycle. Interpreter finalization/reinitialization while NumPy/PETSc/MPI
  native modules are loaded requires a separate future design and proof.

These operations are diagnostic fixtures, not the consumer bridge public API.
Do not implement arbitrary Python eval/exec, general mxArray conversion,
callbacks, sparse/dense object mapping, Python object handles, or backend/module
selection from untrusted arbitrary user inputs.

## Native and embedded execution contracts

1. The exported C interface is the platform-appropriate `mexFunction`
   signature. The MEX resolves the minimal MATLAB/Octave host API dynamically
   from the active host, uses the host's allocator/constructors for result
   objects, and does not require MATLAB/Octave or libpython at link time.
2. Locate the actual MEX module and its staged runtime using canonical,
   runtime-relative paths from the verified package layout. Do not use cwd,
   system Python, user site-packages, `PYTHONPATH`, `PYTHONHOME`, or ambient
   shared libraries as trusted sources. Preserve producer files byte-for-byte.
3. Load the producer CPython provider dynamically and reject conflicting
   previously loaded CPython providers where the platform contract requires.
   Linux must exercise the intended `RTLD_NOW | RTLD_GLOBAL` behavior for
   native extensions; Windows and macOS follow their respective native loader
   constraints. No link-time dependencies on NumPy, FEniCS, MPI, BLAS or PETSc.
4. Resolve the required CPython API and initialize exactly one isolated
   interpreter per Octave process as needed. Configure only explicit controlled
   runtime and mock-backend import roots. Python's normal `site`, user path,
   ambient environment search and bytecode writing must not extend the runtime.
   Use pinned CPython 3.14 headers for the build.
5. Lazy-import `mock_backend` once, resolve a known operation from a fixed
   dispatch table, call it with validated arguments, check Python exceptions,
   and return a host scalar/struct only on success. Bound text buffers and
   release GIL/owned Python references correctly across repeated calls.
6. Limit interpreter teardown to safe process termination unless explicit
   lifecycle evidence justifies more. Preserve clear diagnostics for startup
   failures, missing backend, native-provider collisions and Python exceptions.
7. Source build flags and named exports are declared by the consumer kit and
   target manifest; bridge source and mock tests must stay platform-neutral
   except for a very thin loader adapter. Record the source/config/toolchain
   revision and resulting MEX SHA-256.

The source-level reusable native-loading interface may include small functions
such as `runtime_find`, `runtime_load`, and `runtime_initialize`, but
their final signatures and ownership/lifetime semantics must be set in the
consumer kit contract before implementation. Do not put generic MEX value
conversion APIs or private application semantics into this interface.

## Runtime profiles and tests

| Profile | Required execution through same MEX bytes |
| --- | --- |
| `core` | `scalar` and `runtime` succeed; NumPy/FEniCS operations fail predictably; isolation, relocation, host recovery and repeated execution pass. |
| `numpy` | Core checks plus `numpy(5)==55`; imported NumPy is 2.5.2 and resides inside the verified runtime root. |
| `fenics` | Core and NumPy checks plus `fenics`; activation and FFCx JIT/PETSc numerical work execute in embedded Python using the FEniCS overlay's precisely bound NumPy base. |

The GNU Octave test must invoke the real MEX, including a second valid request
after a handled bad input; report `reference_bridge_result=pass` only if all
requested checks pass. Test both a repeated operation in one process and a
clean new Octave process. Additional negative tests cover missing or modified
backend, wrong/missing CPython runtime, poisoned ambient Python/NumPy paths,
foreign loaded native providers (where constructible), and relocation to a
path containing spaces. Direct Python probes are supplementary.

On Linux, verify exported symbols, no unexpected link dependencies, complete
ELF closure and glibc >= 2.17 / existing version-symbol ceilings. On Windows
and macOS, audit PE/Mach-O imports/exports and provider loading. Platform
limitations must be recorded explicitly; no silent skipped requirement.

## GitHub-runner build-once invariant

For each source revision and platform:

1. Explicitly stage/verify pinned producer runtime inputs via the consumer kit.
2. Compile a **single** mock MEX candidate against the verified cp314 headers
   and record its SHA-256 and compiler/toolchain identity.
3. Use that exact candidate unchanged in `core`, `numpy`, and optional
   `fenics` jobs. If jobs fan out, download the GitHub Actions candidate
   artifact and verify its SHA-256 before running tests.
4. Check recorded MEX SHA equality across all profile qualification receipts;
   packaging must copy the qualified candidate without compiling again.
5. A FEniCS-specific C change requires a new candidate and requalification
   of every selected profile. Prefer selecting FEniCS capability at runtime.

The parent plan owns workflow orchestration, immutable runtime receipts,
package layout and release publication. This spec owns mock behavior and its
host-side acceptance contract.

## Independent downloadable local test

The optional pre-release described in the parent plan bundles the exact
qualified MEX, selected immutable runtime, mock backend, smoke fixture,
checksums/manifest, offline verifier and platform-local `run-local-test`
launcher. Users select the matching OS/architecture/profile package and supply
a compatible installed GNU Octave. After download, verification and test work
without GitHub, compiler, source checkout, pip/conda or runtime mutation.

The launcher verifies package identity, discovers the package root independently
of cwd, invokes GNU Octave in a fresh process and checks the stable smoke result.
All published packages must pass the **same launcher** on a GitHub runner after
extracting into a new path, and intentionally tampered packages must fail before
execution. MATLAB filename aliases may be packaged byte-identically, but GNU
Octave execution alone does not qualify MATLAB behavior.

## Implementation steps and acceptance

| Step | Work | Completion evidence |
| --- | --- | --- |
| A. Fix host contract | Document four operations, argument types, return forms, error behavior and no-reset lifecycle. | API expectations and negative cases are explicit. |
| B. Build C mock | Implement MEX dispatch, minimal host wrapper and isolated CPython wrapper using consumer kit platform support. | GitHub Windows/Linux/macOS compilers build one MEX per target with required exports and loader audits. |
| C. Add backend | Implement scalar/NumPy and reuse public FEniCS solver probe, all with lazy imports. | Deterministic scalar/NumPy result; fresh embedded FEniCS numerical result/tolerance. |
| D. Run host smoke | Implement profile-aware `mock_smoke.m` and error/recovery/isolation/relocation checks. | Octave exercises real MEX on each supported GitHub runner and emits a stable pass marker. |
| E. Prove byte reuse | Fan out selected runtime profiles over one MEX SHA per target. | Qualification records bind exact identical binary bytes. |
| F. Test downloadable mock | Package through parent kit; run identical offline local launcher on freshly extracted GitHub-runner package. | Manifest/hash checks, positive tests, negative tamper test and no rebuild/download are proven. |

## Acceptance checklist

- [ ] The mock contains only independently written public bridge source and
      the minimal fixed host API described above.
- [ ] `scalar(5)==11` and `scalar(10)==21` run through embedded CPython
      from GNU Octave on all four target architectures.
- [ ] `numpy(5)==55` and `fenics` are lazy and profile-restricted, with
      NumPy provenance and fresh FFCx JIT/PETSc evidence where supported.
- [ ] Isolated CPython 3.14.10, native-provider checks, relocation, error
      recovery and repeated host calls pass.
- [ ] All selected profile tests use the exact same platform MEX SHA-256;
      package assembly never rebuilds it.
- [ ] GitHub Actions performs all builds, automated tests and qualification.
- [ ] The optional downloaded package runs the same offline Octave test
      independently on a compatible local machine and rejects tampering.
- [ ] No private source or credentials are needed to build or test the mock.
