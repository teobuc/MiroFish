"""Test bootstrap: make ``src/fable`` importable without installation.

The suite is fully offline -- no test constructs an ``anthropic`` client or
needs an API key. Anything that would hit the network is exercised against
fakes (see test_loop_mock.py).
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
