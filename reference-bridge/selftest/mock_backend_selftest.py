"""Portable direct checks for the pure parts of mock_backend."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def load_backend():
    backend_path = Path(__file__).resolve().parents[1] / "python" / "mock_backend.py"
    spec = importlib.util.spec_from_file_location("reference_bridge_mock_backend_selftest", backend_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load mock backend for selftest")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    backend = load_backend()
    assert backend.scalar(5.0) == 11.0
    assert backend.scalar(10.0) == 21.0
    for value in (float("inf"), float("nan"), 5):
        try:
            backend.scalar(value)
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"scalar accepted invalid value: {value!r}")

    try:
        backend.numpy_sum_squares(5)
    except RuntimeError as exc:
        assert "before runtime configuration" in str(exc)
    else:
        raise AssertionError("NumPy probe ran before runtime configuration")

    print("mock_backend_selftest=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
