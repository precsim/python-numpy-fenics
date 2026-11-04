"""Normalize conda-forge's CPython banner for Python packaging subprocesses.

Python 3.14's platform._sys_version expects the CPython build tuple to follow
the version directly. The pinned conda-forge runtime inserts its distributor
text between them, which causes pip/packaging to raise ValueError.
"""

import sys

if sys.version_info[:2] == (3, 14):
    sys.version = sys.version.replace(" | packaged by conda-forge |", "", 1)
