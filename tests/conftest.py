"""Put the tests dir on sys.path so bare `from support import ...` works.

tests/ is a package (has __init__.py), so pytest prepends the repo root, not
this directory; the test modules import `support` as a top-level module.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
