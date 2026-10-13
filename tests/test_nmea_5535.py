"""diaggpsd tier-1 basic NMEA (GGA / RMC / GSA) from 0x1476.

Pure sentence tests on stand-in reports, a synthetic v13 0x1476 record through
the real diaggrok dispatch, and an end-to-end run of diaggpsd.py fed DGE1
datagrams over udp-listen. The replay against a modem's own NMEA needs a real
capture and lives in test_nmea_replay_5535.py (private; not carved).

Locations here are synthetic (an Atlantic point); no capture coordinates are
committed.
"""
from __future__ import annotations

import math
import re
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import nmea

APP = Path(__file__).resolve().parents[1]
REPO = APP.parents[1]
LAT, LON = 10.5, -20.25          # synthetic: open ocean
WEEK, TOW_MS = 2000, 3_600_500   # 2018-05-06 01:00:00.5 GPS = 00:59:42.5 UTC


def pos(**kw):
    base = dict(pos_source=2, gps_week=WEEK, gps_tow_ms=TOW_MS,
                lat_rad=math.radians(LAT), lon_rad=math.radians(LON), alt_m=123.44,
                heading_rad=math.radians(90.0), vel_e=5.0, vel_n=0.0, vel_u=0.0,
                pdop=1.5, hdop=0.9, vdop=1.2, num_gps_svs=7, num_glo_svs=4, num_bds_svs=0,
                is_cold_start_seed=False)
    base.update(kw)
    return SimpleNamespace(**base)


_DDMM = re.compile(r"^\d{4,5}\.\d{6}$")


def deg(v: str, hemi: str) -> float:
    """Parse an NMEA ``(d)ddmm.mmmmmm`` + hemisphere back to signed degrees."""
    assert _DDMM.match(v), v
    x = float(v)
    d = int(x // 100)
    m = x - d * 100
    assert m < 60.0, v
    return -(d + m / 60) if hemi in "SW" else d + m / 60


def assert_synthetic_position(g: list[str]) -> None:
    assert len(g[2].split(".")[0]) == 4 and len(g[4].split(".")[0]) == 5   # dd / ddd
    assert (g[3], g[5]) == ("N", "W")
    assert deg(g[2], g[3]) == pytest.approx(LAT, abs=1e-8)
    assert deg(g[4], g[5]) == pytest.approx(LON, abs=1e-8)


def fields(s: str) -> list[str]:
    assert s.startswith("$") and s.endswith("\r\n")
    body, cs = s[1:-2].split("*")
    assert cs == nmea.checksum(body)
    return body.split(",")


def test_checksum_known_vector():
    # an independent value: u-blox's antenna-status line, $GPTXT,01,01,02,ANTSTATUS=OK*3B
    # (every other test checks checksums with nmea.checksum itself)
    assert nmea.checksum("GPTXT,01,01,02,ANTSTATUS=OK") == "3B"


def test_fix_sentences():
    gga, rmc, gsa = (fields(s) for s in nmea.from_position(pos()))
    assert gga[0] == "GNGGA" and len(gga) == 15
    assert gga[1] == "005942.50"
    assert_synthetic_position(gga)
    assert gga[6:9] == ["1", "11", "0.9"]
    assert gga[9:13] == ["123.4", "M", "0.0", "M"]     # HAE, N = 0.0 (policy)
    assert rmc[0] == "GNRMC" and len(rmc) == 13
    assert rmc[2] == "A" and rmc[9] == "060518" and rmc[12] == "A"
    assert rmc[7] == f"{5.0 * nmea.MS_TO_KNOTS:.2f}" and rmc[8] == "90.0"
    assert gsa[0] == "GNGSA" and len(gsa) == 18
    assert gsa[1:3] == ["A", "3"] and gsa[3:15] == [""] * 12
    assert gsa[15:] == ["1.5", "0.9", "1.2"]


def test_2d_for_wls_fix():
    gsa = fields(nmea.from_position(pos(pos_source=1))[2])
    assert gsa[2] == "2"


@pytest.mark.parametrize("kw", [
    dict(pos_source=0), dict(pos_source=4), dict(is_cold_start_seed=True),
    dict(lat_rad=0.0, lon_rad=0.0), dict(lat_rad=float("nan")),
])
def test_no_fix_epochs_publish_no_position(kw):
    gga, rmc, gsa = (fields(s) for s in nmea.from_position(pos(**kw)))
    assert gga[2:6] == ["", "", "", ""] and gga[6] == "0" and gga[8:13] == [""] * 5
    assert rmc[2] == "V" and rmc[3:9] == [""] * 6 and rmc[12] == "N"
    assert gsa[2] == "1" and gsa[15:] == ["", "", ""]
    assert gga[1] == "005942.50"                  # time still known


def test_seed_method_form_is_honoured():
    p = pos()
    p.is_cold_start_seed = lambda: True           # diaggrok exposes a property;
    assert fields(nmea.from_position(p)[0])[6] == "0"   # a callable works too


def test_unknown_week_leaves_time_and_date_empty():
    gga, rmc, _ = (fields(s) for s in nmea.from_position(pos(gps_week=0xFFFF)))
    assert gga[1] == "" and rmc[1] == "" and rmc[9] == ""


def test_dop_sentinels_render_empty():
    gga, _, gsa = (fields(s) for s in nmea.from_position(
        pos(hdop=599584.9375, pdop=0.0, vdop=500.0)))
    assert gga[8] == "" and gsa[15:] == ["", "", ""]


def test_hemispheres_and_minute_rounding():
    gga = fields(nmea.from_position(pos(lat_rad=math.radians(-33.5),
                                        lon_rad=math.radians(151.0 - 1e-12)))[0])
    assert (gga[3], gga[5]) == ("S", "E")
    assert deg(gga[2], gga[3]) == pytest.approx(-33.5, abs=1e-8)
    # a hair under 151° must carry into 151° 0′, never print 60 minutes
    assert gga[4][:3] == "151" and float(gga[4][3:]) == 0.0


def test_satellite_count_is_capped_and_ignores_garbage():
    assert nmea.sats_used(pos(num_gps_svs=255, num_glo_svs=3)) == 3
    assert nmea.sats_used(pos(num_gps_svs=60, num_glo_svs=60, num_bds_svs=0)) == 99


# ---------------------------------------------------------------------------
# synthetic v13 record through the real decoder
# ---------------------------------------------------------------------------

def v13_record() -> bytes:
    d = bytearray(2477)
    d[0] = 13
    d[5] = 2                                          # Kalman fix
    struct.pack_into("<H", d, 32, WEEK)
    struct.pack_into("<I", d, 34, TOW_MS)
    struct.pack_into("<dd", d, 49, math.radians(LAT), math.radians(LON))
    struct.pack_into("<6f", d, 65, 123.44, math.radians(90.0), 0.0, 5.0, 0.0, 0.0)
    struct.pack_into("<3f", d, 301, 1.5, 0.9, math.sqrt(1.5 ** 2 - 0.9 ** 2))
    d[358:364] = bytes([7, 9, 4, 6, 0, 0])
    return bytes(d)


def log_frame(code: int, data: bytes) -> bytes:
    n = 12 + len(data)
    return struct.pack("<BBHHHQ", 0x10, 0, n, n, code, 0) + data


def hdlc(payload: bytes) -> bytes:
    from diagmunge.transport import _crc16
    raw = payload + struct.pack("<H", _crc16(payload))
    return raw.replace(b"\x7d", b"\x7d\x5d").replace(b"\x7e", b"\x7d\x5e") + b"\x7e"


def test_synthetic_v13_record_round_trips():
    import gnss_packets as gp
    pm, lt, ts, pl = gp.parse_outer_frame(log_frame(0x1476, v13_record())[1:])
    p = gp.dispatch(lt, ts, pl)
    assert isinstance(p, gp.PositionReport) and p.version == 13
    gga = fields(nmea.from_position(p)[0])
    assert_synthetic_position(gga)
    assert gga[6] == "1"
    assert gga[7] == "11" and gga[8] == "0.9"


# ---------------------------------------------------------------------------
# end to end: diaggpsd.py --nmea-out - over udp-listen
# ---------------------------------------------------------------------------

def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_diaggpsd_emits_nmea_on_stdout_end_to_end(tmp_path):
    port = _free_udp_port()
    nmea_file = tmp_path / "out.nmea"
    proc = subprocess.Popen(
        [sys.executable, str(APP / "diaggpsd.py"), "--diag-port", f"udp-listen:127.0.0.1:{port}",
         "--mask", "none", "--no-gpsd", "--nmea-out", "-", "--nmea-out", f"file:{nmea_file}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(APP))
    try:
        frame = hdlc(log_frame(0x1476, v13_record()))
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out = b""
        deadline = time.monotonic() + 30
        seq = 0
        os.set_blocking(proc.stdout.fileno(), False)
        while time.monotonic() < deadline and out.count(b"GNGSA") < 2:
            for i in range(0, len(frame), 1400):
                chunk = frame[i:i + 1400]
                tx.sendto(b"DGE1" + bytes([1, 1 if seq == 0 else 0]) + struct.pack("<IH", seq, len(chunk))
                          + chunk, ("127.0.0.1", port))
                seq += 1
            time.sleep(0.3)
            try:
                out += proc.stdout.read() or b""
            except BlockingIOError:
                pass
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    import gnss_packets as gp
    pm, lt, ts, pl = gp.parse_outer_frame(log_frame(0x1476, v13_record())[1:])
    want_gga = nmea.from_position(gp.dispatch(lt, ts, pl))[0].strip()
    lines = out.decode().splitlines()
    assert want_gga in lines, proc.stderr.read().decode()[-2000:]
    assert any(ln.startswith("$GNRMC,") for ln in lines)
    assert not any(ln.startswith("{") for ln in lines)            # JSON stdout is off
    assert "GNGGA" in nmea_file.read_text()


def test_tcp_and_pty_sinks(tmp_path):
    t = nmea.open_sink("tcp:127.0.0.1:0")
    c = socket.create_connection(("127.0.0.1", t.port))
    time.sleep(0.2)                                  # accept thread picks it up
    text = "".join(nmea.from_position(pos()))
    t.write(text)
    c.settimeout(3)
    got = b""
    while got.count(b"\r\n") < 3:
        got += c.recv(4096)
    assert got.decode() == text
    t.close()
    link = tmp_path / "gps0"
    p = nmea.open_sink(f"pty:{link}")
    fd = os.open(str(link), os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    p.write(text)
    time.sleep(0.1)
    assert os.read(fd, 4096).decode() == text
    os.close(fd)
    for _ in range(2000):                            # no reader: never blocks
        p.write(text)
    p.close()
    assert not link.exists()
