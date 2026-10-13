"""A failed log subscribe must not leave the modem's log masks armed.

Over ``diag_socket_log`` the stream can back up behind the subscribed logs
until a SET_MASK ack never arrives. ``subscribe_logs`` then raises with the
groups acked so far still armed, and the next DIAG client cannot subscribe.
diaggpsd now sends one LOG_CONFIG DISABLE and drains queued frames for a
bounded time while it waits for the ack. The fake client below stands in for
the modem; no transport is opened.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from struct import pack

import pytest

import diaggpsd
from diagmunge import DIAG_BAD_CMD_F, DIAG_LOG_CONFIG_F, DIAG_LOG_F

DISABLE_REQ = pack('<3xI', diaggpsd.LOG_CFG_DISABLE)


def _disable_ack(status=0):
    return DIAG_LOG_CONFIG_F, pack('<3xII', diaggpsd.LOG_CFG_DISABLE, status)


def _log_frame():
    return DIAG_LOG_F, b'\x00' * 16


class FakeDiag:
    """Replies with ``frames`` in order, then times out, or floods forever."""

    def __init__(self, frames=(), flood=False, subscribe_error=None):
        self.frames = list(frames)
        self.flood = flood
        self.subscribe_error = subscribe_error
        self.sent = []
        self.subscribed = []
        self.closed = False

    def send(self, opcode, payload):
        self.sent.append((opcode, payload))

    def _send_recv(self, opcode, payload, timeout=None):
        return None     # unanswered: own mode leaves the F3 / event masks alone

    def recv(self, timeout=None):
        if self.frames:
            return self.frames.pop(0)
        if self.flood:
            time.sleep(0.001)
            return _log_frame()
        raise TimeoutError("DIAG recv timed out")

    no_crc = False

    def read_raw(self, timeout=None):          # the raw-stream main loop reads here
        raise EOFError("closed")

    def subscribe_logs(self, codes):
        self.subscribed.append(list(codes))
        if codes and self.subscribe_error is not None:
            raise self.subscribe_error

    def close(self):
        self.closed = True


@pytest.fixture
def log():
    return logging.getLogger("diaggpsd-test")


def test_ack_behind_queued_logs_is_found(log, caplog):
    diag = FakeDiag([_log_frame()] * 50 + [_disable_ack()])
    with caplog.at_level(logging.INFO):
        assert diaggpsd.disarm_logs(diag, log, timeout=2.0) is True
    assert diag.sent == [(DIAG_LOG_CONFIG_F, DISABLE_REQ)]
    assert "drained 50" in caplog.text
    assert diag.subscribed == []


def test_rejected_disable_falls_back_to_zero_masks(log):
    diag = FakeDiag([(DIAG_BAD_CMD_F, bytes([DIAG_LOG_CONFIG_F]) + DISABLE_REQ)])
    assert diaggpsd.disarm_logs(diag, log, timeout=2.0) is True
    assert diag.subscribed == [[]]


def test_no_ack_warns_with_the_recovery_step(log, caplog):
    diag = FakeDiag([_log_frame()])
    with caplog.at_level(logging.WARNING):
        assert diaggpsd.disarm_logs(diag, log, timeout=0.5) is False
    assert "AT+CFUN=1,1" in caplog.text


def test_failure_status_is_not_reported_as_cleared(log):
    assert diaggpsd.disarm_logs(FakeDiag([_disable_ack(status=1)]), log) is False


def test_a_stream_that_never_acks_cannot_hold_the_exit(log):
    # Run in a thread so a missing deadline fails here instead of hanging.
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            diaggpsd.disarm_logs(FakeDiag(flood=True), log, timeout=0.3)),
        daemon=True)
    worker.start()
    worker.join(timeout=3.0)
    assert not worker.is_alive(), "disarm_logs ignored its deadline"
    assert result == [False]


def test_failed_subscribe_disarms_and_exits_nonzero(monkeypatch):
    diag = FakeDiag([_disable_ack()],
                    subscribe_error=RuntimeError(
                        "Timed out waiting for SET_MASK response for log type 0xb"))
    monkeypatch.setattr(diaggpsd, "open_diag", lambda *a, **k: diag)
    monkeypatch.setattr(sys, "argv", [
        "diaggpsd.py", "--diag-port", "tcp:127.0.0.1:2500", "--no-gpsd", "--no-stdout"])
    with pytest.raises(SystemExit) as exc:
        diaggpsd.main()
    assert exc.value.code == 1
    assert (DIAG_LOG_CONFIG_F, DISABLE_REQ) in diag.sent
    assert diag.closed


def test_passive_modes_never_write_a_mask(monkeypatch):
    diag = FakeDiag()
    monkeypatch.setattr(diaggpsd, "open_diag", lambda *a, **k: diag)
    monkeypatch.setattr(sys, "argv", [
        "diaggpsd.py", "--diag-port", "udp-listen:127.0.0.1:0", "--no-gpsd", "--no-stdout"])

    def _eof(timeout=None):
        raise EOFError("closed")
    diag.recv = _eof
    with pytest.raises(SystemExit):
        diaggpsd.main()
    assert diag.sent == []
    assert diag.subscribed == []
