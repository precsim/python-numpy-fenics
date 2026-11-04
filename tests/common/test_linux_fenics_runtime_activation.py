"""Contract tests for Linux FEniCS provider activation with relocated paths."""

from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest import mock

from runtime.linux import fenics_embed_runtime as bootstrap


class LinuxFenicsRuntimeActivationTests(unittest.TestCase):
    def test_hashlib_is_deferred_until_after_openssl_activation(self) -> None:
        self.assertNotIn("hashlib", bootstrap.__dict__)

    def test_private_openssl_pair_is_loaded_in_dependency_order(self) -> None:
        private = Path("/tmp/fenics runtime with spaces/conda")
        crypto = (private / "lib/libcrypto.so.3").resolve()
        ssl = (private / "lib/libssl.so.3").resolve()
        openssl_conf = (private / "ssl/openssl.cnf").resolve()
        openssl_modules = (private / "lib/ossl-modules").resolve()
        with mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(Path, "is_dir", return_value=True), \
             mock.patch.object(bootstrap, "_mapped_soname_paths", side_effect=[
                 set(), {crypto}, set(), {ssl}
             ]), \
             mock.patch.object(bootstrap.os, "RTLD_NOW", 2, create=True), \
             mock.patch.object(bootstrap.os, "RTLD_GLOBAL", 256, create=True), \
             mock.patch.object(bootstrap.ctypes, "CDLL") as load, \
             mock.patch.dict(os.environ, {"OPENSSL_CONF": "ambient", "OPENSSL_MODULES": "ambient"}):
            bootstrap._activate_openssl_runtime(private)
            self.assertEqual(os.environ["OPENSSL_CONF"], str(openssl_conf))
            self.assertEqual(os.environ["OPENSSL_MODULES"], str(openssl_modules))
        self.assertEqual([call.args[0] for call in load.call_args_list], [str(crypto), str(ssl)])

    def test_existing_public_crypto_provider_is_rejected(self) -> None:
        private = Path("/tmp/fenics runtime with spaces/conda")
        with mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(bootstrap, "_mapped_soname_paths", return_value={
                 Path("/tmp/public/libcrypto.so.3")
             }), \
             mock.patch.object(bootstrap.ctypes, "CDLL") as load:
            with self.assertRaisesRegex(RuntimeError, "competing libcrypto.so.3"):
                bootstrap._activate_openssl_runtime(private)
        load.assert_not_called()

    def test_mapped_provider_path_preserves_spaces(self) -> None:
        provider_name = "/tmp/fenics runtime with spaces/libgcc_s.so.1"
        provider = Path(provider_name)
        mapping = (
            f"7f010000-7f020000 r-xp 00000000 08:01 12345 {provider_name}\n"
            "7f030000-7f040000 r-xp 00000000 08:01 12346 /usr/lib/libstdc++.so.6\n"
            "7f050000-7f060000 rw-p 00000000 00:00 0\n"
        )
        original_is_file = Path.is_file
        original_read_text = Path.read_text

        def is_file(path: Path) -> bool:
            return True if path == Path("/proc/self/maps") else original_is_file(path)

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            return mapping if path == Path("/proc/self/maps") else original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "is_file", is_file), mock.patch.object(Path, "read_text", read_text):
            self.assertEqual(bootstrap._mapped_soname_paths("libgcc_s.so.1"), {provider.resolve()})
            self.assertEqual(bootstrap._mapped_soname_paths("libstdc++.so.6"), {Path("/usr/lib/libstdc++.so.6").resolve()})


if __name__ == "__main__":
    unittest.main()
