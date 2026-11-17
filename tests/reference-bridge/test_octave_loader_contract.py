"""Focused contract checks for GNU Octave 10.2+ MEX loader metadata."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
MOCK_BRIDGE = ROOT / "reference-bridge" / "src" / "mock_bridge.c"
QUALIFY_WORKFLOW = ROOT / ".github" / "workflows" / "qualify-reference-bridge.yml"


def workflow_step(source: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = source.index(marker)
    end = source.find("\n      - name:", start + len(marker))
    return source[start:] if end < 0 else source[start:end]


class OctaveLoaderContractTests(unittest.TestCase):
    def test_mex_soversion_metadata_is_single_exported_const_int_one(self) -> None:
        source = MOCK_BRIDGE.read_text(encoding="utf-8")
        values = re.findall(
            r"\bMOCK_EXPORT\s+const\s+int\s+__octave_mex_soversion__\s*=\s*([0-9]+)\s*;",
            source,
        )
        self.assertEqual(values, ["1"])

    def test_linux_qualification_uses_digest_pinned_octave_11_3_loader(self) -> None:
        workflow = QUALIFY_WORKFLOW.read_text(encoding="utf-8")
        version_match = re.search(r'^  OCTAVE_LINUX_VERSION: "([0-9]+)\.([0-9]+)\.([0-9]+)"$', workflow, re.MULTILINE)
        self.assertIsNotNone(version_match)
        assert version_match is not None
        version = tuple(int(part) for part in version_match.groups())
        self.assertGreaterEqual(version[:2], (10, 2))
        self.assertEqual(version, (11, 3, 0))

        image_match = re.search(r"^  OCTAVE_LINUX_IMAGE: (\S+)$", workflow, re.MULTILINE)
        self.assertIsNotNone(image_match)
        assert image_match is not None
        self.assertEqual(
            image_match.group(1),
            "ghcr.io/gnu-octave/octave:11.3.0@sha256:185db7993e000d4f3f6e7bbbf7fb3f999f52e799ea52231ad8a15353381e0dcb",
        )

        headers = workflow_step(workflow, "Source pinned GNU Octave headers on Linux")
        self.assertIn('docker run --rm --platform linux/amd64 --user "$(id -u):$(id -g)"', headers)
        self.assertIn("-e HOME=/tmp", headers)
        self.assertIn('"$OCTAVE_LINUX_IMAGE"', headers)
        self.assertIn('octave-config -p OCTINCLUDEDIR', headers)
        self.assertIn('cp -a "$octave_include/." build/octave-headers/', headers)

        build = workflow_step(workflow, "Build Linux candidate in pinned manylinux2014")
        self.assertIn("test -f build/octave-headers/mex.h", build)
        self.assertIn('OCTAVE_INCLUDE_DIRS=$workspace/build/octave-headers', build)

        acceptance = workflow_step(workflow, "Execute GNU Octave host acceptance on Linux")
        self.assertIn("if: matrix.target == 'linux-x64'", acceptance)
        self.assertIn('docker run --rm --platform linux/amd64 --user "$(id -u):$(id -g)"', acceptance)
        self.assertIn("-e HOME=/tmp", acceptance)
        self.assertIn('"$OCTAVE_LINUX_IMAGE"', acceptance)
        self.assertIn('test "$(octave-config -p VERSION)" = "$OCTAVE_LINUX_VERSION"', acceptance)
        self.assertIn("test-octave-linux.sh", acceptance)
        self.assertIn('${{ matrix.profile }}', acceptance)

        packaged = workflow_step(workflow, "Extract and test packaged bytes on Linux")
        self.assertIn("if: matrix.target == 'linux-x64'", packaged)
        self.assertIn('docker run --rm --platform linux/amd64 --user "$(id -u):$(id -g)"', packaged)
        self.assertIn("-e HOME=/tmp", packaged)
        self.assertIn('"$OCTAVE_LINUX_IMAGE"', packaged)
        self.assertIn('"$PACKAGE_ROOT/run-local-test.sh"', packaged)
        self.assertIn('--octave "$(command -v octave-cli)"', packaged)
        self.assertNotIn("octave=6.4.0-2", workflow)

    def test_windows_qualification_uses_sha_pinned_octave_release_archive(self) -> None:
        workflow = QUALIFY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("OCTAVE_WINDOWS_RELEASE_TAG: release-11-3-0", workflow)
        self.assertIn(
            "OCTAVE_WINDOWS_ARCHIVE_SHA256: fd3cf0e885467a15211b8ceded42800432a5462481324d49a896ef257e05d1a0",
            workflow,
        )
        self.assertNotIn("choco install octave", workflow)
        steps = workflow.split("      - name: Install pinned GNU Octave on Windows\n")[1:]
        self.assertEqual(len(steps), 2)
        for raw in steps:
            step = raw.split("\n      - name:", 1)[0]
            self.assertIn("--repo gnu-octave/octave", step)
            self.assertIn('"octave-$env:OCTAVE_WINDOWS_VERSION-w64.7z"', step)
            self.assertIn("Get-FileHash -LiteralPath $archive -Algorithm SHA256", step)
            self.assertIn("$sha -ne $env:OCTAVE_WINDOWS_ARCHIVE_SHA256", step)
            self.assertIn("Get-ChildItem -LiteralPath $root -Recurse -File -Filter octave-cli.exe", step)
            self.assertIn("Get-ChildItem -LiteralPath $root -Recurse -File -Filter mex.h", step)
            self.assertIn("OCTAVE_VERSION=$env:OCTAVE_WINDOWS_VERSION", step)
            self.assertIn("OCTAVE_INCLUDE_DIRS=$($mexHeader.Directory.FullName)", step)
            self.assertNotIn("C:\\Program Files\\GNU Octave", step)


if __name__ == "__main__":
    unittest.main()
