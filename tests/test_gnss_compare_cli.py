"""diaggpsd-gnss-compare command line: --help and a missing argument are
usage, not a traceback."""
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "gnss_compare.py"


def _run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args],
                          capture_output=True, text=True, timeout=60)


def test_help_prints_usage_and_exits_0():
    r = _run("--help")
    assert r.returncode == 0, r.stderr
    assert "usage:" in r.stdout and "diag" in r.stdout and "gpsd" in r.stdout


@pytest.mark.parametrize("args", [(), ("only-one.jsonl",)])
def test_missing_arguments_are_a_usage_error(args):
    r = _run(*args)
    assert r.returncode == 2
    assert "usage:" in r.stderr and "Traceback" not in r.stderr
