import hashlib
import pathlib
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "macos" / "package_fenics_overlay.py"


def load_packaging_definitions():
    source = SCRIPT.read_text(encoding="utf-8")
    definitions, marker, _ = source.partition("parser = argparse.ArgumentParser()")
    if not marker:
        raise AssertionError("macOS FEniCS packaging parser marker not found")
    namespace = {"__file__": str(SCRIPT)}
    exec(compile(definitions, str(SCRIPT), "exec"), namespace)
    return namespace


class ManifestPrefixAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_packaging_definitions()
        cls.helper = staticmethod(cls.mod["manifest_entries_and_verify_no_runtime_prefix_leaks"])

    def test_manifest_hashing_preserves_prefix_leak_rules(self):
        prefix = pathlib.Path("/producer/locked/fenics")
        needle = str(prefix).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            clean = root / "share" / "clean.txt"
            binary = root / "share" / "blob.bin"
            macho = root / "lib" / "extension.dylib"
            leaking = root / "etc" / "leak.conf"
            for path in (clean, binary, macho, leaking):
                path.parent.mkdir(parents=True, exist_ok=True)
            clean.write_bytes(b"relocatable\n")
            binary.write_bytes(b"binary\x00" + needle)
            macho.write_bytes(b"\xcf\xfa\xed\xfe" + needle)
            leaking.write_bytes(b"prefix=" + needle + b"\n")

            owners = {
                path.relative_to(root).as_posix(): ["package-b", "package-a", "package-a"]
                for path in (clean, binary, macho, leaking)
            }

            with self.assertRaisesRegex(
                SystemExit,
                r"producer prefix remains in staged runtime text/configuration: \['etc/leak\.conf'\]",
            ):
                self.helper(root, prefix, owners)

            leaking.write_bytes(b"prefix=@FENICS_RUNTIME_PREFIX@\n")
            entries = self.helper(root, prefix, owners)
            by_path = {entry["path"]: entry for entry in entries}

            self.assertEqual(list(by_path), sorted(by_path))
            self.assertEqual(by_path["share/clean.txt"]["kind"], "file")
            self.assertEqual(
                by_path["share/clean.txt"]["sha256"],
                hashlib.sha256(b"relocatable\n").hexdigest(),
            )
            self.assertEqual(
                by_path["share/clean.txt"]["owners"],
                ["package-a", "package-b"],
            )
            self.assertEqual(by_path["share/blob.bin"]["size"], len(b"binary\x00" + needle))
            self.assertEqual(
                by_path["lib/extension.dylib"]["sha256"],
                hashlib.sha256(b"\xcf\xfa\xed\xfe" + needle).hexdigest(),
            )

    def test_manifest_hashing_still_requires_explicit_owner(self):
        prefix = pathlib.Path("/producer/locked/fenics")
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            path = root / "share" / "unowned.txt"
            path.parent.mkdir(parents=True)
            path.write_text("clean\n", encoding="utf-8")
            with self.assertRaisesRegex(
                SystemExit,
                r"staged overlay file has no explicit owner: share/unowned\.txt",
            ):
                self.helper(root, prefix, {})


if __name__ == "__main__":
    unittest.main()

