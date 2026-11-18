import importlib.util
import unittest
from pathlib import Path


CACHE_KEY_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "linux" / "numpy_build_cache_key.py"
SPEC = importlib.util.spec_from_file_location("numpy_build_cache_key", CACHE_KEY_SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load cache-key helper: {CACHE_KEY_SCRIPT}")
CACHE_KEY_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CACHE_KEY_MODULE)
contract_key = CACHE_KEY_MODULE.contract_key
sha256_file = CACHE_KEY_MODULE.sha256_file


class LinuxNumpyBuildCacheKeyTests(unittest.TestCase):
    def test_contract_key_binds_every_build_contract_input(self):
        inputs = {
            "image": "image@sha256:one",
            "core_build_script_sha": "core-script-one",
            "workflow_sha": "workflow-one",
            "manifest_sha": "manifest-one",
            "numpy_build_script_sha": "numpy-script-one",
            "helper_sha": "helper-one",
        }
        baseline = contract_key(**inputs)

        for name in inputs:
            with self.subTest(name=name):
                changed = dict(inputs)
                changed[name] = f"{inputs[name]}-changed"
                self.assertNotEqual(baseline, contract_key(**changed))

    def test_file_hash_changes_with_content(self):
        self.assertNotEqual(sha256_file(CACHE_KEY_SCRIPT), sha256_file(Path(__file__)))


if __name__ == "__main__":
    unittest.main()
