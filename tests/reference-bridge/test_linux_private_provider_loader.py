"""Regression coverage for Linux host/OpenBLAS symbol preemption."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
PLATFORM_SOURCE = ROOT / "reference-bridge" / "src" / "mock_platform.c"
PLATFORM_INCLUDE = ROOT / "reference-bridge" / "src"
NUMPY_LOCK = ROOT / "locks" / "linux" / "linux-glibc217-numpy-sources.json"
RUNTIME_RECEIPT = ROOT / "locks" / "consumer" / "reference-runtime-v1.json"

PRIVATE_PROVIDER = "libopenblasbridge_np252_.so.0"
PRIVATE_IDENTITY_SYMBOL = "openblas_get_corenamebridge_np252_"
RC1_NUMPY_SHA256 = "e4f116ebae105bc76afc204c32d8a94f7aecf3c929269d7a557c80f1af0a6ab6"
RC2_NUMPY_SHA256 = "b3388ba1b677b8ac6a019525e4e9dfeb2c32d85cca5dd75308a9ad042285c15b"


class LinuxPrivateProviderContractTests(unittest.TestCase):
    def test_loader_identity_matches_pinned_producer_suffix(self) -> None:
        contract = json.loads(NUMPY_LOCK.read_text(encoding="utf-8"))
        identity = contract["openblas_build"]["runtime_identity"]
        self.assertEqual(identity["symbol_suffix"], "bridge_np252_")
        self.assertEqual(identity["library_name_suffix"], "bridge_np252_")

        source = PLATFORM_SOURCE.read_text(encoding="utf-8")
        self.assertIn(PRIVATE_PROVIDER, source)
        self.assertIn(PRIVATE_IDENTITY_SYMBOL, source)

        receipt = json.loads(RUNTIME_RECEIPT.read_text(encoding="utf-8"))
        linux = receipt["profiles"]["linux-x64"]
        self.assertEqual(linux["numpy"]["archive"]["sha256"], RC2_NUMPY_SHA256)
        self.assertEqual(linux["fenics"]["base"]["archive"]["sha256"], RC1_NUMPY_SHA256)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux ELF loader regression")
class LinuxPrivateProviderLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cc = shutil.which("gcc")
        if cls.cc is None:
            raise unittest.SkipTest("gcc is required for Linux loader regression tests")

    def _compile_shared(
        self,
        source: Path,
        output: Path,
        *extra: str,
    ) -> None:
        subprocess.run(
            [
                self.cc,
                "-shared",
                "-fPIC",
                "-O0",
                str(source),
                *extra,
                "-o",
                str(output),
            ],
            check=True,
            text=True,
        )

    def _fixture(
        self,
        *,
        identity: bool = True,
    ) -> tuple[Path, Path, Path, Path, Path]:
        raw = tempfile.mkdtemp(prefix="reference-bridge-loader-")
        self.addCleanup(shutil.rmtree, raw, True)
        root = Path(raw)
        runtime_lib = root / "runtime" / "lib"
        private_root = runtime_lib / "numpy-private"
        private_root.mkdir(parents=True)

        host_source = root / "host.c"
        host_source.write_text(
            "int collision_value(void) { return 11; }\n",
            encoding="utf-8",
        )
        private_source = root / "private.c"
        identity_source = (
            f'void *{PRIVATE_IDENTITY_SYMBOL}(void) {{ return (void *)0x22; }}\n'
            if identity
            else ""
        )
        private_source.write_text(
            textwrap.dedent(
                f"""\
                int collision_value(void) {{ return 22; }}
                int private_value(void) {{ return collision_value(); }}
                {identity_source}
                """
            ),
            encoding="utf-8",
        )
        python_source = root / "python.c"
        python_source.write_text(
            "int reference_fake_python(void) { return 1; }\n",
            encoding="utf-8",
        )
        consumer_source = root / "consumer.c"
        consumer_source.write_text(
            textwrap.dedent(
                """\
                extern int private_value(void);
                int consumer_value(void) { return private_value(); }
                """
            ),
            encoding="utf-8",
        )

        host = root / "libhost-openblas.so"
        private = private_root / PRIVATE_PROVIDER
        python = runtime_lib / "libpython3.14.so.1.0"
        consumer = runtime_lib / "consumer.so"
        self._compile_shared(host_source, host)
        self._compile_shared(
            private_source,
            private,
            f"-Wl,-soname,{PRIVATE_PROVIDER}",
        )
        self._compile_shared(python_source, python)
        self._compile_shared(
            consumer_source,
            consumer,
            f"-L{private_root}",
            "-Wl,--no-as-needed",
            f"-Wl,-l:{PRIVATE_PROVIDER}",
            "-Wl,-rpath,$ORIGIN/numpy-private",
        )

        harness_source = root / "harness.c"
        harness_source.write_text(
            textwrap.dedent(
                r"""
                #define _GNU_SOURCE 1
                #include "mock_platform.h"
                #include <dlfcn.h>
                #include <stdio.h>
                #include <string.h>

                typedef int (*value_fn)(void);

                static int consumer_value(const char *path)
                {
                    void *consumer = dlopen(path, RTLD_NOW | RTLD_LOCAL);
                    value_fn value;
                    if (consumer == NULL) {
                        fprintf(stderr, "consumer-load=%s\n", dlerror());
                        return -100;
                    }
                    value = (value_fn)dlsym(consumer, "consumer_value");
                    if (value == NULL) {
                        fprintf(stderr, "consumer-value-symbol=%s\n", dlerror());
                        return -101;
                    }
                    return value();
                }

                int main(int argc, char **argv)
                {
                    char error[MOCK_ERROR_MAX] = {0};
                    void *host;
                    MockLibrary python;
                    int value;

                    if (argc != 6) return 90;
                    host = dlopen(argv[2], RTLD_NOW | RTLD_GLOBAL);
                    if (host == NULL) {
                        fprintf(stderr, "host-load=%s\n", dlerror());
                        return 91;
                    }

                    if (strcmp(argv[1], "naive") == 0) {
                        value = consumer_value(argv[5]);
                        printf("consumer_value=%d\n", value);
                        return value == 11 ? 0 : 92;
                    }
                    if (strcmp(argv[1], "preloaded") == 0) {
                        if (dlopen(argv[4], RTLD_NOW | RTLD_LOCAL) == NULL) return 93;
                    }

                    python = mock_platform_load_python(argv[3], error, sizeof(error));
                    if (python == NULL) {
                        fprintf(stderr, "loader-error=%s\n", error);
                        return 12;
                    }
                    if (strcmp(argv[1], "core") == 0) {
                        printf("core-load=pass\n");
                        return 0;
                    }
                    if (dlsym(RTLD_DEFAULT, "openblas_get_corenamebridge_np252_") != NULL) {
                        fprintf(stderr, "private identity leaked into RTLD_DEFAULT\n");
                        return 95;
                    }
                    value = consumer_value(argv[5]);
                    printf("consumer_value=%d\n", value);
                    return value == 22 ? 0 : 94;
                }
                """
            ),
            encoding="utf-8",
        )
        harness = root / "harness"
        subprocess.run(
            [
                self.cc,
                "-std=c11",
                "-O0",
                "-I",
                str(PLATFORM_INCLUDE),
                str(PLATFORM_SOURCE),
                str(harness_source),
                "-ldl",
                "-o",
                str(harness),
            ],
            check=True,
            text=True,
        )
        return harness, host, python, private, consumer

    def _run(
        self,
        harness: Path,
        mode: str,
        host: Path,
        python: Path,
        private: Path,
        consumer: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(harness),
                mode,
                str(host),
                str(python),
                str(private),
                str(consumer),
            ],
            check=False,
            text=True,
            capture_output=True,
        )

    def test_reproduces_host_preemption_without_bridge_isolation(self) -> None:
        harness, host, python, private, consumer = self._fixture()
        completed = self._run(harness, "naive", host, python, private, consumer)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.strip(), "consumer_value=11")

    def test_bridge_preload_deepbinds_private_provider_before_python(self) -> None:
        harness, host, python, private, consumer = self._fixture()
        completed = self._run(harness, "bridge", host, python, private, consumer)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.strip(), "consumer_value=22")

    def test_core_runtime_without_numpy_private_directory_is_unchanged(self) -> None:
        harness, host, python, private, consumer = self._fixture()
        shutil.rmtree(private.parent)
        completed = self._run(harness, "core", host, python, private, consumer)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.strip(), "core-load=pass")

    def test_rejects_private_provider_loaded_before_bridge(self) -> None:
        harness, host, python, private, consumer = self._fixture()
        completed = self._run(harness, "preloaded", host, python, private, consumer)
        self.assertEqual(completed.returncode, 12, completed.stdout + completed.stderr)
        self.assertIn("loaded before bridge isolation", completed.stderr)

    def test_rejects_unpinned_second_openblas_provider(self) -> None:
        harness, host, python, private, consumer = self._fixture()
        unexpected = private.parent / "libopenblas-unpinned.so.0"
        shutil.copy2(private, unexpected)
        completed = self._run(harness, "bridge", host, python, private, consumer)
        self.assertEqual(completed.returncode, 12, completed.stdout + completed.stderr)
        self.assertIn("unexpected private NumPy OpenBLAS provider", completed.stderr)

    def test_rejects_provider_without_pinned_identity_symbol(self) -> None:
        harness, host, python, private, consumer = self._fixture(identity=False)
        completed = self._run(harness, "bridge", host, python, private, consumer)
        self.assertEqual(completed.returncode, 12, completed.stdout + completed.stderr)
        self.assertIn("identity validation failed", completed.stderr)

    def test_process_preload_makes_verified_blas_provider_authoritative(self) -> None:
        raw = tempfile.mkdtemp(prefix="reference-bridge-fenics-preload-")
        self.addCleanup(shutil.rmtree, raw, True)
        root = Path(raw)
        package_lib = root / "package with spaces" / "runtime" / "fenics-runtime" / "conda" / "lib"
        host_lib = root / "host"
        override = root / "preload"
        package_lib.mkdir(parents=True)
        host_lib.mkdir()
        override.mkdir()

        host_source = root / "host_lapack.c"
        host_source.write_text(
            "int reference_lapack_value(void) { return 11; }\n",
            encoding="utf-8",
        )
        private_source = root / "private_openblas.c"
        private_source.write_text(
            textwrap.dedent(
                """\
                extern int reference_openblas_dependency(void);
                int reference_lapack_value(void)
                {
                    return 15 + reference_openblas_dependency();
                }
                """
            ),
            encoding="utf-8",
        )
        dependency_source = root / "private_dependency.c"
        dependency_source.write_text(
            "int reference_openblas_dependency(void) { return 7; }\n",
            encoding="utf-8",
        )
        executable_source = root / "consumer.c"
        executable_source.write_text(
            textwrap.dedent(
                r"""
                #include <stdio.h>
                extern int reference_lapack_value(void);
                int main(void)
                {
                    int value = reference_lapack_value();
                    printf("lapack_value=%d\n", value);
                    return value == 11 || value == 22 ? 0 : 1;
                }
                """
            ),
            encoding="utf-8",
        )

        host_provider = host_lib / "liblapack.so.3"
        private_dependency = package_lib / "libopenblas-dependency.so.1"
        private_provider = package_lib / "libopenblasp-r0.3.34.so"
        self._compile_shared(
            host_source,
            host_provider,
            "-Wl,-soname,liblapack.so.3",
        )
        self._compile_shared(
            dependency_source,
            private_dependency,
            "-Wl,-soname,libopenblas-dependency.so.1",
        )
        self._compile_shared(
            private_source,
            private_provider,
            f"-L{package_lib}",
            "-Wl,--no-as-needed",
            "-Wl,-l:libopenblas-dependency.so.1",
            "-Wl,-soname,libopenblas.so.0",
            "-Wl,-rpath,$ORIGIN",
        )
        executable = root / "consumer"
        subprocess.run(
            [
                self.cc,
                "-O0",
                str(executable_source),
                f"-L{host_lib}",
                "-Wl,--no-as-needed",
                "-Wl,-l:liblapack.so.3",
                f"-Wl,-rpath,{host_lib}",
                "-o",
                str(executable),
            ],
            check=True,
            text=True,
        )

        clean_env = os.environ.copy()
        clean_env.pop("LD_PRELOAD", None)
        baseline = subprocess.run(
            [str(executable)],
            check=True,
            text=True,
            capture_output=True,
            env=clean_env,
        )
        self.assertEqual(baseline.stdout.strip(), "lapack_value=11")

        preload_alias = override / "fenics-lib"
        preload_alias.symlink_to(package_lib, target_is_directory=True)
        preload_env = clean_env.copy()
        preload_env["LD_PRELOAD"] = str(preload_alias / private_provider.name)
        preloaded = subprocess.run(
            [str(executable)],
            check=True,
            text=True,
            capture_output=True,
            env=preload_env,
        )
        self.assertEqual(preloaded.stdout.strip(), "lapack_value=22")


if __name__ == "__main__":
    unittest.main()
