"""Host-independent checks for the packaged FEniCS JIT compiler wrapper."""

import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "macos_fenics_embed_runtime", ROOT / "runtime/macos_fenics_embed_runtime.py"
)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class JITWrapperTests(unittest.TestCase):
    def test_generated_wrapper_uses_o1_for_o2_jit_compilation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "relocated runtime"
            tools = root / "fenics-runtime/conda/bin"
            tools.mkdir(parents=True)
            (tools / "x86_64-apple-darwin13.4.0-clang").touch()
            (tools / "x86_64-apple-darwin13.4.0-ld").touch()
            (root / "Python.framework/Versions/3.14/include/python3.14").mkdir(parents=True)
            (root / "fenics-runtime/conda/include").mkdir(parents=True)
            with mock.patch.object(runtime.platform, "machine", return_value="x86_64"), \
                    mock.patch.object(runtime, "_wrapper_root", return_value=Path(temp) / "wrapper"), \
                    mock.patch.object(runtime, "_JIT_WRAPPER", None):
                wrapper = runtime._write_jit_wrapper(root).read_text(encoding="utf-8")
            self.assertIn('        -O2)\n', wrapper)
            self.assertIn('            args+=("-O1")\n', wrapper)
            self.assertIn('        -arch)\n', wrapper)
            self.assertIn('exec "$clang" -B"$tool_bin"', wrapper)


if __name__ == "__main__":
    unittest.main()