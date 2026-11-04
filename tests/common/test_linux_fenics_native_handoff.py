"""Fail-closed tests for the Linux FEniCS native rebuild package handoff."""

from __future__ import annotations

import json
import base64
import hashlib
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest import mock
import zipfile

from scripts.linux import package_fenics_overlay as mod
from scripts.linux import rebuild_fenics_native as producer


class NativeRebuildHandoffTests(unittest.TestCase):
    def test_libxml2_build_helper_is_excluded_from_runtime_handoff(self) -> None:
        self.assertTrue(producer.ignored_build_install_path(PurePosixPath("bin/xml2-config")))
        self.assertTrue(producer.ignored_build_install_path(PurePosixPath("lib/dolfinx/dolfinx.conf")))
        self.assertTrue(producer.ignored_build_install_path(PurePosixPath("include/libxml2/libxml/HTMLparser.h")))
        self.assertFalse(producer.handoff_runtime_prefix_path(PurePosixPath("include/libxml2/libxml/HTMLparser.h")))
        self.assertFalse(producer.ignored_build_install_path(PurePosixPath("bin/dolfinx")))
        self.assertFalse(producer.handoff_runtime_prefix_path(PurePosixPath("bin/xml2-config")))

    def test_libxml2_headers_remain_owned_by_separate_locked_development_package(self) -> None:
        header = PurePosixPath("include/libxml2/libxml/HTMLparser.h")
        metadata = {"libxml2": {"paths_data": {"paths": [{"_path": header.as_posix()}]}}}
        self.assertTrue(producer.retained_libxml2_development_header(header, "libxml2-16", metadata))
        self.assertFalse(producer.retained_libxml2_development_header(header, "fenics-libbasix", metadata))
        self.assertFalse(producer.retained_libxml2_development_header(header, "libxml2-16", {}))
        self.assertFalse(producer.retained_libxml2_development_header(PurePosixPath("include/libxml2/unknown.h"), "libxml2-16", metadata))

    def test_relocated_wheel_rewrites_extension_and_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            wheel = Path(raw) / "test.whl"
            original = b"\x7fELFold"
            extension = "basix/_basixcpp.so"
            record = "fenics_basix-0.11.0.dist-info/RECORD"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(extension, original)
                archive.writestr("basix/__init__.py", b"# import\n")
                archive.writestr(record, f"{extension},sha256=old,{len(original)}\n{record},,\n")
            replacement = b"\x7fELFrelocated"
            producer.rewrite_relocated_wheel(wheel, {extension: replacement})
            with zipfile.ZipFile(wheel) as archive:
                self.assertEqual(archive.read(extension), replacement)
                self.assertEqual(archive.read("basix/__init__.py"), b"# import\n")
                digest = base64.urlsafe_b64encode(hashlib.sha256(replacement).digest()).rstrip(b"=").decode("ascii")
                self.assertIn(f"{extension},sha256={digest},{len(replacement)}", archive.read(record).decode())
            with self.assertRaisesRegex(producer.InputError, "duplicate or missing"):
                producer.rewrite_relocated_wheel(wheel, {"missing.so": replacement})

    def make_fixture(
        self, root: Path, *, stale_candidate: bool = False
    ) -> tuple[Path, Path, Path, Path, dict[str, dict]]:
        prefix = root / "prefix"
        prefix.mkdir()
        fmt_stock = prefix / "lib" / "libfmt.so.12"
        fmt_stock.parent.mkdir(parents=True)
        fmt_stock.write_bytes(b"stock-fmt")
        conda_meta = prefix / "conda-meta"
        conda_meta.mkdir()
        expected_owners = set(producer.REBUILD_COMPONENT_OWNERS.values()) | set(producer.WHEEL_OWNERS.values())
        for name in sorted(expected_owners):
            files = ["lib/libfmt.so.12"] if name == "fmt" else []
            (conda_meta / f"{name}.json").write_text(
                json.dumps({"name": name, "files": files}), encoding="utf-8"
            )

        lock = root / "lock.json"
        lock.write_text('{"fixture":"lock"}\n', encoding="utf-8")
        prefix_evidence = root / "prefix-evidence.json"
        prefix_evidence.write_text('{"fixture":"prefix"}\n', encoding="utf-8")

        preflight = root / "stock-preflight.json"
        report = {
            "format_version": 1,
            "stage": "stock-binary-closure-before-selective-rebuild",
            "package_lock_sha256": mod.sha256_file(lock),
            "prefix_verification_sha256": mod.sha256_file(prefix_evidence),
            "status": "fail",
            "violation_count": 1,
            "ceiling_violations": [{"fixture": "violation"}],
            "unexpected_nonnumeric_requirements": [],
            "violating_packages": list(mod.NATIVE_HANDOFF_EXPECTED_STOCK_VIOLATING_PACKAGES),
            "source_rebuilds_authorized": False,
        }
        report["selection_fingerprint_sha256"] = mod.canonical_json_sha(report)
        preflight.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        candidate_root = root / "candidate"
        candidate_prefix = candidate_root / "prefix"
        candidate_lib = candidate_prefix / "lib"
        candidate_lib.mkdir(parents=True)
        for name in ("wheels", "audit", "evidence"):
            (candidate_root / name).mkdir()
        provider_candidate = candidate_lib / "libfenics_cxxrt.so.1"
        fmt_candidate = candidate_lib / "libfmt.so.12"
        provider_candidate.write_bytes(b"provider")
        fmt_candidate.write_bytes(b"rebuilt-fmt")
        provider_candidate.chmod(0o644)
        fmt_candidate.chmod(0o644)
        candidate_build = candidate_root / "build"
        for component in producer.REBUILD_COMPONENT_OWNERS:
            build_dir = candidate_build / component
            build_dir.mkdir(parents=True)
            if component == "fmt":
                installed = fmt_candidate
            else:
                installed = candidate_prefix / "share" / component / "handoff-marker.txt"
                installed.parent.mkdir(parents=True, exist_ok=True)
                installed.write_text(component, encoding="utf-8")
            (build_dir / "install_manifest.txt").write_text(
                str(installed.resolve()) + "\n", encoding="utf-8"
            )
        (candidate_build / "dolfinx" / "CMakeCache.txt").write_text(
            "DOLFINX_ENABLE_ADIOS2:BOOL=OFF\n"
            "DOLFINX_ENABLE_KAHIP:BOOL=OFF\n",
            encoding="utf-8",
        )

        candidate_output_binding = {
            "replacements": [
                {
                    "destination": "lib/libfenics_cxxrt.so.1",
                    "owner": producer.HANDOFF_PROVIDER_OWNER,
                    "sha256": producer.sha256(provider_candidate),
                    "bytes": provider_candidate.stat().st_size,
                },
                {
                    "destination": "lib/libfmt.so.12",
                    "owner": "fmt",
                    "sha256": producer.sha256(fmt_candidate),
                    "bytes": fmt_candidate.stat().st_size,
                },
            ],
            "elf": [
                {"path": "prefix/lib/libfenics_cxxrt.so.1", "sha256": producer.sha256(provider_candidate)},
                {"path": "prefix/lib/libfmt.so.12", "sha256": producer.sha256(fmt_candidate)},
            ],
            "wheels": [],
        }
        candidate_output_binding["replacements"].sort(key=lambda item: item["destination"])
        candidate_output_binding["elf"].sort(key=lambda item: item["path"])
        candidate_output_binding_sha = producer.canonical_json_sha(candidate_output_binding)
        if stale_candidate:
            fmt_candidate.write_bytes(b"rebuilt-fmt-changed-after-closure-audit")

        closure_sha = "c" * 64
        source_manifest_path = Path(mod.__file__).resolve().parents[2] / mod.NATIVE_HANDOFF_SOURCE_MANIFEST
        source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
        conditional_omissions = {}
        for name in mod.NATIVE_HANDOFF_OMISSIONS:
            evidence = {
                "reached_paths": [],
                "reverse_direct_noncandidate": [],
                "staged_paths": [f"lib/{name}.so"],
                "abi_maxima": {},
            }
            conditional_omissions[name] = {
                "evidence": evidence,
                "evidence_sha256": producer.canonical_json_sha(evidence),
            }

        build_tool_explicit = root / "build-tools.txt"
        python_builder_manifest = root / "python-builder.json"
        libxml2_build_deps_manifest = root / "libxml2-build-deps.json"
        private_provider_manifest = root / "provider.json"
        for fixture, content in (
            (build_tool_explicit, "build-tools\n"),
            (python_builder_manifest, '{"builder":1}\n'),
            (libxml2_build_deps_manifest, '{"deps":1}\n'),
            (private_provider_manifest, '{"provider":1}\n'),
        ):
            fixture.write_text(content, encoding="utf-8")

        handoff = root / "handoff" / "native-rebuild-handoff.json"
        producer.write_package_handoff(
            handoff,
            roots={"prefix": candidate_prefix, "build": candidate_build},
            locked_prefix=prefix,
            package_lock=lock,
            prefix_verification={"verification": {"sha256": mod.sha256_file(prefix_evidence)}},
            stock_abi_preflight={
                "sha256": mod.sha256_file(preflight),
                "selection_fingerprint_sha256": report["selection_fingerprint_sha256"],
            },
            source_manifest=source_manifest_path,
            source_evidence=[
                {"component": name, "sha256": source_manifest["sources"][name]["sha256"]}
                for name in producer.REQUIRED_SOURCES
            ],
            build_tool_explicit=build_tool_explicit,
            python_builder_manifest=python_builder_manifest,
            libxml2_build_deps_manifest=libxml2_build_deps_manifest,
            private_provider_manifest=private_provider_manifest,
            native_closure={
                "sha256": closure_sha,
                "dolfinx_build_flags": {
                    "DOLFINX_ENABLE_ADIOS2": "OFF",
                    "DOLFINX_ENABLE_KAHIP": "OFF",
                },
                "closure_elf_count": 113,
                "conditional_omissions": conditional_omissions,
                "icu_reached_paths": ["lib/libicuuc.so.78"],
                "icu_resolution_required": True,
                "candidate_output_binding": candidate_output_binding,
                "candidate_output_binding_sha256": candidate_output_binding_sha,
            },
            wheels=[],
            elf_records=[
                {"path": str(provider_candidate), "sha256": producer.sha256(provider_candidate)},
                {"path": str(fmt_candidate), "sha256": producer.sha256(fmt_candidate)},
            ],
            wheel_records=[],
        )
        return handoff, prefix, lock, prefix_evidence, {
            "preflight": preflight,
            "records": mod.installed_records(prefix),
            "candidate_root": candidate_root,
            "candidate_prefix": candidate_prefix,
            "candidate_build": candidate_build,
            "provider_candidate": provider_candidate,
            "fmt_candidate": fmt_candidate,
            "source_manifest": source_manifest_path,
            "source_manifest_data": source_manifest,
            "build_tool_explicit": build_tool_explicit,
            "python_builder_manifest": python_builder_manifest,
            "libxml2_build_deps_manifest": libxml2_build_deps_manifest,
            "private_provider_manifest": private_provider_manifest,
        }

    def load_fixture(self, root: Path) -> dict:
        handoff, prefix, lock, prefix_evidence, extra = self.make_fixture(root)
        return mod.load_native_rebuild_handoff(
            handoff,
            prefix=prefix,
            lock_path=lock,
            prefix_evidence_path=prefix_evidence,
            stock_abi_preflight_path=extra["preflight"],
            records=extra["records"],
        )

    def test_valid_handoff_stages_exact_replacements_and_omissions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            handoff = self.load_fixture(root)
            self.assertEqual(handoff["omitted_packages"], set(mod.NATIVE_HANDOFF_OMISSIONS))
            self.assertEqual(
                [item["destination"] for item in handoff["replacements"]],
                sorted(item["destination"] for item in handoff["replacements"]),
            )

            stage_root = root / "stage"
            private_prefix = stage_root / "fenics-runtime" / "conda"
            owners: dict[str, list[str]] = {}
            staged = mod.stage_native_rebuild_replacements(stage_root, private_prefix, handoff, owners)
            self.assertEqual(len(staged), 2)
            self.assertEqual((private_prefix / "lib/libfmt.so.12").read_bytes(), b"rebuilt-fmt")
            self.assertEqual((private_prefix / "lib/libfenics_cxxrt.so.1").read_bytes(), b"provider")
            self.assertEqual(
                owners["fenics-runtime/conda/lib/libfmt.so.12"],
                ["fmt"],
            )

    def test_rejects_traversal_numpy_ownership_and_unsorted_replacements(self) -> None:
        mutations = (
            ("traversal", lambda data: data["replacements"][0].update(destination="../escape"), "canonical POSIX"),
            (
                "numpy",
                lambda data: data["replacements"][0].update(destination="lib/python3.14/site-packages/numpy/core.so"),
                "CPython/NumPy ownership",
            ),
            ("unsorted", lambda data: data["replacements"].reverse(), "uniquely sorted"),
            (
                "unknown-owner",
                lambda data: data["replacements"][0].update(owner="curl"),
                "unexpected replacement owner",
            ),
            (
                "source-hash",
                lambda data: data["inputs"]["source_sha256"].update(fmt="0" * 64),
                "source hash mismatch for fmt",
            ),
            (
                "omission-output-binding",
                lambda data: data["conditional_omissions"][0].update(candidate_output_binding_sha256="0" * 64),
                "not bound to candidate outputs",
            ),
        )
        for label, mutate, message in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                handoff, prefix, lock, prefix_evidence, extra = self.make_fixture(root)
                data = json.loads(handoff.read_text(encoding="utf-8"))
                mutate(data)
                handoff.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, message):
                    mod.load_native_rebuild_handoff(
                        handoff,
                        prefix=prefix,
                        lock_path=lock,
                        prefix_evidence_path=prefix_evidence,
                        stock_abi_preflight_path=extra["preflight"],
                        records=extra["records"],
                    )

    def test_rejects_stale_closure_proof_after_candidate_bytes_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(producer.InputError, "does not match exact candidate outputs"):
                self.make_fixture(Path(raw), stale_candidate=True)

    def test_candidate_build_handoff_round_trips_through_independent_closure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, prefix, lock, prefix_evidence, extra = self.make_fixture(root)
            candidate_root = extra["candidate_root"]
            roots = {
                name: candidate_root / name
                for name in ("inputs", "src", "build", "prefix", "wheels", "audit", "evidence")
            }
            source_manifest = extra["source_manifest_data"]
            candidate_handoff = roots["evidence"] / "native-build-handoff.json"
            payload = producer.write_candidate_build_handoff(
                candidate_handoff,
                roots=roots,
                locked_prefix=prefix,
                package_lock=lock,
                prefix_verification={
                    "verification": {
                        "path": str(prefix_evidence),
                        "sha256": producer.sha256(prefix_evidence),
                    }
                },
                stock_abi_preflight={
                    "path": str(extra["preflight"]),
                    "sha256": producer.sha256(extra["preflight"]),
                    "selection_fingerprint_sha256": json.loads(extra["preflight"].read_text())["selection_fingerprint_sha256"],
                },
                source_manifest=extra["source_manifest"],
                source_evidence=[
                    {"component": name, "sha256": source_manifest["sources"][name]["sha256"]}
                    for name in producer.REQUIRED_SOURCES
                ],
                build_tool_explicit=extra["build_tool_explicit"],
                python_builder_manifest=extra["python_builder_manifest"],
                libxml2_build_deps_manifest=extra["libxml2_build_deps_manifest"],
                private_provider_manifest=extra["private_provider_manifest"],
                wheels=[],
                elf_records=[
                    {
                        "path": str(extra["provider_candidate"]),
                        "sha256": producer.sha256(extra["provider_candidate"]),
                    },
                    {"path": str(extra["fmt_candidate"]), "sha256": producer.sha256(extra["fmt_candidate"])},
                ],
                wheel_records=[],
            )
            validated = producer.validate_candidate_build_handoff(candidate_handoff)
            self.assertEqual(
                validated["candidate_output_binding_sha256"],
                payload["candidate_output_binding_sha256"],
            )
            self.assertEqual(
                validated["dolfinx_build_configuration"]["flags"],
                producer.DOLFINX_OMISSION_BUILD_FLAGS,
            )

            closure = {
                "dolfinx_build_flags": {
                    "DOLFINX_ENABLE_ADIOS2": "OFF",
                    "DOLFINX_ENABLE_KAHIP": "OFF",
                },
                "unresolved_non_system": [],
                "closure_elf_count": 2,
                "candidate_output_binding": payload["candidate_output_binding"],
                "candidate_output_binding_sha256": payload["candidate_output_binding_sha256"],
                "candidates": {
                    name: {
                        "reached_paths": [],
                        "reverse_direct_noncandidate": [],
                        "staged_paths": [f"lib/{name}.so"],
                        "abi_maxima": {},
                    }
                    for name in producer.HANDOFF_OMISSION_PACKAGES
                },
            }
            closure["candidates"]["icu"] = {"reached_paths": [], "reverse_direct_noncandidate": []}
            closure_path = root / "native-closure.json"
            closure_path.write_text(json.dumps(closure), encoding="utf-8")
            final_path, final_payload = producer.finalize_candidate_build_handoff(
                candidate_handoff, closure_path
            )
            self.assertTrue(final_path.is_file())
            self.assertEqual(final_payload["status"], producer.HANDOFF_STATUS)
            self.assertEqual(
                final_payload["inputs"]["native_closure"]["candidate_output_binding_sha256"],
                payload["candidate_output_binding_sha256"],
            )

    def test_candidate_build_handoff_rejects_changed_dolfinx_build_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, prefix, lock, prefix_evidence, extra = self.make_fixture(root)
            candidate_root = extra["candidate_root"]
            roots = {
                name: candidate_root / name
                for name in ("inputs", "src", "build", "prefix", "wheels", "audit", "evidence")
            }
            source_manifest = extra["source_manifest_data"]
            candidate_handoff = roots["evidence"] / "native-build-handoff.json"
            producer.write_candidate_build_handoff(
                candidate_handoff,
                roots=roots,
                locked_prefix=prefix,
                package_lock=lock,
                prefix_verification={
                    "verification": {"path": str(prefix_evidence), "sha256": producer.sha256(prefix_evidence)}
                },
                stock_abi_preflight={
                    "path": str(extra["preflight"]),
                    "sha256": producer.sha256(extra["preflight"]),
                    "selection_fingerprint_sha256": json.loads(extra["preflight"].read_text())["selection_fingerprint_sha256"],
                },
                source_manifest=extra["source_manifest"],
                source_evidence=[
                    {"component": name, "sha256": source_manifest["sources"][name]["sha256"]}
                    for name in producer.REQUIRED_SOURCES
                ],
                build_tool_explicit=extra["build_tool_explicit"],
                python_builder_manifest=extra["python_builder_manifest"],
                libxml2_build_deps_manifest=extra["libxml2_build_deps_manifest"],
                private_provider_manifest=extra["private_provider_manifest"],
                wheels=[],
                elf_records=[
                    {"path": str(extra["provider_candidate"]), "sha256": producer.sha256(extra["provider_candidate"])},
                    {"path": str(extra["fmt_candidate"]), "sha256": producer.sha256(extra["fmt_candidate"])},
                ],
                wheel_records=[],
            )
            (roots["build"] / "dolfinx" / "CMakeCache.txt").write_text(
                "DOLFINX_ENABLE_ADIOS2:BOOL=ON\nDOLFINX_ENABLE_KAHIP:BOOL=OFF\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(producer.InputError, "DOLFINx build cache does not prove"):
                producer.validate_candidate_build_handoff(candidate_handoff)

    def test_candidate_build_handoff_rejects_changed_live_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, prefix, lock, prefix_evidence, extra = self.make_fixture(root)
            candidate_root = extra["candidate_root"]
            roots = {
                name: candidate_root / name
                for name in ("inputs", "src", "build", "prefix", "wheels", "audit", "evidence")
            }
            source_manifest = extra["source_manifest_data"]
            candidate_handoff = roots["evidence"] / "native-build-handoff.json"
            producer.write_candidate_build_handoff(
                candidate_handoff,
                roots=roots,
                locked_prefix=prefix,
                package_lock=lock,
                prefix_verification={
                    "verification": {
                        "path": str(prefix_evidence),
                        "sha256": producer.sha256(prefix_evidence),
                    }
                },
                stock_abi_preflight={
                    "path": str(extra["preflight"]),
                    "sha256": producer.sha256(extra["preflight"]),
                    "selection_fingerprint_sha256": json.loads(extra["preflight"].read_text())["selection_fingerprint_sha256"],
                },
                source_manifest=extra["source_manifest"],
                source_evidence=[
                    {"component": name, "sha256": source_manifest["sources"][name]["sha256"]}
                    for name in producer.REQUIRED_SOURCES
                ],
                build_tool_explicit=extra["build_tool_explicit"],
                python_builder_manifest=extra["python_builder_manifest"],
                libxml2_build_deps_manifest=extra["libxml2_build_deps_manifest"],
                private_provider_manifest=extra["private_provider_manifest"],
                wheels=[],
                elf_records=[
                    {
                        "path": str(extra["provider_candidate"]),
                        "sha256": producer.sha256(extra["provider_candidate"]),
                    },
                    {"path": str(extra["fmt_candidate"]), "sha256": producer.sha256(extra["fmt_candidate"])},
                ],
                wheel_records=[],
            )
            extra["fmt_candidate"].write_bytes(b"candidate-mutated-after-build-handoff")
            with self.assertRaisesRegex(producer.InputError, "candidate build ELF SHA-256 mismatch"):
                producer.validate_candidate_build_handoff(candidate_handoff)

    def test_candidate_build_handoff_rejects_tampered_source_and_preflight_bindings(self) -> None:
        for label, mutate, message in (
            (
                "source-hash",
                lambda data: data["inputs"]["source_sha256"].update(fmt="0" * 64),
                "source hash mismatch for fmt",
            ),
            (
                "stock-selection-fingerprint",
                lambda data: data["inputs"]["stock_abi_preflight"].update(
                    selection_fingerprint_sha256="0" * 64
                ),
                "selection fingerprint differs from referenced evidence",
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                _, prefix, lock, prefix_evidence, extra = self.make_fixture(root)
                candidate_root = extra["candidate_root"]
                roots = {
                    name: candidate_root / name
                    for name in ("inputs", "src", "build", "prefix", "wheels", "audit", "evidence")
                }
                source_manifest = extra["source_manifest_data"]
                candidate_handoff = roots["evidence"] / "native-build-handoff.json"
                producer.write_candidate_build_handoff(
                    candidate_handoff,
                    roots=roots,
                    locked_prefix=prefix,
                    package_lock=lock,
                    prefix_verification={
                        "verification": {
                            "path": str(prefix_evidence),
                            "sha256": producer.sha256(prefix_evidence),
                        }
                    },
                    stock_abi_preflight={
                        "path": str(extra["preflight"]),
                        "sha256": producer.sha256(extra["preflight"]),
                        "selection_fingerprint_sha256": json.loads(extra["preflight"].read_text())[
                            "selection_fingerprint_sha256"
                        ],
                    },
                    source_manifest=extra["source_manifest"],
                    source_evidence=[
                        {"component": name, "sha256": source_manifest["sources"][name]["sha256"]}
                        for name in producer.REQUIRED_SOURCES
                    ],
                    build_tool_explicit=extra["build_tool_explicit"],
                    python_builder_manifest=extra["python_builder_manifest"],
                    libxml2_build_deps_manifest=extra["libxml2_build_deps_manifest"],
                    private_provider_manifest=extra["private_provider_manifest"],
                    wheels=[],
                    elf_records=[
                        {
                            "path": str(extra["provider_candidate"]),
                            "sha256": producer.sha256(extra["provider_candidate"]),
                        },
                        {
                            "path": str(extra["fmt_candidate"]),
                            "sha256": producer.sha256(extra["fmt_candidate"]),
                        },
                    ],
                    wheel_records=[],
                )
                data = json.loads(candidate_handoff.read_text(encoding="utf-8"))
                mutate(data)
                candidate_handoff.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(producer.InputError, message):
                    producer.validate_candidate_build_handoff(candidate_handoff)

    def test_rejects_tampered_omission_evidence_and_symlink_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            handoff, prefix, lock, prefix_evidence, extra = self.make_fixture(root)
            data = json.loads(handoff.read_text(encoding="utf-8"))
            data["conditional_omissions"][0]["evidence"]["staged_paths"].append("lib/tampered.so")
            handoff.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "omission evidence hash mismatch"):
                mod.load_native_rebuild_handoff(
                    handoff,
                    prefix=prefix,
                    lock_path=lock,
                    prefix_evidence_path=prefix_evidence,
                    stock_abi_preflight_path=extra["preflight"],
                    records=extra["records"],
                )

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            handoff, prefix, lock, prefix_evidence, extra = self.make_fixture(root)
            with mock.patch.object(type(handoff), "is_symlink", return_value=True):
                with self.assertRaisesRegex(SystemExit, "not a regular file"):
                    mod.load_native_rebuild_handoff(
                        handoff,
                        prefix=prefix,
                        lock_path=lock,
                        prefix_evidence_path=prefix_evidence,
                        stock_abi_preflight_path=extra["preflight"],
                        records=extra["records"],
                    )

    def test_prefix_relocation_skips_exact_replacement_destination(self) -> None:
        metadata = [{"installed_relative": PurePosixPath("lib/libfmt.so.12")}]
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(
            mod, "package_cache_prefix_entries", return_value=metadata
        ):
            root = Path(raw)
            result = mod.relocate_metadata_prefixes(
                root,
                root / "private",
                root / "source",
                root / "cache",
                {"fmt": {}},
                ["fmt"],
                {},
                {"lib/libfmt.so.12"},
            )
            self.assertEqual(result, [])

    def test_install_owner_collection_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prefix = root / "prefix"
            build = root / "build"
            prefix.mkdir()
            for component in producer.REBUILD_COMPONENT_OWNERS:
                build_dir = build / component
                build_dir.mkdir(parents=True)
                installed = prefix / "share" / component / "marker"
                installed.parent.mkdir(parents=True, exist_ok=True)
                installed.write_text(component, encoding="utf-8")
                (build_dir / "install_manifest.txt").write_text(
                    str(installed.resolve()) + "\n", encoding="utf-8"
                )
            (build / "fmt" / "install_manifest.txt").write_text(
                str((root / "escape.so").resolve()) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(producer.InputError, "escapes rebuild prefix"):
                producer.collect_install_owners({"prefix": prefix, "build": build})


if __name__ == "__main__":
    unittest.main()
