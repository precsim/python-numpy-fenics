from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.linux.fetch_fenics_rebuild_sources import (
    EXPECTED_SOURCES,
    SourceFetchError,
    load_manifest,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "locks" / "linux" / "linux-x86_64-fenics-rebuild-sources.json"


class LinuxFenicsRebuildSourceTests(unittest.TestCase):
    def test_checked_in_manifest_is_public_and_fixed(self) -> None:
        data = load_manifest(MANIFEST)
        self.assertEqual(tuple(data["sources"]), EXPECTED_SOURCES)
        self.assertTrue(all(record["url"].startswith("https://") for record in data["sources"].values()))

    def test_private_or_path_escaping_source_is_rejected(self) -> None:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "manifest.json"
            private = copy.deepcopy(data)
            private["sources"]["basix"]["url"] = "https://internal.example.invalid/basix.tar.gz"
            path.write_text(json.dumps(private), encoding="utf-8")
            with self.assertRaisesRegex(SourceFetchError, "allowed public HTTPS origin"):
                load_manifest(path)

            escaping = copy.deepcopy(data)
            escaping["sources"]["basix"]["filename"] = "../basix.tar.gz"
            path.write_text(json.dumps(escaping), encoding="utf-8")
            with self.assertRaisesRegex(SourceFetchError, "invalid source filename"):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
