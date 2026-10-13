"""diaggpsd as a diagbroker client: ``--diag-port broker[:HOST[:PORT]]``.

The broker owns the mask, so the mode is passive (``--mask none``). diaggpsd
declares exactly ``required_codes()`` for its enabled outputs, and the OK line
is stripped before the HDLC reader sees the stream.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import diaggpsd
import nmea
from test_nmea_5535 import hdlc, log_frame, v13_record

APP = Path(__file__).resolve().parents[1]


def test_parse_broker_spec():
    assert diaggpsd.parse_diag_port("broker") == ("broker", "127.0.0.1", 2700)
    assert diaggpsd.parse_diag_port("broker:10.0.0.5") == ("broker", "10.0.0.5", 2700)
    assert diaggpsd.parse_diag_port("broker:10.0.0.5:2701") == ("broker", "10.0.0.5", 2701)


def test_broker_is_passive():
    assert diaggpsd.default_mask_mode("broker") == "none"


def test_declaration_is_exactly_the_required_codes():
    codes = diaggpsd.required_codes(gpsd=False, json_out=False, rinex=False)
    assert diaggpsd.broker_declaration(codes) == {"name": "diaggpsd", "log_codes": [0x1476]}
    codes = diaggpsd.required_codes(gpsd=True, json_out=False, rinex=False)
    assert diaggpsd.broker_declaration(codes)["log_codes"] == sorted(codes)


def test_mask_own_is_refused_on_a_broker():
    r = subprocess.run([sys.executable, str(APP / "diaggpsd.py"), "--diag-port", "broker:127.0.0.1:9",
                        "--mask", "own", "--no-gpsd"], capture_output=True, text=True, cwd=str(APP),
                       timeout=60)
    assert r.returncode == 2
    assert "broker owns the mask" in r.stderr


def test_end_to_end_behind_a_fake_broker():
    """SUB carries only 0x1476; NMEA comes out of the frames after the OK line."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got: dict = {}
    stop = threading.Event()
    frame = hdlc(log_frame(0x1476, v13_record()))

    def broker():
        srv.settimeout(30)
        c, _ = srv.accept()
        buf = b""
        while b"\n" not in buf:
            buf += c.recv(4096)
        got["sub"] = buf
        # first frame in the same segment as the OK line, as a real broker may
        c.sendall(b'OK {"client_id":1,"effective_mask_epoch":0}\n' + frame)
        while not stop.is_set():
            try:
                c.sendall(frame)
            except OSError:
                break
            time.sleep(0.3)
        c.close()

    th = threading.Thread(target=broker, daemon=True)
    th.start()
    proc = subprocess.Popen(
        [sys.executable, str(APP / "diaggpsd.py"), "--diag-port", f"broker:127.0.0.1:{port}",
         "--no-gpsd", "--nmea-out", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(APP))
    out = b""
    try:
        os.set_blocking(proc.stdout.fileno(), False)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and b"GNGGA" not in out:
            time.sleep(0.2)
            try:
                out += proc.stdout.read() or b""
            except BlockingIOError:
                pass
    finally:
        stop.set()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        srv.close()
    assert got["sub"].startswith(b"SUB ")
    assert json.loads(got["sub"][4:]) == {"name": "diaggpsd", "log_codes": [0x1476]}
    import gnss_packets as gp
    pm, lt, ts, pl = gp.parse_outer_frame(log_frame(0x1476, v13_record())[1:])
    want = nmea.from_position(gp.dispatch(lt, ts, pl))[0].strip()
    assert want in out.decode().splitlines(), proc.stderr.read().decode()[-2000:]
