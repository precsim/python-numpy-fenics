"""Exercise the standalone native provider audit against real ldd output shapes."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/linux/test-fenics-overlay.sh"


class NativeProviderResolutionTests(unittest.TestCase):
    def run_audit(self, provider: str) -> None:
        shell = SCRIPT.read_text(encoding="utf-8")
        marker = '"$staged_python" -B -I -S - "$runtime" "$overlay_manifest" <<\'PY\'\n'
        self.assertEqual(shell.count(marker), 1)
        code = shell.split(marker, 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory(prefix="fenics native spaces ") as temp:
            root = Path(temp) / "runtime with spaces"
            library = root / "fenics-runtime/conda/lib/libcares.so.2.19.8"
            library.parent.mkdir(parents=True)
            library.touch()
            census_path = root / "census.json"
            census_path.write_text(json.dumps({"census": [{
                "path": "fenics-runtime/conda/bin/adig",
                "dependencies": [{
                    "name": "libcares.so.2",
                    "resolution": "bundled",
                    "provider": "fenics-runtime/conda/lib/libcares.so.2.19.8",
                }],
            }]}), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"native": {"census": "census.json"}}), encoding="utf-8")
            ldd_output = f"\tlibcares.so.2 => {provider.format(library=library)} (0x00007f123456abcd)\n"
            with (
                mock.patch.object(sys, "argv", ["audit", str(root), str(manifest)]),
                mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=ldd_output)) as run,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                exec(compile(code, str(SCRIPT), "exec"), {"__name__": "__main__"})
            run.assert_called_once()
            self.assertIn("fenics_native_dependency_resolution=pass", output.getvalue())

    def test_ldd_resolves_provider_in_path_with_spaces(self) -> None:
        self.run_audit("{library}")

    def test_ldd_rejects_wrong_provider_even_with_spaces(self) -> None:
        with self.assertRaisesRegex(SystemExit, "bundled dependency resolved outside overlay"):
            self.run_audit("/tmp/ambient provider/libcares.so.2")


if __name__ == "__main__":
    unittest.main()
