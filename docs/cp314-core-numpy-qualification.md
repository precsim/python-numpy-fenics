# CPython 3.14.10 core/NumPy migration qualification

The public producer core and NumPy profiles use CPython 3.14.10 (`cp314`),
NumPy 2.5.2 and producer-owned Tcl/Tk 8.6.18 on all four supported targets.

PR [#3](https://github.com/precsim/python-numpy-fenics/pull/3) merged the
qualified feature revision `95d56a5d00545fe90240c21eac1d39e0027cb12b`
into `main` as merge commit
`f6e6e77869cb56f6be7dca0179853d9a611e12fd` on 2026-11-03 (UTC).

## Feature-revision qualification evidence

All of the following GitHub Actions runs succeeded at feature SHA `95d56a5`:

| Scope | Run | Qualification |
| --- | --- | --- |
| Windows x64 core and NumPy | [41481959316](https://github.com/precsim/python-numpy-fenics/actions/runs/41481959316) | Relocation, hostile ambient state, PE closure, Tcl/Tk and packaging |
| Linux x86_64 core and NumPy | [41481958891](https://github.com/precsim/python-numpy-fenics/actions/runs/41481958891) | glibc 2.17 controlled build, offline independent tests, native providers and packaging |
| macOS arm64 core and NumPy | [41481959138](https://github.com/precsim/python-numpy-fenics/actions/runs/41481959138) | Relocated runtime, Tcl/Tk, NumPy and archive qualification |
| macOS x86_64 core and NumPy | [41481959138](https://github.com/precsim/python-numpy-fenics/actions/runs/41481959138) | Rosetta 2, relocated runtime, Tcl/Tk, NumPy and archive qualification |
| Repository checks | [41481959122](https://github.com/precsim/python-numpy-fenics/actions/runs/41481959122) | Python/policy tests, shell syntax, PowerShell syntax and immutable lock rendering |

The successful jobs uploaded qualified producer runtime artifacts. The run
records provide the exact generated asset identities and qualification
evidence; this document does not substitute for the manifests or SHA-256
records attached to those artifacts.

## Limits of the result

These runs establish successful **feature-revision** qualification and the
merge of that implementation into `main`. They are not evidence of a completed
pre-release or final downstream consumer acceptance. The release collector
requires the selected qualification runs to originate from `main`, use one
producer revision, and match the publisher's checkout revision. Feature-branch
run IDs above cannot be passed to the pre-release workflow. See
[the pre-release guide](prerelease-guide.md).

FEniCS qualification is deferred. The retained workflows now target
CPython 3.14.10 (`cp314`) and Tcl/Tk 8.6.18, but their existing FEniCS package
locks and older experiments remain tied to cp312. These do not establish
compatibility with the new cp314 base. No FEniCS profile was qualified as part
of this migration.
