#!/usr/bin/env python3
"""Audit a compiled reference MEX for exports and forbidden load-time providers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


FORBIDDEN = (
    "python", "octave", "liboct", "matlab", "libmx", "libmex",
    "openblas", "blas", "lapack", "mpi", "petsc",
)
GLIBC = re.compile(r"GLIBC_([0-9]+)\.([0-9]+)")
EXPECTED_EXPORTS = {"mexFunction", "__octave_mex_soversion__"}
PE_DEPENDENCY_HEADERS = {
    "image has the following dependencies:",
    "image has the following delay load dependencies:",
}


def run(command: list[str]) -> str:
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    return result.stdout + result.stderr


def run_msvc_tool(arguments: list[str]) -> str:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft Visual Studio/Installer/vswhere.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft Visual Studio/Installer/vswhere.exe",
    ]
    vswhere = next((path for path in candidates if path.is_file()), None)
    if vswhere is None:
        raise SystemExit("vswhere.exe was not found for Windows native audit")
    query = subprocess.run(
        [
            str(vswhere),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    vcvars = Path(query.stdout.strip()) / "VC/Auxiliary/Build/vcvars64.bat"
    if not vcvars.is_file():
        raise SystemExit("Visual Studio x64 developer environment was not found")
    with tempfile.TemporaryDirectory(prefix="reference-bridge-audit-") as raw:
        script = Path(raw) / "audit.cmd"
        command_line = subprocess.list2cmdline(arguments)
        script.write_text(
            "@echo off\r\n"
            f"call {subprocess.list2cmdline([str(vcvars)])} >nul\r\n"
            "if errorlevel 1 exit /b %errorlevel%\r\n"
            f"{command_line}\r\n"
            "exit /b %errorlevel%\r\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", str(script)],
            text=True,
            capture_output=True,
        )
    if result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    return result.stdout + result.stderr


def forbidden_dependencies(lines) -> list[str]:
    return sorted({
        line.strip()
        for line in lines
        if line.strip()
        and any(token in line.casefold() for token in FORBIDDEN)
    })


def pe_dependencies(output: str) -> list[str]:
    """Extract PE import names from dumpbin /dependents output only."""

    dependencies: list[str] = []
    in_section = False
    saw_section = False
    saw_dependency = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        folded = line.casefold()
        if folded in PE_DEPENDENCY_HEADERS:
            in_section = True
            saw_section = True
            saw_dependency = False
            continue
        if not in_section:
            continue
        if not line:
            if saw_dependency:
                in_section = False
            continue
        if folded == "summary":
            in_section = False
            continue
        if re.fullmatch(r".+\.dll", line, flags=re.IGNORECASE):
            dependencies.append(line)
            saw_dependency = True
            continue
        if saw_dependency:
            in_section = False

    if not saw_section:
        raise ValueError("dumpbin output has no PE dependency table")
    return dependencies


def pe_exports(output: str) -> list[str]:
    """Extract named PE exports from dumpbin /exports output."""

    exports: list[str] = []
    in_table = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if re.fullmatch(r"ordinal\s+hint\s+RVA\s+name", line, flags=re.IGNORECASE):
            in_table = True
            continue
        if not in_table:
            continue
        if not line:
            continue
        if line.casefold() == "summary":
            break
        match = re.fullmatch(
            r"\d+\s+[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+(\S+)(?:\s+=.*)?",
            line,
        )
        if match:
            exports.append(match.group(1))
    if not in_table:
        raise ValueError("dumpbin output has no PE export table")
    return exports


def nm_exports(output: str) -> list[str]:
    """Extract defined symbol names from nm output."""

    exports: list[str] = []
    for raw_line in output.splitlines():
        fields = raw_line.split()
        if len(fields) >= 2:
            exports.append(fields[-1].split("@", 1)[0])
    return exports


def normalize_exports(platform: str, raw_exports: list[str]) -> list[str]:
    if platform.startswith("macos-"):
        return [symbol[1:] if symbol.startswith("_") else symbol for symbol in raw_exports]
    return list(raw_exports)


def require_exact_exports(platform: str, raw_exports: list[str]) -> list[str]:
    normalized = normalize_exports(platform, raw_exports)
    # GNU ld emits these CRT entry points into ELF shared objects even when
    # all application symbols use hidden visibility. They are not MEX APIs.
    platform_crt = {"_init", "_fini"} if platform == "linux-x64" else set()
    observed = set(normalized) - platform_crt
    missing = sorted(EXPECTED_EXPORTS - observed)
    unexpected = sorted(observed - EXPECTED_EXPORTS)
    duplicates = sorted({symbol for symbol in normalized if symbol not in platform_crt and normalized.count(symbol) > 1})
    if missing or unexpected or duplicates:
        raise ValueError(
            f"{platform} export table differs from required MEX ABI: "
            f"missing={missing!r} unexpected={unexpected!r} duplicates={duplicates!r} "
            f"raw={raw_exports!r}"
        )
    return sorted(observed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True,
                        choices=("win-x64", "linux-x64", "macos-arm64", "macos-x64"))
    parser.add_argument("--mex", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    mex = args.mex.resolve()
    if not mex.is_file():
        raise SystemExit(f"MEX is missing: {mex}")
    mex_sha = hashlib.sha256(mex.read_bytes()).hexdigest()

    checks: dict[str, object] = {}
    if args.target == "win-x64":
        export_output = run_msvc_tool(["dumpbin", "/nologo", "/exports", str(mex)])
        imports = run_msvc_tool(["dumpbin", "/nologo", "/dependents", str(mex)])
        try:
            raw_exports = pe_exports(export_output)
            exports = require_exact_exports(args.target, raw_exports)
            dependencies = pe_dependencies(imports)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        bad = forbidden_dependencies(dependencies)
        if bad:
            raise SystemExit(f"forbidden PE load-time dependency: {bad}")
        checks.update(
            exports=exports,
            platform_exports=raw_exports,
            load_time_dependencies="system-only",
            observed_dependencies=dependencies,
        )
    elif args.target == "linux-x64":
        dynamic = run(["readelf", "-d", str(mex)])
        symbol_output = run(["nm", "-D", "--defined-only", str(mex)])
        raw_exports = nm_exports(symbol_output)
        try:
            exports = require_exact_exports(args.target, raw_exports)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        bad = forbidden_dependencies(
            line for line in dynamic.splitlines() if "(NEEDED)" in line
        )
        if bad:
            raise SystemExit(f"forbidden ELF load-time dependency: {bad}")
        versions = run(["objdump", "-T", str(mex)])
        observed = sorted({
            (int(match.group(1)), int(match.group(2)))
            for match in GLIBC.finditer(versions)
        })
        if observed and max(observed) > (2, 17):
            raise SystemExit(f"ELF requires glibc newer than 2.17: {max(observed)}")
        checks.update(
            exports=exports,
            platform_exports=raw_exports,
            load_time_dependencies="system-only",
            glibc_ceiling="2.17",
            observed_glibc_versions=[f"{major}.{minor}" for major, minor in observed],
        )
    else:
        imports = run(["otool", "-L", str(mex)])
        symbol_output = run(["nm", "-gU", str(mex)])
        raw_exports = nm_exports(symbol_output)
        try:
            exports = require_exact_exports(args.target, raw_exports)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        dependency_lines = imports.splitlines()[1:]
        bad = forbidden_dependencies(dependency_lines)
        if bad:
            raise SystemExit(f"forbidden Mach-O load-time dependency: {bad}")
        checks.update(
            exports=exports,
            platform_exports=raw_exports,
            load_time_dependencies="system-only",
        )

    if hashlib.sha256(mex.read_bytes()).hexdigest() != mex_sha:
        raise SystemExit("MEX changed during native audit")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "format_version": 1,
                "target": args.target,
                "mex_sha256": mex_sha,
                "checks": checks,
            },
            indent=2, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    print("reference_bridge_native_audit=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
