#!/usr/bin/env python3
"""Audit a staged macOS runtime's Mach-O closure and deployment floor."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


DEPENDENCY_COMMANDS = {
    "LC_LOAD_DYLIB",
    "LC_LOAD_WEAK_DYLIB",
    "LC_REEXPORT_DYLIB",
    "LC_LOAD_UPWARD_DYLIB",
    "LC_LAZY_LOAD_DYLIB",
}
SYSTEM_PREFIXES = ("/usr/lib/", "/System/Library/")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_key(value: str) -> tuple[int, ...]:
    parts = [int(part) for part in value.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def parse_load_commands(text: str) -> dict[str, object]:
    dependencies: list[str] = []
    rpaths: list[str] = []
    minos: list[str] = []
    dylib_id: str | None = None
    current_cmd = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("cmd "):
            current_cmd = line.split(None, 1)[1]
            continue
        if current_cmd in DEPENDENCY_COMMANDS and line.startswith("name "):
            dependencies.append(line.removeprefix("name ").split(" (offset ", 1)[0])
            continue
        if current_cmd == "LC_ID_DYLIB" and line.startswith("name "):
            dylib_id = line.removeprefix("name ").split(" (offset ", 1)[0]
            continue
        if current_cmd == "LC_RPATH" and line.startswith("path "):
            rpaths.append(line.removeprefix("path ").split(" (offset ", 1)[0])
            continue
        if current_cmd == "LC_BUILD_VERSION" and line.startswith("minos "):
            minos.append(line.split(None, 1)[1])
            continue
        if current_cmd == "LC_VERSION_MIN_MACOSX" and line.startswith("version "):
            minos.append(line.split(None, 1)[1])

    return {
        "dependencies": list(dict.fromkeys(dependencies)),
        "rpaths": list(dict.fromkeys(rpaths)),
        "minos": minos,
        "dylib_id": dylib_id,
    }


def run(*args: str) -> str:
    proc = subprocess.run(
        list(args),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {args!r}\n{proc.stdout}")
    return proc.stdout


def under(path: Path, root: Path) -> bool:
    path = path.resolve()
    root = root.resolve()
    return path == root or root in path.parents


def expand_anchor(value: str, binary: Path, executable_dir: Path) -> Path | None:
    if value == "@loader_path":
        return binary.parent
    if value.startswith("@loader_path/"):
        return binary.parent / value.removeprefix("@loader_path/")
    if value == "@executable_path":
        return executable_dir
    if value.startswith("@executable_path/"):
        return executable_dir / value.removeprefix("@executable_path/")
    if value.startswith("/"):
        return Path(value)
    return None


def system_dependency(value: str) -> bool:
    return value.startswith(SYSTEM_PREFIXES)


def bundled_resolution(
    dependency: str,
    *,
    binary: Path,
    root: Path,
    executable_dir: Path,
    rpaths: list[str],
) -> tuple[Path | None, list[str]]:
    candidates: list[Path] = []
    diagnostics: list[str] = []
    if dependency.startswith("@rpath/"):
        suffix = dependency.removeprefix("@rpath/")
        for rpath in rpaths:
            base = expand_anchor(rpath, binary, executable_dir)
            if base is None:
                diagnostics.append(f"unsupported-rpath:{rpath}")
                continue
            candidate = base / suffix
            candidates.append(candidate)
    else:
        candidate = expand_anchor(dependency, binary, executable_dir)
        if candidate is not None:
            candidates.append(candidate)

    resolved: list[Path] = []
    for candidate in candidates:
        if system_dependency(str(candidate)):
            diagnostics.append(f"system-candidate:{candidate}")
            continue
        try:
            provider = candidate.resolve(strict=True)
        except OSError:
            diagnostics.append(f"missing:{candidate}")
            continue
        if not under(provider, root):
            diagnostics.append(f"escaped:{provider}")
            continue
        resolved.append(provider)

    unique = sorted({item for item in resolved}, key=str)
    if len(unique) == 1 and not any(item.startswith("system-candidate:") for item in diagnostics):
        return unique[0], diagnostics
    if len(unique) > 1:
        diagnostics.append("ambiguous:" + ",".join(str(item) for item in unique))
    return None, diagnostics


def audit(
    root: Path,
    output: Path,
    target_arch: str,
    strict: bool,
    max_minos: str,
    executable: Path | None = None,
) -> dict[str, object]:
    root = root.resolve()
    max_minos_key = version_key(max_minos)
    executable = (
        executable.resolve()
        if executable is not None
        else root / "Python.framework/Versions/3.12/bin/python3.12"
    )
    executable_dir = executable.parent
    if strict and executable is not None and not executable.is_file():
        raise SystemExit(f"staged Python executable is missing: {executable}")

    entries: list[dict[str, object]] = []
    measured_versions: list[str] = []
    bundled_dependency_count = 0
    system_dependency_count = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        kind = run("file", "-b", str(path)).strip()
        if "Mach-O" not in kind:
            continue
        architectures = run("lipo", "-archs", str(path)).strip().split()
        if target_arch not in architectures:
            if strict:
                raise SystemExit(
                    f"Mach-O payload lacks target architecture {target_arch}: "
                    f"{path.relative_to(root).as_posix()} architectures={architectures!r}"
                )
            continue

        parsed = parse_load_commands(run("otool", "-arch", target_arch, "-l", str(path)))
        dependencies = list(parsed["dependencies"])
        rpaths = list(parsed["rpaths"])
        minos = list(parsed["minos"])
        for rpath in rpaths:
            if rpath.startswith("/") and not system_dependency(rpath):
                if strict:
                    raise SystemExit(
                        f"Mach-O payload contains non-relocatable LC_RPATH: "
                        f"{path.relative_to(root).as_posix()} -> {rpath}"
                    )
            elif not (
                rpath.startswith("@loader_path")
                or rpath.startswith("@executable_path")
                or system_dependency(rpath)
            ):
                if strict:
                    raise SystemExit(
                        f"Mach-O payload contains unsupported LC_RPATH: "
                        f"{path.relative_to(root).as_posix()} -> {rpath}"
                    )
        if strict and not minos:
            raise SystemExit(
                "Mach-O payload has no macOS minimum-version load command: "
                f"{path.relative_to(root).as_posix()}"
            )
        for value in minos:
            if strict and version_key(value) > max_minos_key:
                raise SystemExit(
                    f"Mach-O payload raises macOS floor above {max_minos}: "
                    f"{path.relative_to(root).as_posix()} minos={value}"
                )
        measured_versions.extend(minos)

        resolutions: list[dict[str, object]] = []
        for dependency in dependencies:
            if system_dependency(dependency):
                resolutions.append({"name": dependency, "resolution": "system"})
                system_dependency_count += 1
                continue
            provider, diagnostics = bundled_resolution(
                dependency,
                binary=path,
                root=root,
                executable_dir=executable_dir,
                rpaths=rpaths,
            )
            if provider is None:
                if strict:
                    raise SystemExit(
                        f"Mach-O dependency is not relocatably resolved inside runtime: "
                        f"{path.relative_to(root).as_posix()} -> {dependency}; "
                        f"diagnostics={diagnostics!r} rpaths={rpaths!r}"
                    )
                resolutions.append(
                    {
                        "name": dependency,
                        "resolution": "unresolved",
                        "diagnostics": diagnostics,
                    }
                )
                continue
            provider_rel = provider.relative_to(root).as_posix()
            resolutions.append(
                {
                    "name": dependency,
                    "resolution": "bundled",
                    "provider": provider_rel,
                    "provider_sha256": sha256(provider),
                }
            )
            bundled_dependency_count += 1

        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256(path),
                "architectures": architectures,
                "target_arch": target_arch,
                "dependencies": dependencies,
                "dependency_resolutions": resolutions,
                "rpaths": rpaths,
                "dylib_id": parsed["dylib_id"],
                "minos": max(minos, key=version_key) if minos else None,
                "file": kind,
            }
        )

    if strict and not entries:
        raise SystemExit(f"no Mach-O files found under {root}")
    if strict and not measured_versions:
        raise SystemExit(f"no macOS compatibility floor measured under {root}")

    measured_max = max(measured_versions, key=version_key) if measured_versions else None
    payload = {
        "format_version": 2,
        "target_arch": target_arch,
        "root": ".",
        "status": "pass" if strict else "observed",
        "max_allowed_minos": max_minos,
        "macho_count": len(entries),
        "bundled_dependency_count": bundled_dependency_count,
        "system_dependency_count": system_dependency_count,
        "target_measured_max_minos": measured_max,
        "mach_o": entries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--arch", choices=("arm64", "x86_64"), required=True)
    parser.add_argument("--executable")
    parser.add_argument("--max-minos", default="14.0")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    payload = audit(
        Path(args.root),
        Path(args.output),
        args.arch,
        args.strict,
        args.max_minos,
        Path(args.executable) if args.executable else None,
    )
    print(f"macho_count={payload['macho_count']}")
    print(f"bundled_dependency_count={payload['bundled_dependency_count']}")
    print(f"system_dependency_count={payload['system_dependency_count']}")
    print(f"target_measured_max_minos={payload['target_measured_max_minos']}")
    print(f"native_closure_status={payload['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
