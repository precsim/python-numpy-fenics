import importlib.util
import subprocess
import sys
import tempfile
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
            "core_provenance_sha": "core-provenance-one",
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

    def test_cli_binds_core_provenance_without_workflow_yaml(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paths = {
                "core_build_script": root / "core.sh",
                "core_provenance": root / "build-provenance-linux.json",
                "sources_manifest": root / "sources.json",
                "build_script": root / "numpy.sh",
            }
            for name, path in paths.items():
                path.write_text(name + "\n", encoding="utf-8")
            workflow = root / "qualify-linux-core-numpy.yml"
            workflow.write_text("jobs: {}\n", encoding="utf-8")

            def derive_key():
                output = root / "github-output"
                output.write_text("", encoding="utf-8")
                subprocess.run(
                    [
                        sys.executable, str(CACHE_KEY_SCRIPT),
                        "--core-build-script", str(paths["core_build_script"]),
                        "--core-provenance", str(paths["core_provenance"]),
                        "--sources-manifest", str(paths["sources_manifest"]),
                        "--build-script", str(paths["build_script"]),
                        "--image", "image@sha256:pinned",
                        "--github-output", str(output),
                    ],
                    check=True, capture_output=True, text=True,
                )
                values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
                self.assertEqual(values["core_provenance_sha256"], sha256_file(paths["core_provenance"]))
                self.assertNotIn("workflow_sha256", values)
                return values["key"]

            baseline = derive_key()
            workflow.write_text("jobs: {changed: true}\n", encoding="utf-8")
            self.assertEqual(derive_key(), baseline)
            paths["core_provenance"].write_text("different-core\n", encoding="utf-8")
            self.assertNotEqual(derive_key(), baseline)

    def test_cli_rejects_missing_core_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inputs = [root / name for name in ("core.sh", "sources.json", "numpy.sh")]
            for path in inputs:
                path.write_text("test", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable, str(CACHE_KEY_SCRIPT),
                    "--core-build-script", str(inputs[0]),
                    "--core-provenance", str(root / "missing.json"),
                    "--sources-manifest", str(inputs[1]),
                    "--build-script", str(inputs[2]),
                    "--image", "image@sha256:pinned",
                    "--github-output", str(root / "github-output"),
                ],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("qualified core provenance is missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
