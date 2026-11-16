#!/usr/bin/env python3
"""Validate a bridge build manifest and compile one host-loadable MEX candidate.

This tool never acquires a runtime. The caller must provide an already staged,
verified producer runtime so the build is bound to its CPython 3.14 headers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tempfile


TARGETS = {"win-x64", "linux-x64", "macos-arm64", "macos-x64"}
SAFE_BASENAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class BuildContractError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildContractError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_relative(raw: str, kind: str) -> PurePosixPath:
    require(isinstance(raw, str) and raw, f"{kind} path is empty")
    require("\\" not in raw and "\x00" not in raw and ":" not in raw,
            f"{kind} path is not portable: {raw!r}")
    path = PurePosixPath(raw)
    require(not path.is_absolute() and all(part not in ("", ".", "..") for part in path.parts),
            f"{kind} path is unsafe: {raw!r}")
    return path


def unique_paths(values: object, kind: str) -> list[PurePosixPath]:
    require(isinstance(values, list), f"{kind} must be a list")
    result = [safe_relative(item, kind) for item in values]
    require(len(result) == len(set(result)), f"{kind} contains duplicate paths")
    return result


def load_build(manifest_path: Path, source_root: Path, target: str) -> tuple[dict, list[Path], list[Path]]:
    require(target in TARGETS, f"unsupported target: {target}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest.get("format_version") == 1, "bridge build format_version must be 1")
    require(manifest.get("mex_name") == "bridge", "reference bridge mex_name must be bridge")
    common_sources = unique_paths(manifest.get("sources"), "sources")
    common_includes = unique_paths(manifest.get("include_dirs", []), "include_dirs")
    unique_paths(manifest.get("python_helpers", []), "python_helpers")
    unique_paths(manifest.get("host_fixtures", []), "host_fixtures")
    targets = manifest.get("targets")
    require(isinstance(targets, dict) and target in targets, f"manifest has no target {target}")
    target_record = targets[target]
    require(isinstance(target_record, dict), f"target record is invalid: {target}")
    target_sources = unique_paths(target_record.get("sources", []), "target sources")
    all_sources = common_sources + target_sources
    require(len(all_sources) == len(set(all_sources)), "source path appears in common and target inputs")
    output = target_record.get("output")
    alias = target_record.get("matlab_alias")
    require(output == "bridge.mex", f"{target} output must be bridge.mex")
    require(isinstance(alias, str) and SAFE_BASENAME.fullmatch(alias) is not None,
            f"{target} has unsafe MATLAB alias")
    require(target_record.get("exports") == ["mexFunction"],
            f"{target} must export only mexFunction")

    root = source_root.resolve()
    source_paths: list[Path] = []
    for relative in all_sources:
        resolved = root.joinpath(*relative.parts).resolve()
        require(root in resolved.parents and resolved.is_file(),
                f"declared source does not exist under source root: {relative}")
        source_paths.append(resolved)
    include_paths: list[Path] = []
    for relative in common_includes:
        resolved = root.joinpath(*relative.parts).resolve()
        require((resolved == root or root in resolved.parents) and resolved.is_dir(),
                f"declared include directory does not exist: {relative}")
        include_paths.append(resolved)
    return manifest, source_paths, include_paths


def python_include(runtime: Path, target: str) -> Path:
    candidates = {
        "win-x64": [runtime / "Include"],
        "linux-x64": [runtime / "include/python3.14", runtime / "include/python3.14t"],
        "macos-arm64": [
            runtime / "Python.framework/Versions/3.14/Headers",
            runtime / "include/python3.14",
        ],
        "macos-x64": [
            runtime / "Python.framework/Versions/3.14/Headers",
            runtime / "include/python3.14",
        ],
    }[target]
    for candidate in candidates:
        if (candidate / "Python.h").is_file():
            return candidate.resolve()
    raise BuildContractError(f"verified runtime has no CPython 3.14 headers for {target}")


def octave_include_dirs() -> list[Path]:
    override = os.environ.get("OCTAVE_INCLUDE_DIRS")
    if override:
        parts = [Path(item).resolve() for item in override.split(os.pathsep) if item]
        require(parts and all(path.is_dir() for path in parts),
                "OCTAVE_INCLUDE_DIRS contains a missing directory")
        require(any((path / "mex.h").is_file() for path in parts),
                "OCTAVE_INCLUDE_DIRS does not contain mex.h")
        return parts

    command = shutil.which("mkoctfile")
    require(command is not None, "mkoctfile is required to discover GNU Octave headers")
    result = subprocess.run(
        [command, "-p", "INCFLAGS"], check=True, text=True, capture_output=True
    )
    flags = result.stdout.strip()
    includes: list[Path] = []
    for token in shlex.split(flags, posix=os.name != "nt"):
        if token.startswith("-I") and len(token) > 2:
            value = token[2:].strip('"')
            path = Path(value).resolve()
            if path.is_dir():
                includes.append(path)
    if not any((path / "mex.h").is_file() for path in includes):
        config = shutil.which("octave-config")
        if config:
            base = subprocess.run(
                [config, "-p", "OCTINCLUDEDIR"], check=True, text=True, capture_output=True
            ).stdout.strip()
            root = Path(base)
            if root.is_dir():
                matches = sorted(root.rglob("mex.h"))
                if matches:
                    includes.append(matches[0].parent.resolve())
    require(any((path / "mex.h").is_file() for path in includes),
            "could not find mex.h from the installed GNU Octave")
    deduped: list[Path] = []
    for path in includes:
        if path not in deduped:
            deduped.append(path)
    return deduped


def octave_version() -> str:
    override = os.environ.get("OCTAVE_VERSION")
    if override:
        require(bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", override)),
                "OCTAVE_VERSION is not a numeric GNU Octave version")
        return override
    config = shutil.which("octave-config")
    if config:
        result = subprocess.run(
            [config, "-p", "VERSION"], check=True, text=True, capture_output=True
        )
        value = result.stdout.strip()
        if value:
            return value
    command = shutil.which("mkoctfile")
    require(command is not None, "mkoctfile is required to identify GNU Octave")
    result = subprocess.run(
        [command, "-p", "VERSION"], check=True, text=True, capture_output=True
    )
    value = result.stdout.strip()
    require(bool(value), "GNU Octave did not report a version")
    return value


def compiler_version(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True)
    text = (result.stdout + result.stderr).strip()
    return text[:8192]


def locate_vcvars() -> Path:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft Visual Studio/Installer/vswhere.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft Visual Studio/Installer/vswhere.exe",
    ]
    vswhere = next((path for path in candidates if path.is_file()), None)
    require(vswhere is not None, "vswhere.exe was not found")
    result = subprocess.run(
        [
            str(vswhere), "-latest", "-products", "*",
            "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property", "installationPath",
        ],
        check=True, text=True, capture_output=True,
    )
    install = Path(result.stdout.strip())
    vcvars = install / "VC/Auxiliary/Build/vcvars64.bat"
    require(vcvars.is_file(), "Visual Studio x64 developer environment was not found")
    return vcvars


def run_msvc(args: list[str]) -> tuple[str, str]:
    vcvars = locate_vcvars()
    with tempfile.TemporaryDirectory(prefix="reference-bridge-msvc-") as directory:
        script = Path(directory) / "build.cmd"
        command_line = subprocess.list2cmdline(["cl", *args])
        script.write_text(
            "@echo off\r\n"
            f"call {subprocess.list2cmdline([str(vcvars)])} >nul\r\n"
            "if errorlevel 1 exit /b %errorlevel%\r\n"
            f"{command_line}\r\n"
            "exit /b %errorlevel%\r\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", str(script)], text=True, capture_output=True
        )
        if result.returncode:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, result.stdout, result.stderr
            )
    with tempfile.TemporaryDirectory(prefix="reference-bridge-msvc-version-") as directory:
        script = Path(directory) / "version.cmd"
        script.write_text(
            "@echo off\r\n"
            f"call {subprocess.list2cmdline([str(vcvars)])} >nul\r\n"
            "cl /Bv\r\n",
            encoding="utf-8",
        )
        version = subprocess.run(
            ["cmd.exe", "/d", "/c", str(script)], text=True, capture_output=True
        )
    return "cl", (version.stdout + version.stderr).strip()[:8192]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    source_root = args.source_root.resolve()
    runtime = args.runtime.resolve()
    require(runtime.is_dir(), "verified runtime directory is missing")
    manifest, sources, includes = load_build(manifest_path, source_root, args.target)
    python_headers = python_include(runtime, args.target)
    octave_headers = octave_include_dirs()
    host_version = octave_version()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "bridge.mex"
    output.unlink(missing_ok=True)

    all_includes = [*includes, python_headers, *octave_headers]
    if args.target == "win-x64":
        cl_args = [
            "/nologo", "/LD", "/O2", "/std:c11", "/utf-8",
            "/DWIN32_LEAN_AND_MEAN", "/DMOCK_BRIDGE_BUILD",
        ]
        cl_args += [f"/I{path}" for path in all_includes]
        cl_args += [str(path) for path in sources]
        cl_args += ["/link", f"/OUT:{output}", "/EXPORT:mexFunction"]
        compiler, version = run_msvc(cl_args)
        command_record = ["cl", *cl_args]
    elif args.target == "linux-x64":
        compiler = os.environ.get("CC", "gcc")
        command_record = [
            compiler, "-std=c11", "-O2", "-fPIC", "-fvisibility=hidden",
            "-D_GNU_SOURCE", "-shared", "-Wl,-z,defs", "-Wl,--no-undefined",
        ]
        command_record += [f"-I{path}" for path in all_includes]
        command_record += [str(path) for path in sources]
        command_record += ["-ldl", "-o", str(output)]
        subprocess.run(command_record, check=True)
        version = compiler_version([compiler, "--version"])
    else:
        compiler = os.environ.get("CC", "clang")
        arch = "arm64" if args.target == "macos-arm64" else "x86_64"
        command_record = [
            compiler, "-std=c11", "-O2", "-fPIC", "-fvisibility=hidden",
            "-arch", arch, "-bundle", "-Wl,-undefined,error",
            "-Wl,-exported_symbol,_mexFunction",
            "-Wl,-exported_symbol,___octave_mex_soversion__",
        ]
        command_record += [f"-I{path}" for path in all_includes]
        command_record += [str(path) for path in sources]
        command_record += ["-o", str(output)]
        subprocess.run(command_record, check=True)
        version = compiler_version([compiler, "--version"])

    require(output.is_file() and output.stat().st_size > 0, "compiler produced no bridge.mex")
    runtime_manifest = runtime / "runtime-manifest.json"
    require(runtime_manifest.is_file(), "verified runtime has no runtime-manifest.json")
    target_record = manifest["targets"][args.target]
    provenance = {
        "format_version": 1,
        "target": args.target,
        "source_revision": os.environ.get("GITHUB_SHA", "local"),
        "mock_revision": os.environ.get("GITHUB_SHA", "local"),
        "manifest_sha256": sha256(manifest_path),
        "runtime_manifest_sha256": sha256(runtime_manifest),
        "octave_version": host_version,
        "compiler": compiler,
        "compiler_version": version,
        "command": command_record,
        "sources": [
            {
                "path": path.relative_to(source_root).as_posix(),
                "sha256": sha256(path),
            }
            for path in sources
        ],
        "exports": target_record["exports"],
        "mex_sha256": sha256(output),
        "mex_bytes": output.stat().st_size,
    }
    args.provenance.parent.mkdir(parents=True, exist_ok=True)
    args.provenance.write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"reference_bridge_mex={output}")
    print(f"reference_bridge_mex_sha256={provenance['mex_sha256']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildContractError as exc:
        raise SystemExit(str(exc)) from exc
