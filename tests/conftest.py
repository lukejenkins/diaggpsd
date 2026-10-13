"""Add apps/diaggpsd to sys.path so the shim module is importable as
``gnss_packets``. There is no ``__init__.py`` in apps/ or apps/diaggpsd/
— the daemon is run as a script, not a package — so this conftest
emulates the same sys.path layout a real `diaggpsd.py` invocation has.
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
