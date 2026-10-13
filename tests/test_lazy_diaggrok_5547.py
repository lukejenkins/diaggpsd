"""diaggpsd imports diaggrok lazily, and its codes decode exactly as in eager mode."""

import json
import os
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]

_PROBE = """
import json, sys
sys.path.insert(0, %r)
import diaggpsd
import diaggrok.registry as r
codes = sorted(diaggpsd.required_codes(gpsd=True, json_out=True, rinex=True))
# Read _registry/_overrides directly: the public accessors would trigger the sweep.
ent = {}
for c in codes:
    rows = []
    if c in r._registry:
        e = r._registry[c]; rows.append(('*', e.func.__module__, e.func.__qualname__))
    for v, e in sorted(r._overrides.get(c, {}).items()):
        rows.append((v, e.func.__module__, e.func.__qualname__))
    ent[hex(c)] = rows
print(json.dumps({'pending': sys.modules['diaggrok.parsers']._lazy_pending,
                  'n': len(r._registry), 'entries': ent,
                  'env_left': 'DIAGGROK_LAZY_PARSERS' in __import__('os').environ}))
""" % str(APP)


def _probe(pre_sweep: bool) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "DIAGGROK_LAZY_PARSERS"}
    code = ("import diaggrok\n" + _PROBE) if pre_sweep else _PROBE
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=APP,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_diaggpsd_skips_the_sweep_and_does_not_leak_the_variable():
    got = _probe(pre_sweep=False)
    assert got["pending"] is True          # the ~1,000-module sweep never ran
    assert got["n"] < 20
    assert got["env_left"] is False        # child processes stay eager


def test_every_required_code_resolves_to_the_same_parsers_as_eager():
    """The lazy gap: an override in a module diaggpsd does not import. Pinned closed."""
    lazy = _probe(pre_sweep=False)["entries"]
    eager = _probe(pre_sweep=True)["entries"]
    assert all(lazy[c] for c in lazy), lazy
    assert lazy == eager
