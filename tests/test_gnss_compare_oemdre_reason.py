"""Tests for gnss_compare.oemdre_no_comparison_reason.

The OEM DRE SV-comparison block falls through to a "zero comparisons" branch
for three structurally distinct reasons, which the original code mislabelled
uniformly as a "time sync issue". The Galileo case is actually
*empty OEM DRE shells* (constellation tracked in RTCM MSM7 but never decoded,
so the Decoded Range Engine emits reports carrying zero per-SV measurements) —
not a timing offset. These tests lock the disambiguation contract.

The conftest in this directory puts apps/diaggpsd on sys.path.
"""
from __future__ import annotations

import gnss_compare as gc


def test_empty_shells_not_blamed_on_timing():
    """The Galileo case: reports exist, but none carry per-SV data
    (reports_with_svs == 0). Must read as empty shells / tracked-but-decoded,
    explicitly NOT a timing issue."""
    msg = gc.oemdre_no_comparison_reason(n_reports=332, reports_with_svs=0,
                                         matched=0)
    assert "empty shells" in msg
    assert "not a timing issue" in msg
    assert "0 per-SV measurements" in msg
    # The old, wrong label must not appear for this cause.
    assert "time sync issue" not in msg
    assert "332 reports" in msg


def test_genuine_time_sync_when_svs_present_but_unmatched():
    """Reports DO carry SV data but none time-align to a reference SKY epoch
    (matched == 0). This is the only case the 'time sync issue' label is
    correct for — and it must still surface it."""
    msg = gc.oemdre_no_comparison_reason(n_reports=40, reports_with_svs=37,
                                         matched=0)
    assert "time sync issue" in msg
    assert "37 with SV data" in msg
    assert "empty shells" not in msg


def test_matched_but_no_overlapping_reference_svs():
    """Reports time-matched, but no usable reference SVs (no gpsd counterpart
    or all filtered). Neither empty-shell nor a timing problem."""
    msg = gc.oemdre_no_comparison_reason(n_reports=50, reports_with_svs=50,
                                         matched=12)
    assert "12 time-matched" in msg
    assert "no overlapping reference SVs" in msg
    assert "empty shells" not in msg
    assert "time sync issue" not in msg


def test_three_causes_produce_distinct_messages():
    """The whole point of the fix: the three causes must be distinguishable,
    not collapsed into one string."""
    empty = gc.oemdre_no_comparison_reason(10, 0, 0)
    timing = gc.oemdre_no_comparison_reason(10, 8, 0)
    no_ref = gc.oemdre_no_comparison_reason(10, 10, 5)
    assert len({empty, timing, no_ref}) == 3
