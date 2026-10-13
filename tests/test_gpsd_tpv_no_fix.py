"""gpsd TPV/SKY mapping for no-fix PositionReport epochs.

Live on an RM520N-GL-AP (R01A08, no GNSS antenna) every 0x1476 epoch was a
v21 647 B pos_source=4 prediction at the 38N/117W cold-start seed with
gps_week=0xFFFF and the 599584.9375 no-DOP sentinel. GpsdServer published it
as a mode-2 fix at the seed, dated year 3236, with hdop 599584.9.
"""
from __future__ import annotations

import math

import pytest

from diaggrok.parsers.diag_0x1476 import Diag0x1476
from gpsd_server import GpsdServer

SENTINEL = 599584.9375


def _pos(**kw) -> Diag0x1476:
    base = dict(
        log_time=0, version=0x15, f_count=0, pos_source=4,
        gps_week=0xFFFF, gps_tow_ms=581273523,
        lat_rad=math.radians(38.0), lon_rad=math.radians(-117.0), alt_m=0.0,
        heading_rad=-999.0, heading_unc_rad=1.8,
        vel_e=0.0, vel_n=0.0, vel_u=0.0,
        vel_sigma_e=0.0, vel_sigma_n=0.0, vel_sigma_u=0.0,
        pdop=SENTINEL, hdop=SENTINEL, vdop=SENTINEL,
        num_gps_svs=0, total_gps_svs=0, num_glo_svs=0, total_glo_svs=15,
        num_bds_svs=0, total_bds_svs=18,
    )
    base.update(kw)
    return Diag0x1476(**base)


@pytest.fixture
def server(monkeypatch):
    s = GpsdServer(port=0)
    pushed: list[dict] = []
    monkeypatch.setattr(s, '_push_to_watchers', pushed.append)
    s.pushed = pushed
    return s


def test_cold_start_seed_is_mode_1_without_position(server):
    pos = _pos()
    assert pos.is_cold_start_seed
    server.push_position(pos)
    tpv = server.pushed[-1]
    assert tpv['mode'] == 1
    assert 'lat' not in tpv and 'lon' not in tpv


def test_prediction_epoch_off_seed_is_still_no_fix(server):
    server.push_position(_pos(lat_rad=math.radians(10.0), lon_rad=math.radians(20.0)))
    assert server.pushed[-1] == {'class': 'TPV', 'mode': 1}


def test_unknown_gps_week_emits_no_time(server):
    server.push_position(_pos(pos_source=2, hdop=1.1, vdop=1.4, pdop=1.8))
    assert 'time' not in server.pushed[-1]


def test_no_dop_sentinel_is_omitted_from_sky(server):
    server.push_position(_pos())
    server.push_sky(type('Empty', (), {'svs': []})())
    sky = server.pushed[-1]
    assert sky['class'] == 'SKY'
    assert not {'hdop', 'vdop', 'pdop'} & sky.keys()


@pytest.mark.parametrize('src,mode', [(2, 3), (8, 3), (1, 2), (3, 2)])
def test_real_fix_keeps_position_and_dop(server, src, mode):
    server.push_position(_pos(pos_source=src, gps_week=2400, gps_tow_ms=1000,
                              lat_rad=math.radians(10.0), lon_rad=math.radians(20.0),
                              num_gps_svs=8, hdop=0.9, vdop=1.3, pdop=1.6))
    tpv = server.pushed[-1]
    assert tpv['mode'] == mode
    assert tpv['lat'] == pytest.approx(10.0)
    assert tpv['epv'] == pytest.approx(1.3)
    assert tpv['time'].startswith('2026-')
    server.push_sky(type('Empty', (), {'svs': []})())
    assert server.pushed[-1]['hdop'] == pytest.approx(0.9)
