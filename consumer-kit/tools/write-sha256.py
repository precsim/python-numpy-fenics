#!/usr/bin/env python3
"""Write the conventional SHA-256 sidecar for one immutable artifact."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    if not artifact.is_file():
        raise SystemExit(f"artifact is missing: {artifact}")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    sidecar = Path(str(artifact) + ".sha256")
    sidecar.write_text(f"{digest}  {artifact.name}\n", encoding="utf-8", newline="\n")
    print(f"sha256_sidecar={sidecar}")
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
