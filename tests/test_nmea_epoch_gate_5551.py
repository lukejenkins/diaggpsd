"""NMEA epoch gate: one sentence set per epoch, from one solution.

The RM500Q-AE emits four 0x1476 records per second: the combined fix
(pos_source 8) twice, byte-identical, a Kalman fix (2) 447 ms later whose
altitude differs by metres, and a pos_source-4 record whose TOW is stuck hours
in the past. Without a gate, --nmea-out wrote all four, so a consumer saw a
duplicate GGA, an altitude that jittered, and a no-fix GGA with a wrong time
every second. The replay against that capture is in test_nmea_replay_5535.py.
"""
from __future__ import annotations

import pytest

import nmea
from test_nmea_5535 import WEEK, pos

TOW_MS = 56_981_000       # the capture's live TOW
STUCK_TOW = 42_443_451    # its stuck pos_source-4 TOW: behind the live one
AHEAD_TOW = 99_000_000    # a stuck TOW can as easily sit ahead


def gate_run(gate, items):
    """Feed ``(now, report)`` pairs; return the accepted reports' (source, tow)."""
    return [(p.pos_source, p.gps_tow_ms) for now, p in items if gate.accept(p, now)]


def rm500q_second(t: float, tow: int):
    """One second of the RM500Q-AE pattern, starting at monotonic time ``t``."""
    return [(t, pos(pos_source=8, gps_tow_ms=tow)),
            (t + 0.001, pos(pos_source=8, gps_tow_ms=tow)),
            (t + 0.447, pos(pos_source=2, gps_tow_ms=tow + 447, alt_m=116.0)),
            (t + 0.448, pos(pos_source=4, gps_tow_ms=STUCK_TOW, lat_rad=0.0, lon_rad=0.0))]


def test_rm500q_pattern_gives_one_combined_fix_per_second():
    g = nmea.EpochGate()
    items = [x for s in range(5) for x in rm500q_second(100.0 + s, TOW_MS + 1000 * s)]
    assert gate_run(g, items) == [(8, TOW_MS + 1000 * s) for s in range(5)]


def test_identical_duplicate_is_dropped():
    g = nmea.EpochGate()
    assert gate_run(g, [(0.0, pos(gps_tow_ms=TOW_MS)), (0.04, pos(gps_tow_ms=TOW_MS))]) == [(2, TOW_MS)]


@pytest.mark.parametrize("stuck", [STUCK_TOW, AHEAD_TOW])
def test_stuck_no_fix_is_dropped_while_the_fix_is_fresh(stuck):
    g = nmea.EpochGate()
    got = gate_run(g, [(0.0, pos(gps_tow_ms=TOW_MS)),
                       (0.5, pos(pos_source=4, gps_tow_ms=stuck)),
                       (1.0, pos(gps_tow_ms=TOW_MS + 1000))])
    assert got == [(2, TOW_MS), (2, TOW_MS + 1000)]


def test_time_going_backwards_is_dropped_while_the_stream_is_fresh():
    g = nmea.EpochGate()
    got = gate_run(g, [(0.0, pos(pos_source=0, gps_tow_ms=TOW_MS)),
                       (0.5, pos(pos_source=0, gps_tow_ms=STUCK_TOW))])
    assert got == [(0, TOW_MS)]


def test_a_second_fix_source_is_dropped_while_the_first_is_fresh():
    g = nmea.EpochGate()
    got = gate_run(g, [(0.0, pos(pos_source=8, gps_tow_ms=TOW_MS)),
                       (0.4, pos(pos_source=2, gps_tow_ms=TOW_MS + 400))])
    assert got == [(8, TOW_MS)]


def test_switches_source_when_the_first_goes_quiet():
    g = nmea.EpochGate()
    got = gate_run(g, [(0.0, pos(pos_source=8, gps_tow_ms=TOW_MS)),
                       (2.0, pos(pos_source=2, gps_tow_ms=TOW_MS + 2000)),
                       (2.5, pos(pos_source=1, gps_tow_ms=TOW_MS + 2500)),
             (3.0, pos(pos_source=8, gps_tow_ms=TOW_MS + 3000))])
    # locked to 2: the worse 1 is dropped, the better 8 takes the lock back
    assert got == [(8, TOW_MS), (2, TOW_MS + 2000), (8, TOW_MS + 3000)]


def test_no_fix_only_stream_still_heartbeats():
    """A stuck-time no-fix stream must not go silent: gpsd needs to see 'no fix'."""
    g = nmea.EpochGate()
    items = [(float(s), pos(pos_source=4, gps_tow_ms=STUCK_TOW)) for s in range(6)]
    got = gate_run(g, items)
    assert 2 <= len(got) <= 4
    assert got[0] == (4, STUCK_TOW)


def test_loss_of_fix_is_reported_after_the_holdover():
    g = nmea.EpochGate()
    items = [(0.0, pos(pos_source=8, gps_tow_ms=TOW_MS)),
             (1.0, pos(pos_source=4, gps_tow_ms=STUCK_TOW)),
             (2.0, pos(pos_source=4, gps_tow_ms=STUCK_TOW))]
    assert gate_run(g, items) == [(8, TOW_MS), (4, STUCK_TOW)]


def test_loss_of_fix_with_advancing_time_waits_for_the_holdover():
    """Time alone never outranks a fresh fix, so even a well-timed no-fix waits."""
    g = nmea.EpochGate()
    items = [(0.0, pos(pos_source=8, gps_tow_ms=TOW_MS)),
             (1.0, pos(pos_source=0, gps_tow_ms=TOW_MS + 1000)),
             (2.0, pos(pos_source=0, gps_tow_ms=TOW_MS + 2000)),
             (3.0, pos(pos_source=0, gps_tow_ms=TOW_MS + 3000))]
    assert gate_run(g, items) == [(8, TOW_MS), (0, TOW_MS + 2000), (0, TOW_MS + 3000)]


def test_week_rollover_is_forward():
    g = nmea.EpochGate()
    items = [(0.0, pos(gps_week=WEEK, gps_tow_ms=604_799_000)),
             (1.0, pos(gps_week=WEEK + 1, gps_tow_ms=0))]
    assert len(gate_run(g, items)) == 2


@pytest.mark.parametrize("week", [0, 0xFFFF])
def test_unknown_week_then_known_week_is_forward(week):
    g = nmea.EpochGate()
    items = [(0.0, pos(pos_source=0, gps_week=week, gps_tow_ms=TOW_MS + 5000)),
             (1.0, pos(pos_source=8, gps_week=WEEK, gps_tow_ms=TOW_MS))]
    assert len(gate_run(g, items)) == 2


def test_combined_fix_takes_over_whichever_source_arrived_first():
    """Live, the Kalman (2) record can be the first one after startup. The
    combined (8) solution is the one the modem's own GGA matches, so it takes
    the lock at the next epoch and keeps it."""
    g = nmea.EpochGate()
    items = [(0.0, pos(pos_source=2, gps_tow_ms=TOW_MS + 447))]
    items += [x for s in range(1, 4) for x in rm500q_second(float(s), TOW_MS + 1000 * s)]
    assert gate_run(g, items) == [(2, TOW_MS + 447)] + [(8, TOW_MS + 1000 * s) for s in range(1, 4)]


def test_a_worse_source_never_takes_over_a_fresh_better_one():
    g = nmea.EpochGate()
    items = [(0.0, pos(pos_source=8, gps_tow_ms=TOW_MS)),
             (0.5, pos(pos_source=1, gps_tow_ms=TOW_MS + 500)),
             (0.6, pos(pos_source=2, gps_tow_ms=TOW_MS + 600))]
    assert gate_run(g, items) == [(8, TOW_MS)]


def test_equal_rank_sources_do_not_flap():
    """Injected (3) and an unknown source both rank 0: neither takes the other's lock."""
    g = nmea.EpochGate()
    items = [(0.0, pos(pos_source=3, gps_tow_ms=TOW_MS)),
             (0.5, pos(pos_source=6, gps_tow_ms=TOW_MS + 500)),
             (1.0, pos(pos_source=3, gps_tow_ms=TOW_MS + 1000))]
    assert gate_run(g, items) == [(3, TOW_MS), (3, TOW_MS + 1000)]
