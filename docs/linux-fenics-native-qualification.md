# Linux FEniCS native qualification: evidence and remaining gates

This document distinguishes executable feasibility experiments from a
releasable Linux producer overlay. Nothing described as a prototype here
authorizes `source_rebuilds` provenance, a hosted qualification result, or a
public prerelease.

## Frozen compatibility contract

- Consumer-owned CPython 3.12 and NumPy 2.5.2 remain outside the FEniCS
  overlay. The FEniCS overlay layers cumulatively on that same NumPy base.
- Linux host version needs must not exceed `GLIBC_2.17`, `GLIBCXX_3.4.19`,
  `CXXABI_1.3.7`, and `GCC_4.8.0`.
- `GLIBC_PRIVATE` needs are accepted only for `sysroot_linux-64`-owned files
  in the bundled JIT sysroot, never for consumer-loaded ELF objects.
- The 128-record FEniCS runtime package lock is immutable. The separate
  41-record `linux-x86_64-fenics-build-tools-explicit.txt` is a **build-only**
  solver-free closure and does not modify or replace the runtime lock.

## Stock package audit and relocation

The stock overlay fails the strict `GLIBCXX`/`CXXABI` audit after the proven
unused native extras are pruned. Required symbols from stock components such
as Basix, DOLFINx, fmt, spdlog, pugixml, ICU, KaHIP, ADIOS2 and ZeroMQ exceed
the fixed host ceilings. This failure is **required** until the smallest
validated replacement/omission set is implemented. The packaging workflow
writes `stock-native-abi-preflight.json`, containing package- and file-owned
stock violations, SHA-256 identities, maximum versions and a deterministic
selection fingerprint, before it rejects the stock overlay.

Conda text and binary prefix substitution is reconstructed from exact cached
package `info/paths.json` and installed prefix metadata. Cache payload bytes,
original installed hashes, `sha256_in_prefix`, staged bytes, substitutions
and final file hashes are independently checked. A controlled inventory
reproduced all **571** declared substitutions; the current selected payload
contains **396** metadata-relocated files and drops **19** prefix-bound
command scripts. Neither raw producer prefixes nor undeclared prefix
substitutions are globally exempted. The final raw-prefix gate stays strict.

A separate read-only ELF `DT_NEEDED` closure audit, rooted in rebuilt
Basix/DOLFINx, PETSc, MPI, HDF5, three fresh FFCx modules and 19 NumPy
native extensions, reached 113 ELF objects with no unresolved non-system
dependencies. With DOLFINx actually configured `KAHIP=OFF` and `ADIOS2=OFF`,
neither KaHIP nor ADIOS2 nor its downstream ZeroMQ provider was in that
closure. Removing 610 package-owned KaHIP/ADIOS2/ZeroMQ files from a scratch
probe preserved full Poisson correctness and loaded none of those providers.
This is conditional omission evidence, **not** a general assertion that
optional KaHIP partitioning or ADIOS2/HDF5 VOL functionality is supported.

ICU cannot be treated the same way: `libicuuc` and `libicudata` remain live
through HDF5 → curl → libpsl → ICU and MPI → hwloc → libxml2 → ICU. The stock
ICU C++ symbols exceed the host ceilings, so its remaining consumers must
be rebuilt/reconfigured or supplied a separately qualified native provider
before ICU is removed. Generic PETSc/HDF5 plugin `dlopen` facilities mean
ELF `DT_NEEDED` alone cannot qualify arbitrary opt-in plugins.

## Controlled C++20 runtime feasibility, not production evidence

The initial rebuilt Basix/DOLFINx prototype used a versioned private static
libstdc++ provider, but mesh construction crashed inside `std::codecvt` when
NumPy loaded a competing stock libstdc++. GNU-unique locale facet exports
were coalescing across the two providers despite the private version
namespace. Replacing **146** runtime `.dynsym` `STB_GNU_UNIQUE` bindings with
`STB_GLOBAL` in the GCC 16.2 locked-compiler-linked provider eliminated the
observed collision in controlled tests.

The resulting experimental provider SHA-256 is
`39473bb7046bd62af7fbafda82c18d6f755de28a9e32ec65734985d72ff71cb4`.
Two independent scratch links and transformations produced identical bytes.
The provider requires no host `GLIBCXX`, `CXXABI` or `GLIBC_PRIVATE` versions;
its maximum numeric requirements are `GLIBC_2.17` and `GCC_4.3.0`.

Under the controlled rebuild, NumPy 2.5.2 loaded first and a full
`tests/fenics/test_poisson.py` solve passed with L2 error
`0.009053553714814033` (tolerance `0.05`). A separate scratch test loaded
the corrected provider through ordinary dependencies from a copied overlay,
without `ctypes` preloading, after converting eight native ELF RPATHs to
overlay-relative locations; Poisson passed again. That scratch test still
had an absolute fallback search path into the stock conda prefix, so it is
**not** a standalone relocatable qualification.

`scripts/linux/elf_gnu_unique.py` is a fail-closed, exact-hash ELF
transformation/auditor with standalone tests. It explicitly labels its output
as a *symbol-binding transformation*, **not** a source rebuild. The linked
provider alone does not qualify the surrounding Basix/DOLFINx binaries.

The immutable runtime lock supplies the GCC 16.2 C driver and the static
libstdc++ archive, sufficient for the provider-link experiment, but does
**not** contain a C++ frontend (`cc1plus`). A separately pinned GCC 14.4
C++20 build-tool closure was replayed offline with zero package downloads
and passed a C++20 compiler/CMake/Ninja smoke test. It still needs to be
used in a deterministic, independently audited full native rebuild.

## Remaining release blockers

1. Prove package-by-package which optional stock native providers can be
   omitted and which must be rebuilt; validate the actual dependency and
   plugin/dlopen closure rather than relying on compilation flags alone.
2. Rebuild the minimal required Basix, DOLFINx and dependent C++ components
   and Python extensions from pinned sources using the frozen build-only
   toolchain. Keep the private provider and output transformations
   independently hash-audited, without inventing source provenance.
3. Replace temporary paths with fully relative RPATHs, verify immutable
   package ownership, strict ELF/native provider closure, staged privacy,
   prefix replacement and relocated standalone fresh-FFCx/PETSc/Poisson.
4. Rebuild deterministic assets and attach valid source, license, archive,
   manifest and qualification evidence to the **exact feature SHA**.
5. Require the complete 12-identity producer GitHub Actions matrix green at
   that one SHA, then merge and requalify `main`. Only a fully qualified
   `main` prerelease can become the immutable public downstream input.

Private MATLAB/Octave/MEX bridge qualification and final private release
authority remain downstream of those public producer gates.
