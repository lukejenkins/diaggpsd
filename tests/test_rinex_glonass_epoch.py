"""GLONASS 0x1480 epochs from GLONASS time, not from a shared DIAG tick.

``group_records_by_epoch`` used to place a 0x1480 only when a GPS-time record
had the identical ``log_time``. SDX62 logs a measurement's 0x1480 about 1,700
ticks after its 0x1477, so every GLONASS row was dropped. The 0x1480 header
carries GLONASS time (four-year cycle N4, day in cycle N_T, ms of day in
UTC(SU) = UTC + 3 h), which converts to GPS time without the tick.

The records below are synthetic. Their time fields copy the relationship seen
in captures: the same instant is GPS week 2418, TOW 455_452_893 ms and GLONASS
cycle 8, day 866, 34_234_893 ms, with GPS - UTC = 18 s.
"""
from __future__ import annotations

import io

import rinex_writer as rw

GPS_WEEK, GPS_MS = 2418, 455_452_893
GLO_CYCLE, GLO_DAY, GLO_MS = 8, 866, 34_234_893


def _gps(log_time, ms=GPS_MS, time_bias=0.49):
    return {"type": "Diag0x1477", "log_time": log_time, "gps_week": GPS_WEEK,
            "gps_milliseconds": ms, "time_bias": time_bias,
            "svs": [{"sv_id": 5, "cno_db": 40.0, "latency": 0,
                     "pseudorange_ms_integral": 0, "pseudorange_ms_fraction": 0.0,
                     "carrier_phase_integral": 0, "carrier_phase_fraction": 0,
                     "fine_speed": 0.0}]}


def _glo(log_time, ms=GLO_MS, time_bias=0.49, cno=30.0):
    # One tracked SV under the BASE convention: travel 70.35 ms.
    tx = ms - time_bias - 70.35
    return {"type": "Diag0x1480", "log_time": log_time,
            "glonass_cycle_number": GLO_CYCLE, "glonass_days": GLO_DAY,
            "milliseconds": ms, "time_bias": time_bias,
            "svs": [{"sv_id": 65, "freq_index": 0, "cno_db": cno,
                     "obs_state": 5, "latency": 973,
                     "pseudorange_ms_integral": int(tx),
                     "pseudorange_ms_fraction": tx - int(tx),
                     "carrier_phase_integral": 0, "carrier_phase_fraction": 0,
                     "speed_mps": 0.0}]}


def test_glonass_time_converts_to_the_gps_epoch_of_the_same_instant():
    assert rw.glonass_to_gps_epoch(GLO_CYCLE, GLO_DAY, GLO_MS) == (GPS_WEEK, GPS_MS)


def test_glonass_conversion_uses_the_leap_seconds_of_its_date():
    # Noon UTC on 1 June 2016, when GPS - UTC was 17 s: the GLONASS ms of
    # day 15:00:00.000 (UTC + 3 h) is GPS 12:00:17. Cycle 6 opens on
    # 1 January 2016 (N_T 1), so 1 June is day 153.
    week, tow = rw.glonass_to_gps_epoch(6, 153, 15 * 3_600_000)
    gps = rw.GPS_EPOCH + rw.timedelta(weeks=week, milliseconds=tow)
    assert (gps.month, gps.day, gps.hour, gps.minute, gps.second) == (6, 1, 12, 0, 17)


def test_an_unset_or_impossible_glonass_date_has_no_epoch():
    assert rw.glonass_to_gps_epoch(0, GLO_DAY, GLO_MS) is None
    assert rw.glonass_to_gps_epoch(GLO_CYCLE, 0, GLO_MS) is None
    assert rw.glonass_to_gps_epoch(GLO_CYCLE, 1462, GLO_MS) is None
    assert rw.glonass_to_gps_epoch(GLO_CYCLE, GLO_DAY, 86_400_000) is None


def test_glonass_record_with_a_different_tick_joins_the_gps_epoch():
    (ep,) = rw.group_records_by_epoch([_gps(1000), _glo(2700)])
    assert (ep.gps_week, ep.gps_tow_ms) == (GPS_WEEK, GPS_MS)
    (obs,) = [o for o in ep.observations if o.system == "R"]
    assert obs.prn == 1
    assert 1.8e7 < obs.pseudorange_m < 3.0e7


def test_one_instant_split_across_a_ms_boundary_is_one_epoch():
    # The receiver clock bias is near 0.5 ms, so GPS rounds to ...893 and
    # GLONASS to ...892. ms - time_bias is the same instant to ~5 us.
    recs = [_gps(1000, time_bias=0.496), _glo(2700, ms=GLO_MS - 1, time_bias=-0.499)]
    (ep,) = rw.group_records_by_epoch(recs)
    assert (ep.gps_week, ep.gps_tow_ms) == (GPS_WEEK, GPS_MS)
    assert any(o.system == "R" for o in ep.observations)


def test_a_glonass_report_one_ms_away_at_another_instant_keeps_its_epoch():
    # Same integer ms neighbour, but the bias-corrected instants are 1 ms apart.
    recs = [_gps(1000, time_bias=0.1), _glo(2700, ms=GLO_MS - 1, time_bias=0.1)]
    epochs = rw.group_records_by_epoch(recs)
    assert [(e.gps_week, e.gps_tow_ms) for e in epochs] == [
        (GPS_WEEK, GPS_MS - 1), (GPS_WEEK, GPS_MS)]


def test_glonass_without_time_still_borrows_a_same_tick_gps_epoch():
    rec = _glo(1000)
    del rec["glonass_cycle_number"], rec["glonass_days"]
    (ep,) = rw.group_records_by_epoch([_gps(1000), rec])
    assert any(o.system == "R" for o in ep.observations)


def test_live_writer_writes_one_epoch_for_reports_on_different_ticks():
    out = io.StringIO()
    lw = rw.LiveRinexWriter(out, systems=("G", "R"))
    for rec in (_gps(1000), _glo(2700),
                _gps(1_230_000, ms=GPS_MS + 1000), _glo(1_231_700, ms=GLO_MS + 1000)):
        lw.feed(rec)
    lw.close()
    body = out.getvalue().split("END OF HEADER")[1].splitlines()
    epochs = [ln for ln in body if ln.startswith(">")]
    assert lw.epochs_written == 2
    assert len(epochs) == 2
    assert sum(ln.startswith("R01") for ln in body) == 2
    assert sum(ln.startswith("G05") for ln in body) == 2
