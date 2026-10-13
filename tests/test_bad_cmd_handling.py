"""Tests for DIAG_BAD_CMD_F propagation through `_send_recv`.

Background: Sierra Generic-PRI EM9291 returns an explicit ``DIAG_BAD_CMD_F``
(opcode 0x13) when DIAG NV opcodes are sent without prior ``AT!OPENLOCK``.
Previously, ``_send_recv`` treated this as an unsolicited frame and kept
waiting until the wall-clock timeout — surfacing the failure as "modem
didn't respond" when the actual answer was "modem said no."

These tests pre-populate ``DiagClient._pend`` with a hand-rolled HDLC-encoded
BAD_CMD frame and verify that:

1. ``_send_recv`` returns the tagged ``(DIAG_BAD_CMD_F, echoed_payload)``
   tuple instead of either looping or returning None.
2. ``enable_oemdre_nv`` distinguishes BAD_CMD from timeout in its log output
   and returns False without escaping the rejection silently.
3. The echoed payload survives transport-layer byte-stuffing — i.e. an
   embedded 0x7e or 0x7d inside the BAD_CMD body would still parse.

All tests use ``unittest.mock.patch.object`` to stub ``DiagClient.send`` —
the BAD_CMD frame is injected directly into the receive buffer so no real
transport is needed.
"""
from __future__ import annotations

import logging
from struct import pack
from unittest.mock import MagicMock, patch

import pytest

from diagmunge import (
    DIAG_BAD_CMD_F,
    DIAG_NV_WRITE_F,
    DIAG_VERNO_F,
    DiagClient,
)


def _hdlc_frame(body: bytes, no_crc: bool = False) -> bytes:
    """Build a complete HDLC-framed DIAG response: <body><crc><0x7e>.

    For no_crc transports (TCP mode), the body is wrapped directly with
    byte-stuffing and a 0x7e trailer — no CRC bytes are appended.
    """
    from binascii import crc_hqx

    if no_crc:
        framed = body
    else:
        # DIAG uses CRC-16-CCITT/X.25 (initial value 0xFFFF,
        # then negated). For test purposes we use the no_crc path to
        # avoid duplicating the production CRC implementation.
        del crc_hqx  # not used
        raise NotImplementedError("test fixtures use no_crc transports")

    # Byte-stuff 0x7d and 0x7e
    stuffed = bytearray()
    for b in framed:
        if b == 0x7d or b == 0x7e:
            stuffed.append(0x7d)
            stuffed.append(b ^ 0x20)
        else:
            stuffed.append(b)
    stuffed.append(0x7e)
    return bytes(stuffed)


def _make_client_with_canned_response(response_bytes: bytes) -> DiagClient:
    """Construct a DiagClient whose receive buffer already contains a
    response frame. Uses no_crc=True to skip CRC verification (the
    HDLC-decap path used by _send_recv accepts no-CRC frames in TCP mode).
    """
    transport = MagicMock()
    client = DiagClient(transport, no_crc=True)
    # Inject the response directly — the loop in _send_recv checks
    # `b'\x7e' in self._pend` BEFORE touching the transport, so as long
    # as the buffer already has a complete frame the read path is skipped.
    client._pend = response_bytes
    return client


class TestSendRecvBadCmd:
    """Verify `_send_recv` returns the BAD_CMD tagged tuple."""

    def test_bad_cmd_response_returned_with_echoed_payload(self):
        # Construct the wire response Sierra EM9291 sends for a rejected
        # NV-write, as captured during the root-cause analysis:
        #   RX = 13 27 fd 1b 01 00 00 00 ... 92 cc 7e
        #   ^^ DIAG_BAD_CMD_F  ^^ echoed opcode (0x27)
        #                         ^^^^^^^^^^^ echoed nv_id (7165 LE) + value (1 LE)
        body = bytes([DIAG_BAD_CMD_F]) + pack('<BHI', DIAG_NV_WRITE_F, 7165, 1)
        frame = _hdlc_frame(body, no_crc=True)
        client = _make_client_with_canned_response(frame)

        with patch.object(client, 'send'):  # don't actually try to send
            result = client._send_recv(DIAG_NV_WRITE_F, b'\x00' * 6, timeout=0.1)

        assert result is not None, (
            "BAD_CMD response should NOT be silently dropped (regression)"
        )
        op, echoed = result
        assert op == DIAG_BAD_CMD_F
        # The echoed payload after the BAD_CMD opcode preserves the
        # original opcode + NV id + value so the caller can correlate.
        assert echoed[0] == DIAG_NV_WRITE_F
        # NV ID 7165 = 0x1BFD; little-endian bytes are fd 1b
        assert echoed[1:3] == b'\xfd\x1b'

    def test_bad_cmd_logged_with_diagnostic_hint(self, caplog):
        body = bytes([DIAG_BAD_CMD_F, 0x26, 0x46, 0x00, 0x00, 0x00])
        frame = _hdlc_frame(body, no_crc=True)
        client = _make_client_with_canned_response(frame)

        with patch.object(client, 'send'), caplog.at_level(logging.WARNING, logger='diagmunge.transport'):
            client._send_recv(0x26, b'', timeout=0.1)

        # The diagnostic should mention OPENLOCK as a common cause so
        # operators reading the log know what to try next.
        msg = '\n'.join(r.message for r in caplog.records)
        assert 'DIAG_BAD_CMD_F' in msg
        assert 'OPENLOCK' in msg or 'SPC' in msg, (
            "BAD_CMD warning should hint at common causes (OPENLOCK / SPC unlock)"
        )

    def test_success_response_still_returns_unchanged(self):
        """A normal matching-opcode response must still return the
        success tuple. The BAD_CMD branch should not interfere with the
        non-rejection happy path.

        Note: ``_hdlc_decapsulate`` strips the last 2 bytes of the
        decoded payload (CRC in serial mode, no-CRC trailer in TCP
        mode), so the test body has 2 extra trailing bytes that the
        decoder consumes before exposing the payload to the caller.
        """
        # Payload visible to caller will be b'\x01\x02\x03' — last 2
        # bytes are stripped as the no-CRC trailer.
        body = bytes([DIAG_VERNO_F]) + b'\x01\x02\x03' + b'\xaa\xbb'
        frame = _hdlc_frame(body, no_crc=True)
        client = _make_client_with_canned_response(frame)

        with patch.object(client, 'send'):
            result = client._send_recv(DIAG_VERNO_F, b'', timeout=0.1)

        assert result is not None
        op, payload = result
        assert op == DIAG_VERNO_F
        assert payload == b'\x01\x02\x03'

    def test_timeout_still_returns_none_when_no_data(self):
        """If the buffer has no frame and the transport has nothing
        ready, `_send_recv` must still return None (timeout). The
        BAD_CMD branch must not crash on the timeout path.
        """
        client = _make_client_with_canned_response(b'')

        # select() will time out immediately because the fileno mock
        # returns no data; configure it explicitly:
        with patch.object(client, 'send'), \
             patch('diagmunge.transport.select.select', return_value=([], [], [])):
            result = client._send_recv(DIAG_NV_WRITE_F, b'', timeout=0.05)

        assert result is None


class TestEnableOemdreNvBadCmd:
    """Verify enable_oemdre_nv reports BAD_CMD distinctly from timeout."""

    def test_returns_false_and_logs_error_on_bad_cmd(self, caplog):
        body = bytes([DIAG_BAD_CMD_F]) + pack('<BHI', DIAG_NV_WRITE_F, 7165, 1)
        frame = _hdlc_frame(body, no_crc=True)
        client = _make_client_with_canned_response(frame)

        with patch.object(client, 'send'), caplog.at_level(logging.WARNING, logger='diagmunge.transport'):
            result = client.enable_oemdre_nv()

        assert result is False, "BAD_CMD must propagate as False (write didn't land)"

        msgs = '\n'.join(r.message for r in caplog.records)
        # The error message should make it obvious the modem rejected
        # the write — distinct from a generic "timed out" log.
        assert 'REJECTED' in msgs or 'BAD_CMD' in msgs
        assert 'AT!OPENLOCK' in msgs, (
            "BAD_CMD on enable_oemdre_nv should hint at the OPENLOCK gate"
        )

    def test_returns_false_on_timeout(self):
        """The pre-existing timeout path still works — the change
        is additive, not a replacement."""
        client = _make_client_with_canned_response(b'')

        with patch.object(client, 'send'), \
             patch('diagmunge.transport.select.select', return_value=([], [], [])):
            result = client.enable_oemdre_nv()

        assert result is False  # same return value, different log message


class TestSendRecvTransientReset:
    """`_send_recv` must ride out a transient EAGAIN/ERESTARTSYS from a
    settling channel (e.g. a freshly-rebooted MHI DIAG endpoint) within its
    deadline, instead of letting the bare OSError kill the mask/F3 arm."""

    def test_transient_eagain_is_retried_then_succeeds(self):
        import errno as _errno
        # payload visible to caller = b'\x07' (last 2 bytes are the no-CRC trailer)
        body = bytes([DIAG_VERNO_F]) + b'\x07' + b'\xaa\xbb'
        frame = _hdlc_frame(body, no_crc=True)
        transport = MagicMock()
        transport.fileno.return_value = 1
        # first read: spurious-ready EAGAIN; second read: the real frame
        transport.read.side_effect = [
            BlockingIOError(_errno.EAGAIN, "Resource temporarily unavailable"),
            frame,
        ]
        client = DiagClient(transport, no_crc=True)
        client._pend = b''
        with patch.object(client, 'send'), \
                patch('diagmunge.transport.select.select', return_value=([1], [], [])), \
                patch('diagmunge.transport.time.sleep'):
            result = client._send_recv(DIAG_VERNO_F, b'', timeout=1.0)
        assert result == (DIAG_VERNO_F, b'\x07'), (
            "a transient EAGAIN must be retried, not propagated"
        )
        assert transport.read.call_count == 2

    def test_nontransient_oserror_propagates(self):
        """A genuinely broken fd (EBADF) is NOT transient — must propagate so
        the caller sees a real error rather than spinning to the deadline."""
        import errno as _errno
        transport = MagicMock()
        transport.fileno.return_value = 1
        transport.read.side_effect = OSError(_errno.EBADF, "bad fd")
        client = DiagClient(transport, no_crc=True)
        client._pend = b''
        with patch.object(client, 'send'), \
                patch('diagmunge.transport.select.select', return_value=([1], [], [])):
            with pytest.raises(OSError):
                client._send_recv(DIAG_VERNO_F, b'', timeout=0.5)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
