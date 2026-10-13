"""A measurement report from a receiver with no time has no RINEX epoch.

Before the receiver resolves time, 0x1477 reports GPS week 0xFFFF and 0x1480
reports GLONASS cycle 255, day 0xFFFF, with ``clock_time_unc`` near 3e18 ms.
The report ms is then a free-running count, so no transmit-time convention can
turn it into a range. Taken literally, week 0xFFFF dated the RINEX file in the
year 3236. The records below are synthetic copies of that header shape.
"""
from __future__ import annotations

import io

import rinex_writer as rw


def _gps(log_time, week, ms=10_305_517):
    return {"type": "Diag0x1477", "log_time": log_time, "gps_week": week,
            "gps_milliseconds": ms, "time_bias": -0.02,
            "clock_time_unc": 3.1536e18,
            "svs": [{"sv_id": 4, "cno_db": 24.0, "latency": 6300,
                     "pseudorange_ms_integral": 10_299_216,
                     "pseudorange_ms_fraction": 0.6,
                     "carrier_phase_integral": 0, "carrier_phase_fraction": 0,
                     "fine_speed": 0.0}]}


def _glo_unknown(log_time, ms=10_305_517):
    return {"type": "Diag0x1480", "log_time": log_time,
            "glonass_cycle_number": 255, "glonass_days": 0xFFFF,
            "milliseconds": ms, "time_bias": -0.02,
            "svs": [{"sv_id": 255, "freq_index": -7, "cno_db": 34.7,
                     "latency": 400, "obs_state": 4,
                     "pseudorange_ms_integral": 10_305_116,
                     "pseudorange_ms_fraction": 0.7,
                     "carrier_phase_integral": 0, "carrier_phase_fraction": 0,
                     "speed_mps": 0.0}]}


def test_gps_week_unknown_has_no_epoch():
    assert rw.group_records_by_epoch([_gps(1000, rw.GPS_WEEK_UNKNOWN)]) == []


def test_glonass_time_unknown_has_no_epoch():
    assert rw.group_records_by_epoch([_glo_unknown(1000)]) == []


def test_glonass_cannot_borrow_an_unknown_gps_epoch_at_its_tick():
    recs = [_gps(1000, rw.GPS_WEEK_UNKNOWN), _glo_unknown(1000)]
    assert rw.group_records_by_epoch(recs) == []


def test_a_real_week_is_still_an_epoch():
    (ep,) = rw.group_records_by_epoch([_gps(1000, 2418)])
    assert (ep.gps_week, ep.gps_tow_ms) == (2418, 10_305_517)


def test_live_writer_writes_nothing_for_a_receiver_without_time():
    out = io.StringIO()
    lw = rw.LiveRinexWriter(out, systems=("G", "R"))
    for tick in (1000, 2000, 3000):
        lw.feed(_gps(tick, rw.GPS_WEEK_UNKNOWN))
        lw.feed(_glo_unknown(tick))
    lw.close()
    assert lw.epochs_written == 0
    assert out.getvalue() == ""
