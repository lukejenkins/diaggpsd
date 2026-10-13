"""RINEX pseudoranges from 0x1477 / 0x1480 / OEM DRE transmit times.

The ``*_pseudorange_ms_*`` pair on these reports is the SV transmit time, not a
range. Written straight to RINEX it gave ~1.6e14 m, and every idle SV slot
(C/N0 0, predicted transmit time) became an observation. The records below are
synthetic, but each copies the shape of one firmware convention seen in
captures:

- LATENCY (SDX20 / MDM9x07 / MDM9x30 / MDM9x50): the transmit time sits
  ``latency`` ms before the report; tracked latencies are tens to a few hundred
  ms.
- BASE (SDX55 / SDX62): the transmit time is already at the report epoch;
  ``latency`` is small and negative or a large age counter, and must be ignored.

No real pseudorange set travels here: four of them plus a time fix a position.
"""
from __future__ import annotations

import io

import pytest

import gnss_packets
import rinex_writer as rw

C_KM_PER_MS = rw.C_LIGHT / 1e6


def _glo(ms, svs, time_bias=0.25):
    return {"type": "Diag0x1480", "log_time": 1000, "milliseconds": ms,
            "time_bias": time_bias, "svs": svs}


def _sv(sv_id, tx_int, tx_frac, latency, cno=30.0, obs_state=5):
    return {"sv_id": sv_id, "freq_index": 0, "cno_db": cno,
            "obs_state": obs_state, "latency": latency,
            "pseudorange_ms_integral": tx_int, "pseudorange_ms_fraction": tx_frac,
            "carrier_phase_integral": 0, "carrier_phase_fraction": 0,
            "speed_mps": 0.0}


# LATENCY shape. Report at 22_225_440 ms, bias 0.25 ms.
#   SV 70: tx 22_225_349.29, latency 19  -> base 90.46 (in window), latency 71.46
#   SV 71: tx 22_225_100.00, latency 268 -> base 339.75 (out),      latency 71.75
LATENCY_RECORD = _glo(22_225_440, [
    _sv(70, 22_225_349, 0.29, 19),
    _sv(71, 22_225_100, 0.00, 268),
    # idle slot: C/N0 0, predicted transmit time that still looks plausible
    _sv(72, 22_225_360, 0.10, 0, cno=0.0, obs_state=1),
])

# BASE shape. Report at 34_229_900 ms.
#   SV 65: tx 34_229_829.40, latency -27 -> base 70.35, latency 97.35 (in window)
#   SV 66: tx 34_229_825.10, latency 973 -> base 74.65, latency -898.35 (out)
BASE_RECORD = _glo(34_229_900, [
    _sv(65, 34_229_829, 0.40, -27),
    _sv(66, 34_229_825, 0.10, 973),
])


def _ranges_km(obs):
    return {o.prn: o.pseudorange_m / 1000 for o in obs if o.pseudorange_m is not None}


def test_latency_convention_is_learned_and_applied():
    r = rw.TxTimeResolver()
    obs = rw.record_to_svobs(LATENCY_RECORD, r)
    assert r.mode("Diag0x1480") == rw.TxTimeResolver.LATENCY
    km = _ranges_km(obs)
    assert km[6] == pytest.approx(71.46 * C_KM_PER_MS, abs=0.01)   # sv 70 -> slot 6
    assert km[7] == pytest.approx(71.75 * C_KM_PER_MS, abs=0.01)


def test_base_convention_is_learned_and_applied():
    r = rw.TxTimeResolver()
    obs = rw.record_to_svobs(BASE_RECORD, r)
    assert r.mode("Diag0x1480") == rw.TxTimeResolver.BASE
    km = _ranges_km(obs)
    assert km[1] == pytest.approx(70.35 * C_KM_PER_MS, abs=0.01)
    assert km[2] == pytest.approx(74.65 * C_KM_PER_MS, abs=0.01)


def test_idle_slots_are_not_observations():
    obs = rw.record_to_svobs(LATENCY_RECORD, rw.TxTimeResolver())
    assert sorted(o.prn for o in obs) == [6, 7]


def test_every_written_pseudorange_is_a_satellite_range():
    for rec in (LATENCY_RECORD, BASE_RECORD):
        for o in rw.record_to_svobs(rec, rw.TxTimeResolver()):
            assert 1.8e7 < o.pseudorange_m < 3.0e7, o


def test_undecided_stream_blanks_an_ambiguous_range_but_keeps_the_sv():
    # Only SV 70: base 90.46 and latency 71.46 are both plausible, so nothing
    # decides the convention and the pseudorange must stay blank.
    rec = _glo(22_225_440, [_sv(70, 22_225_349, 0.29, 19)])
    r = rw.TxTimeResolver()
    (obs,) = rw.record_to_svobs(rec, r)
    assert r.mode("Diag0x1480") is None
    assert obs.pseudorange_m is None
    assert obs.snr_dbhz == pytest.approx(30.0)


def test_a_learned_convention_carries_to_later_ambiguous_records():
    r = rw.TxTimeResolver()
    rw.record_to_svobs(LATENCY_RECORD, r)
    rec = _glo(22_226_440, [_sv(70, 22_226_349, 0.29, 19)])
    (obs,) = rw.record_to_svobs(rec, r)
    assert obs.pseudorange_m / 1000 == pytest.approx(71.46 * C_KM_PER_MS, abs=0.01)


def test_live_writer_keeps_one_resolver_across_ticks():
    gps = {"type": "Diag0x1477", "gps_week": 2400, "gps_milliseconds": 345_600_000,
           "time_bias": 0.0, "svs": []}
    later = dict(_glo(22_226_440, [_sv(70, 22_226_349, 0.29, 19)]), log_time=2000)
    out = io.StringIO()
    lw = rw.LiveRinexWriter(out, systems=("G", "R"))
    for rec in (dict(gps, log_time=1000), LATENCY_RECORD,
                dict(gps, log_time=2000, gps_milliseconds=345_601_000), later):
        lw.feed(rec)
    lw.close()
    rows = [ln for ln in out.getvalue().splitlines() if ln.startswith("R06")]
    assert len(rows) == 2
    # second tick's R06 row carries a C1C, learned from the first tick
    assert rows[1][3:17].strip() != ""


def test_oemdre_gps_pseudorange_is_report_time_minus_transmit_time():
    rec = {"type": "Diag0x14DE", "constellation": "GPS", "band": "L1",
           "gps_week": 2400, "gps_milliseconds": 72_689_442, "gps_time_bias": 0.0,
           "svs": [{"prn": 3, "cno_db": 25.0,
                    "filtered_pseudorange_ms_integral": 72_689_370,
                    "filtered_pseudorange_ms_fraction": 0.29,
                    "carrier_phase": 0, "fine_speed_mps": 0.0}]}
    (obs,) = rw.record_to_svobs(rec, rw.TxTimeResolver())
    assert obs.pseudorange_m / 1000 == pytest.approx(71.71 * C_KM_PER_MS, abs=0.01)


def test_every_measurement_report_class_has_an_extractor():
    """A parser class rename must not silently drop a constellation again.

    0x1480 and 0x1886 were renamed to ``Diag0xNNNN`` and their records then
    matched no extractor key, so every RINEX run dropped GLONASS and Galileo.
    """
    names = {getattr(gnss_packets, a).__name__ for a in dir(gnss_packets)
             if a.endswith("MeasurementReport")}
    assert names, "gnss_packets exposes no *MeasurementReport aliases"
    assert names <= set(rw._EXTRACTORS), names - set(rw._EXTRACTORS)
