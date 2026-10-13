"""Mask ownership + filtered packet loop (consumer-filtering design, Part A).

diaggpsd arms only the log codes its enabled outputs consume, and its loop reads
the raw stream through ``diagmunge.framefilter.FrameFilter`` so unwanted frames
never pay unescape + CRC. Replay of a 60 s RM500Q-AE max-mask capture: the same
239 ``0x1476`` packets, byte-identical JSON, CPU 9.96 s -> 0.47 s.
"""
import sys
from struct import pack

import pytest

import diaggpsd
from diagmunge import DiagClient
from diagmunge.framefilter import FrameFilter
from diagmunge.presets import GNSS_EXTENDED


# --- required_codes -------------------------------------------------------

def test_tier1_alone_is_just_the_position_report():
    assert diaggpsd.required_codes(gpsd=False, json_out=False, rinex=False) == {0x1476}


def test_gpsd_adds_the_sky_measurement_reports():
    got = diaggpsd.required_codes(gpsd=True, json_out=False, rinex=False)
    assert got == {0x1476, 0x1477, 0x1480, 0x14DE}


def test_rinex_adds_galileo_measurements():
    got = diaggpsd.required_codes(gpsd=False, json_out=False, rinex=True)
    assert 0x1886 in got and 0x14E1 not in got


def test_every_output_on_is_the_gnss_extended_preset():
    # diagbarf --mask gnss arms GNSS_EXTENDED (via the generated C header), so
    # it must carry everything diaggpsd can consume (spec §3).
    every = diaggpsd.required_codes(gpsd=True, json_out=True, rinex=True)
    assert tuple(sorted(every)) == GNSS_EXTENDED


def test_mask_defaults_none_only_for_shared_streams():
    assert diaggpsd.default_mask_mode('udp-listen') == 'none'
    assert diaggpsd.default_mask_mode('udp') == 'none'
    assert diaggpsd.default_mask_mode('serial') == 'own'
    assert diaggpsd.default_mask_mode('tcp') == 'own'   # diag_socket_log link


# --- log_frames: FrameFilter loop ----------------------------------------

def _log(code: int, body: bytes = b'\xAA' * 4) -> bytes:
    """Unescaped LOG frame (opcode 0x10 + outer header), no CRC."""
    inner = pack('<HHQ', 12 + len(body), code, 0) + body
    return bytes([0x10, 0]) + pack('<H', len(inner)) + inner


def _hdlc(frame: bytes) -> bytes:
    return DiagClient(None)._hdlc_encapsulate(frame)


class _Stream:
    no_crc = False

    def __init__(self, data: bytes):
        self.chunks = [data[i:i + 7] for i in range(0, len(data), 7)]  # unaligned

    def read_raw(self, timeout=None):
        if not self.chunks:
            raise EOFError
        return self.chunks.pop(0)


def _collect(data: bytes, codes) -> list[bytes]:
    out = []
    with pytest.raises(EOFError):
        for p in diaggpsd.log_frames(_Stream(data), FrameFilter(log_codes=codes)):
            out.append(p)
    return out


def test_log_frames_keeps_wanted_codes_and_drops_the_rest():
    data = (_hdlc(_log(0x1476)) + _hdlc(_log(0xB0C0)) + _hdlc(bytes([0x79]) + b'f3')
            + _hdlc(_log(0x1477)))
    got = _collect(data, {0x1476})
    assert got == [_log(0x1476)[1:]]


def test_log_frames_unwraps_a_multi_radio_0x98_log():
    wrapped = bytes([0x98]) + b'\x00' * 7 + _log(0x1476)    # inner at offset 8
    got = _collect(_hdlc(wrapped), {0x1476})
    assert got == [_log(0x1476)[1:]]


# --- main: who writes the mask --------------------------------------------

class _FakeDiag:
    no_crc = False

    def __init__(self):
        self.sent, self.subscribed, self.closed = [], [], False
        self.commands = []

    def _send_recv(self, opcode, payload, timeout=None):
        # A modem that answers no command: the F3 / event masks read as
        # unreadable, so own mode must leave them alone and carry on.
        self.commands.append(opcode)
        return None

    def send(self, opcode, payload):
        self.sent.append((opcode, payload))

    def recv(self, timeout=None):
        raise TimeoutError

    def read_raw(self, timeout=None):
        raise EOFError

    def subscribe_logs(self, codes):
        self.subscribed.append(list(codes))

    def close(self):
        self.closed = True


def _run_main(monkeypatch, argv):
    diag = _FakeDiag()
    monkeypatch.setattr(diaggpsd, "open_diag", lambda *a, **k: diag)
    monkeypatch.setattr(diaggpsd, "disarm_logs", lambda d, log: d.sent.append("disarm"))
    monkeypatch.setattr(sys, "argv", ["diaggpsd.py", "--no-gpsd", *argv])
    with pytest.raises(SystemExit):
        diaggpsd.main()
    return diag


def test_own_mode_subscribes_only_the_required_codes(monkeypatch):
    diag = _run_main(monkeypatch, ["--diag-port", "/dev/null", "--no-stdout"])
    assert diag.subscribed == [[0x1476]]
    assert "disarm" in diag.sent           # own => clears its mask on exit
    # own => reads the F3 (0x7D) and event (0x81) masks before quieting them
    assert 0x7D in diag.commands and 0x81 in diag.commands


def test_mask_none_on_a_tty_never_writes_a_mask(monkeypatch):
    diag = _run_main(monkeypatch, ["--diag-port", "/dev/null", "--mask", "none"])
    assert diag.subscribed == [] and diag.sent == []
    assert diag.commands == []             # none => never touches F3 / event masks
