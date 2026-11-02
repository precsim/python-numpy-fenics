"""Fail-closed tests for producer pre-release identity and privacy checks."""

from __future__ import annotations

import io
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

from scripts.common import prepare_prerelease as prerelease


class PreparePrereleaseTests(unittest.TestCase):
    @staticmethod
    def json_bytes(payload: dict[str, object]) -> bytes:
        return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def write_artifact(
        self,
        path: Path,
        manifest: dict[str, object],
        *,
        extra_members: dict[str, bytes] | None = None,
    ) -> bytes:
        manifest_name = (
            "fenics-embed-manifest.json"
            if manifest.get("profile") == "fenics"
            else "runtime-manifest.json"
        )
        members = {manifest_name: self.json_bytes(manifest), **(extra_members or {})}
        if path.name.endswith(".tar.gz"):
            with tarfile.open(path, "w:gz") as archive:
                for name, payload in members.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
        else:
            with zipfile.ZipFile(path, "w") as archive:
                for name, payload in members.items():
                    archive.writestr(name, payload)
        return members[manifest_name]

    def numpy_manifest(self, platform: str) -> dict[str, object]:
        prefix = prerelease.NUMPY_SITE_PREFIXES[platform]
        entries: list[dict[str, object]] = [
            {
                "path": f"{prefix}numpy/__init__.py",
                "kind": "file",
                "bytes": 7,
                "sha256": "1" * 64,
            },
            {
                "path": f"{prefix}numpy.libs/libopenblas.so",
                "kind": "file",
                "bytes": 11,
                "sha256": "2" * 64,
            },
            {
                "path": f"{prefix}numpy-2.5.2.dist-info/METADATA",
                "kind": "file",
                "bytes": 13,
                "sha256": "3" * 64,
            },
            {
                "path": f"{prefix}unrelated/package.py",
                "kind": "file",
                "bytes": 17,
                "sha256": "4" * 64,
            },
        ]
        if platform == "win-x64":
            for entry in entries:
                entry.pop("kind", None)
        return {
            "format_version": 1,
            "platform": platform,
            "profile": "numpy",
            "python_abi": "cp314",
            "version": "0.1.0",
            "producer": {
                "repository": "precsim/python-numpy-fenics",
                "revision": "a" * 40,
            },
            "files": entries,
        }

    def artifact_records(self) -> dict[tuple[str, str], dict[str, object]]:
        records: dict[tuple[str, str], dict[str, object]] = {}
        for index, platform in enumerate(sorted(prerelease.NUMPY_SITE_PREFIXES), start=1):
            manifest = self.numpy_manifest(platform)
            identity = prerelease.numpy_tree_identity_from_manifest(
                manifest,
                context=f"{platform}-numpy",
            )
            manifest_sha = f"{index:x}" * 64
            manifest_sha = manifest_sha[:64]
            numpy_record = {
                "artifact": f"numpy-{platform}.zip",
                "artifact_sha256": f"{index + 4:x}" * 64,
                "manifest": manifest,
                "manifest_sha256": manifest_sha,
                "source_provenance": None,
            }
            fenics_numpy: dict[str, object] = {
                "version": "2.5.2",
                "base_tree_sha256": identity["tree_sha256"],
                "base_entry_count": identity["entry_count"],
            }
            source_provenance = None
            if platform == "linux-x64":
                source_provenance = {"numpy_base": {"manifest_sha256": manifest_sha}}
            else:
                fenics_numpy["base_manifest_sha256"] = manifest_sha
            records[(platform, "numpy")] = numpy_record
            records[(platform, "fenics")] = {
                "artifact": f"fenics-{platform}.zip",
                "artifact_sha256": f"{index + 8:x}" * 64,
                "manifest": {
                    "format_version": 1,
                    "platform": platform,
                    "profile": "fenics",
                    "numpy": fenics_numpy,
                },
                "manifest_sha256": f"{index:x}" * 64,
                "source_provenance": source_provenance,
            }
        return records

    def test_numpy_tree_identity_is_platform_normalized(self) -> None:
        identities = {
            platform: prerelease.numpy_tree_identity_from_manifest(
                self.numpy_manifest(platform),
                context=platform,
            )
            for platform in prerelease.NUMPY_SITE_PREFIXES
        }
        self.assertEqual({identity["entry_count"] for identity in identities.values()}, {3})
        self.assertEqual(len({identity["tree_sha256"] for identity in identities.values()}), 1)

    def test_linux_existing_numpy_tree_digest_matches_prerelease_normalization(self) -> None:
        from scripts.linux.package_fenics_overlay import numpy_tree_identity

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # The deferred Linux FEniCS packager still targets its existing cp312
            # lock. Its tree identity is prefix-independent, so compare that
            # historical base tree with the active cp314 manifest normalization.
            site = root / "lib" / "python3.12" / "site-packages"
            payloads = {
                "numpy/__init__.py": b"numpy\n",
                "numpy.libs/libopenblas.so": b"openblas\n",
                "numpy-2.5.2.dist-info/METADATA": b"metadata\n",
            }
            files = []
            for relative, payload in payloads.items():
                path = site / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                files.append(
                    {
                        "path": f"lib/python3.14/site-packages/{relative}",
                        "kind": "file",
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            manifest = {
                "format_version": 1,
                "platform": "linux-x64",
                "profile": "numpy",
                "files": files,
            }
            existing = numpy_tree_identity(root)
            normalized = prerelease.numpy_tree_identity_from_manifest(manifest, context="linux-fixture")
            self.assertEqual(normalized, existing)

    def test_cross_profile_binding_records_exact_numpy_artifact(self) -> None:
        bindings = prerelease.validate_cross_profile_base_identities(self.artifact_records())
        self.assertEqual(len(bindings), len(prerelease.NUMPY_SITE_PREFIXES))
        linux = next(binding for binding in bindings if binding["platform"] == "linux-x64")
        self.assertEqual(linux["numpy_artifact"], "numpy-linux-x64.zip")
        self.assertEqual(linux["numpy_entry_count"], 3)

    def test_cross_profile_binding_rejects_tree_or_manifest_mismatch(self) -> None:
        records = self.artifact_records()
        records[("win-x64", "fenics")]["manifest"]["numpy"]["base_tree_sha256"] = "0" * 64
        with self.assertRaisesRegex(SystemExit, "base tree identity"):
            prerelease.validate_cross_profile_base_identities(records)

        records = self.artifact_records()
        records[("macos-arm64", "fenics")]["manifest"]["numpy"]["base_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(SystemExit, "base manifest identity"):
            prerelease.validate_cross_profile_base_identities(records)

    def test_qualification_check_contract_is_exact(self) -> None:
        identity = ("win-x64", "numpy")
        expected = dict(prerelease.EXPECTED_QUALIFICATION_CHECKS[identity])
        prerelease.validate_qualification_checks(*identity, expected, Path("qualification.json"))

        variants = []
        missing = dict(expected)
        missing.pop("openblas")
        variants.append(missing)
        extra = dict(expected)
        extra["invented_check"] = "pass"
        variants.append(extra)
        wrong = dict(expected)
        wrong["openblas"] = "not-applicable"
        variants.append(wrong)
        for checks in variants:
            with self.subTest(checks=checks), self.assertRaisesRegex(SystemExit, "check contract mismatch"):
                prerelease.validate_qualification_checks(*identity, checks, Path("qualification.json"))

    def test_archive_member_scan_rejects_private_source_and_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "good.zip"
            with zipfile.ZipFile(good, "w") as archive:
                archive.writestr("runtime/package.py", b"ok")
                archive.writestr("runtime/src/public_header.cpp", b"public")
                archive.writestr("fenics-runtime/conda/include/petsc/private/petscimpl.h", b"public dependency")
            prerelease.validate_archive_members(good)

            forbidden_members = (
                "src/private_backend.cpp",
                "private/backend.cpp",
                "fenics-runtime/private/backend.cpp",
                "runtime/src/private/backend.cpp",
                "runtime/python-matlab-octave-bridge/backend.cpp",
                "runtime/private_bridge.mexw64",
                "../consumer-secret.txt",
            )
            for index, member in enumerate(forbidden_members):
                archive_path = root / f"bad-{index}.zip"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(member, b"private")
                with self.subTest(member=member), self.assertRaises(SystemExit):
                    prerelease.validate_archive_members(archive_path)

    def test_archive_member_scan_applies_to_tar_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "bad.tar.gz"
            payload = b"private"
            with tarfile.open(artifact, "w:gz") as archive:
                info = tarfile.TarInfo("private/private_backend.cpp")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            with self.assertRaisesRegex(SystemExit, "private-consumer source payload"):
                prerelease.validate_archive_members(artifact)

    def test_linux_source_provenance_json_can_be_read_from_tar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "fenics.tar.gz"
            payload = json.dumps({"numpy_base": {"manifest_sha256": "a" * 64}}).encode()
            with tarfile.open(artifact, "w:gz") as archive:
                info = tarfile.TarInfo("fenics-runtime/provenance/source-provenance.json")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            parsed = prerelease.read_archive_json_member(
                artifact,
                "fenics-runtime/provenance/source-provenance.json",
            )
            self.assertEqual(parsed["numpy_base"]["manifest_sha256"], "a" * 64)

    def test_full_prerelease_fixture_records_cross_profile_bindings(self) -> None:
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming"
            output = root / "release-assets"
            incoming.mkdir()

            manifests: dict[tuple[str, str], dict[str, object]] = {}
            extras: dict[tuple[str, str], dict[str, bytes]] = {}
            for platform in prerelease.NUMPY_SITE_PREFIXES:
                core_manifest = {
                    "format_version": 1,
                    "version": "0.1.0",
                    "platform": platform,
                    "profile": "core",
                    "python_abi": "cp314",
                    "producer": {
                        "repository": "precsim/python-numpy-fenics",
                        "revision": revision,
                    },
                    "files": [],
                }
                numpy_manifest = self.numpy_manifest(platform)
                numpy_manifest["producer"] = {
                    "repository": "precsim/python-numpy-fenics",
                    "revision": revision,
                }
                numpy_raw = self.json_bytes(numpy_manifest)
                numpy_manifest_sha = hashlib.sha256(numpy_raw).hexdigest()
                tree_identity = prerelease.numpy_tree_identity_from_manifest(
                    numpy_manifest,
                    context=f"fixture-{platform}",
                )
                fenics_numpy: dict[str, object] = {
                    "version": "2.5.2",
                    "base_tree_sha256": tree_identity["tree_sha256"],
                    "base_entry_count": tree_identity["entry_count"],
                }
                if platform != "linux-x64":
                    fenics_numpy["base_manifest_sha256"] = numpy_manifest_sha
                fenics_manifest = {
                    "format_version": 1,
                    "version": "0.1.0",
                    "platform": platform,
                    "profile": "fenics",
                    "python_abi": "cp314",
                    "producer": {
                        "repository": "precsim/python-numpy-fenics",
                        "revision": revision,
                    },
                    "numpy": fenics_numpy,
                    "files": [],
                }
                manifests[(platform, "core")] = core_manifest
                manifests[(platform, "numpy")] = numpy_manifest
                manifests[(platform, "fenics")] = fenics_manifest
                if platform == "linux-x64":
                    extras[(platform, "fenics")] = {
                        "fenics-runtime/provenance/source-provenance.json": self.json_bytes(
                            {"numpy_base": {"manifest_sha256": numpy_manifest_sha}}
                        )
                    }

            run_id = 1000
            for workflow, identities in prerelease.EXPECTED_WORKFLOW_COVERAGE.items():
                run_id += 1
                run_dir = incoming / f"run-{run_id}"
                run_dir.mkdir()
                (run_dir / "run-metadata.json").write_text(
                    json.dumps(
                        {
                            "databaseId": run_id,
                            "workflowName": workflow,
                            "headSha": revision,
                            "headBranch": "main",
                            "conclusion": "success",
                            "event": "push",
                        }
                    ),
                    encoding="utf-8",
                )
                for platform, profile in sorted(identities):
                    extension = ".tar.gz" if platform == "linux-x64" else ".zip"
                    stem = "fenics-embed-runtime" if profile == "fenics" else "python-runtime"
                    profile_suffix = "" if profile == "fenics" else f"-{profile}"
                    artifact_name = f"{stem}-v0.1.0-{platform}-cp314{profile_suffix}{extension}"
                    artifact = run_dir / artifact_name
                    self.write_artifact(
                        artifact,
                        manifests[(platform, profile)],
                        extra_members=extras.get((platform, profile)),
                    )
                    artifact_sha = prerelease.sha256_file(artifact)
                    (run_dir / f"{artifact_name}.sha256").write_text(
                        f"{artifact_sha}  {artifact_name}\n",
                        encoding="utf-8",
                    )
                    qualification = {
                        "format_version": 1,
                        "producer_repository": "precsim/python-numpy-fenics",
                        "producer_revision": revision,
                        "workflow_run_id": str(run_id),
                        "platform": platform,
                        "profile": profile,
                        "artifact": artifact_name,
                        "artifact_sha256": artifact_sha,
                        "checks": prerelease.EXPECTED_QUALIFICATION_CHECKS[(platform, profile)],
                    }
                    (run_dir / f"qualification-{platform}-{profile}.json").write_text(
                        json.dumps(qualification, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )

            core_numpy_incoming = root / "incoming-core-numpy"
            core_numpy_output = root / "release-assets-core-numpy"
            core_numpy_incoming.mkdir()
            for run_dir in incoming.glob("run-*"):
                metadata = json.loads(
                    (run_dir / "run-metadata.json").read_text(encoding="utf-8")
                )
                if metadata["workflowName"] in prerelease.CORE_NUMPY_WORKFLOWS:
                    shutil.copytree(run_dir, core_numpy_incoming / run_dir.name)
            default_argv = [
                "prepare_prerelease.py",
                "--input",
                str(core_numpy_incoming),
                "--output",
                str(core_numpy_output),
                "--tag",
                "runtime-v0.1.0-rc1",
            ]
            with mock.patch("sys.argv", default_argv):
                self.assertEqual(prerelease.main(), 0)
            default_manifest = json.loads(
                (core_numpy_output / "prerelease-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(default_manifest["profiles"], ["core", "numpy"])
            self.assertEqual(default_manifest["base_runtime_bindings"], [])
            self.assertEqual(len(default_manifest["qualifications"]), 8)

            argv = [
                "prepare_prerelease.py",
                "--input",
                str(incoming),
                "--output",
                str(output),
                "--tag",
                "runtime-v0.1.0-rc1",
                "--include-fenics",
            ]
            # An unrelated downloaded workflow may contain the same basename;
            # each qualification must resolve its own run's artifact.
            foreign_run = incoming / "unrelated-run"
            foreign_run.mkdir()
            first_artifact = next(incoming.glob("run-*/*.zip"))
            shutil.copy2(first_artifact, foreign_run / first_artifact.name)
            with mock.patch("sys.argv", argv):
                self.assertEqual(prerelease.main(), 0)

            release_manifest = json.loads(
                (output / "prerelease-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(release_manifest["profiles"], ["core", "numpy", "fenics"])
            self.assertEqual(len(release_manifest["base_runtime_bindings"]), 4)
            self.assertEqual(len(release_manifest["qualifications"]), 12)
            for binding in release_manifest["base_runtime_bindings"]:
                self.assertRegex(binding["numpy_artifact_sha256"], r"^[0-9a-f]{64}$")

    def test_default_prerelease_scope_excludes_fenics(self) -> None:
        self.assertEqual(
            prerelease.CORE_NUMPY_WORKFLOWS,
            {
                "qualify-windows-core-numpy",
                "qualify-macos-core-numpy",
                "qualify-linux-core-numpy",
            },
        )
        self.assertEqual(
            prerelease.FENICS_WORKFLOWS,
            {
                "qualify-windows-fenics",
                "qualify-macos-fenics",
                "qualify-linux-fenics",
            },
        )


if __name__ == "__main__":
    unittest.main()
