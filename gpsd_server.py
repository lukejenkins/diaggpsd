"""GpsdServer — gpsd-compatible TCP server on port 2947.

Implements a subset of the gpsd JSON protocol (version 3.14) sufficient
for standard clients such as gpspipe, cgps, and GNSS analysis tools.

Protocol summary:
  On connect  → send VERSION
  On ?WATCH=  → send WATCH ack, begin streaming TPV/SKY to this client
  Push        → send TPV (position) and SKY (satellite view) to all watchers

GPS epoch:  1980-01-06 00:00:00 UTC
Leap seconds: 18 (valid since June 2017; update when a new leap second occurs)

Usage:
    srv = GpsdServer(port=2947)
    srv.start()
    srv.push_position(pos_report)
    srv.push_sky(meas_report)
    srv.stop()
"""

import datetime
import json
import logging
import math
import socket
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gnss_packets import (GlonassMeasurementReport, GpsMeasurementReport,
                               PositionReport)

logger = logging.getLogger(__name__)

# GPS epoch and leap seconds for UTC conversion
_GPS_EPOCH = datetime.datetime(1980, 1, 6, 0, 0, 0, tzinfo=datetime.timezone.utc)
_LEAP_SECONDS = 18  # as of June 2017


def _valid_dop(value: float | None) -> float | None:
    """Return *value* if it is a real DOP, else None (0.0 / sentinel = no DOP)."""
    if value is None or not 0.0 < value < 100.0:
        return None
    return value


def _gps_to_utc_iso(week: int, tow_ms: int) -> str:
    """Convert GPS week + time-of-week (ms) to ISO 8601 UTC string."""
    dt = (_GPS_EPOCH
          + datetime.timedelta(weeks=week)
          + datetime.timedelta(seconds=tow_ms * 1e-3 - _LEAP_SECONDS))
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


class GpsdServer:
    """Threaded gpsd-compatible TCP server."""

    VERSION_MSG = {
        'class': 'VERSION',
        'release': '0.1',
        'rev': 'diaggpsd-0.1',
        'proto_major': 3,
        'proto_minor': 14,
    }

    def __init__(self, host: str = '0.0.0.0', port: int = 2947):
        self._host = host
        self._port = port
        self._server_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._running = False

        self._watchers: set[socket.socket] = set()
        self._watchers_lock = threading.Lock()

        # Last known DOP values (updated from PositionReport); None = no DOP
        self._hdop: float | None = None
        self._vdop: float | None = None
        self._pdop: float | None = None

    def start(self) -> None:
        """Start the server in a background accept thread."""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self._host, self._port))
        self._server_sock.listen(8)
        self._server_sock.settimeout(1.0)
        self._running = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name='gpsd-accept')
        self._accept_thread.start()
        logger.info(f"gpsd server listening on {self._host}:{self._port}")

    def stop(self) -> None:
        """Stop the server and close all connections."""
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
        with self._watchers_lock:
            for sock in list(self._watchers):
                try:
                    sock.close()
                except Exception:
                    pass
            self._watchers.clear()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            logger.debug(f"gpsd client connected from {addr}")
            t = threading.Thread(
                target=self._client_handler, args=(conn, addr),
                daemon=True, name=f'gpsd-client-{addr[1]}')
            t.start()

    def _send_json(self, sock: socket.socket, obj: dict) -> bool:
        """Send a JSON line to a client. Returns False if the send fails."""
        try:
            data = (json.dumps(obj, separators=(',', ':')) + '\r\n').encode()
            sock.sendall(data)
            return True
        except OSError:
            return False

    def _client_handler(self, conn: socket.socket, addr) -> None:
        """Handle a single gpsd client connection."""
        conn.settimeout(None)
        # Send VERSION on connect
        if not self._send_json(conn, self.VERSION_MSG):
            conn.close()
            return

        buf = b''
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    self._handle_command(conn, line.decode(errors='replace'))
        except OSError:
            pass
        finally:
            with self._watchers_lock:
                self._watchers.discard(conn)
            conn.close()
            logger.debug(f"gpsd client {addr} disconnected")

    def _handle_command(self, conn: socket.socket, command: str) -> None:
        """Process a single gpsd client command."""
        if command.startswith('?WATCH'):
            # Parse optional JSON argument: ?WATCH={"enable":true,...}
            enable = True
            if '=' in command:
                try:
                    args = json.loads(command.split('=', 1)[1])
                    enable = args.get('enable', True)
                except (json.JSONDecodeError, IndexError):
                    pass

            if enable:
                with self._watchers_lock:
                    self._watchers.add(conn)
                watch_ack = {
                    'class': 'WATCH',
                    'enable': True,
                    'json': True,
                    'nmea': False,
                    'raw': 0,
                    'scaled': False,
                    'timing': False,
                }
                self._send_json(conn, watch_ack)
                logger.debug("gpsd client subscribed to WATCH")
            else:
                with self._watchers_lock:
                    self._watchers.discard(conn)

        elif command.startswith('?DEVICES') or command.startswith('?DEVICE'):
            devices = {
                'class': 'DEVICES',
                'devices': [{'class': 'DEVICE', 'path': 'diaggpsd', 'activated': True}],
            }
            self._send_json(conn, devices)

        elif command.startswith('?POLL'):
            # Respond with empty poll ack; we push data asynchronously
            self._send_json(conn, {'class': 'POLL', 'time': '', 'active': 0, 'tpv': [], 'sky': []})

    def _push_to_watchers(self, obj: dict) -> None:
        """Broadcast a JSON object to all current watchers."""
        dead: list[socket.socket] = []
        with self._watchers_lock:
            for sock in list(self._watchers):
                if not self._send_json(sock, obj):
                    dead.append(sock)
        if dead:
            with self._watchers_lock:
                for sock in dead:
                    self._watchers.discard(sock)
                    try:
                        sock.close()
                    except Exception:
                        pass

    # --- Public push API ---

    def push_position(self, pos: 'PositionReport') -> None:
        """Convert a PositionReport to a TPV message and push to all watchers.

        Also updates the internally cached DOP values used by push_sky().
        """
        # Cache DOP for SKY messages. 0.0 and the 599584.9375 / 500.0 sentinels
        # mean "no DOP this epoch" (same window as Diag0x1476.to_dict).
        self._hdop = _valid_dop(pos.hdop)
        self._vdop = _valid_dop(pos.vdop)
        self._pdop = _valid_dop(pos.pdop)

        # pos_source: 0=None 1=WLS 2=Kalman 3=Injected 4=DB(prediction) 8=combined.
        # 0 and 4 are not satellite fixes: a pos_source=4 epoch carries the
        # engine's prediction / cold-start seed (38N 117W) and must not be
        # published as a fix. An RM520N-GL-AP with no antenna emitted a
        # mode-2 TPV at the Nevada seed.
        no_fix = (pos.pos_source in (0, 4)
                  or getattr(pos, 'is_cold_start_seed', False))
        if no_fix:
            tpv: dict = {'class': 'TPV', 'mode': 1}
        else:
            mode = 3 if pos.pos_source in (2, 8) else 2
            speed = math.sqrt(pos.vel_e ** 2 + pos.vel_n ** 2)
            tpv = {
                'class': 'TPV',
                'mode': mode,
                'lat': pos.lat_rad * 180.0 / math.pi,
                'lon': pos.lon_rad * 180.0 / math.pi,
                'alt': pos.alt_m,
                'altHAE': pos.alt_m,
                'speed': speed,
                'track': pos.heading_rad * 180.0 / math.pi,
            }
            if self._vdop is not None:
                tpv['epv'] = self._vdop

        # gps_week 0xFFFF = week unknown (rendered as year 3236 otherwise)
        if 0 < pos.gps_week < 0xFFFF:
            try:
                tpv['time'] = _gps_to_utc_iso(pos.gps_week, pos.gps_tow_ms)
            except Exception:
                pass

        self._push_to_watchers(tpv)

    # gpsd gnssid encoding (gpsd protocol 3.14+)
    _GNSSID_GPS = 0
    _GNSSID_GALILEO = 2
    _GNSSID_BEIDOU = 3
    _GNSSID_GLONASS = 6

    # OEM DRE sequence number to gpsd gnssid mapping
    _OEMDRE_SEQ_TO_GNSSID = {
        1: _GNSSID_GPS,
        2: _GNSSID_GLONASS,
        3: _GNSSID_GALILEO,
        4: _GNSSID_BEIDOU,
    }

    def push_sky(self, meas) -> None:
        """Convert a measurement report to a SKY message and push to all watchers.

        Accepts GpsMeasurementReport, GlonassMeasurementReport, or
        OemdreMeasurementReport. Uses DOP values from the most recent
        push_position() call.

        A satellite is considered "used" if its carrier_noise > 0 and the
        measurementNotUsable bit (bit 13) is not set in measurement_status.
        """
        from gnss_packets import OemdreMeasurementReport
        sats = []

        if isinstance(meas, OemdreMeasurementReport):
            gnssid = self._OEMDRE_SEQ_TO_GNSSID.get(meas.seq_num)
            if gnssid is None:
                return  # Unknown constellation (L5 candidates)
            for sv in meas.svs:
                used = bool(sv.carrier_noise > 0
                            and not (sv.measurement_status & (1 << 13)))
                az_deg = sv.azimuth * 180.0 / math.pi
                el_deg = sv.elevation * 180.0 / math.pi
                sat: dict = {
                    'PRN': sv.sv_id,
                    'gnssid': gnssid,
                    'az': round(az_deg, 1),
                    'el': round(el_deg, 1),
                    'ss': sv.carrier_noise * sv.cno_divisor,
                    'used': used,
                }
                sats.append(sat)
        else:
            for sv in meas.svs:
                used = bool(sv.carrier_noise > 0
                            and not (sv.measurement_status & (1 << 13)))
                az_deg = sv.azimuth * 180.0 / math.pi
                el_deg = sv.elevation * 180.0 / math.pi
                sat: dict = {
                    'PRN': sv.sv_id,
                    'az': round(az_deg, 1),
                    'el': round(el_deg, 1),
                    'ss': sv.carrier_noise * 0.1,
                    'used': used,
                }
                # Include GLONASS frequency slot if present
                if hasattr(sv, 'frequency_index'):
                    sat['gnssid'] = self._GNSSID_GLONASS
                    sat['freqid'] = sv.frequency_index
                else:
                    sat['gnssid'] = self._GNSSID_GPS
                sats.append(sat)

        sky: dict = {'class': 'SKY'}
        for key, val in (('hdop', self._hdop), ('vdop', self._vdop),
                         ('pdop', self._pdop)):
            if val is not None:
                sky[key] = val
        sky['satellites'] = sats
        self._push_to_watchers(sky)
