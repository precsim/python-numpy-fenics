"""Consumer-kit staging and offline runtime verification regression tests."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "consumer-kit" / "tools"
sys.path.insert(0, str(TOOLS))

import runtime_lib as runtime  # noqa: E402


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def file_record(path: str, content: bytes) -> dict[str, object]:
    return {"path": path, "bytes": len(content), "sha256": digest(content)}


def write_zip(path: Path, members: list[tuple[str, bytes]]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members:
            archive.writestr(name, content)
    return path.read_bytes()


def write_tar_with_omitted_symlink(
    path: Path,
    *,
    manifest_name: str,
    manifest_bytes: bytes,
    payload_path: str,
    payload: bytes,
    symlink_path: str,
    symlink_target: str,
) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for name, content, mode in (
            (manifest_name, manifest_bytes, 0o644),
            (payload_path, payload, 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            archive.addfile(info, io.BytesIO(content))
        link = tarfile.TarInfo(symlink_path)
        link.type = tarfile.SYMTYPE
        link.linkname = symlink_target
        link.mode = 0o777
        archive.addfile(link)
    return path.read_bytes()


def make_casefold_verification_inputs(
    root: Path,
    *,
    platform: str,
    relative: str = "include/ufcx.h",
    content: bytes = b"qualified ufcx header\n",
) -> tuple[runtime.RuntimeInputs, runtime.ArchiveFacts]:
    manifest = {"files": [file_record(relative, content)]}
    manifest_bytes = json_bytes(manifest)
    pin = runtime.LayerPin(
        profile="core",
        producer_revision="a" * 40,
        artifact=runtime.AssetPin("runtime-v0.1.0-rc2", "runtime.zip", "c" * 64),
        manifest=runtime.AssetPin(
            "runtime-v0.1.0-rc2", "runtime-manifest.json", digest(manifest_bytes)
        ),
        qualification=runtime.AssetPin(
            "runtime-v0.1.0-rc2", f"qualification-{platform}-core.json", "b" * 64
        ),
        manifest_external=False,
    )
    material = runtime.LayerMaterial(
        pin=pin,
        archive_path=root / "runtime.zip",
        manifest_path=None,
        qualification_path=root / "qualification.json",
        manifest_bytes=manifest_bytes,
        manifest=manifest,
        qualification={},
    )
    plan = runtime.RuntimePlan(
        repository=runtime.PRODUCER_REPOSITORY,
        release_tag=pin.artifact.tag,
        release_revision=pin.producer_revision,
        version="0.1.0",
        platform=platform,
        profile="core",
        release_manifest=None,
        layers=(pin,),
        base_binding=None,
    )
    receipt = root / "receipt.json"
    receipt.write_text("{}\n", encoding="utf-8")
    facts = runtime.ArchiveFacts(
        modes={relative: 0o644},
        directories={str(PurePosixPath(relative).parent): None},
        symlinks={},
    )
    return runtime.RuntimeInputs(receipt, plan, None, None, (material,)), facts


class RuntimeFixture:
    platform = "win-x64"
    rc1_tag = "runtime-v0.1.0-rc1"
    rc2_tag = "runtime-v0.1.0-rc2"
    rc1_revision = "b" * 40
    rc2_revision = "a" * 40

    def __init__(
        self,
        root: Path,
        *,
        wrong_base_binding: bool = False,
        bad_overlay_payload: bool = False,
        overlay_collision: bool = False,
        traversal_member: bool = False,
    ) -> None:
        self.root = root
        self.cache = root / "cache"
        self.receipt = root / "receipt.json"

        base_files = {
            "python.exe": b"qualified rc1 python",
            "Lib/site-packages/numpy/__init__.py": b"rc1 numpy\n",
            "Lib/site-packages/numpy-2.5.2.dist-info/METADATA": b"Version: 2.5.2\n",
        }
        base_manifest = self.runtime_manifest("numpy", self.rc1_revision, base_files)
        base_tree_sha, base_entry_count = runtime.numpy_tree_identity(base_manifest, self.platform)
        base_manifest_bytes = json_bytes(base_manifest)
        base_name = "python-runtime-v0.1.0-win-x64-cp314-numpy.zip"
        base_archive = self.cache_path(self.rc1_tag, base_name)
        base_archive_bytes = write_zip(
            base_archive,
            [("runtime-manifest.json", base_manifest_bytes), *base_files.items()],
        )
        self.base_python = base_files["python.exe"]
        base_qualification_name = "qualification-win-x64-numpy.json"
        base_qualification = self.qualification(
            "numpy", self.rc1_revision, base_name, digest(base_archive_bytes)
        )
        base_qualification_bytes = json_bytes(base_qualification)
        self.write_cache(self.rc1_tag, base_name + ".manifest.json", base_manifest_bytes)
        self.write_cache(self.rc1_tag, base_qualification_name, base_qualification_bytes)

        rc2_numpy_files = {
            "python.exe": b"qualified rc2 python",
            "Lib/site-packages/numpy/__init__.py": b"rc2 numpy\n",
            "Lib/site-packages/numpy-2.5.2.dist-info/METADATA": b"Version: 2.5.2\n",
        }
        rc2_numpy_manifest = self.runtime_manifest("numpy", self.rc2_revision, rc2_numpy_files)
        rc2_numpy_manifest_bytes = json_bytes(rc2_numpy_manifest)
        rc2_numpy_name = "python-runtime-v0.1.0-win-x64-cp314-numpy-rc2.zip"
        rc2_numpy_archive = self.cache_path(self.rc2_tag, rc2_numpy_name)
        rc2_numpy_archive_bytes = write_zip(
            rc2_numpy_archive,
            [("runtime-manifest.json", rc2_numpy_manifest_bytes), *rc2_numpy_files.items()],
        )
        self.rc2_python = rc2_numpy_files["python.exe"]
        rc2_numpy_qualification_name = "qualification-win-x64-numpy.json"
        rc2_numpy_qualification = self.qualification(
            "numpy", self.rc2_revision, rc2_numpy_name, digest(rc2_numpy_archive_bytes)
        )
        rc2_numpy_qualification_bytes = json_bytes(rc2_numpy_qualification)
        rc2_numpy_manifest_name = rc2_numpy_name + ".manifest.json"
        self.write_cache(self.rc2_tag, rc2_numpy_manifest_name, rc2_numpy_manifest_bytes)
        self.write_cache(self.rc2_tag, rc2_numpy_qualification_name, rc2_numpy_qualification_bytes)

        overlay_path = "python.exe" if overlay_collision else "Lib/site-packages/fenics_embed_runtime.py"
        expected_overlay_payload = b"qualified fenics overlay\n"
        actual_overlay_payload = b"tampered fenics overlay\n" if bad_overlay_payload else expected_overlay_payload
        overlay_manifest = {
            "format_version": 1,
            "artifact": "fenics-embed-runtime-win-x64-cp314",
            "version": "0.1.0",
            "platform": self.platform,
            "profile": "fenics",
            "python_abi": "cp314",
            "producer": {"repository": runtime.PRODUCER_REPOSITORY, "revision": self.rc2_revision},
            "tcl_tk": self.tcl_tk(),
            "numpy": {
                "version": "2.5.2",
                "base_release_tag": self.rc1_tag,
                "base_producer_revision": self.rc1_revision,
                "base_archive_sha256": digest(base_archive_bytes),
                "base_qualification_sha256": digest(base_qualification_bytes),
                "base_manifest_sha256": digest(base_manifest_bytes),
                "base_tree_sha256": base_tree_sha,
                "base_entry_count": base_entry_count,
            },
            "files": [file_record(overlay_path, expected_overlay_payload)],
        }
        overlay_manifest_bytes = json_bytes(overlay_manifest)
        overlay_name = "fenics-embed-runtime-v0.1.0-win-x64-cp314.zip"
        overlay_archive = self.cache_path(self.rc2_tag, overlay_name)
        overlay_members = [
            ("fenics-embed-manifest.json", overlay_manifest_bytes),
            (overlay_path, actual_overlay_payload),
        ]
        if traversal_member:
            overlay_members.append(("../escape.txt", b"escape"))
        overlay_archive_bytes = write_zip(overlay_archive, overlay_members)
        self.overlay_path = overlay_path
        self.overlay_payload = expected_overlay_payload
        overlay_qualification_name = "qualification-win-x64-fenics.json"
        overlay_qualification = self.qualification(
            "fenics", self.rc2_revision, overlay_name, digest(overlay_archive_bytes)
        )
        overlay_qualification_bytes = json_bytes(overlay_qualification)
        overlay_manifest_name = overlay_name + ".manifest.json"
        self.write_cache(self.rc2_tag, overlay_manifest_name, overlay_manifest_bytes)
        self.write_cache(self.rc2_tag, overlay_qualification_name, overlay_qualification_bytes)

        release_files = {
            rc2_numpy_name: digest(rc2_numpy_archive_bytes),
            rc2_numpy_manifest_name: digest(rc2_numpy_manifest_bytes),
            rc2_numpy_qualification_name: digest(rc2_numpy_qualification_bytes),
            overlay_name: digest(overlay_archive_bytes),
            overlay_manifest_name: digest(overlay_manifest_bytes),
            overlay_qualification_name: digest(overlay_qualification_bytes),
        }
        binding_tag = self.rc2_tag if wrong_base_binding else self.rc1_tag
        release = {
            "format_version": 1,
            "tag": self.rc2_tag,
            "version": "0.1.0",
            "producer_repository": runtime.PRODUCER_REPOSITORY,
            "producer_revision": self.rc2_revision,
            "profiles": ["numpy", "fenics"],
            "qualifications": [
                {
                    "platform": self.platform,
                    "profile": "numpy",
                    "artifact": rc2_numpy_name,
                    "artifact_sha256": digest(rc2_numpy_archive_bytes),
                    "manifest": rc2_numpy_manifest_name,
                    "manifest_sha256": digest(rc2_numpy_manifest_bytes),
                    "producer_revision": self.rc2_revision,
                    "workflow_run_id": "200",
                    "workflow": "qualify-windows-core-numpy",
                },
                {
                    "platform": self.platform,
                    "profile": "fenics",
                    "artifact": overlay_name,
                    "artifact_sha256": digest(overlay_archive_bytes),
                    "manifest": overlay_manifest_name,
                    "manifest_sha256": digest(overlay_manifest_bytes),
                    "producer_revision": self.rc2_revision,
                    "workflow_run_id": "201",
                    "workflow": "qualify-windows-fenics",
                },
            ],
            "base_runtime_bindings": [
                {
                    "platform": self.platform,
                    "fenics_artifact": overlay_name,
                    "fenics_artifact_sha256": digest(overlay_archive_bytes),
                    "numpy_artifact": base_name,
                    "numpy_artifact_sha256": digest(base_archive_bytes),
                    "numpy_manifest_sha256": digest(base_manifest_bytes),
                    "numpy_tree_sha256": base_tree_sha,
                    "numpy_entry_count": base_entry_count,
                    "numpy_base_release_tag": binding_tag,
                    "numpy_base_producer_revision": self.rc1_revision,
                }
            ],
            "files": [
                {"name": name, "sha256": sha}
                for name, sha in sorted(release_files.items())
            ],
        }
        self.release = release
        release_bytes = json_bytes(release)
        self.write_cache(self.rc2_tag, "prerelease-manifest.json", release_bytes)

        receipt = {
            "format_version": 1,
            "repository": runtime.PRODUCER_REPOSITORY,
            "tag": self.rc2_tag,
            "producer_revision": self.rc2_revision,
            "prerelease_manifest_sha256": digest(release_bytes),
            "platforms": {
                self.platform: {
                    "fenics": {
                        "name": overlay_name,
                        "sha256": digest(overlay_archive_bytes),
                        "manifest_sha256": digest(overlay_manifest_bytes),
                    },
                    "numpy_base": {
                        "name": base_name,
                        "sha256": digest(base_archive_bytes),
                        "manifest_sha256": digest(base_manifest_bytes),
                        "tag": self.rc1_tag,
                        "producer_revision": self.rc1_revision,
                        "numpy_tree_sha256": base_tree_sha,
                        "numpy_entry_count": base_entry_count,
                        "qualification_name": base_qualification_name,
                        "qualification_sha256": digest(base_qualification_bytes),
                    },
                }
            },
        }
        self.legacy_receipt = receipt
        self.receipt.write_bytes(json_bytes(receipt))

    @staticmethod
    def tcl_tk() -> dict[str, object]:
        return {
            "version": "8.6.18",
            "ownership": "producer",
            "sources": {},
            "providers": {},
        }

    def runtime_manifest(
        self, profile: str, revision: str, files: dict[str, bytes]
    ) -> dict[str, object]:
        return {
            "format_version": 1,
            "artifact": f"python-runtime-win-x64-cp314-{profile}",
            "version": "0.1.0",
            "platform": self.platform,
            "profile": profile,
            "python_abi": "cp314",
            "producer": {"repository": runtime.PRODUCER_REPOSITORY, "revision": revision},
            "tcl_tk": self.tcl_tk(),
            "files": [file_record(path, content) for path, content in files.items()],
        }

    def qualification(
        self, profile: str, revision: str, artifact: str, artifact_sha256: str
    ) -> dict[str, object]:
        return {
            "format_version": 1,
            "producer_repository": runtime.PRODUCER_REPOSITORY,
            "producer_revision": revision,
            "workflow_run_id": "12345",
            "platform": self.platform,
            "profile": profile,
            "artifact": artifact,
            "artifact_sha256": artifact_sha256,
            "checks": {"relocation": "pass", "private_consumer_checkout": "not-applicable"},
        }

    def cache_path(self, tag: str, name: str) -> Path:
        path = runtime.cache_asset_path(self.cache, tag, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_cache(self, tag: str, name: str, content: bytes) -> Path:
        path = self.cache_path(tag, name)
        path.write_bytes(content)
        return path

    def write_contract_receipt(self, profiles: tuple[str, ...] = ("numpy", "fenics")) -> Path:
        files = {item["name"]: item["sha256"] for item in self.release["files"]}

        def archive_record(profile: str) -> dict[str, object]:
            summary = next(
                item
                for item in self.release["qualifications"]
                if item["platform"] == self.platform and item["profile"] == profile
            )
            qualification_name = f"qualification-{self.platform}-{profile}.json"
            return {
                "name": summary["artifact"],
                "sha256": summary["artifact_sha256"],
                "manifest_name": "fenics-embed-manifest.json" if profile == "fenics" else "runtime-manifest.json",
                "manifest_sha256": summary["manifest_sha256"],
                "qualification_name": qualification_name,
                "qualification_sha256": files[qualification_name],
            }

        records: dict[str, object] = {}
        if "numpy" in profiles:
            records["numpy"] = {
                "release_tag": self.rc2_tag,
                "producer_revision": self.rc2_revision,
                "archive": archive_record("numpy"),
            }
        if "fenics" in profiles:
            legacy_base = self.legacy_receipt["platforms"][self.platform]["numpy_base"]
            overlay = {
                "release_tag": self.rc2_tag,
                "producer_revision": self.rc2_revision,
                "archive": archive_record("fenics"),
            }
            base = {
                "release_tag": legacy_base["tag"],
                "producer_revision": legacy_base["producer_revision"],
                "archive": {
                    "name": legacy_base["name"],
                    "sha256": legacy_base["sha256"],
                    "manifest_name": "runtime-manifest.json",
                    "manifest_sha256": legacy_base["manifest_sha256"],
                    "qualification_name": legacy_base["qualification_name"],
                    "qualification_sha256": legacy_base["qualification_sha256"],
                },
            }
            records["fenics"] = {
                **overlay,
                "base": base,
                "overlay": overlay,
            }
        receipt = {
            "format_version": 1,
            "producer_repository": runtime.PRODUCER_REPOSITORY,
            "profiles": {self.platform: records},
        }
        path = self.root / "contract-receipt.json"
        path.write_bytes(json_bytes(receipt))
        return path


class RuntimeStagingTests(unittest.TestCase):
    def prepare(self, fixture: RuntimeFixture, profile: str) -> runtime.RuntimeInputs:
        return runtime.prepare_runtime_inputs(
            fixture.receipt,
            platform=fixture.platform,
            profile=profile,
            cache=fixture.cache,
            offline=True,
        )

    def test_rc2_numpy_and_fenics_use_distinct_pinned_numpy_bases(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RuntimeFixture(Path(raw))
            rc2_numpy = Path(raw) / "rc2 numpy"
            numpy_result = runtime.stage_runtime(self.prepare(fixture, "numpy"), rc2_numpy)
            self.assertEqual((rc2_numpy / "python.exe").read_bytes(), fixture.rc2_python)
            self.assertEqual(numpy_result["layers"][0]["tag"], fixture.rc2_tag)

            fenics = Path(raw) / "fenics runtime"
            fenics_result = runtime.stage_runtime(self.prepare(fixture, "fenics"), fenics)
            self.assertEqual((fenics / "python.exe").read_bytes(), fixture.base_python)
            self.assertEqual(
                (fenics / "Lib/site-packages/fenics_embed_runtime.py").read_bytes(),
                fixture.overlay_payload,
            )
            self.assertEqual([layer["tag"] for layer in fenics_result["layers"]], [fixture.rc1_tag, fixture.rc2_tag])

    def test_v1_contract_receipt_stages_without_release_manifest_or_external_manifest_assets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RuntimeFixture(Path(raw))
            receipt = fixture.write_contract_receipt()
            runtime.cache_asset_path(fixture.cache, fixture.rc2_tag, "prerelease-manifest.json").unlink()
            for tag, name in (
                (fixture.rc1_tag, "python-runtime-v0.1.0-win-x64-cp314-numpy.zip.manifest.json"),
                (fixture.rc2_tag, "python-runtime-v0.1.0-win-x64-cp314-numpy-rc2.zip.manifest.json"),
                (fixture.rc2_tag, "fenics-embed-runtime-v0.1.0-win-x64-cp314.zip.manifest.json"),
            ):
                runtime.cache_asset_path(fixture.cache, tag, name).unlink()

            inputs = runtime.prepare_runtime_inputs(
                receipt,
                platform=fixture.platform,
                profile="fenics",
                cache=fixture.cache,
                offline=True,
            )
            output = Path(raw) / "contract runtime"
            result = runtime.stage_runtime(inputs, output)
            self.assertEqual((output / "python.exe").read_bytes(), fixture.base_python)
            self.assertEqual([layer["tag"] for layer in result["layers"]], [fixture.rc1_tag, fixture.rc2_tag])

    def test_v1_contract_receipt_rejects_rc2_numpy_substitution_for_fenics(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RuntimeFixture(Path(raw))
            receipt_path = fixture.write_contract_receipt()
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            records = receipt["profiles"][fixture.platform]
            records["fenics"]["base"] = copy.deepcopy(records["numpy"])
            receipt_path.write_bytes(json_bytes(receipt))

            with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "pinned NumPy base identity"):
                runtime.prepare_runtime_inputs(
                    receipt_path,
                    platform=fixture.platform,
                    profile="fenics",
                    cache=fixture.cache,
                    offline=True,
                )

    def test_rejects_rc2_substitution_for_fenics_rc1_base(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RuntimeFixture(Path(raw), wrong_base_binding=True)
            with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "pinned prior release"):
                self.prepare(fixture, "fenics")

    def test_full_tree_verification_is_transactional(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RuntimeFixture(Path(raw), bad_overlay_payload=True)
            output = Path(raw) / "runtime"
            inputs = self.prepare(fixture, "fenics")
            with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "differs from producer manifest"):
                runtime.stage_runtime(inputs, output)
            self.assertFalse(output.exists())

    def test_offline_verifier_rejects_tamper_and_extra_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RuntimeFixture(Path(raw))
            inputs = self.prepare(fixture, "fenics")
            output = Path(raw) / "runtime"
            runtime.stage_runtime(inputs, output)

            python = output / "python.exe"
            python.write_bytes(b"tampered")
            with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "differs from producer manifest"):
                runtime.verify_staged_runtime(self.prepare(fixture, "fenics"), output)

            python.write_bytes(fixture.base_python)
            (output / "unexpected.txt").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "inventory differs"):
                runtime.verify_staged_runtime(self.prepare(fixture, "fenics"), output)

    def test_offline_verifier_rejects_added_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RuntimeFixture(Path(raw))
            inputs = self.prepare(fixture, "fenics")
            output = Path(raw) / "runtime"
            runtime.stage_runtime(inputs, output)
            (output / "unexpected-empty-directory").mkdir()
            with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "directory tree differs"):
                runtime.verify_staged_runtime(self.prepare(fixture, "fenics"), output)

    def test_windows_verifier_casefolds_unique_leaf_and_directory_spelling_but_checks_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs, facts = make_casefold_verification_inputs(root, platform="win-x64")
            output = root / "runtime"
            (output / "Include").mkdir(parents=True)
            header = output / "Include" / "ufcx.h"
            header.write_bytes(b"qualified ufcx header\n")
            (output / "runtime-manifest.json").write_bytes(inputs.layers[0].manifest_bytes)

            result = runtime.verify_staged_runtime(
                inputs,
                output,
                archive_metadata=(facts,),
            )
            self.assertEqual(result["entry_count"], 2)
            self.assertEqual(result["directory_count"], 1)

            header.write_bytes(b"tampered ufcx header!\n")
            with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "differs from producer manifest"):
                runtime.verify_staged_runtime(inputs, output, archive_metadata=(facts,))

            header.write_bytes(b"qualified ufcx header\n")
            with mock.patch.object(
                runtime,
                "_walk_runtime",
                return_value={
                    "Include": "dir",
                    "Include/ufcx.h": "symlink",
                    "runtime-manifest.json": "file",
                },
            ):
                with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "entry kind mismatch"):
                    runtime.verify_staged_runtime(inputs, output, archive_metadata=(facts,))

    def test_windows_casefold_leaf_matching_rejects_expected_and_actual_collisions(self) -> None:
        record = {
            "path": "include/ufcx.h",
            "kind": "file",
            "bytes": 1,
            "sha256": digest(b"x"),
        }
        with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "casefold collision"):
            runtime._match_runtime_leaves(
                {
                    "include/ufcx.h": (record, 0),
                    "Include/UFCX.H": ({**record, "path": "Include/UFCX.H"}, 0),
                },
                {"Include/ufcx.h": "file"},
                windows_casefold=True,
            )

        with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "casefold collision"):
            runtime._match_runtime_leaves(
                {"include/ufcx.h": (record, 0)},
                {
                    "include/ufcx.h": "file",
                    "Include/UFCX.H": "file",
                },
                windows_casefold=True,
            )

    def test_posix_verifier_remains_case_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs, facts = make_casefold_verification_inputs(root, platform="linux-x64")
            output = root / "runtime"
            (output / "Include").mkdir(parents=True)
            (output / "Include" / "ufcx.h").write_bytes(b"qualified ufcx header\n")
            (output / "runtime-manifest.json").write_bytes(inputs.layers[0].manifest_bytes)

            with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "inventory differs"):
                runtime.verify_staged_runtime(inputs, output, archive_metadata=(facts,))

    def test_offline_missing_asset_does_not_attempt_download(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RuntimeFixture(Path(raw))
            missing = runtime.cache_asset_path(
                fixture.cache, fixture.rc1_tag, "qualification-win-x64-numpy.json"
            )
            missing.unlink()
            with mock.patch.object(runtime.subprocess, "run") as run:
                with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "offline cache is missing"):
                    self.prepare(fixture, "fenics")
            run.assert_not_called()

    def test_layer_collision_fails_without_publishing_partial_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RuntimeFixture(Path(raw), overlay_collision=True)
            output = Path(raw) / "runtime"
            with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "overwritten runtime entry"):
                runtime.stage_runtime(self.prepare(fixture, "fenics"), output)
            self.assertFalse(output.exists())

    def test_archive_traversal_is_rejected_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RuntimeFixture(Path(raw), traversal_member=True)
            output = Path(raw) / "runtime"
            with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "unsafe archive path"):
                runtime.stage_runtime(self.prepare(fixture, "fenics"), output)
            self.assertFalse(output.exists())
            self.assertFalse((Path(raw) / "escape.txt").exists())

    def test_stage_cli_writes_deterministic_selected_layer_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            fixture = RuntimeFixture(Path(raw))
            receipt = fixture.write_contract_receipt()
            output = Path(raw) / "runtime with spaces"
            record = Path(raw) / "evidence" / "runtime.json"
            command = [
                sys.executable,
                str(TOOLS / "stage-runtime.py"),
                "--receipt",
                str(receipt),
                "--platform",
                fixture.platform,
                "--profile",
                "fenics",
                "--cache",
                str(fixture.cache),
                "--output",
                str(output),
                "--record",
                str(record),
                "--offline",
            ]
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            stdout_record = json.loads(completed.stdout)
            disk_record = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(stdout_record, disk_record)
            self.assertEqual(disk_record["platform"], fixture.platform)
            self.assertEqual(disk_record["profile"], "fenics")
            self.assertEqual(disk_record["archive"]["name"], "fenics-embed-runtime-v0.1.0-win-x64-cp314.zip")
            self.assertEqual(disk_record["manifest"]["name"], "fenics-embed-manifest.json")
            self.assertEqual(disk_record["qualification"]["name"], "qualification-win-x64-fenics.json")
            expected_bytes = (json.dumps(disk_record, indent=2, sort_keys=True) + "\n").encode("utf-8")
            self.assertEqual(record.read_bytes(), expected_bytes)

            verify_record = Path(raw) / "evidence" / "verified-runtime.json"
            verify_command = [
                sys.executable,
                str(TOOLS / "verify-runtime.py"),
                "--receipt",
                str(receipt),
                "--platform",
                fixture.platform,
                "--profile",
                "fenics",
                "--cache",
                str(fixture.cache),
                "--runtime",
                str(output),
                "--record",
                str(verify_record),
            ]
            verified = subprocess.run(verify_command, text=True, capture_output=True, check=False)
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            verify_payload = json.loads(verify_record.read_text(encoding="utf-8"))
            self.assertEqual(verify_payload["archive"], disk_record["archive"])
            self.assertEqual(verify_payload["manifest"], disk_record["manifest"])
            self.assertEqual(verify_payload["qualification"], disk_record["qualification"])
            self.assertNotIn("runtime", verify_payload)

    def test_pinned_archive_symlink_omitted_from_manifest_is_attested_from_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload_path = "fenics-runtime/conda/lib/icu/78.3/data.bin"
            payload = b"icu payload"
            symlink_path = "fenics-runtime/conda/lib/icu/current"
            symlink_target = "78.3"
            manifest = {"files": [file_record(payload_path, payload)]}
            manifest_bytes = json_bytes(manifest)
            archive_path = root / "overlay.tar.gz"
            archive_bytes = write_tar_with_omitted_symlink(
                archive_path,
                manifest_name="runtime-manifest.json",
                manifest_bytes=manifest_bytes,
                payload_path=payload_path,
                payload=payload,
                symlink_path=symlink_path,
                symlink_target=symlink_target,
            )
            pin = runtime.LayerPin(
                profile="core",
                producer_revision="a" * 40,
                artifact=runtime.AssetPin("runtime-v0.1.0-rc2", archive_path.name, digest(archive_bytes)),
                manifest=runtime.AssetPin("runtime-v0.1.0-rc2", "runtime-manifest.json", digest(manifest_bytes)),
                qualification=runtime.AssetPin(
                    "runtime-v0.1.0-rc2", "qualification-linux-x64-core.json", "b" * 64
                ),
                manifest_external=False,
            )
            material = runtime.LayerMaterial(
                pin=pin,
                archive_path=archive_path,
                manifest_path=None,
                qualification_path=root / "qualification.json",
                manifest_bytes=manifest_bytes,
                manifest=manifest,
                qualification={},
            )
            plan = runtime.RuntimePlan(
                repository=runtime.PRODUCER_REPOSITORY,
                release_tag=pin.artifact.tag,
                release_revision=pin.producer_revision,
                version="0.1.0",
                platform="linux-x64",
                profile="core",
                release_manifest=None,
                layers=(pin,),
                base_binding=None,
            )
            receipt = root / "receipt.json"
            receipt.write_text("{}\n", encoding="utf-8")
            inputs = runtime.RuntimeInputs(receipt, plan, None, None, (material,))

            facts = runtime.archive_facts(archive_path)
            self.assertEqual(facts.symlinks[symlink_path], symlink_target)
            expected = runtime._expected_runtime_entries(inputs, (facts,))
            self.assertEqual(expected[symlink_path][0]["kind"], "symlink")
            self.assertEqual(expected[symlink_path][0]["target"], symlink_target)
            self.assertNotIn(symlink_path, runtime._manifest_entries(material))

            conflicting_manifest = {
                "files": [
                    file_record(payload_path, payload),
                    {"path": symlink_path, "kind": "symlink", "target": "79.0"},
                ]
            }
            conflicting_material = runtime.LayerMaterial(
                pin=pin,
                archive_path=archive_path,
                manifest_path=None,
                qualification_path=root / "qualification.json",
                manifest_bytes=json_bytes(conflicting_manifest),
                manifest=conflicting_manifest,
                qualification={},
            )
            conflicting_inputs = runtime.RuntimeInputs(
                receipt,
                plan,
                None,
                None,
                (conflicting_material,),
            )
            with self.assertRaisesRegex(
                runtime.ConsumerRuntimeError, "manifest/archive symlink target mismatch"
            ):
                runtime._expected_runtime_entries(conflicting_inputs, (facts,))

    @unittest.skipIf(os.name == "nt", "POSIX symlink staging regression")
    def test_manifest_omitted_archive_symlink_stages_and_target_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload_path = "fenics-runtime/conda/lib/icu/78.3/data.bin"
            payload = b"icu payload"
            symlink_path = "fenics-runtime/conda/lib/icu/current"
            symlink_target = "78.3"
            manifest = {"files": [file_record(payload_path, payload)]}
            manifest_bytes = json_bytes(manifest)
            archive_path = root / "overlay.tar.gz"
            archive_bytes = write_tar_with_omitted_symlink(
                archive_path,
                manifest_name="runtime-manifest.json",
                manifest_bytes=manifest_bytes,
                payload_path=payload_path,
                payload=payload,
                symlink_path=symlink_path,
                symlink_target=symlink_target,
            )
            pin = runtime.LayerPin(
                profile="core",
                producer_revision="a" * 40,
                artifact=runtime.AssetPin("runtime-v0.1.0-rc2", archive_path.name, digest(archive_bytes)),
                manifest=runtime.AssetPin("runtime-v0.1.0-rc2", "runtime-manifest.json", digest(manifest_bytes)),
                qualification=runtime.AssetPin(
                    "runtime-v0.1.0-rc2", "qualification-linux-x64-core.json", "b" * 64
                ),
                manifest_external=False,
            )
            material = runtime.LayerMaterial(
                pin=pin,
                archive_path=archive_path,
                manifest_path=None,
                qualification_path=root / "qualification.json",
                manifest_bytes=manifest_bytes,
                manifest=manifest,
                qualification={},
            )
            plan = runtime.RuntimePlan(
                repository=runtime.PRODUCER_REPOSITORY,
                release_tag=pin.artifact.tag,
                release_revision=pin.producer_revision,
                version="0.1.0",
                platform="linux-x64",
                profile="core",
                release_manifest=None,
                layers=(pin,),
                base_binding=None,
            )
            receipt = root / "receipt.json"
            receipt.write_text("{}\n", encoding="utf-8")
            inputs = runtime.RuntimeInputs(receipt, plan, None, None, (material,))
            output = root / "runtime"

            result = runtime.stage_runtime(inputs, output)
            link = output.joinpath(*symlink_path.split("/"))
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), symlink_target)
            self.assertEqual(result["entry_count"], 3)

            link.unlink()
            link.symlink_to("79.0")
            with self.assertRaisesRegex(runtime.ConsumerRuntimeError, "symlink target mismatch"):
                runtime.verify_staged_runtime(inputs, output)

    def test_macos_overlay_manifest_size_mode_and_hashed_symlink_are_normalized(self) -> None:
        target = "../lib/libexample.dylib"
        file_content = b"binary"
        manifest = {
            "files": [
                {
                    "path": "fenics-runtime/conda/bin/tool",
                    "kind": "file",
                    "size": len(file_content),
                    "sha256": digest(file_content),
                    "mode": "0o755",
                },
                {
                    "path": "fenics-runtime/conda/bin/tool-link",
                    "kind": "symlink",
                    "size": len(target.encode("utf-8")),
                    "sha256": digest(target.encode("utf-8")),
                    "mode": "0o755",
                },
            ]
        }
        manifest_bytes = json_bytes(manifest)
        pin = runtime.LayerPin(
            profile="fenics",
            producer_revision="a" * 40,
            artifact=runtime.AssetPin("runtime-v0.1.0-rc2", "overlay.zip", "a" * 64),
            manifest=runtime.AssetPin(
                "runtime-v0.1.0-rc2", "fenics-embed-manifest.json", digest(manifest_bytes)
            ),
            qualification=runtime.AssetPin(
                "runtime-v0.1.0-rc2", "qualification-macos-arm64-fenics.json", "b" * 64
            ),
            manifest_external=False,
        )
        material = runtime.LayerMaterial(
            pin=pin,
            archive_path=Path("overlay.zip"),
            manifest_path=None,
            qualification_path=Path("qualification.json"),
            manifest_bytes=manifest_bytes,
            manifest=manifest,
            qualification={},
        )
        entries = runtime._manifest_entries(material)
        self.assertEqual(entries["fenics-runtime/conda/bin/tool"]["bytes"], len(file_content))
        self.assertEqual(entries["fenics-runtime/conda/bin/tool"]["mode"], 0o755)
        self.assertEqual(
            entries["fenics-runtime/conda/bin/tool-link"]["target_sha256"],
            digest(target.encode("utf-8")),
        )


if __name__ == "__main__":
    unittest.main()
