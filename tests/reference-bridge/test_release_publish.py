"""Fail-closed tests for reference-bridge release indexing and promotion policy."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "consumer-kit" / "tools" / "write-release-index.py"
WORKFLOW = ROOT / ".github" / "workflows" / "publish-reference-bridge.yml"
QUALIFY_WORKFLOW = ROOT / ".github" / "workflows" / "qualify-reference-bridge.yml"
SPEC = importlib.util.spec_from_file_location("write_release_index", TOOL)
assert SPEC is not None and SPEC.loader is not None
release_index = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_index)


SOURCE = "a" * 40
RUN_ID = "123456789"
RUN_ATTEMPT = "2"
VERSION = "v0.1.0-rc1"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qualification_record(target: str, profile: str, mex_sha256: str) -> dict[str, object]:
    return {
        "format_version": 1,
        "producer_repository": "precsim/python-numpy-fenics",
        "source_revision": SOURCE,
        "workflow_run_id": RUN_ID,
        "workflow_run_attempt": RUN_ATTEMPT,
        "platform": target,
        "profile": profile,
        "python_abi": "cp314",
        "mex_sha256": mex_sha256,
        "build_provenance_sha256": "b" * 64,
        "runtime_receipt_sha256": "c" * 64,
        "native_audit_sha256": "d" * 64,
        "checks": {
            "octave_host": "pass",
            "repeated_calls": "pass",
            "fresh_process": "pass",
            "relocated_runtime": "pass",
            "native_boundary": "pass",
        },
    }


def archive_name(target: str, profile: str) -> str:
    extension = "zip" if target == "win-x64" else "tar.gz"
    return f"reference-bridge-{target}-cp314-{profile}-{VERSION}.{extension}"


def write_archive(path: Path, manifest: dict[str, object]) -> None:
    package = str(manifest["package"])
    data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    member = f"{package}/reference-manifest.json"
    if path.name.endswith(".zip"):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member, data)
        return
    info = tarfile.TarInfo(member)
    info.size = len(data)
    info.mode = 0o644
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(info, io.BytesIO(data))


def read_archive_manifest(path: Path) -> dict[str, object]:
    package = path.name[: -len(".zip")] if path.name.endswith(".zip") else path.name[: -len(".tar.gz")]
    member = f"{package}/reference-manifest.json"
    if path.name.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            return json.loads(archive.read(member).decode("utf-8"))
    with tarfile.open(path, "r:gz") as archive:
        source = archive.extractfile(member)
        assert source is not None
        with source:
            return json.loads(source.read().decode("utf-8"))


def rewrite_qualification_checks(
    root: Path, target: str, profile: str, checks: dict[str, str]
) -> None:
    qualification_path = root / f"qualification-reference-{target}-{profile}.json"
    qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
    qualification["checks"] = checks
    qualification_path.write_text(
        json.dumps(qualification, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    name = archive_name(target, profile)
    archive = root / "packages" / name
    manifest = read_archive_manifest(archive)
    manifest["qualification"]["evidence_sha256"] = sha256_file(qualification_path)
    write_archive(archive, manifest)
    (root / "packages" / f"{name}.sha256").write_text(
        f"{sha256_file(archive)}  {name}\n", encoding="utf-8", newline="\n"
    )


def create_package(root: Path, target: str, profile: str, *, mex_sha256: str | None = None) -> None:
    packages = root / "packages"
    packages.mkdir(exist_ok=True)
    mex_sha256 = mex_sha256 or sha256_bytes(f"mex:{target}".encode("utf-8"))
    qualification_path = root / f"qualification-reference-{target}-{profile}.json"
    qualification = qualification_record(target, profile, mex_sha256)
    qualification_path.write_text(
        json.dumps(qualification, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    qualification_sha256 = sha256_file(qualification_path)
    name = archive_name(target, profile)
    archive = packages / name
    package = name[: -len(".zip")] if name.endswith(".zip") else name[: -len(".tar.gz")]
    manifest = {
        "package": package,
        "version": VERSION,
        "platform": target,
        "profile": profile,
        "python_abi": "cp314",
        "mock": {"source_revision": SOURCE},
        "qualification": {
            "run_identity": f"github-actions:{RUN_ID}",
            "evidence_path": "evidence/qualification-evidence.json",
            "evidence_sha256": qualification_sha256,
        },
        "mex": {"sha256": mex_sha256},
    }
    write_archive(archive, manifest)
    (packages / f"{name}.sha256").write_text(
        f"{sha256_file(archive)}  {name}\n", encoding="utf-8", newline="\n"
    )


def create_matrix(root: Path, profiles: tuple[str, ...]) -> None:
    for target in release_index.TARGETS:
        for profile in profiles:
            create_package(root, target, profile)


def build(root: Path) -> dict[str, object]:
    return release_index.build_release_index(
        root,
        source_revision=SOURCE,
        qualification_run_id=RUN_ID,
        qualification_run_attempt=RUN_ATTEMPT,
    )


class ReleaseIndexTests(unittest.TestCase):
    def test_accepts_only_complete_core_numpy_or_full_profile_matrices(self) -> None:
        for profiles in (("core", "numpy"), ("core", "numpy", "fenics")):
            with self.subTest(profiles=profiles), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                create_matrix(root, profiles)
                payload = build(root)
                self.assertEqual(payload["profiles"], list(profiles))
                self.assertEqual(payload["targets"], list(release_index.TARGETS))
                self.assertEqual(payload["version"], VERSION)
                self.assertEqual(payload["qualification_run_identity"], f"github-actions:{RUN_ID}")
                self.assertEqual(len(payload["packages"]), 4 * len(profiles))
                self.assertEqual(len(payload["files"]), 3 * 4 * len(profiles))

    def test_rejects_missing_partial_and_unexpected_release_assets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            create_matrix(root, ("core", "numpy"))
            (root / "packages" / archive_name("win-x64", "numpy")).unlink()
            with self.assertRaisesRegex(release_index.ReleaseIndexError, "exactly match|every target"):
                build(root)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            create_matrix(root, ("core", "numpy"))
            create_package(root, "linux-x64", "fenics")
            with self.assertRaisesRegex(release_index.ReleaseIndexError, "every target"):
                build(root)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            create_matrix(root, ("core", "numpy"))
            (root / "unexpected.txt").write_text("not releasable\n", encoding="utf-8")
            with self.assertRaisesRegex(release_index.ReleaseIndexError, "unexpected promotion file"):
                build(root)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            create_matrix(root, ("core", "numpy"))
            original = root / "packages" / archive_name("linux-x64", "core")
            duplicate = root / "packages" / original.name.replace(VERSION, "v0.1.0-rc2")
            duplicate.write_bytes(original.read_bytes())
            with self.assertRaisesRegex(release_index.ReleaseIndexError, "duplicate reference package"):
                build(root)

    def test_rejects_stale_or_unbound_qualification_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            create_matrix(root, ("core", "numpy"))
            path = root / "qualification-reference-linux-x64-core.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record["source_revision"] = "f" * 40
            path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(release_index.ReleaseIndexError, "source revision mismatch"):
                build(root)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            create_matrix(root, ("core", "numpy"))
            path = root / "qualification-reference-linux-x64-core.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record["workflow_run_attempt"] = "1"
            path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(release_index.ReleaseIndexError, "run attempt mismatch"):
                build(root)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            create_matrix(root, ("core", "numpy"))
            path = root / "qualification-reference-linux-x64-core.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record["checks"]["octave_host"] = "fail"
            path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(release_index.ReleaseIndexError, "must all pass"):
                build(root)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            create_matrix(root, ("core", "numpy"))
            path = root / "qualification-reference-linux-x64-core.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record["checks"]["extra"] = "pass"
            path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(release_index.ReleaseIndexError, "check contract mismatch"):
                build(root)

    def test_rejects_incomplete_or_nonpassing_qualification_checks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            create_matrix(root, ("core", "numpy"))
            rewrite_qualification_checks(root, "linux-x64", "core", {"native_boundary": "pass"})
            with self.assertRaisesRegex(release_index.ReleaseIndexError, "check contract mismatch"):
                build(root)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            create_matrix(root, ("core", "numpy"))
            checks = {name: "pass" for name in release_index.EXPECTED_QUALIFICATION_CHECKS}
            checks["octave_host"] = "not-applicable"
            rewrite_qualification_checks(root, "linux-x64", "core", checks)
            with self.assertRaisesRegex(release_index.ReleaseIndexError, "must all pass"):
                build(root)

    def test_rejects_different_mex_bytes_across_profiles_and_bad_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            create_matrix(root, ("core", "numpy"))
            for suffix in (
                archive_name("linux-x64", "numpy"),
                f"{archive_name('linux-x64', 'numpy')}.sha256",
            ):
                (root / "packages" / suffix).unlink()
            (root / "qualification-reference-linux-x64-numpy.json").unlink()
            create_package(root, "linux-x64", "numpy", mex_sha256="e" * 64)
            with self.assertRaisesRegex(release_index.ReleaseIndexError, "different MEX bytes"):
                build(root)

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            create_matrix(root, ("core", "numpy"))
            sidecar = root / "packages" / f"{archive_name('linux-x64', 'core')}.sha256"
            sidecar.write_text(f"{'0' * 64}  {archive_name('linux-x64', 'core')}\n", encoding="utf-8")
            with self.assertRaisesRegex(release_index.ReleaseIndexError, "package SHA-256 mismatch"):
                build(root)

    def test_publish_workflow_uses_trusted_run_exact_index_and_draft_promotion(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('workflow_path="$(jq -r \'.path\'', workflow)
        self.assertIn('event" != "push"', workflow)
        self.assertIn('head_branch" != "$default_branch"', workflow)
        self.assertIn('head_repository" != "$GITHUB_REPOSITORY"', workflow)
        self.assertIn("Validate qualified package artifact matrix", workflow)
        self.assertIn("for profile in core numpy fenics", workflow)
        self.assertNotIn("qualified package artifact matrix: core+numpy\"", workflow)
        self.assertIn("--qualification-run-attempt", workflow)
        self.assertIn(".files[].path", workflow)
        self.assertIn("--verify-tag --draft --prerelease", workflow)
        self.assertIn("gh release upload", workflow)
        self.assertIn("(.digest // \"\")", workflow)
        self.assertIn("stat -c '%s'", workflow)
        self.assertIn('sha256sum "$asset"', workflow)
        self.assertIn('diff -u "$expected_uploaded" "$actual_uploaded"', workflow)
        self.assertIn("-F draft=false", workflow)
        self.assertNotIn('"${#assets[@]}" -lt 19', workflow)

        qualify = QUALIFY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("name: qualify-reference-bridge", qualify)
        self.assertIn("reference_release_set=non-promotable-core+numpy", qualify)
        self.assertIn("full publication requires FEniCS qualification on every target", qualify)
        self.assertIn('if [[ "$PROFILE_SET" != "core+numpy+fenics" ]]', qualify)
        self.assertIn("profiles=(core numpy fenics)", qualify)
        self.assertNotIn("core+numpy|core+numpy+fenics)", qualify)


if __name__ == "__main__":
    unittest.main()
