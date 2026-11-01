#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "usage: scripts/macos/inspect-macos-runtime.sh --root <tree> --output <json> --arch arm64|x86_64 [--strict]" >&2
    exit 2
}

root=""
output=""
arch=""
strict=false
while (($#)); do
    case "$1" in
        --root) root="$2"; shift 2 ;;
        --output) output="$2"; shift 2 ;;
        --arch) arch="$2"; shift 2 ;;
        --strict) strict=true; shift ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

[[ -d "$root" ]] || { echo "inspection root is missing: $root" >&2; exit 1; }
[[ -n "$output" ]] || { echo "inspection output is required" >&2; exit 1; }
[[ "$arch" == arm64 || "$arch" == x86_64 ]] || { echo "unsupported architecture: $arch" >&2; exit 1; }
for tool in file lipo otool python3; do
    command -v "$tool" >/dev/null 2>&1 || { echo "$tool is required" >&2; exit 1; }
done

python3 - "$root" "$output" "$arch" "$strict" <<'PY'
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

root = pathlib.Path(sys.argv[1]).resolve()
output = pathlib.Path(sys.argv[2]).resolve()
target_arch = sys.argv[3]
strict = sys.argv[4].lower() == "true"


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


def version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def target_load_commands(path: pathlib.Path) -> tuple[list[str], list[str]]:
    dependencies: list[str] = []
    for line in run("otool", "-arch", target_arch, "-L", str(path)).splitlines()[1:]:
        match = re.match(r"^\s*(.+?)\s+\(compatibility version ", line)
        if match:
            dependencies.append(match.group(1))

    load_text = run("otool", "-arch", target_arch, "-l", str(path))
    rpaths: list[str] = []
    minos: list[str] = []
    current_cmd = ""
    for raw in load_text.splitlines():
        line = raw.strip()
        if line.startswith("cmd "):
            current_cmd = line.split(None, 1)[1]
            continue
        if current_cmd == "LC_RPATH" and line.startswith("path "):
            value = line.removeprefix("path ").split(" (offset ", 1)[0]
            rpaths.append(value)
            continue
        if current_cmd == "LC_BUILD_VERSION" and line.startswith("minos "):
            minos.append(line.split(None, 1)[1])
            continue
        if current_cmd == "LC_VERSION_MIN_MACOSX" and line.startswith("version "):
            minos.append(line.split(None, 1)[1])

    return sorted(set(dependencies)), sorted(set(rpaths)), minos


entries: list[dict[str, object]] = []
measured_versions: list[str] = []
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
    dependencies, rpaths, minos = target_load_commands(path)
    if strict and not minos:
        raise SystemExit(
            f"Mach-O payload has no macOS minimum-version load command: "
            f"{path.relative_to(root).as_posix()}"
        )
    if minos:
        measured_versions.extend(minos)
    entries.append(
        {
            "path": path.relative_to(root).as_posix(),
            "architectures": architectures,
            "target_arch": target_arch,
            "dependencies": dependencies,
            "rpaths": rpaths,
            "minos": max(minos, key=version_key) if minos else None,
            "file": kind,
        }
    )

if strict and not entries:
    raise SystemExit(f"no Mach-O files found under {root}")
if strict and not measured_versions:
    raise SystemExit(f"no macOS compatibility floor measured under {root}")

payload = {
    "format_version": 1,
    "target_arch": target_arch,
    "root": str(root),
    "macho_count": len(entries),
    "target_measured_max_minos": (
        max(measured_versions, key=version_key) if measured_versions else None
    ),
    "mach_o": entries,
}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"macho_count={len(entries)}")
print(f"target_measured_max_minos={payload['target_measured_max_minos']}")
PY
