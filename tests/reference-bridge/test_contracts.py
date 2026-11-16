"""Stdlib-only contract tests for the public reference bridge phase-01 files."""

from __future__ import annotations

import copy
import json
from pathlib import Path, PurePosixPath
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "consumer-kit" / "contracts"
BRIDGE_BUILD = ROOT / "reference-bridge" / "bridge-build.json"
REFERENCE_RECEIPT = ROOT / "locks" / "consumer" / "reference-runtime-v1.json"


class ContractError(ValueError):
    """A contract fixture violates the supported schema subset or kit semantics."""


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_ref(root_schema: dict, ref: str) -> dict:
    if not ref.startswith("#/"):
        raise ContractError(f"only local schema refs are supported in tests: {ref}")
    value: object = root_schema
    for token in ref[2:].split("/"):
        if not isinstance(value, dict) or token not in value:
            raise ContractError(f"unresolvable schema ref: {ref}")
        value = value[token]
    if not isinstance(value, dict):
        raise ContractError(f"schema ref does not resolve to an object: {ref}")
    return value


def _matches_type(value: object, expected: str) -> bool:
    return {
        "array": lambda item: isinstance(item, list),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "null": lambda item: item is None,
        "object": lambda item: isinstance(item, dict),
        "string": lambda item: isinstance(item, str),
    }[expected](value)


def validate_schema(value: object, schema: dict, root_schema: dict | None = None, path: str = "$") -> None:
    """Validate the small JSON-Schema subset used by these phase-01 contracts."""

    root_schema = schema if root_schema is None else root_schema
    if "$ref" in schema:
        validate_schema(value, _resolve_ref(root_schema, schema["$ref"]), root_schema, path)
        return
    if "oneOf" in schema:
        matches = 0
        for candidate in schema["oneOf"]:
            try:
                validate_schema(value, candidate, root_schema, path)
            except ContractError:
                continue
            matches += 1
        if matches != 1:
            raise ContractError(f"{path}: expected exactly one oneOf schema match, got {matches}")
        return

    expected_type = schema.get("type")
    if expected_type is not None:
        choices = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_matches_type(value, choice) for choice in choices):
            raise ContractError(f"{path}: expected type {expected_type!r}")

    if "const" in schema and value != schema["const"]:
        raise ContractError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError(f"{path}: unsupported value {value!r}")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ContractError(f"{path}: string is too short")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            raise ContractError(f"{path}: value does not match {pattern!r}")

    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ContractError(f"{path}: integer is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ContractError(f"{path}: integer is above maximum")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ContractError(f"{path}: array has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ContractError(f"{path}: array has too many items")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(encoded) != len(set(encoded)):
                raise ContractError(f"{path}: duplicate array item")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_schema(item, schema["items"], root_schema, f"{path}[{index}]")

    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0):
            raise ContractError(f"{path}: object has too few properties")
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise ContractError(f"{path}: missing required properties {missing!r}")
        properties = schema.get("properties", {})
        for name, item in value.items():
            if name in properties:
                validate_schema(item, properties[name], root_schema, f"{path}.{name}")
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                raise ContractError(f"{path}: unexpected property {name!r}")
            if isinstance(additional, dict):
                validate_schema(item, additional, root_schema, f"{path}.{name}")


def _safe_relative_path(value: str, *, leaf: bool = False) -> None:
    if not value or "\x00" in value or "\\" in value or ":" in value or value.startswith("/"):
        raise ContractError(f"unsafe path: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ContractError(f"unsafe path segment: {value!r}")
    if PurePosixPath(value).is_absolute() or (leaf and len(parts) != 1):
        raise ContractError(f"path must be a safe {'leaf name' if leaf else 'relative path'}: {value!r}")


def validate_bridge_contract(manifest: dict, schema: dict) -> None:
    validate_schema(manifest, schema)

    file_inputs: list[str] = []
    for key in ("sources", "python_helpers", "host_fixtures"):
        for value in manifest[key]:
            _safe_relative_path(value)
            file_inputs.append(value)
    for value in manifest["include_dirs"]:
        _safe_relative_path(value)

    for target_name, target in manifest["targets"].items():
        for value in target["sources"]:
            _safe_relative_path(value)
            file_inputs.append(value)
        _safe_relative_path(target["output"], leaf=True)
        _safe_relative_path(target["matlab_alias"], leaf=True)
        if target["output"] != manifest["mex_name"] + ".mex":
            raise ContractError(f"{target_name}: output does not match mex_name")
        if target["matlab_alias"] == target["output"]:
            raise ContractError(f"{target_name}: MATLAB alias must be distinct from canonical output")

    duplicates = sorted({path for path in file_inputs if file_inputs.count(path) > 1})
    if duplicates:
        raise ContractError(f"duplicate build input paths: {duplicates!r}")


def validate_reference_package(manifest: dict, schema: dict) -> None:
    validate_schema(manifest, schema)
    aliases = {
        "win-x64": "bridge.mexw64",
        "linux-x64": "bridge.mexa64",
        "macos-arm64": "bridge.mexmaca64",
        "macos-x64": "bridge.mexmaci64",
    }
    launchers = {
        "win-x64": "run-local-test.ps1",
        "linux-x64": "run-local-test.sh",
        "macos-arm64": "run-local-test.sh",
        "macos-x64": "run-local-test.sh",
    }
    if manifest["mex"]["matlab_alias"] != aliases[manifest["platform"]]:
        raise ContractError("MATLAB alias does not match package platform")
    if manifest["local_host"]["launcher"] != launchers[manifest["platform"]]:
        raise ContractError("local test launcher does not match package platform")

    inventory: dict[str, dict] = {}
    for item in manifest["files"]:
        _safe_relative_path(item["path"])
        if item["path"] in inventory:
            raise ContractError(f"duplicate package inventory path: {item['path']}")
        if item["path"] in {"reference-manifest.json", "SHA256SUMS.txt"}:
            raise ContractError(f"package metadata cannot self-attest: {item['path']}")
        inventory[item["path"]] = item

    mex = manifest["mex"]
    _safe_relative_path(mex["path"], leaf=True)
    _safe_relative_path(mex["matlab_alias"], leaf=True)
    for path in (mex["path"], mex["matlab_alias"]):
        if path not in inventory:
            raise ContractError(f"MEX package entry missing from inventory: {path}")
        if inventory[path]["kind"] != "file":
            raise ContractError(f"MEX package entry must be a file: {path}")
        if inventory[path]["sha256"] != mex["sha256"]:
            raise ContractError(f"MEX package entry SHA-256 mismatch: {path}")


def archive_fixture(name: str, marker: str) -> dict:
    return {
        "name": name,
        "sha256": marker * 64,
        "manifest_name": "runtime-manifest.json",
        "manifest_sha256": marker * 64,
        "qualification_name": "qualification-linux-x64-numpy.json",
        "qualification_sha256": marker * 64,
    }


class PhaseOneContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bridge_schema = load_json(CONTRACTS / "bridge-build-v1.schema.json")
        cls.receipt_schema = load_json(CONTRACTS / "consumer-runtime-receipt-v1.schema.json")
        cls.package_schema = load_json(CONTRACTS / "reference-package-v1.schema.json")
        cls.bridge_manifest = load_json(BRIDGE_BUILD)

    def test_contract_schemas_are_draft_2020_12_json(self) -> None:
        for schema in (self.bridge_schema, self.receipt_schema, self.package_schema):
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertEqual(schema["type"], "object")

    def test_reference_bridge_build_manifest_is_valid(self) -> None:
        validate_bridge_contract(self.bridge_manifest, self.bridge_schema)
        self.assertEqual(set(self.bridge_manifest["targets"]), {
            "win-x64", "linux-x64", "macos-arm64", "macos-x64"
        })
        expected_aliases = {
            "win-x64": "bridge.mexw64",
            "linux-x64": "bridge.mexa64",
            "macos-arm64": "bridge.mexmaca64",
            "macos-x64": "bridge.mexmaci64",
        }
        for name, target in self.bridge_manifest["targets"].items():
            self.assertEqual(target["output"], "bridge.mex")
            self.assertEqual(target["exports"], ["mexFunction"])
            self.assertEqual(target["matlab_alias"], expected_aliases[name])

    def test_bridge_build_rejects_path_traversal(self) -> None:
        manifest = copy.deepcopy(self.bridge_manifest)
        manifest["sources"][0] = "../private/mock_bridge.c"
        with self.assertRaisesRegex(ContractError, "does not match|unsafe"):
            validate_bridge_contract(manifest, self.bridge_schema)

    def test_bridge_build_rejects_duplicate_inputs_across_source_sets(self) -> None:
        manifest = copy.deepcopy(self.bridge_manifest)
        manifest["targets"]["linux-x64"]["sources"] = [manifest["sources"][0]]
        with self.assertRaisesRegex(ContractError, "duplicate build input"):
            validate_bridge_contract(manifest, self.bridge_schema)

    def test_bridge_build_rejects_unsafe_output_and_alias(self) -> None:
        output = copy.deepcopy(self.bridge_manifest)
        output["targets"]["win-x64"]["output"] = "../bridge.mex"
        with self.assertRaises(ContractError):
            validate_bridge_contract(output, self.bridge_schema)

        alias = copy.deepcopy(self.bridge_manifest)
        alias["targets"]["win-x64"]["matlab_alias"] = "../bridge.mexw64"
        with self.assertRaises(ContractError):
            validate_bridge_contract(alias, self.bridge_schema)

    def test_runtime_receipt_accepts_cross_release_fenics_binding(self) -> None:
        receipt = {
            "format_version": 1,
            "producer_repository": "precsim/python-numpy-fenics",
            "profiles": {
                "linux-x64": {
                    "fenics": {
                        "release_tag": "runtime-v0.1.0-rc2",
                        "producer_revision": "a" * 40,
                        "archive": archive_fixture("fenics-runtime.tar.gz", "a"),
                        "base": {
                            "release_tag": "runtime-v0.1.0-rc1",
                            "producer_revision": "b" * 40,
                            "archive": archive_fixture("numpy-runtime.tar.gz", "b"),
                        },
                        "overlay": {
                            "release_tag": "runtime-v0.1.0-rc2",
                            "producer_revision": "a" * 40,
                            "archive": archive_fixture("fenics-overlay.tar.gz", "c"),
                        },
                    }
                }
            },
        }
        validate_schema(receipt, self.receipt_schema)

        unsafe = copy.deepcopy(receipt)
        unsafe["profiles"]["linux-x64"]["fenics"]["archive"]["name"] = "../fenics-runtime.tar.gz"
        with self.assertRaises(ContractError):
            validate_schema(unsafe, self.receipt_schema)

    def test_checked_in_runtime_receipt_covers_all_targets_and_profiles(self) -> None:
        receipt = load_json(REFERENCE_RECEIPT)
        validate_schema(receipt, self.receipt_schema)
        self.assertEqual(
            set(receipt["profiles"]),
            {"win-x64", "linux-x64", "macos-arm64", "macos-x64"},
        )
        for target, profiles in receipt["profiles"].items():
            self.assertEqual(set(profiles), {"core", "numpy", "fenics"}, target)
            fenics = profiles["fenics"]
            self.assertEqual(fenics["base"]["release_tag"], "runtime-v0.1.0-rc1")
            self.assertEqual(fenics["overlay"]["release_tag"], "runtime-v0.1.0-rc2")
            self.assertEqual(fenics["archive"], fenics["overlay"]["archive"])

    def test_reference_package_inventory_rejects_traversal_and_duplicate_paths(self) -> None:
        mex_sha = "d" * 64
        package = {
            "format_version": 1,
            "package": "reference-bridge-linux-x64-cp314-core-v1.0.0-rc1",
            "version": "v1.0.0-rc1",
            "platform": "linux-x64",
            "profile": "core",
            "python_abi": "cp314",
            "producer": {
                "repository": "precsim/python-numpy-fenics",
                "revision": "a" * 40,
                "receipt_path": "evidence/runtime-receipt.json",
                "receipt_sha256": "b" * 64,
                "archive_sha256s": ["c" * 64],
                "manifest_sha256s": ["e" * 64],
            },
            "mock": {
                "source_revision": "f" * 40,
                "backend_path": "python/mock_backend.py",
                "host_fixture_path": "host/mock_smoke.m",
            },
            "qualification": {
                "run_identity": "github-actions:12345",
                "evidence_path": "evidence/qualification-evidence.json",
                "evidence_sha256": "2" * 64,
            },
            "build": {
                "evidence_path": "evidence/build-evidence.json",
                "evidence_sha256": "3" * 64,
            },
            "native_closure": {
                "evidence_path": "evidence/native-closure-evidence.json",
                "evidence_sha256": "4" * 64,
                "summary": "MEX imports and runtime native closure audited",
            },
            "runtime": {
                "path": "runtime",
                "manifest": {
                    "path": "runtime/runtime-manifest.json",
                    "sha256": "5" * 64,
                },
            },
            "mex": {
                "path": "bridge.mex",
                "matlab_alias": "bridge.mexa64",
                "sha256": mex_sha,
            },
            "local_host": {
                "launcher": "run-local-test.sh",
                "octave_required": True,
                "network_required": False,
                "compiler_required": False,
                "matlab_qualified": False,
            },
            "files": [
                {"path": "bridge.mex", "kind": "file", "sha256": mex_sha, "bytes": 100, "mode": 420},
                {"path": "bridge.mexa64", "kind": "file", "sha256": mex_sha, "bytes": 100, "mode": 420},
                {"path": "python/mock_backend.py", "kind": "file", "sha256": "1" * 64, "bytes": 25, "mode": 420},
                {
                    "path": "runtime/lib/libpython3.14.so",
                    "kind": "symlink",
                    "target": "libpython3.14.so.1.0",
                    "mode": 511,
                },
            ],
        }
        validate_reference_package(package, self.package_schema)

        traversal = copy.deepcopy(package)
        traversal["files"][2]["path"] = "../mock_backend.py"
        with self.assertRaises(ContractError):
            validate_reference_package(traversal, self.package_schema)

        duplicate = copy.deepcopy(package)
        duplicate["files"].append({
            "path": "bridge.mex",
            "kind": "file",
            "sha256": "2" * 64,
            "bytes": 100,
            "mode": 420,
        })
        with self.assertRaisesRegex(ContractError, "duplicate package inventory"):
            validate_reference_package(duplicate, self.package_schema)

    def test_reference_package_requires_file_and_symlink_inventory_shapes(self) -> None:
        package = {
            "format_version": 1,
            "package": "reference-bridge-macos-arm64-cp314-core-v1.0.0",
            "version": "v1.0.0",
            "platform": "macos-arm64",
            "profile": "core",
            "python_abi": "cp314",
            "producer": {
                "repository": "precsim/python-numpy-fenics",
                "revision": "a" * 40,
                "receipt_path": "evidence/runtime-receipt.json",
                "receipt_sha256": "b" * 64,
                "archive_sha256s": ["c" * 64],
                "manifest_sha256s": ["d" * 64],
            },
            "mock": {
                "source_revision": "e" * 40,
                "backend_path": "python/mock_backend.py",
                "host_fixture_path": "host/mock_smoke.m",
            },
            "qualification": {
                "run_identity": "run-1",
                "evidence_path": "evidence/qualification-evidence.json",
                "evidence_sha256": "f" * 64,
            },
            "build": {
                "evidence_path": "evidence/build-evidence.json",
                "evidence_sha256": "1" * 64,
            },
            "native_closure": {
                "evidence_path": "evidence/native-closure-evidence.json",
                "evidence_sha256": "2" * 64,
                "summary": "pass",
            },
            "runtime": {
                "path": "runtime",
                "manifest": {
                    "path": "runtime/runtime-manifest.json",
                    "sha256": "3" * 64,
                },
            },
            "mex": {
                "path": "bridge.mex",
                "matlab_alias": "bridge.mexmaca64",
                "sha256": "4" * 64,
            },
            "local_host": {
                "launcher": "run-local-test.sh",
                "octave_required": True,
                "network_required": False,
                "compiler_required": False,
                "matlab_qualified": False,
            },
            "files": [
                {"path": "bridge.mex", "kind": "file", "bytes": 1, "sha256": "4" * 64, "mode": 420},
                {"path": "bridge.mexmaca64", "kind": "file", "bytes": 1, "sha256": "4" * 64, "mode": 420},
                {"path": "runtime/lib/current", "kind": "symlink", "target": "libpython3.14.dylib", "mode": 511},
            ],
        }
        validate_reference_package(package, self.package_schema)

        missing_mode = copy.deepcopy(package)
        del missing_mode["files"][0]["mode"]
        with self.assertRaises(ContractError):
            validate_reference_package(missing_mode, self.package_schema)

        wrong_alias = copy.deepcopy(package)
        wrong_alias["mex"]["matlab_alias"] = "bridge.mexmaci64"
        with self.assertRaisesRegex(ContractError, "alias"):
            validate_reference_package(wrong_alias, self.package_schema)


if __name__ == "__main__":
    unittest.main()
