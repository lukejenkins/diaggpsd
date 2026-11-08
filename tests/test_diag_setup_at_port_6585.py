"""diag_setup.py defaulted --at-port to /dev/ttyUSB2, so the max-gnss
recipe (which passed only --diag-port) sent AT+QGPS=1 to whatever modem owned
ttyUSB2 -- on one bench host an RM520N-GL, not the EG25-G being configured.

--at-port now has no default. When the profile would start GNSS over AT, a
missing --at-port is refused BEFORE any DIAG write, so the modem is never left
half-configured and no AT port is ever guessed.
"""
from __future__ import annotations

import sys

import pytest

from diaggpsd import diag_setup


class _Recorder:
    def __init__(self):
        self.diag_opened = []
        self.at_ports = []


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()

    class FakeDiag:
        def close(self):
            pass

    def from_serial(port):
        r.diag_opened.append(port)
        return FakeDiag()

    monkeypatch.setattr(diag_setup.DiagClient, "from_serial", staticmethod(from_serial))
    monkeypatch.setattr(diag_setup, "run_diag_setup", lambda diag, profile, **kw: {})
    monkeypatch.setattr(diag_setup, "run_at_setup",
                        lambda port, profile, cold_start=False: r.at_ports.append(port))
    return r


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["diag_setup.py", *argv])
    diag_setup.main()


def test_at_port_has_no_default():
    assert diag_setup.build_arg_parser().parse_args([]).at_port is None


def test_missing_at_port_is_refused_before_any_diag_write(monkeypatch, capsys, rec):
    # The exact recipe shape from the issue: an AT-vendor profile, --diag-port only.
    with pytest.raises(SystemExit) as e:
        _main(monkeypatch, "--chipset", "mdm9607",
              "--diag-port", "/dev/serial/by-id/usb-Quectel_EG25-G-if00-port0")
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--at-port is required" in err and "--no-at" in err
    assert rec.diag_opened == []
    assert rec.at_ports == []


def test_non_by_id_diag_port_without_at_port_sends_no_at(monkeypatch, rec):
    with pytest.raises(SystemExit):
        _main(monkeypatch, "--chipset", "mdm9607", "--diag-port", "/dev/ttyUSB0")
    assert rec.at_ports == []


def test_explicit_at_port_is_the_one_used(monkeypatch, rec):
    at = "/dev/serial/by-id/usb-Quectel_EG25-G-if02-port0"
    _main(monkeypatch, "--chipset", "mdm9607", "--diag-port", "/dev/ttyUSB0",
          "--at-port", at)
    assert rec.at_ports == [at]


def test_no_at_skips_the_refusal_and_the_at_step(monkeypatch, rec):
    _main(monkeypatch, "--chipset", "mdm9607", "--diag-port", "/dev/ttyUSB0", "--no-at")
    assert rec.diag_opened == ["/dev/ttyUSB0"]
    assert rec.at_ports == []


def test_profile_without_at_vendor_needs_no_at_port(monkeypatch, rec):
    # sdx62 has at_vendor None: run_at_setup only warns, so nothing to refuse.
    _main(monkeypatch, "--chipset", "sdx62", "--diag-port", "/dev/ttyUSB0")
    assert rec.diag_opened == ["/dev/ttyUSB0"]
