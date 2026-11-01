"""diag_setup.py defaulted --diag-port to /dev/ttyUSB0, the port the
NV 7165 (OEMDRE) write lands on. A hand-typed invocation that omitted it would
write persistent NV to whichever modem owned ttyUSB0. Same hazard as --at-port
, worse in kind: NV persists across reboots.

--diag-port now has no default. Setup without it is refused (exit 2) before
any DIAG port is opened; --list-profiles and --info still work without it.
"""
from __future__ import annotations

import pytest

import diag_setup
from test_diag_setup_at_port_6585 import _main, rec  # noqa: F401  (fixture)


def test_diag_port_has_no_default():
    assert diag_setup.build_arg_parser().parse_args([]).diag_port is None


@pytest.mark.parametrize("extra", [["--no-at"],
                                   ["--at-port", "/dev/serial/by-id/x-if02"]])
def test_missing_diag_port_is_refused_before_any_diag_open(monkeypatch, capsys,
                                                           rec, extra):
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, "--chipset", "mdm9607", *extra)
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--diag-port is required" in err and "/dev/serial/by-id" in err
    assert rec.diag_opened == [] and rec.at_ports == []


def test_explicit_diag_port_is_used_verbatim(monkeypatch, rec):
    port = "/dev/serial/by-id/usb-Quectel_EG25-G-if00-port0"
    _main(monkeypatch, "--chipset", "mdm9607", "--diag-port", port, "--no-at",
          "--no-verify")
    assert rec.diag_opened == [port]


def test_list_profiles_needs_no_diag_port(monkeypatch, capsys, rec):
    _main(monkeypatch, "--list-profiles")
    assert "Available chipset profiles" in capsys.readouterr().out
    assert rec.diag_opened == []


def test_info_needs_no_diag_port(monkeypatch, capsys, rec):
    _main(monkeypatch, "--chipset", "mdm9607", "--info")
    assert "Profile:" in capsys.readouterr().out
    assert rec.diag_opened == []
