"""`_RawFdTransport.read()` must survive a transient MHI DIAG channel
reset (EINTR / ERESTARTSYS=512) by restarting the syscall a bounded number of
times, instead of letting the raw errno kill the capture.

ERESTARTSYS (512) is a kernel-internal "restart this syscall" errno that leaks
through the MHI DIAG endpoint when the channel resets (T99W640 SDX72
radio-revert). It is not in the `errno` module and Python does NOT auto-retry it.
"""
from __future__ import annotations

import errno
from unittest.mock import patch

import diagmunge.transport as diag  # relocated transport core


def test_read_retries_erestartsys_then_succeeds():
    t = diag._RawFdTransport(7)
    # os.read raises ERESTARTSYS(512) twice, then delivers data.
    with patch("os.read", side_effect=[OSError(512, "x"), OSError(512, "x"), b"DATA"]):
        assert t.read(64) == b"DATA"


def test_read_retries_eintr_then_succeeds():
    t = diag._RawFdTransport(7)
    eintr = OSError(errno.EINTR, "interrupted")   # promotes to InterruptedError
    with patch("os.read", side_effect=[eintr, b"DATA"]):
        assert t.read(64) == b"DATA"


def test_read_propagates_nontransient_errno():
    t = diag._RawFdTransport(7)
    # EBADF is a real failure, not a restartable reset — must propagate.
    with patch("os.read", side_effect=OSError(errno.EBADF, "bad fd")):
        try:
            t.read(64)
            assert False, "expected OSError to propagate"
        except OSError as exc:
            assert exc.errno == errno.EBADF


def test_read_propagates_when_reset_outlasts_retries():
    t = diag._RawFdTransport(7)
    # A reset that never clears (more than the bounded restart count) still
    # propagates so the caller's loop can apply its own ceiling.
    with patch("os.read", side_effect=OSError(512, "x")):
        try:
            t.read(64)
            assert False, "expected the persistent reset to propagate"
        except OSError as exc:
            assert exc.errno == 512
