"""Fail-closed tests for archive-exact macOS Octave host installation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "consumer-kit" / "tools" / "install-pinned-octave-macos.py"
SPEC = importlib.util.spec_from_file_location("pinned_macos_octave", SCRIPT)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class PinnedMacOSOctaveTests(unittest.TestCase):
    def test_explicit_spec_replaces_only_the_two_verified_archives(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archives = {
                "octave": root / "octave-10.3.0-build_1.conda",
                "openssl": root / "openssl-3.5.9-build_2.conda",
            }
            source = (
                "# platform: osx-arm64\n@EXPLICIT\n"
                "https://conda.anaconda.org/conda-forge/osx-arm64/octave-10.3.0-build_1.conda#abc\n"
                "https://conda.anaconda.org/conda-forge/osx-arm64/openssl-3.5.9-build_2.conda#def\n"
                "https://conda.anaconda.org/conda-forge/noarch/dependency-1.0-any.conda#ghi\n"
            )
            output = installer.pinned_explicit_spec(source, archives)
            self.assertEqual(output.count("file://"), 2)
            self.assertIn(archives["octave"].as_uri(), output)
            self.assertIn(archives["openssl"].as_uri(), output)
            self.assertIn("https://conda.anaconda.org/conda-forge/noarch/dependency-1.0-any.conda#ghi", output)
            self.assertNotIn("https://conda.anaconda.org/conda-forge/osx-arm64/octave", output)
            for malformed in (
                source.replace("octave-10.3.0-build_1", "octave-10.3.0-build_9"),
                source.replace("https://conda.anaconda.org/conda-forge/osx-arm64/openssl-3.5.9-build_2.conda#def\n", ""),
                source.replace("@EXPLICIT", "@INVALID"),
            ):
                with self.subTest(malformed=malformed):
                    with self.assertRaises(SystemExit):
                        installer.pinned_explicit_spec(malformed, archives)

    def test_installer_resolves_dependencies_then_installs_verified_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archives = {
                "octave": root / "octave-10.3.0-build_1.conda",
                "openssl": root / "openssl-3.5.9-build_2.conda",
            }
            for name, archive in archives.items():
                archive.write_bytes((name + " binary archive").encode("utf-8"))
            hashes = {name: hashlib.sha256(archive.read_bytes()).hexdigest() for name, archive in archives.items()}
            prefix = root / "actual host"
            seen: list[list[str]] = []

            def fake_run(argv, *, env, check, **kwargs):
                seen.append(argv)
                self.assertEqual(env["CONDA_SUBDIR"], "osx-arm64")
                self.assertTrue(check)
                if "--file" not in argv:
                    self.assertIn("--dry-run", argv)
                    self.assertIn("--json", argv)
                    self.assertTrue(kwargs["capture_output"])
                    self.assertTrue(kwargs["text"])
                    records = []
                    for name, archive in archives.items():
                        records.append({
                            "name": name,
                            "url": f"https://conda.anaconda.org/conda-forge/osx-arm64/{archive.name}",
                        })
                    records.append({
                        "name": "dependency",
                        "url": "https://conda.anaconda.org/conda-forge/noarch/dependency-1.0-any.conda",
                    })
                    return SimpleNamespace(stdout=json.dumps({"success": True, "dry_run": True, "actions": {"LINK": records}}))
                if "--file" in argv:
                    spec = Path(argv[argv.index("--file") + 1]).read_text(encoding="utf-8")
                    self.assertEqual(spec.count("file://"), 2)
                    metadata = prefix / "conda-meta"
                    metadata.mkdir(parents=True)
                    for name, archive in archives.items():
                        (metadata / f"{name}.json").write_text(json.dumps({
                            "name": name,
                            "version": "10.3.0" if name == "octave" else "3.5.9",
                            "build_string": "build_1" if name == "octave" else "build_2",
                            "url": archive.as_uri(),
                            "sha256": hashes[name],
                        }), encoding="utf-8")
                return SimpleNamespace(stdout="")

            arguments = [
                str(SCRIPT), "--micromamba", str(root / "micromamba"),
                "--prefix", str(prefix), "--subdir", "osx-arm64",
            ]
            for name, archive in archives.items():
                arguments += [
                    f"--{name}-archive", str(archive),
                    f"--{name}-sha256", hashes[name],
                    f"--{name}-version", "10.3.0" if name == "octave" else "3.5.9",
                    f"--{name}-build", "build_1" if name == "octave" else "build_2",
                ]
            with patch.object(sys, "argv", arguments), patch.object(subprocess, "run", side_effect=fake_run):
                self.assertEqual(installer.main(), 0)
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0][1], "create")
            self.assertEqual(seen[1][1], "create")
            self.assertIn("--dry-run", seen[0])
            self.assertNotIn("--dry-run", seen[1])

            arguments[arguments.index(hashes["octave"])] = "f" * 64
            with patch.object(sys, "argv", arguments), patch.object(subprocess, "run") as run:
                with self.assertRaisesRegex(SystemExit, "pinned octave archive SHA-256 mismatch"):
                    installer.main()
                run.assert_not_called()

    def test_dry_run_transaction_rejects_missing_or_ambiguous_package_urls(self) -> None:
        good = {"success": True, "dry_run": True, "actions": {"LINK": [
            {"name": "dependency", "url": "https://conda.anaconda.org/conda-forge/noarch/dep-1.conda"},
        ]}}
        self.assertIn("@EXPLICIT", installer.resolved_explicit_spec(json.dumps(good)))
        malformed = [
            "not json",
            json.dumps({"success": False, "actions": good["actions"]}),
            json.dumps({"success": True, "dry_run": False, "actions": good["actions"]}),
            json.dumps({"success": True, "dry_run": True, "actions": {"LINK": []}}),
            json.dumps({"success": True, "dry_run": True, "actions": {"LINK": [{"name": "dependency"}]}}),
            json.dumps({"success": True, "dry_run": True, "actions": {"LINK": good["actions"]["LINK"] * 2}}),
        ]
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(SystemExit):
                installer.resolved_explicit_spec(payload)

    def test_reused_resolution_requires_correct_architecture_and_channel(self) -> None:
        valid = ("@EXPLICIT\n"
                 "https://conda.anaconda.org/conda-forge/osx-arm64/octave-10.3.0-build_1.conda\n"
                 "https://conda.anaconda.org/conda-forge/osx-arm64/openssl-3.5.9-build_2.conda\n"
                 "https://conda.anaconda.org/conda-forge/noarch/dependency-1.0-any.conda\n")
        installer.verify_reused_resolution(valid, "osx-arm64")
        for wrong in (
            valid.replace("osx-arm64", "osx-64"),
            valid.replace("conda.anaconda.org", "evil.example"),
            valid.replace("https://", "file:///"),
            valid.replace("@EXPLICIT", "@INVALID"),
            valid + valid.splitlines()[1] + "\n",
            valid.replace("dependency-1.0-any.conda", "dependency-1.0-any.exe"),
        ):
            with self.subTest(spec=wrong), self.assertRaises(SystemExit):
                installer.verify_reused_resolution(wrong, "osx-arm64")

    def test_reused_resolution_replaces_only_local_pinned_packages(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archives = {
                "octave": root / "octave-10.3.0-build_1.conda",
                "openssl": root / "openssl-3.5.9-build_2.conda",
            }
            source = ("@EXPLICIT\n"
                      "https://conda.anaconda.org/conda-forge/osx-arm64/octave-10.3.0-build_1.conda\n"
                      "https://conda.anaconda.org/conda-forge/osx-arm64/openssl-3.5.9-build_2.conda\n"
                      "https://conda.anaconda.org/conda-forge/noarch/dependency-1.0-any.conda\n")
            installer.verify_reused_resolution(source, "osx-arm64")
            installed_spec = installer.pinned_explicit_spec(source, archives)
            self.assertEqual(installed_spec.count("file://"), 2)
            self.assertIn("https://conda.anaconda.org/conda-forge/noarch/dependency-1.0-any.conda", installed_spec)
            for invalid in (source.replace("build_1", "other_build"),
                            source.replace("openssl-3.5.9-build_2.conda\n", "")):
                with self.subTest(spec=invalid), self.assertRaises(SystemExit):
                    installer.pinned_explicit_spec(invalid, archives)

    def test_installed_host_must_name_the_local_source_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "octave-10.3.0-build_1.conda"
            archive.write_bytes(b"archive")
            metadata = root / "host" / "conda-meta"
            metadata.mkdir(parents=True)
            (metadata / "octave.json").write_text(json.dumps({
                "name": "octave", "version": "10.3.0", "build": "build_1",
                "url": "https://conda.anaconda.org/conda-forge/osx-arm64/" + archive.name,
            }), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "did not originate from the verified local archive"):
                installer.verify_installed(
                    metadata.parent, {"octave": archive},
                    {"octave": hashlib.sha256(archive.read_bytes()).hexdigest()},
                    {"octave": "10.3.0"}, {"octave": "build_1"},
                )


if __name__ == "__main__":
    unittest.main()
