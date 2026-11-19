"""Catch invalid inline Python in the macOS exact-overlay test shell script."""

import ast
from pathlib import Path
import re
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/macos/test-fenics-overlay.sh"


class MacOSOverlayHeredocSyntaxTests(unittest.TestCase):
    def test_all_embedded_python_snippets_compile(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        snippets = re.findall(r"<<'PY'\n(.*?)\nPY(?:\n|$)", text, re.DOTALL)
        self.assertGreaterEqual(len(snippets), 3)
        for index, snippet in enumerate(snippets, start=1):
            with self.subTest(snippet=index):
                ast.parse(snippet, filename=f"{SCRIPT.name}:heredoc-{index}")


if __name__ == "__main__":
    unittest.main()
