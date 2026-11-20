# macOS FEniCS qualification and optimization record

This record describes the completed macOS FEniCS producer qualification and
performance work for CPython 3.14.10 (`cp314`). It is producer evidence only;
downstream applications still require their own host/product qualification.

## Final verified state

The final verification was run from `main` revision
`a9d484fcb2b6da8980768f070a41273334736bf8` with GitHub Actions run
[44712399272](https://github.com/precsim/python-numpy-fenics/actions/runs/44712399272).
Both macOS jobs completed successfully:

| Target | Job | Result | Wall time |
| --- | --- | --- | --- |
| macOS arm64 | `qualify-macos-fenics-arm64` | success | 5m35s |
| macOS x86_64 under Rosetta 2 | `qualify-macos-fenics-x86_64` | success | 7m49s |

The run replayed the immutable package lock without solving, restored the
qualified NumPy base, packaged the relocatable overlay, executed the exact
packaged overlay, rebuilt it and proved deterministic identity, audited the
packaged ZIP, removed producer source environments, and emitted immutable
handoff/qualification evidence.

The exact-overlay qualification retains the runtime guarantees that matter to
the shipped artifact: relocation and native Mach-O closure checks, architecture
and deployment-target checks, install-name/RPATH/dependency validation,
codesign checks, packaged compiler/linker validation, NumPy ownership and
identity checks, import/version/path validation, a fresh FFCx JIT/PETSc solve,
and audit of the freshly generated JIT Mach-O extensions. The deterministic
second packaging pass remains byte-identical for the ZIP, JSON summary and
SHA-256 evidence.

This final run is a correctness verification, not a stable performance
benchmark. Hosted macOS/Rosetta timings vary substantially between runners, so
performance changes were accepted only when the directly enclosing phase
improved and the result repeated on an unchanged head.

## Retained performance result

The largest retained x86_64/Rosetta improvement is PR
[#54](https://github.com/precsim/python-numpy-fenics/pull/54), which warms the
packaged Clang with one trivial object compilation before the first real FFCx
JIT compile. On its hosted validation run, the warm-up took about one second,
the first real FFCx compile fell from about 72.4s to 2.4s, FFCx/problem
construction fell from about 80.4s to 9.6s, and the exact-overlay qualification
step fell from about 3m27s to 2m07s. The warm-up is restricted to the
x86_64-on-arm64 Rosetta path; arm64 qualification remains unchanged.

Other retained changes reduced repeated license scans and Mach-O inspection,
batched independent audits, reused parsed load-command/install-name metadata,
removed a duplicate JIT solve, and reduced ZIP compression cost while retaining
Deflate level 6 and deterministic artifact identity. Profiling-only changes that
remain on `main` are diagnostic instrumentation and do not relax qualification
requirements.

## Exhausted optimization directions

The following experiment families were measured on hosted GitHub Actions and
were closed when their enclosing phase failed to improve repeatably or when the
change weakened an accepted invariant:

| Family | Representative PRs | Outcome |
| --- | --- | --- |
| Source hashing/staging/copy overlap | #47, #49, #57 | inner work moved or overlapped, enclosing packaging did not improve reliably |
| Compression level/parallel Deflate | #60, #77 | lower level increased artifact size and regressed packaging; safe bounded parallel compression lost the raw gain in deterministic rebuild/whole packaging |
| Mach-O metadata mutation batching | #63, #71 | fewer tool invocations, but enclosing relocation/rebuild did not improve |
| Deterministic rebuild shortcuts | #72, #73 | enclosing rebuild regressed and/or introduced provenance/trust concerns |
| Runtime-audit fusion / duplicate ZIP extraction | #76, #78, #79 | duplicate extraction could be removed safely, but the changed audit phase did not retain a material repeatable x86_64 win; independent `unzip -t` remains |
| JIT compiler flag/cold-start variants | #50, #52, #53, #55 | no repeatable enclosing improvement beyond the retained PR #54 warm-up |
| Native-library preload/import overlap | #58, #64, #66-#70 | first loads are expensive, but overlap/preload moved contention rather than reducing enclosing wall time |
| Payload/tool pruning | #75 | smaller payload did not improve enclosing packaging/rebuild/whole-job time |
| Qualification/rebuild overlap | #62 | contention removed the apparent first-sample gain |

The final ZIP-structure profiling showed that the remaining material audit cost
is dominated by a full `unzip -t` integrity pass. Removing that independent
full-archive check would weaken the accepted packaged-artifact guarantee and was
not adopted.

## Guidance for future optimization

Do not reopen the exhausted experiment families solely because a single hosted
sample looks faster. A new optimization should start from a new profiling
signal on current `main`, preserve the existing artifact/runtime guarantees, and
be evaluated on the directly enclosing phase. A material improvement requires
an unchanged-head repeat before merge.

In particular, retain deterministic second-pass packaging, byte-for-byte
ZIP/JSON/SHA identity, source SHA verification, exact-overlay qualification,
fresh FFCx/PETSc JIT execution, NumPy ownership/identity, native dependency and
RPATH checks, deployment-target/codesign checks, and the fail-closed archive
integrity audit.
