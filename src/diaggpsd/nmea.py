"""Basic NMEA 0183 sentences (GGA, RMC, GSA) from a 0x1476 PositionReport.

diaggpsd goal tier 1: live DIAG → basic NMEA location, for modems with no NMEA
service of their own, on low-CPU hosts. Everything here comes from ONE 0x1476 record, so this tier needs a
single log code (``required_codes`` stays ``{0x1476}``). The functions are pure,
and the hot path does no allocation beyond the output strings.

Policy decisions (also in the README):

- **Altitude is HAE, geoid separation 0.0.** 0x1476 reports WGS84 ellipsoidal
  height. GGA wants MSL plus a geoid separation N, and no geoid model ships
  here. So the altitude field carries HAE with N = 0.0. A consumer that rebuilds
  HAE as ``alt + N`` (gpsd's ``altHAE``) gets the right value. The MSL field is
  off by the local geoid height (about −100…+80 m worldwide). Users who need MSL
  should use the modem's own NMEA where it exists.
- **No fix → empty position.** ``pos_source`` 0 (none) or 4 (database / predicted)
  and the cold-start seed (38N 117W) are not satellite fixes. Those epochs give
  GGA quality 0, RMC status ``V`` and GSA fix type 1, with empty
  lat/lon/alt/speed — the same rule as the TPV path.
- **Fix type** is 3D for Kalman/combined fixes (``pos_source`` 2 / 8) and 2D
  otherwise. This matches the gpsd TPV ``mode``.
- **UTC** = GPS week + TOW − 18 leap seconds (since 2017), the same constant
  gpsd_server uses. When the week is unknown (0 or 0xFFFF), time and date are empty.
- **Talker** ``GN`` (multi-constellation). The satellite count is the GPS +
  GLONASS + BeiDou used counts. GSA's PRN list is empty, because 0x1476 carries
  used PRNs only on a few versions.
- A DOP of 0.0 or a sentinel (≥ 100) is "no DOP" and leaves the field empty.
- **One sentence set per epoch** (``EpochGate``): later epochs only,
  from one locked fix source, with a 1.5 s holdover for a lost fix.
"""
from __future__ import annotations

import datetime
import math

LEAP_SECONDS = 18
_GPS_EPOCH = datetime.datetime(1980, 1, 6, tzinfo=datetime.timezone.utc)
MS_TO_KNOTS = 3600.0 / 1852.0       # m/s -> knots (1 kn = 1852 m/h)
SEED_LAT = 38.0          # GNSS-engine cold-start seed, degrees
SEED_LON = -117.0


def checksum(body: str) -> str:
    """XOR of every character between ``$`` and ``*``, as two hex digits."""
    c = 0
    for ch in body.encode("ascii"):
        c ^= ch
    return f"{c:02X}"


def sentence(body: str) -> str:
    """``$BODY*CS\\r\\n``."""
    return f"${body}*{checksum(body)}\r\n"


def _dop(v) -> str:
    if v is None or not 0.0 < v < 100.0:
        return ""
    return f"{v:.1f}"


def _latlon(lat: float, lon: float) -> tuple[str, str, str, str]:
    """``ddmm.mmmmmm`` / ``dddmm.mmmmmm`` with hemispheres."""
    def fmt(v: float, deg_digits: int) -> str:
        a = abs(v)
        d = int(a)
        m = (a - d) * 60.0
        if round(m, 6) >= 60.0:          # 59.9999999 rounding up to 60
            d += 1
            m = 0.0
        return f"{d:0{deg_digits}d}{m:09.6f}"
    return (fmt(lat, 2), "N" if lat >= 0 else "S",
            fmt(lon, 3), "E" if lon >= 0 else "W")


def gps_to_utc(week: int, tow_ms: int) -> datetime.datetime | None:
    if not 0 < week < 0xFFFF:
        return None
    return _GPS_EPOCH + datetime.timedelta(weeks=week, milliseconds=tow_ms - LEAP_SECONDS * 1000)


def is_fix(pos) -> bool:
    if pos.pos_source in (0, 4):
        return False
    seed = getattr(pos, "is_cold_start_seed", False)
    if callable(seed):
        seed = seed()
    if seed:
        return False
    lat = math.degrees(pos.lat_rad)
    lon = math.degrees(pos.lon_rad)
    if not (math.isfinite(lat) and math.isfinite(lon)) or (lat == 0.0 and lon == 0.0):
        return False
    return abs(lat) <= 90.0 and abs(lon) <= 180.0


def sats_used(pos) -> int:
    n = 0
    for name in ("num_gps_svs", "num_glo_svs", "num_bds_svs"):
        v = getattr(pos, name, 0) or 0
        if 0 < v < 64:
            n += v
    return min(n, 99)


def from_position(pos, talker: str = "GN") -> list[str]:
    """GGA, RMC and GSA sentences for one PositionReport."""
    t = gps_to_utc(pos.gps_week, pos.gps_tow_ms)
    hhmmss = t.strftime("%H%M%S") + f".{t.microsecond // 10000:02d}" if t else ""
    ddmmyy = t.strftime("%d%m%y") if t else ""
    fix = is_fix(pos)
    mode3d = pos.pos_source in (2, 8)
    hdop, vdop, pdop = _dop(pos.hdop), _dop(pos.vdop), _dop(pos.pdop)
    nsat = sats_used(pos)

    if fix:
        la, ns, lo, ew = _latlon(math.degrees(pos.lat_rad), math.degrees(pos.lon_rad))
        alt = f"{pos.alt_m:.1f}" if math.isfinite(pos.alt_m) else ""
        sep = "0.0" if alt else ""
        speed = math.hypot(pos.vel_e, pos.vel_n)
        sog = f"{speed * MS_TO_KNOTS:.2f}" if math.isfinite(speed) else ""
        trk = math.degrees(pos.heading_rad) % 360.0 if math.isfinite(pos.heading_rad) else None
        cog = f"{trk:.1f}" if trk is not None else ""
        gga_q, rmc_s, rmc_m, gsa_t = "1", "A", "A", "3" if mode3d else "2"
    else:
        la = ns = lo = ew = alt = sep = sog = cog = ""
        gga_q, rmc_s, rmc_m, gsa_t = "0", "V", "N", "1"
    hdop_gga = hdop if fix else ""

    gga = (f"{talker}GGA,{hhmmss},{la},{ns},{lo},{ew},{gga_q},{nsat:02d},{hdop_gga},"
           f"{alt},{'M' if alt else ''},{sep},{'M' if sep else ''},,")
    rmc = f"{talker}RMC,{hhmmss},{rmc_s},{la},{ns},{lo},{ew},{sog},{cog},{ddmmyy},,,{rmc_m}"
    gsa = f"{talker}GSA,A,{gsa_t}," + "," * 12 + (
        f"{pdop},{hdop},{vdop}" if fix else ",,")
    return [sentence(gga), sentence(rmc), sentence(gsa)]


_RANK = {8: 3, 2: 2, 1: 1}     # pos_source preference: combined > Kalman > WLS


class EpochGate:
    """Pass one 0x1476 report per epoch, from one solution source.

    A modem can emit several 0x1476 per second. The RM500Q-AE sends the combined
    fix (pos_source 8) twice, byte-identical; a Kalman fix (2) 447 ms later with
    a different altitude; and a pos_source-4 record whose TOW is stuck hours in
    the past. Written as-is, that is a duplicate GGA, a jittering altitude and a
    false "no fix" with a wrong time, every second. While the last emitted
    report is younger than ``hold_s``, a report passes only if its epoch
    (week, TOW) is later than the last one, and while a fix is locked, only a
    fix from that same ``pos_source`` passes. A stuck TOW can sit ahead of the
    live one as easily as behind it, so a no-fix report never outranks a fresh
    fix by its time alone. A better-ranked fix source (``_RANK``: combined 8
    first, the one the modem's own GGA matches) takes the lock at once, so the
    output does not depend on which record happened to arrive first. Once ``hold_s`` passes with nothing emitted, the next
    report passes regardless, so a stream with no fix still heartbeats and a
    lost fix is reported. ``now`` is a monotonic time in seconds.
    """

    def __init__(self, hold_s: float = 1.5):
        self.hold_s = hold_s
        self._key: tuple[int, int] | None = None
        self._source: int | None = None      # locked fix source; None after a no-fix
        self._at = -math.inf

    def accept(self, pos, now: float) -> bool:
        week = pos.gps_week if 0 < pos.gps_week < 0xFFFF else 0
        key = (week, pos.gps_tow_ms)
        fix = is_fix(pos)
        if now - self._at < self.hold_s:
            if self._key is not None and key <= self._key:
                return False
            if self._source is not None and not (
                    fix and (pos.pos_source == self._source
                             or _RANK.get(pos.pos_source, 0) > _RANK.get(self._source, 0))):
                return False
        self._key, self._at = key, now
        self._source = pos.pos_source if fix else None
        return True


# ---------------------------------------------------------------------------
# Output sinks: never block the DIAG loop (a stalled reader loses sentences)
# ---------------------------------------------------------------------------

import errno  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402


class StreamSink:
    """stdout or a file (``file:PATH``, appended)."""

    def __init__(self, fh, close: bool = False):
        self.fh = fh
        self._close = close

    def write(self, text: str) -> None:
        self.fh.write(text)
        self.fh.flush()

    def close(self) -> None:
        if self._close:
            self.fh.close()


class TcpSink:
    """``tcp:[HOST:]PORT`` — a listener that fans the sentences out to every
    client (gpsd can read it: ``gpsd tcp://HOST:PORT``). A client that cannot
    keep up loses sentences; it never stalls the loop. Binds 127.0.0.1 unless
    a host is given."""

    def __init__(self, host: str, port: int):
        self.srv = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((host, port))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.clients: list[socket.socket] = []
        self.lock = threading.Lock()
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._stop:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            c.setblocking(False)
            with self.lock:
                self.clients.append(c)

    def write(self, text: str) -> None:
        data = text.encode("ascii")
        with self.lock:
            dead = []
            for c in self.clients:
                try:
                    c.send(data)
                except BlockingIOError:
                    pass
                except OSError:
                    dead.append(c)
            for c in dead:
                self.clients.remove(c)
                c.close()

    def close(self) -> None:
        self._stop = True
        self.srv.close()
        with self.lock:
            for c in self.clients:
                c.close()
            self.clients.clear()


class PtySink:
    """``pty:LINK`` — a pseudo-terminal, with LINK symlinked to its slave so
    gpsd can open it as an NMEA device (``gpsd -n LINK``). Writes go to the
    master. With no reader, the pty buffer fills and later sentences are dropped."""

    def __init__(self, link: str):
        import tty
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.slave_name = os.ttyname(slave)
        self._slave = slave            # held open so a reader can come and go
        os.set_blocking(self.master, False)
        self.link = link
        try:
            os.unlink(link)
        except FileNotFoundError:
            pass
        os.symlink(self.slave_name, link)

    def write(self, text: str) -> None:
        try:
            os.write(self.master, text.encode("ascii"))
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise

    def close(self) -> None:
        try:
            if os.path.islink(self.link) and os.readlink(self.link) == self.slave_name:
                os.unlink(self.link)
        except OSError:
            pass
        os.close(self.master)
        os.close(self._slave)


def open_sink(spec: str):
    """``-`` | ``file:PATH`` | ``tcp:[HOST:]PORT`` | ``pty:LINK``."""
    import sys
    if spec == "-":
        return StreamSink(sys.stdout)
    if spec.startswith("file:"):
        return StreamSink(open(spec[5:], "a", encoding="ascii"), close=True)
    if spec.startswith("tcp:"):
        host, _, port = spec[4:].rpartition(":")
        return TcpSink(host.strip("[]") or "127.0.0.1", int(port))
    if spec.startswith("pty:"):
        return PtySink(spec[4:])
    raise ValueError(f"bad --nmea-out {spec!r} (- | file:PATH | tcp:[HOST:]PORT | pty:LINK)")
