#!/usr/bin/env python3
"""Compare diaggpsd DIAG output with gpsd ground truth.

Supports two ground truth sources:
  - SKY reports (any NMEA-capable receiver: u-blox, LG290P, etc.)
    → Per-SV azimuth, elevation, C/N0 comparison
  - RTCM3 MSM7 reports (LG290P with gpsd patches)
    → Per-signal CNR comparison including L5/E5a/B2a

Matches by GPS time (week + milliseconds) and compares:
- Position (lat/lon/alt) via TPV
- Per-SV azimuth, elevation, C/N0 via SKY
- Per-signal CNR via RTCM3 MSM7 (multi-frequency)
"""

import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta


def _open_text(path):
    """Open a text file, transparently handling .xz / .bz2 compression.

    Used so consumers can read sidecars produced by the new
    GNSS post-decode step (which xz-compresses everything) without
    losing backwards compatibility with historical uncompressed files.
    """
    if path.endswith('.xz'):
        import lzma
        return lzma.open(path, 'rt')
    if path.endswith('.bz2'):
        import bz2
        return bz2.open(path, 'rt')
    return open(path)

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
LEAP_SECONDS = 18  # as of 2026

def gps_to_utc(week, ms):
    """Convert GPS week + milliseconds to UTC datetime."""
    gps_time = GPS_EPOCH + timedelta(weeks=week, milliseconds=ms)
    return gps_time - timedelta(seconds=LEAP_SECONDS)


# The GPS week number is a 10-bit field that wraps every 1024 weeks (~19.6 yr).
# A receiver that resolves it into the wrong 1024-week era emits DIAG GNSS
# timestamps a whole number of rollover cycles away from true time — e.g. the
# MDM9600 / SWI9200X reports the 2006 era instead of 2026, exactly one
# 1024-week cycle stale. Reference receivers (LG290P/gpsd) are at the correct
# era, so absolute-time epoch matching fails by ~20 yr until the cycle offset
# is removed. The per-signal CNR path keys off time-of-week and is unaffected,
# which is why C/N0 still compares cleanly on a rollover-affected modem.
_GPS_ROLLOVER = timedelta(weeks=1024)
_GPS_ROLLOVER_YEARS = 1024 * 7 / 365.25  # ≈ 19.6 yr, for human-readable notes


def gps_rollover_offset(diag_utc, ref_utc):
    """Whole-cycle ``timedelta`` to add to a DIAG-side UTC so it lands in the
    same 1024-week GPS-rollover era as ``ref_utc``.

    Snaps ``diag_utc`` to the rollover cycle nearest ``ref_utc``. Returns
    ``timedelta(0)`` when the two are already in the same era — the common
    modern-receiver case — so correctly-timed captures are left untouched
    (n==0) and only rollover-affected chipsets get corrected. The capture
    span (minutes) is negligible against the 19.6-yr cycle, so rounding to the
    nearest cycle is unambiguous.
    """
    if diag_utc is None or ref_utc is None:
        return timedelta(0)
    return round((ref_utc - diag_utc) / _GPS_ROLLOVER) * _GPS_ROLLOVER


# OEM DRE non-GPS reports encode their own constellation's system-time week
# in the ``gps_week`` field. GST epoch is 1999-08-22 (GPS week 1024); BDT
# epoch is 2006-01-01 (GPS week 1356). GLONASS has no week concept and uses
# a zero placeholder plus glo_year/glo_day/glo_milliseconds instead.
_GAL_GPS_WEEK_OFFSET = 1024
_BDS_GPS_WEEK_OFFSET = 1356

# Azimuth is mathematically ill-conditioned near zenith: d(az)/d(pos) → ∞ as
# el → 90°. SVs with el > this threshold are excluded from azimuth stats so a
# single near-zenith SV doesn't dominate P95.
_AZ_EL_MAX = 75.0


def _resolve_oemdre_utc(report, gps_week_ctx=None):
    """Resolve an OEM DRE report's measurement epoch to UTC.

    ``gps_week_ctx`` is an optional concurrent GPS-slot week number used as
    a fallback when the report is GLONASS (gps_week=0). GLONASS reports
    have two known field encodings depending on firmware:

    - **N4/NT encoding** (confirmed on LM960 32.01.110):
      ``glo_year`` is the N4 4-year-cycle index (1=1996-1999, 2=2000-2003,
      ..., 8=2024-2027). ``glo_day`` is NT day-of-cycle (1..1461).
      ``glo_milliseconds`` is ms-of-day in Moscow time (UTC+3). Detected
      when ``glo_year < 100``.
    - **Calendar encoding** (presumed older firmwares): ``glo_year`` is
      the calendar year (e.g. 2026), ``glo_day`` is day-of-year. Detected
      when ``glo_year >= 1996``.

    Earlier comment claimed SDX20/SDX55 firmware emit glo_year=0/glo_day=0
    and required the GPS-context fallback; that was true for older parser
    output but the current LM960 32.01.110 capture populates the GLONASS
    fields and the GPS-context fallback fails (gps_milliseconds=0 on
    GLONASS records, so the ``and ms`` guard never passes).
    """
    const = report.get('constellation')
    week = report.get('gps_week', 0) or 0
    ms = report.get('gps_milliseconds', 0) or 0

    if const == 'GLONASS':
        glo_year = report.get('glo_year', 0)
        glo_day = report.get('glo_day', 0)
        glo_ms = report.get('glo_milliseconds', 0)
        if glo_year and glo_day:
            if glo_year < 100:
                # N4/NT encoding (LM960 32.01.110): N4 cycle since 1996,
                # NT day-of-cycle, ms-of-day in Moscow time (UTC+3).
                cycle_start_year = 1996 + 4 * (glo_year - 1)
                msk = (datetime(cycle_start_year, 1, 1, tzinfo=timezone.utc)
                       + timedelta(days=glo_day - 1, milliseconds=glo_ms))
                return msk - timedelta(hours=3)
            # Calendar encoding: glo_year is the actual year, glo_day is
            # day-of-year, glo_ms is ms-of-day in Moscow time.
            year_start = datetime(glo_year, 1, 1, tzinfo=timezone.utc)
            msk = year_start + timedelta(days=glo_day - 1, milliseconds=glo_ms)
            return msk - timedelta(hours=3)
        # Fallback: use concurrent GPS slot's week + this report's ms.
        # The two are emitted within the same fix epoch on all observed
        # SDX20/SDX55 firmware at 1 Hz.
        if gps_week_ctx and ms:
            return gps_to_utc(gps_week_ctx, ms)
        return None

    if const == 'Galileo' and 0 < week < 2000:
        # GAL System Time week → GPS week
        return gps_to_utc(week + _GAL_GPS_WEEK_OFFSET, ms)

    if const == 'BeiDou' and 0 < week < 1500:
        # BDT week → GPS week
        return gps_to_utc(week + _BDS_GPS_WEEK_OFFSET, ms)

    if week == 0:
        return None  # no reference and not a recognized non-GPS case
    return gps_to_utc(week, ms)


def _meas_gps_time(rec):
    """Extract (gps_week, gps_milliseconds) from a GpsMeasurementReport, tolerating either schema.

    The diaggrok parser for 0x1477 emits `gps_week` + `gps_milliseconds`. Older
    diaggpsd JSONL files (e.g. early FN980m captures) were produced by a pre-diaggrok
    parser that emitted `week` + `milliseconds`. Both schemas carry the same data
    under different names. This helper lets gnss_compare.py work against both
    without re-parsing legacy captures.

    Returns `(None, None)` if neither schema is present.
    """
    if 'gps_week' in rec and 'gps_milliseconds' in rec:
        return rec['gps_week'], rec['gps_milliseconds']
    if 'week' in rec and 'milliseconds' in rec:
        return rec['week'], rec['milliseconds']
    return None, None


def _detect_cno_scale(measurements) -> float:
    """Detect the C/N0 scale factor used in a diaggpsd JSONL.

    Older parsers emitted ``cno_db`` in 0.1 dB-Hz units (values 0..700).
    Current parsers emit it directly in dB-Hz (values 0..70). Any
    ``cno_db`` value over 100 indicates the old scale; return 0.1 to
    convert to dB-Hz. Otherwise return 1.0.
    """
    for meas in measurements:
        for sv in meas.get('svs', []):
            c = sv.get('cno_db')
            if c is not None and c > 100:
                return 0.1
    return 1.0

def load_diag(path):
    """Load diaggpsd JSONL output.

    Also detects the C/N0 scale in the file (older 0.1-dB-Hz vs current
    dB-Hz) and normalizes every ``cno_db`` field to dB-Hz in-place so the
    rest of the tool can ignore the distinction.
    """
    positions = []
    measurements = []
    oemdre = []  # OEM DRE multi-constellation reports
    with _open_text(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get('type') in ('PositionReport', 'Diag0x1476') and obj.get('pos_source') == 2:
                # pos_source 2 = Kalman filter (ME measurement engine)
                # Parser renamed to Diag0x1476; accept both for
                # JSON files written before/after the rename.
                positions.append(obj)
            elif obj.get('type') in ('GpsMeasurementReport', 'Diag0x1477'):
                # Parser renamed to Diag0x1477; accept
                # both for JSON files written before/after the rename.
                measurements.append(obj)
            elif obj.get('type') in ('OemdreMeasurementReport', 'Diag0x14DE'):
                # Parser renamed to Diag0x14DE; accept both for
                # JSON files written before/after the rename.
                oemdre.append(obj)

    # Detect C/N0 scale from measurements and normalize in-place
    scale = _detect_cno_scale(measurements)
    if scale != 1.0:
        for meas in measurements:
            for sv in meas.get('svs', []):
                if 'cno_db' in sv:
                    sv['cno_db'] = sv['cno_db'] * scale
        for rep in oemdre:
            for sv in rep.get('svs', []):
                if 'cno_db' in sv:
                    sv['cno_db'] = sv['cno_db'] * scale
    return positions, measurements, oemdre

# MSM7 message type → constellation name
_MSM7_TYPE_MAP = {1077: 'GPS', 1087: 'GLONASS', 1097: 'Galileo', 1127: 'BeiDou'}

# pyrtcm sidecar source for MSM7 lookups. gpsd 3.27.6~dev's
# MSM7 cnr decode is broken — values bear no consistent relation
# to dB-Hz. pyrtcm decodes DF408 directly to dB-Hz with sub-quarter-dB
# resolution, in agreement with rtklib RINEX SNR and OEM DRE cno_db in a
# cross-decoder audit. gnss_compare.py prefers a
# `*.pyrtcm.jsonl.xz` sibling produced by the GNSS post-decode step
# whenever one is available, falling back to skip-with-note otherwise.

# Per-msg-type field names used by pyrtcm:
#   - tow source field (used as the lookup key; same per-constellation time
#     scale that `_resolve_oemdre_utc` round-trips back to, so cross-side
#     matching is exact)
#   - L1-equivalent RINEX-3 signal code (mapped to RTCM3 sigid=2 so the
#     existing OEM-DRE lookup keys keep working)
_PYRTCM_TOW_FIELD = {
    "1077": "DF004",   # GPS week TOW (ms)
    "1097": "DF248",   # Galileo System Time (ms; ≈ GPS TOW)
    "1127": "DF427",   # BeiDou System Time (ms; GPS TOW − 14000)
    # 1087 (GLONASS): DF034 is Moscow-day-epoch ms (mod 3 h). The OEM DRE
    # GLONASS comparator computes GPS TOW; matching that to a Moscow-day
    # tick requires day-of-week resolution that we don't have at MSM7-frame
    # parse time. The current report doesn't surface a GLONASS L1 vs MSM7
    # line either, so leaving GLONASS out keeps parity with the gpsd path.
}

# RINEX-3 signal code (CELLSIG_NN value in pyrtcm output) for the L1-equivalent
# primary OS signal of each constellation. Stored under MSM sigid=2 so the
# downstream `(gnssid, prn, 2)` lookup in main() keeps working.
_PYRTCM_L1_RINEX = {
    "1077": ("1C",),                     # GPS L1 C/A
    "1097": ("1C", "1B", "1X"),          # Galileo E1 (OS-C, OS-B, B+C)
    "1127": ("2I",),                     # BeiDou B1I (RINEX-3 BDS uses freq band 2)
}

# pyrtcm msg type (string) → gpsd-style gnssid (used as part of the lookup key)
_PYRTCM_GNSSID = {"1077": 0, "1097": 2, "1127": 3}


def _pyrtcm_path_for(gpsd_path: str) -> str | None:
    """Return the conventional pyrtcm sidecar path for a given gpsd JSONL path.

    The GNSS post-decode step writes `<base>.pyrtcm.jsonl` next to
    `<base>.gpsd.jsonl` (raw, uncompressed). Older
    captures may still have the `.xz` form.
    Returns the raw path if it exists, the `.xz` form if only that does,
    or None if neither exists / the gpsd path doesn't fit the naming
    convention (older single-decoder captures).
    """
    for ext in (".gpsd.jsonl.xz", ".gpsd.jsonl"):
        if gpsd_path.endswith(ext):
            base = gpsd_path[: -len(ext)] + ".pyrtcm.jsonl"
            if os.path.exists(base):
                return base
            xz = base + ".xz"
            if os.path.exists(xz):
                return xz
            return None
    return None


def load_pyrtcm_msm7(path: str):
    """Load MSM7 per-signal C/N0 from a post-decode pyrtcm sidecar.

    Returns dict[tow_ms] → {(gnssid, prn, sigid): cn0_dbhz} matching the
    shape of the gpsd-derived `msm7_lookup` consumed downstream. Stores
    only L1-equivalent cells (RTCM3 sigid=2) — the only sigid downstream
    consumers look up.

    Returns an empty defaultdict if `path` doesn't exist or contains no
    MSM7 records of the supported types — caller treats that as "skip".
    """
    lookup = defaultdict(dict)
    if not path or not os.path.exists(path):
        return lookup

    with _open_text(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ident = obj.get("identity")
            tow_field = _PYRTCM_TOW_FIELD.get(ident)
            if tow_field is None:
                continue
            tow = obj.get(tow_field)
            if tow is None:
                continue
            gnssid = _PYRTCM_GNSSID[ident]
            l1_codes = _PYRTCM_L1_RINEX[ident]

            ncell = obj.get("NCell", 0)
            for i in range(1, ncell + 1):
                sig = obj.get(f"CELLSIG_{i:02d}")
                if sig not in l1_codes:
                    continue
                prn_str = obj.get(f"CELLPRN_{i:02d}")
                if prn_str is None:
                    continue
                try:
                    prn = int(prn_str)
                except (TypeError, ValueError):
                    continue
                cn0 = obj.get(f"DF408_{i:02d}")
                if cn0 is None or cn0 <= 0:
                    continue
                # pyrtcm DF408 is already dB-Hz with sub-quarter-dB resolution.
                lookup[tow][(gnssid, prn, 2)] = cn0
    return lookup


def load_gpsd(path):
    """Load gpsd JSONL output (TPV + SKY + RTCM3 MSM7)."""
    fixes = []
    skys = []
    msm7 = []  # RTCM3 MSM7 per-signal observations
    with _open_text(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            cls = obj.get('class')
            if cls == 'TPV' and obj.get('mode', 0) >= 2 and 'time' in obj:
                fixes.append(obj)
            elif cls == 'SKY':
                skys.append(obj)
            elif cls == 'RTCM3' and obj.get('subtype') == 'MSM7':
                msm7.append(obj)
    return fixes, skys, msm7



# Signal ID → signal name (for display)
_SIGNAL_NAMES = {
    (0, 2): 'L1', (0, 17): 'L2', (0, 23): 'L5',
    (2, 2): 'E1', (2, 8): 'E6', (2, 15): 'E5a', (2, 23): 'E5b',
    (3, 2): 'B1', (3, 8): 'B3', (3, 14): 'B2a', (3, 23): 'B2b', (3, 25): 'B1C',
    (6, 2): 'G1', (6, 8): 'G2',
}

def decode_msm7_cells(msg):
    """Decode MSM7 MaskSat/MaskSig/MaskCell into (sat_id, sig_id, sig_data) tuples.

    Returns list of (sat_prn, sig_id, sig_entry) for each active cell.
    """
    mask_sat = msg.get('MaskSat', 0)
    mask_sig = msg.get('MaskSig', 0)
    mask_cell = msg.get('MaskCell', 0)
    sigs = msg.get('sig', [])
    sat_data = msg.get('sat', [])

    # Decode satellite PRNs from MaskSat (bit 63 = PRN 1)
    sat_ids = [64 - bit for bit in range(63, -1, -1) if mask_sat & (1 << bit)]
    # Decode signal IDs from MaskSig (bit 31 = sigid 1)
    sig_ids = [32 - bit for bit in range(31, -1, -1) if mask_sig & (1 << bit)]

    if not sat_ids or not sig_ids:
        return []

    cells = []
    cell_idx = 0
    bit = len(sat_ids) * len(sig_ids) - 1
    for si in range(len(sat_ids)):
        for sigi in range(len(sig_ids)):
            if mask_cell & (1 << bit):
                if cell_idx < len(sigs):
                    cells.append((sat_ids[si], sig_ids[sigi], sigs[cell_idx],
                                  sat_data[si] if si < len(sat_data) else {}))
                cell_idx += 1
            bit -= 1

    return cells

def haversine_m(lat1, lon1, lat2, lon2):
    """Distance in meters between two lat/lon points."""
    R = 6371000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(rlat1)*math.cos(rlat2)*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def _find_closest(items, target_time, time_fn, max_dt=2.0):
    """Find item closest to target_time. Returns (item, delta_seconds) or (None, inf)."""
    best, best_dt = None, float('inf')
    for item in items:
        dt = abs((time_fn(item) - target_time).total_seconds())
        if dt < best_dt:
            best_dt = dt
            best = item
    return (best, best_dt) if best_dt < max_dt else (None, best_dt)


def oemdre_no_comparison_reason(n_reports, reports_with_svs, matched):
    """Explain why an OEM DRE constellation produced zero SV comparisons.

    Disambiguates the three structurally distinct causes that the original
    code collapsed into one uniform "time sync issue" label:

    - ``reports_with_svs == 0`` — every report is an **empty shell**: the
      constellation is *tracked but not decoded* (no ephemeris ⇒ the OEM
      *Decoded* Range Engine emits report records carrying zero per-SV
      measurements). There is simply no SV data to compare; this is **not**
      a timing problem. This is the Galileo case (e.g. 332 OEM DRE
      reports, all empty) — which RTCM MSM7 still tracks, but OEM DRE never
      decodes, so it yields no GAGSV.
    - ``matched == 0`` (but some reports carry SVs) — reports DO have per-SV
      data but none time-aligned to a reference SKY epoch: a **genuine
      time-sync issue** (the only case the old label was ever right about).
    - otherwise — reports time-matched, but no overlapping/usable reference
      SVs (no gpsd counterpart for the DIAG SV ids, or all SVs filtered out,
      e.g. ``el_deg < 0``).

    Returns a human-readable tail (the caller prepends ``"{const}: "``).
    """
    if reports_with_svs == 0:
        return (f"{n_reports} reports, all empty shells "
                f"(tracked-but-not-decoded: 0 per-SV measurements) — "
                f"no SV data to compare, not a timing issue")
    if matched == 0:
        return (f"{n_reports} reports ({reports_with_svs} with SV data), "
                f"no time-matched reference epochs (time sync issue)")
    return (f"{n_reports} reports, {matched} time-matched, but 0 SV "
            f"comparisons (no overlapping reference SVs / all filtered)")


def main():
    diag_path = sys.argv[1]
    gpsd_path = sys.argv[2]

    positions, measurements, oemdre = load_diag(diag_path)
    fixes, skys, msm7 = load_gpsd(gpsd_path)

    # Prefer the pyrtcm sidecar for MSM7 ground truth: gpsd 3.27.6~dev's
    # MSM7 cnr decode is broken. pyrtcm DF408 agrees with rtklib
    # RINEX SNR and OEM DRE cno_db within ±0.6 dB in a cross-decoder audit.
    pyrtcm_msm7_path = _pyrtcm_path_for(gpsd_path)
    pyrtcm_msm7_lookup = load_pyrtcm_msm7(pyrtcm_msm7_path) if pyrtcm_msm7_path else defaultdict(dict)

    # Count OEM DRE by constellation
    oemdre_by_const = defaultdict(list)
    for o in oemdre:
        oemdre_by_const[o.get('constellation', '?')].append(o)

    # Count MSM7 by constellation
    msm7_by_const = defaultdict(list)
    for m in msm7:
        name = _MSM7_TYPE_MAP.get(m.get('type'), f"type{m.get('type')}")
        msm7_by_const[name].append(m)

    print(f"=== Data Loaded ===")
    print(f"DIAG: {len(positions)} position reports, {len(measurements)} GPS measurement reports")
    if oemdre:
        parts = [f"{c}: {len(v)}" for c, v in sorted(oemdre_by_const.items())]
        print(f"DIAG OEM DRE: {len(oemdre)} reports ({', '.join(parts)})")
    print(f"gpsd: {len(fixes)} TPV fixes, {len(skys)} SKY reports")
    if msm7:
        parts = [f"{c}: {len(v)}" for c, v in sorted(msm7_by_const.items())]
        print(f"gpsd MSM7: {len(msm7)} reports ({', '.join(parts)})")
    print()

    gpsd_time = lambda x: datetime.fromisoformat(x['time'].replace('Z', '+00:00'))

    # GPS week-rollover correction. Rollover-affected modems (e.g.
    # MDM9600/SWI9200X) timestamp DIAG GNSS records in a stale 1024-week era,
    # so absolute-time matching against the correct-era reference fails by
    # ~20 yr (matched_epochs: 0) even though the per-signal CNR path passes.
    # Compute the constant whole-cycle offset ONCE from a reference anchor
    # (median gpsd fix time) vs a representative DIAG record, then apply it at
    # every DIAG→UTC match site below. The offset is timedelta(0) for modern
    # receivers, so this is a pure no-op on rollover-clean captures.
    _ref_anchor = None
    if fixes:
        try:
            _ref_anchor = gpsd_time(fixes[len(fixes) // 2])
        except (KeyError, ValueError):
            _ref_anchor = None
    if _ref_anchor is None:
        for _s in skys:
            if 'time' in _s:
                try:
                    _ref_anchor = gpsd_time(_s)
                    break
                except (KeyError, ValueError):
                    continue
    # A valid GNSS time-of-week is in [0, 604800000) ms (one week). Prefer the
    # 0x1477 measurement clock as the rollover anchor — it's the DIAG time
    # source the per-signal CNR path matches against, so it's the reliable
    # epoch — then fall back to positions. (Historically the MDM9600/SWI9200X
    # 0x1476 PositionReport was mis-parsed on this firmware — garbage lat/lon,
    # week 3880, TOW > 1 wk — making it useless as an anchor. A parser
    # fix means 0x1476 TOW is now in-range, but 0x1477 stays the preferred
    # anchor by the rationale above. The _valid_tow guard keeps either source
    # honest.)
    _WEEK_MS = 7 * 24 * 3600 * 1000  # 604_800_000

    def _valid_tow(ms):
        return isinstance(ms, (int, float)) and 0 <= ms < _WEEK_MS

    _diag_anchor = None
    for _m in measurements:
        _w, _ms = _meas_gps_time(_m)
        if _w is not None and _valid_tow(_ms):
            _diag_anchor = gps_to_utc(_w, _ms)
            break
    if _diag_anchor is None:
        for _p in positions:
            if _valid_tow(_p.get('gps_tow_ms')):
                _diag_anchor = gps_to_utc(_p['gps_week'], _p['gps_tow_ms'])
                break
    rollover = gps_rollover_offset(_diag_anchor, _ref_anchor)
    if rollover:
        _cycles = round(rollover / _GPS_ROLLOVER)
        print(
            f"  NOTE: GPS week-rollover correction applied — DIAG timestamps "
            f"shifted {_cycles:+d}×1024 weeks (~{_cycles * _GPS_ROLLOVER_YEARS:+.0f} yr) "
            f"to align with the reference era."
        )
        print()

    def _corr(utc):
        """Apply the rollover offset to a DIAG-side UTC (None-safe)."""
        return utc + rollover if utc is not None else None

    # --- Position comparison (all matched pairs) ---
    if positions and fixes:
        print(f"=== Position Comparison (all matched epochs) ===")
        # DIAG PositionReport alt_m is ellipsoidal (HAE, height above WGS84).
        # gpsd TPV historically had ambiguous `alt` semantics: pre-3.20 it was
        # HAE; since 3.20 it's an alias for altMSL (orthometric). Prefer the
        # unambiguous `altHAE` when gpsd provides it so the comparison is
        # apples-to-apples; fall back to `alt` when altHAE is missing and
        # annotate the output so the geoid-undulation bias is traceable.
        dists = []
        alt_diffs = []
        alt_field_used = None
        for pos in positions:
            utc = _corr(gps_to_utc(pos['gps_week'], pos['gps_tow_ms']))
            fix, dt = _find_closest(fixes, utc, gpsd_time)
            if fix is None:
                continue
            dist = haversine_m(pos['lat_deg'], pos['lon_deg'], fix['lat'], fix['lon'])
            if 'altHAE' in fix:
                gpsd_alt = fix['altHAE']
                alt_field_used = 'altHAE'
            elif 'alt' in fix:
                gpsd_alt = fix['alt']
                alt_field_used = alt_field_used or 'alt (ambiguous — may be orthometric)'
            else:
                gpsd_alt = pos['alt_m']
                alt_field_used = alt_field_used or 'none (falling back to DIAG)'
            alt_diff = pos['alt_m'] - gpsd_alt
            dists.append(dist)
            alt_diffs.append(alt_diff)

        if dists:
            print(f"  Matched epochs: {len(dists)}")
            print(f"  Horizontal distance — mean: {sum(dists)/len(dists):.2f}m, median: {sorted(dists)[len(dists)//2]:.2f}m, max: {max(dists):.2f}m")
            print(f"  Altitude difference (DIAG HAE − gpsd {alt_field_used}) — mean: {sum(alt_diffs)/len(alt_diffs):.2f}m, std: {(sum((x-sum(alt_diffs)/len(alt_diffs))**2 for x in alt_diffs)/len(alt_diffs))**0.5:.2f}m")
            if alt_field_used and 'alt (ambiguous' in alt_field_used:
                print(f"    NOTE: gpsd reported `alt` only (no altHAE). Expect ~|N| m bias from geoid undulation if gpsd is orthometric.")
            print(f"  PASS horizontal (mean<5m): {'YES' if sum(dists)/len(dists) < 5.0 else 'NO'}")

        # Show one representative sample
        mid = positions[len(positions)//2]
        mid_utc = _corr(gps_to_utc(mid['gps_week'], mid['gps_tow_ms']))
        mid_fix, mid_dt = _find_closest(fixes, mid_utc, gpsd_time)
        if mid_fix:
            print(f"\n  Sample (mid-capture, Δt={mid_dt:.3f}s):")
            print(f"    DIAG: {mid['lat_deg']:.8f}, {mid['lon_deg']:.8f}, alt={mid['alt_m']:.1f}m, HDOP={mid['hdop']:.2f}")
            gpsd_alt_sample = mid_fix.get('altHAE', mid_fix.get('alt', 0))
            print(f"    gpsd: {mid_fix['lat']:.8f}, {mid_fix['lon']:.8f}, alt={gpsd_alt_sample:.1f}m ({alt_field_used}), eph={mid_fix.get('eph',0):.2f}m")

    print()

    # --- SV comparison (all matched epochs) ---
    if measurements and skys:
        print(f"=== SV Comparison — Full Statistical Analysis ===")

        # Accumulate per-SV errors across all matched epochs.
        # Azimuth el≤_AZ_EL_MAX filter is applied (see module-level
        # constant for the geometry rationale).
        all_az_errors = []          # filtered (el ≤ _AZ_EL_MAX) — drives PASS/FAIL
        all_az_errors_unfilt = []   # all SVs — shown for reference
        all_el_errors = []
        all_cn0_errors = []
        per_sv_errors = defaultdict(lambda: {'az': [], 'el': [], 'cn0': []})
        matched_epochs = 0

        timed_skys = [s for s in skys if 'time' in s]

        # If SKY reports lack timestamps (e.g., LG290P), assign from nearest TPV
        if not timed_skys and fixes:
            sky_with_sats = [s for s in skys if s.get('satellites')]
            for i, sky in enumerate(sky_with_sats):
                # Assign time from the closest TPV by position in the stream
                # (SKY and TPV alternate at ~1Hz, so nearest TPV by index is close enough)
                fix_idx = min(i, len(fixes) - 1)
                if 'time' in fixes[fix_idx]:
                    sky['time'] = fixes[fix_idx]['time']
            timed_skys = [s for s in sky_with_sats if 'time' in s]

        for meas in measurements:
            week, ms = _meas_gps_time(meas)
            if week is None:
                continue
            meas_utc = _corr(gps_to_utc(week, ms))
            sky, dt = _find_closest(timed_skys, meas_utc, gpsd_time)
            if sky is None:
                continue
            matched_epochs += 1

            gpsd_svs = {}
            for sat in sky.get('satellites', []):
                if sat.get('gnssid', 0) == 0:
                    # LG290P emits one SKY entry per PRN per signal band
                    # (no sigid / sigid=1 = L1 C/A, sigid=3 = L2C, sigid=7 = L5).
                    # DIAG GpsMeasurementReport (0x1477) is L1-only, so filter
                    # to L1 before the C/N0 compare or the last-seen band wins
                    # the dict slot and inflates apparent deltas by 10+ dB.
                    sigid = sat.get('sigid')
                    if sigid is not None and sigid != 1:
                        continue
                    gpsd_svs[sat['PRN']] = sat

            for sv in meas['svs']:
                sv_id = sv['sv_id']
                if sv_id not in gpsd_svs or sv['el_deg'] < 0:
                    continue
                gsv = gpsd_svs[sv_id]
                # `cno_db` from the diaggrok 0x1477 / 0x1480 parsers is
                # already in dB-Hz — no scaling needed.  Older captures incorrectly left it in 0.1 dB
                # units; this code used to divide by 10 to compensate.
                d_cn0 = sv['cno_db']

                daz = sv['az_deg'] - gsv.get('az', 0)
                if daz > 180: daz -= 360
                if daz < -180: daz += 360
                d_el = sv['el_deg'] - gsv.get('el', 0)
                dcn0 = d_cn0 - gsv.get('ss', 0)

                all_az_errors_unfilt.append(abs(daz))
                if sv['el_deg'] <= _AZ_EL_MAX:
                    all_az_errors.append(abs(daz))
                all_el_errors.append(abs(d_el))
                all_cn0_errors.append(abs(dcn0))
                per_sv_errors[sv_id]['az'].append(daz)
                per_sv_errors[sv_id]['el'].append(d_el)
                per_sv_errors[sv_id]['cn0'].append(dcn0)

        print(f"  Matched epochs: {matched_epochs}")
        print(f"  Total SV comparisons: {len(all_az_errors)}")
        print()

        if all_az_errors:
            def stats(arr):
                n = len(arr)
                mean = sum(arr)/n
                std = (sum((x-mean)**2 for x in arr)/n)**0.5
                return mean, std, max(arr), sorted(arr)[int(n*0.95)]

            az_mean, az_std, az_max, az_p95 = stats(all_az_errors) if all_az_errors else (0.0, 0.0, 0.0, 0.0)
            el_mean, el_std, el_max, el_p95 = stats(all_el_errors)
            cn0_mean, cn0_std, cn0_max, cn0_p95 = stats(all_cn0_errors)
            n_filt = len(all_az_errors)
            n_zenith = len(all_az_errors_unfilt) - n_filt

            print(f"  {'Metric':>12} | {'Mean':>6} {'Std':>6} {'P95':>6} {'Max':>6} | Threshold | Pass")
            print(f"  {'----------':>12} | {'------':>6} {'------':>6} {'------':>6} {'------':>6} | --------- | ----")
            print(f"  {f'Azimuth (el≤{int(_AZ_EL_MAX)}°)':>12} | {az_mean:6.2f} {az_std:6.2f} {az_p95:6.2f} {az_max:6.2f} | ±3°       | {'YES' if az_p95 <= 3.0 else 'NO'}")
            if n_zenith:
                az_u_mean, az_u_std, az_u_max, az_u_p95 = stats(all_az_errors_unfilt)
                print(f"  {'Azimuth (all)':>12} | {az_u_mean:6.2f} {az_u_std:6.2f} {az_u_p95:6.2f} {az_u_max:6.2f} | (reference; {n_zenith} near-zenith SV-epochs excluded)")
            print(f"  {'Elevation (°)':>12} | {el_mean:6.2f} {el_std:6.2f} {el_p95:6.2f} {el_max:6.2f} | ±3°       | {'YES' if el_p95 <= 3.0 else 'NO'}")
            print(f"  {'C/N0 (dB)':>12} | {cn0_mean:6.2f} {cn0_std:6.2f} {cn0_p95:6.2f} {cn0_max:6.2f} | ±3dB      | {'YES' if cn0_p95 <= 3.0 else 'NO'}")

            print(f"\n  Per-SV breakdown (mean signed error):")
            print(f"  {'SV':>4} | {'N':>4} | {'Az bias':>8} {'El bias':>8} {'CN0 bias':>9}")
            print(f"  {'----':>4} | {'----':>4} | {'--------':>8} {'--------':>8} {'---------':>9}")
            for sv_id in sorted(per_sv_errors.keys()):
                e = per_sv_errors[sv_id]
                n = len(e['az'])
                az_bias = sum(e['az'])/n
                el_bias = sum(e['el'])/n
                cn0_bias = sum(e['cn0'])/n
                print(f"  G{sv_id:02d} | {n:4d} | {az_bias:+8.2f} {el_bias:+8.2f} {cn0_bias:+9.2f}")

        # Show one detailed epoch
        print(f"\n  --- Detailed sample (mid-capture) ---")
        meas = measurements[len(measurements)//2]
        week, ms = _meas_gps_time(meas)
        meas_utc = _corr(gps_to_utc(week, ms)) if week is not None else None
        sky, dt = _find_closest(timed_skys, meas_utc, gpsd_time) if meas_utc else (None, 0)
        if sky:
            # L1-only filter to match 0x1477 GPS L1 measurements.
            gpsd_svs = {
                s['PRN']: s
                for s in sky.get('satellites', [])
                if s.get('gnssid', 0) == 0
                and (s.get('sigid') is None or s.get('sigid') == 1)
            }
            print(f"  Time delta: {dt:.3f}s, DIAG SVs: {len(meas['svs'])}, gpsd GPS SVs: {len(gpsd_svs)}")
            print(f"  {'SV':>4} | {'DIAG Az':>8} {'gpsd Az':>8} {'dAz':>6} | {'DIAG El':>8} {'gpsd El':>8} {'dEl':>6} | {'DIAG CN0':>8} {'gpsd CN0':>8} {'dCN0':>6}")
            for sv in meas['svs']:
                sv_id = sv['sv_id']
                d_cn0 = sv['cno_db']  # already in dB-Hz
                if sv_id in gpsd_svs:
                    gsv = gpsd_svs[sv_id]
                    daz = sv['az_deg'] - gsv.get('az', 0)
                    if daz > 180: daz -= 360
                    if daz < -180: daz += 360
                    print(f"  G{sv_id:02d} | {sv['az_deg']:8.1f} {gsv['az']:8.1f} {daz:+6.1f} | {sv['el_deg']:8.1f} {gsv['el']:8.1f} {sv['el_deg']-gsv['el']:+6.1f} | {d_cn0:8.1f} {gsv['ss']:8.1f} {d_cn0-gsv['ss']:+6.1f}")
                else:
                    print(f"  G{sv_id:02d} | {sv['az_deg']:8.1f} {'n/a':>8} {'':>6} | {sv['el_deg']:8.1f} {'n/a':>8} {'':>6} | {d_cn0:8.1f} {'n/a':>8} {'':>6}")

    # --- OEM DRE multi-constellation comparison ---
    if oemdre:
        print(f"\n=== OEM DRE Multi-Constellation Analysis ===")
        timed_skys = [s for s in skys if 'time' in s and s.get('satellites')]
        # gnssid mapping for gpsd: 0=GPS, 6=GLONASS, 2=Galileo, 3=BeiDou
        GNSSID_MAP = {'GPS': 0, 'GLONASS': 6, 'Galileo': 2, 'BeiDou': 3}
        # DIAG SV ID to gpsd PRN mapping:
        #   GPS: 1-32 → PRN 1-32
        #   GLONASS: 65-96 → slot 1-32 (subtract 64), gpsd uses svid 1-32 with gnssid=6
        #   Galileo: 301-336 → E01-E36
        #   BeiDou: 201-237 → C01-C37

        # Pre-compute a concurrent GPS-slot week for GLONASS fallback:
        # GLONASS reports emit gps_week=0, so they need the current GPS
        # slot's week as a time reference.
        gps_ctx_reports = oemdre_by_const.get('GPS', [])
        gps_week_ctx = gps_ctx_reports[0].get('gps_week') if gps_ctx_reports else None

        for const_name in ['GPS', 'GLONASS', 'Galileo', 'BeiDou']:
            const_reports = oemdre_by_const.get(const_name, [])
            if not const_reports:
                continue
            gnssid = GNSSID_MAP.get(const_name)

            all_az, all_az_unfilt, all_el, all_cn0 = [], [], [], []
            per_sv = defaultdict(lambda: {'az': [], 'el': [], 'cn0': []})
            matched = 0
            reports_with_svs = 0

            for report in const_reports:
                if not report.get('svs'):
                    continue
                reports_with_svs += 1
                # Constellation-aware UTC resolution: GAL → +1024 week
                # offset, BDS → +1356 week offset, GLONASS → borrow concurrent
                # GPS slot's week number (glo_year/day are zeroed on SDX55).
                utc = _corr(_resolve_oemdre_utc(report, gps_week_ctx=gps_week_ctx))
                if utc is None:
                    continue
                sky, dt = _find_closest(timed_skys, utc, gpsd_time)
                if sky is None:
                    continue
                matched += 1

                # Build gpsd SV lookup for this constellation.
                # For GPS, OEM DRE GpsDreMeasReport entries are L1-only, so
                # filter the LG290P SKY multi-band emissions to sigid=1 (L1 C/A)
                # to avoid the last-seen band overwriting L1 and inflating
                # apparent C/N0 deltas. Non-GPS constellations keep
                # vendor-specific signal-band semantics and are left untouched.
                gpsd_svs = {}
                for sat in sky.get('satellites', []):
                    if sat.get('gnssid') != gnssid:
                        continue
                    if const_name == 'GPS':
                        sigid = sat.get('sigid')
                        if sigid is not None and sigid != 1:
                            continue
                    gpsd_svs[sat.get('svid', sat.get('PRN', 0))] = sat

                for sv in report['svs']:
                    sv_id = sv['sv_id']
                    # Map DIAG SV ID to gpsd svid
                    if const_name == 'GLONASS':
                        gpsd_id = sv_id - 64  # DIAG 65-96 → gpsd slot 1-32
                    elif const_name == 'Galileo':
                        gpsd_id = sv_id - 300  # DIAG 301-336 → gpsd E01-E36
                    elif const_name == 'BeiDou':
                        gpsd_id = sv_id - 200  # DIAG 201-237 → gpsd C01-C37
                    else:
                        gpsd_id = sv_id  # GPS: same

                    if gpsd_id not in gpsd_svs or sv['el_deg'] < 0:
                        continue
                    gsv = gpsd_svs[gpsd_id]
                    d_cn0 = sv['cno_db']  # OEM DRE cno_db already scaled by dispatch()

                    daz = sv['az_deg'] - gsv.get('az', 0)
                    if daz > 180: daz -= 360
                    if daz < -180: daz += 360
                    d_el = sv['el_deg'] - gsv.get('el', 0)
                    dcn0 = d_cn0 - gsv.get('ss', 0)

                    all_az_unfilt.append(abs(daz))
                    if sv['el_deg'] <= _AZ_EL_MAX:
                        all_az.append(abs(daz))
                    all_el.append(abs(d_el))
                    all_cn0.append(abs(dcn0))
                    per_sv[sv_id]['az'].append(daz)
                    per_sv[sv_id]['el'].append(d_el)
                    per_sv[sv_id]['cn0'].append(dcn0)

            if not all_az_unfilt:
                reason = oemdre_no_comparison_reason(
                    len(const_reports), reports_with_svs, matched)
                print(f"\n  {const_name}: {reason}")
                continue

            def stats(arr):
                n = len(arr)
                mean = sum(arr)/n
                std = (sum((x-mean)**2 for x in arr)/n)**0.5
                return mean, std, max(arr), sorted(arr)[int(n*0.95)]

            el_m, el_s, el_x, el_p = stats(all_el)
            cn_m, cn_s, cn_x, cn_p = stats(all_cn0)

            print(f"\n  {const_name}: {len(const_reports)} reports, {matched} time-matched, {len(all_az_unfilt)} SV comparisons")
            print(f"  {'Metric':>12} | {'Mean':>6} {'Std':>6} {'P95':>6} {'Max':>6} | Pass")
            if all_az:
                az_m, az_s, az_x, az_p = stats(all_az)
                n_zenith = len(all_az_unfilt) - len(all_az)
                print(f"  {f'Azimuth (el≤{int(_AZ_EL_MAX)}°)':>12} | {az_m:6.2f} {az_s:6.2f} {az_p:6.2f} {az_x:6.2f} | {'YES' if az_p <= 3.0 else 'NO'}")
                if n_zenith > 0:
                    az_u_m, az_u_s, az_u_x, az_u_p = stats(all_az_unfilt)
                    print(f"  {'Azimuth (all)':>12} | {az_u_m:6.2f} {az_u_s:6.2f} {az_u_p:6.2f} {az_u_x:6.2f} | (reference; {n_zenith} near-zenith SV-epochs excluded)")
            else:
                # Every SV is above the zenith cut — fall back to unfiltered stats.
                az_u_m, az_u_s, az_u_x, az_u_p = stats(all_az_unfilt)
                print(f"  {'Azimuth (all)':>12} | {az_u_m:6.2f} {az_u_s:6.2f} {az_u_p:6.2f} {az_u_x:6.2f} | (no SVs at el≤{int(_AZ_EL_MAX)}°)")
            print(f"  {'Elevation (°)':>12} | {el_m:6.2f} {el_s:6.2f} {el_p:6.2f} {el_x:6.2f} | {'YES' if el_p <= 3.0 else 'NO'}")
            print(f"  {'C/N0 (dB)':>12} | {cn_m:6.2f} {cn_s:6.2f} {cn_p:6.2f} {cn_x:6.2f} | {'YES' if cn_p <= 3.0 else 'NO'}")

            # Per-SV
            prefix = {'GPS': 'G', 'GLONASS': 'R', 'Galileo': 'E', 'BeiDou': 'C'}.get(const_name, '?')
            for sv_id in sorted(per_sv.keys()):
                e = per_sv[sv_id]
                n = len(e['az'])
                print(f"    {prefix}{sv_id:03d} | n={n:3d} | az={sum(e['az'])/n:+6.2f} el={sum(e['el'])/n:+6.2f} cn0={sum(e['cn0'])/n:+6.2f}")


    # --- MSM7 per-signal CNR comparison ---
    if (pyrtcm_msm7_lookup or msm7) and (measurements or oemdre):
        print(f"\n=== MSM7 Per-Signal CNR Comparison ===")

        # Source preference: pyrtcm sidecar (correct DF408 dB-Hz) over
        # gpsd MSM7 (broken cnr in 3.27.6~dev). When neither is
        # present, skip the comparison rather than print known-wrong numbers.
        if pyrtcm_msm7_lookup:
            msm7_lookup = pyrtcm_msm7_lookup
            msm7_source = f"pyrtcm sidecar ({os.path.basename(pyrtcm_msm7_path)})"
        elif msm7:
            print("  WARNING: no pyrtcm sidecar found; falling back to gpsd MSM7.")
            print("  gpsd 3.27.6~dev MSM7 cnr decode is broken; P95 will be ~35 dB.")
            print(f"  Re-run the GNSS post-decode step to produce a *.pyrtcm.jsonl sidecar.")
            msm7_lookup = defaultdict(dict)
            gnssid_map = {1077: 0, 1087: 6, 1097: 2, 1127: 3}
            for m in msm7:
                tow = m.get('tow')
                if tow is None:
                    continue
                gnssid = gnssid_map.get(m.get('type'))
                if gnssid is None:
                    continue
                cells = decode_msm7_cells(m)
                for sat_prn, sig_id, sig_entry, _sat_entry in cells:
                    cnr_raw = sig_entry.get('cnr', 0)
                    if cnr_raw > 0:
                        # Stale-guess /16 retained only for the legacy fallback
                        # path; pyrtcm DF408 (preferred path above) is already
                        # in dB-Hz and needs no scaling.
                        msm7_lookup[tow][(gnssid, sat_prn, sig_id)] = cnr_raw / 16.0
            msm7_source = "gpsd MSM7 (FALLBACK — cnr scale unverified)"
        else:
            print("  No MSM7 data available (no pyrtcm sidecar, no gpsd MSM7 records)")
            msm7_lookup = defaultdict(dict)
            msm7_source = None

        if msm7_source:
            print(f"  Source: {msm7_source}")

        if not msm7_lookup:
            print("  No MSM7 data with valid CNR")
        else:
            print(f"  MSM7 epochs with data: {len(msm7_lookup)}")

            # Compare GPS measurement reports (0x1477) against MSM7 GPS L1 (sigid=2)
            if measurements:
                cn0_errors = []
                matched_epochs = 0
                for meas in measurements:
                    _, ms = _meas_gps_time(meas)
                    if ms is None:
                        continue
                    # Find closest MSM7 epoch (within 500ms)
                    best_tow = None
                    best_dt = float('inf')
                    for tow in msm7_lookup:
                        dt = abs(tow - ms)
                        if dt < best_dt:
                            best_dt = dt
                            best_tow = tow
                    if best_tow is None or best_dt > 500:
                        continue
                    matched_epochs += 1
                    msm_cells = msm7_lookup[best_tow]
                    for sv in meas['svs']:
                        sv_id = sv['sv_id']
                        diag_cn0 = sv['cno_db']  # dB-Hz directly
                        # Look up MSM7 GPS L1 (gnssid=0, sigid=2)
                        msm_cn0 = msm_cells.get((0, sv_id, 2))
                        if msm_cn0 is not None and sv['el_deg'] >= 0:
                            cn0_errors.append(diag_cn0 - msm_cn0)

                if cn0_errors:
                    n = len(cn0_errors)
                    abs_errors = [abs(e) for e in cn0_errors]
                    mean_abs = sum(abs_errors) / n
                    mean_signed = sum(cn0_errors) / n
                    p95 = sorted(abs_errors)[int(n * 0.95)]
                    print(f"\n  GPS L1 (0x1477 vs MSM7 sigid=2): {n} comparisons across {matched_epochs} epochs")
                    print(f"    Mean |error|: {mean_abs:.2f} dB, bias: {mean_signed:+.2f} dB, P95: {p95:.2f} dB, max: {max(abs_errors):.2f} dB")
                    print(f"    PASS (P95 < 3dB): {'YES' if p95 < 3.0 else 'NO'}")

            # Compare OEM DRE reports against MSM7 per-signal
            if oemdre:
                # Map OEM DRE constellation → MSM7 gnssid + L1 sigid
                oemdre_to_msm = {
                    'GPS': (0, 2), 'GLONASS': (6, 2), 'Galileo': (2, 2), 'BeiDou': (3, 2),
                }
                # DIAG SV ID → MSM7 PRN mapping
                def diag_to_prn(sv_id, const):
                    if const == 'GLONASS': return sv_id - 64
                    if const == 'Galileo': return sv_id - 300 if sv_id > 300 else sv_id
                    if const == 'BeiDou': return sv_id - 200 if sv_id > 200 else sv_id
                    return sv_id

                for const_name in ['GPS', 'GLONASS', 'Galileo', 'BeiDou']:
                    reports = oemdre_by_const.get(const_name, [])
                    if not reports:
                        continue
                    msm_gnssid, msm_sigid = oemdre_to_msm.get(const_name, (None, None))
                    if msm_gnssid is None:
                        continue

                    cn0_errors = []
                    matched = 0
                    # Pre-compute a concurrent GPS week for GLONASS fallback
                    _gps_ctx = oemdre_by_const.get('GPS', [])
                    _gps_week_ctx = _gps_ctx[0].get('gps_week') if _gps_ctx else None
                    for report in reports:
                        # Resolve UTC, then derive GPS-tow-ms for MSM7 matching.
                        utc = _resolve_oemdre_utc(report, gps_week_ctx=_gps_week_ctx)
                        if utc is None:
                            continue
                        # GPS TOW(ms) = milliseconds since week start; recover from UTC:
                        # UTC → GPS time (add leap seconds) → week start → ms offset.
                        gps_time = utc + timedelta(seconds=LEAP_SECONDS)
                        days_since_epoch = (gps_time - GPS_EPOCH).total_seconds() / 86400.0
                        dow = int(days_since_epoch) % 7  # day-of-week (GPS week starts Sunday)
                        secs_into_day = (gps_time - GPS_EPOCH).total_seconds() - int(days_since_epoch) * 86400
                        ms = int((dow * 86400 + secs_into_day) * 1000)
                        best_tow = None
                        best_dt = float('inf')
                        for tow in msm7_lookup:
                            dt = abs(tow - ms)
                            if dt < best_dt:
                                best_dt = dt
                                best_tow = tow
                        if best_tow is None or best_dt > 500:
                            continue
                        matched += 1
                        msm_cells = msm7_lookup[best_tow]
                        for sv in report.get('svs', []):
                            if sv['cno_db'] <= 0 or sv['el_deg'] < 0:
                                continue
                            prn = diag_to_prn(sv['sv_id'], const_name)
                            msm_cn0 = msm_cells.get((msm_gnssid, prn, msm_sigid))
                            if msm_cn0 is not None:
                                cn0_errors.append(sv['cno_db'] - msm_cn0)

                    if cn0_errors:
                        n = len(cn0_errors)
                        abs_errors = [abs(e) for e in cn0_errors]
                        mean_abs = sum(abs_errors) / n
                        mean_signed = sum(cn0_errors) / n
                        p95 = sorted(abs_errors)[int(n * 0.95)]
                        print(f"\n  OEM DRE {const_name} L1 vs MSM7: {n} comparisons, {matched} epochs")
                        print(f"    Mean |error|: {mean_abs:.2f} dB, bias: {mean_signed:+.2f} dB, P95: {p95:.2f} dB")
                        print(f"    PASS (P95 < 3dB): {'YES' if p95 < 3.0 else 'NO'}")


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <diag.jsonl> <gpsd.jsonl>")
        sys.exit(1)
    main()
