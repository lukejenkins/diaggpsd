"""The ``udp-listen`` transport: passively consume a device that pushes its DIAG
stream as DGE1-framed UDP datagrams (a diagbarf node).

That device is the sole log-mask arbiter, so diaggpsd must never write a mask to
it. These tests pin the CLI spelling, the default port, the choice of
``DiagClient.from_udp_listen``, and that the mode counts as passive.
"""
from __future__ import annotations

import pytest

import diaggpsd


@pytest.mark.parametrize("spec, expected", [
    ("udp-listen", ("udp-listen", "0.0.0.0", 12399)),
    ("udp-listen:0.0.0.0:12509", ("udp-listen", "0.0.0.0", 12509)),
    ("udp-listen:10.0.0.254", ("udp-listen", "10.0.0.254", 12399)),
])
def test_parse_udp_listen(spec, expected):
    assert diaggpsd.parse_diag_port(spec) == expected


def test_plain_udp_is_not_swallowed_by_udp_listen():
    # 'udp-listen' starts with 'udp'; the older QMDL2 broadcast mode must keep
    # its own meaning and default port.
    assert diaggpsd.parse_diag_port("udp") == ("udp", "0.0.0.0", 2500)
    assert diaggpsd.parse_diag_port("udp:0.0.0.0:2600") == ("udp", "0.0.0.0", 2600)


def test_existing_modes_unchanged():
    assert diaggpsd.parse_diag_port("tcp") == ("tcp", "0.0.0.0", 2500)
    assert diaggpsd.parse_diag_port("/dev/ttyUSB0") == ("serial", "/dev/ttyUSB0", 0)


def test_udp_listen_is_passive():
    assert "udp-listen" in diaggpsd.PASSIVE_MODES
    assert "udp" in diaggpsd.PASSIVE_MODES
    assert "serial" not in diaggpsd.PASSIVE_MODES
    assert "tcp" not in diaggpsd.PASSIVE_MODES


def test_open_diag_uses_from_udp_listen(monkeypatch):
    calls = []

    class FakeClient:
        @classmethod
        def from_udp_listen(cls, host, port):
            calls.append(("udp-listen", host, port))
            return "client"

        @classmethod
        def from_udp_broadcast(cls, host, port):
            calls.append(("udp", host, port))
            return "client"

    monkeypatch.setattr(diaggpsd, "DiagClient", FakeClient)
    assert diaggpsd.open_diag("udp-listen", "0.0.0.0", 12509, "unused") == "client"
    assert calls == [("udp-listen", "0.0.0.0", 12509)]
