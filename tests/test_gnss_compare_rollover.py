"""Tests for gnss_compare.py's GPS week-rollover correction.

The GPS week number is a 10-bit field that wraps every 1024 weeks (~19.6 yr).
Rollover-affected modems (MDM9600 / SWI9200X) timestamp DIAG GNSS records in a
stale era (2006 instead of 2026), so gnss_compare.py's absolute-time epoch
matcher found 0 position/SV epochs even though the time-of-week-keyed MSM7 CNR
path passed cleanly. ``gps_rollover_offset`` computes the whole-cycle timedelta
that re-aligns the DIAG side to the reference era; these tests lock its
contract.

The conftest in this directory puts apps/diaggpsd on sys.path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import gnss_compare as gc


_REF_2026 = datetime(2026, 6, 2, 22, 37, 45, tzinfo=timezone.utc)
_ONE_CYCLE = timedelta(weeks=1024)  # GPS WN rollover period (~19.6 yr)


def test_modern_receiver_is_a_noop():
    """A correctly-timed receiver (same era as the reference) gets a zero
    offset, so rollover-clean captures are untouched (n==0)."""
    diag = _REF_2026 - timedelta(seconds=0.2)  # ~same instant, same era
    assert gc.gps_rollover_offset(diag, _REF_2026) == timedelta(0)


def test_one_cycle_stale_is_corrected_forward():
    """The MDM9600 case: DIAG one rollover cycle (1024 wk) behind the
    reference → offset is +1024 weeks, snapping the DIAG epoch back onto the
    reference era to within sub-second."""
    diag_2006 = _REF_2026 - _ONE_CYCLE
    off = gc.gps_rollover_offset(diag_2006, _REF_2026)
    assert off == _ONE_CYCLE
    # Applying the offset lands the DIAG epoch on the reference instant.
    assert abs(((diag_2006 + off) - _REF_2026).total_seconds()) < 1.0


def test_real_mc7700_era_offset():
    """Grounded on an MC7700 rollover capture: DIAG 0x1477 measurement resolves to
    2006-10-17T22:37:44.884Z, reference TPV is 2026-06-02T22:37:45Z. The
    correction must be exactly +1 cycle and land them at the same time-of-day
    (both receivers co-fed from one antenna splitter)."""
    diag = datetime(2006, 10, 17, 22, 37, 44, 884000, tzinfo=timezone.utc)
    ref = datetime(2026, 6, 2, 22, 37, 45, tzinfo=timezone.utc)
    off = gc.gps_rollover_offset(diag, ref)
    assert off == _ONE_CYCLE
    # After correction the two are within the ~minute-scale capture window,
    # not ~20 years apart.
    assert abs(((diag + off) - ref).total_seconds()) < 90.0


def test_future_era_is_corrected_backward():
    """A DIAG epoch a cycle *ahead* of the reference snaps backward (−1
    cycle), so the rounding is symmetric, not forward-only."""
    diag_future = _REF_2026 + _ONE_CYCLE
    assert gc.gps_rollover_offset(diag_future, _REF_2026) == -_ONE_CYCLE


def test_none_inputs_are_zero():
    """Missing anchor on either side → zero offset (graceful no-op, matching
    the pre-fix behavior when no reference is available)."""
    assert gc.gps_rollover_offset(None, _REF_2026) == timedelta(0)
    assert gc.gps_rollover_offset(_REF_2026, None) == timedelta(0)
    assert gc.gps_rollover_offset(None, None) == timedelta(0)


def test_within_capture_span_does_not_trigger_a_cycle():
    """A multi-minute intra-capture spread must never round to a nonzero
    cycle — the 19.6-yr cycle dwarfs any real capture span, so same-era
    records always resolve to offset 0."""
    diag = _REF_2026 - timedelta(minutes=7)
    assert gc.gps_rollover_offset(diag, _REF_2026) == timedelta(0)
