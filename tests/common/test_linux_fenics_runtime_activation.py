"""Contract tests for Linux FEniCS provider activation with relocated paths."""

from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

from runtime.linux import fenics_embed_runtime as bootstrap


class LinuxFenicsRuntimeActivationTests(unittest.TestCase):
    def test_mapped_provider_path_preserves_spaces(self) -> None:
        provider_name = "/tmp/fenics runtime with spaces/libgcc_s.so.1"
        provider = Path(provider_name)
        mapping = (
            f"7f010000-7f020000 r-xp 00000000 08:01 12345 {provider_name}\n"
            "7f030000-7f040000 r-xp 00000000 08:01 12346 /usr/lib/libstdc++.so.6\n"
            "7f050000-7f060000 rw-p 00000000 00:00 0\n"
        )
        original_is_file = Path.is_file
        original_read_text = Path.read_text

        def is_file(path: Path) -> bool:
            return True if path == Path("/proc/self/maps") else original_is_file(path)

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            return mapping if path == Path("/proc/self/maps") else original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "is_file", is_file), mock.patch.object(Path, "read_text", read_text):
            self.assertEqual(bootstrap._mapped_soname_paths("libgcc_s.so.1"), {provider.resolve()})
            self.assertEqual(bootstrap._mapped_soname_paths("libstdc++.so.6"), {Path("/usr/lib/libstdc++.so.6").resolve()})


if __name__ == "__main__":
    unittest.main()
