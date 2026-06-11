"""Make tests/ importable and ensure project root is on sys.path.

Loaded both by pytest (via conftest.py mirror) and by ``python -m unittest``
when run from the project root.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
