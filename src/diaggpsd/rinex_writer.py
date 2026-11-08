"""RINEX 3.04 Observation-file writer — Phase 1.

Converts parsed DIAG GNSS measurement records (emitted by
the diaggrok GPS / GLONASS / Galileo / OEM DRE measurement parsers)
into RINEX 3.04 observation text files suitable for post-processing
with RTKLIB, gLAB, georinex, or any other compliant toolchain.

Scope
=====

Phase 1 covers L1 single-band observations for GPS, GLONASS, Galileo,
and BeiDou with the following RINEX obs codes:

    G  GPS       C1C L1C D1C S1C
    R  GLONASS   C1C L1C D1C S1C
    E  Galileo   C1C L1C D1C S1C
    C  BeiDou    C2I L2I D2I S2I   (BDS B1I nominal)

Input records accepted:

- ``Diag0x1477``  GPS measurement report
- ``Diag0x1480``  GLONASS measurement report
- ``Diag0x1886``  Galileo measurement report (no transmit time decoded yet)
- ``Diag0x14DE``  OEM DRE measurement report (preferred when present:
                  exposes filtered pseudorange and fine-speed from the
                  same struct)

Pseudoranges come from per-SV transmit times; see ``TxTimeResolver``.

Output
------

A single RINEX 3.04 observation file (``.YYo``) per invocation.  One
epoch is emitted per incoming measurement-report packet; per-SV
observations for the same ``(gps_week, gps_milliseconds)`` epoch are
merged across constellations (GPS + GLONASS + ... on one ``>`` line).

See ``apps/diaggpsd/docs/rinex-output.md`` for spec coverage, column
layout, and edge cases (zero carrier-phase, missing observables,
leap-second handling, BeiDou time offset).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import IO, Iterable, Optional

# ---------------------------------------------------------------------------
# Physical / RINEX constants
# ---------------------------------------------------------------------------

C_LIGHT = 299_792_458.0  # m/s — exact by SI definition
GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)

# L-band centre frequencies (Hz) — L1 / B1 land in Phase 1; L2 / L5 / E5a /
# B2a land as multiband-ready infrastructure. Only the
# constants actually used by ``_extract_oemdre()`` produce RINEX output
# today; the rest are consumed by ``_extract_oemdre_multiband()`` when a
# capture yields per-SV ``band`` labels.
F_L1_GPS = 1_575_420_000.0          # GPS L1 C/A
LAMBDA_L1_GPS = C_LIGHT / F_L1_GPS  # 0.19029 m
F_L5_GPS = 1_176_450_000.0          # GPS L5 (same as Galileo E5a, BDS B2a)
LAMBDA_L5_GPS = C_LIGHT / F_L5_GPS  # 0.25483 m
F_E1_GAL = 1_575_420_000.0
LAMBDA_E1_GAL = C_LIGHT / F_E1_GAL
F_E5A_GAL = 1_176_450_000.0         # Galileo E5a (shares L5 band)
LAMBDA_E5A_GAL = C_LIGHT / F_E5A_GAL
F_E5B_GAL = 1_207_140_000.0         # Galileo E5b (same carrier as BDS B2b)
LAMBDA_E5B_GAL = C_LIGHT / F_E5B_GAL
F_E6_GAL = 1_278_750_000.0          # Galileo E6 (commercial service)
LAMBDA_E6_GAL = C_LIGHT / F_E6_GAL
F_B1I_BDS = 1_561_098_000.0         # BDS B1I (legacy)
LAMBDA_B1I_BDS = C_LIGHT / F_B1I_BDS
F_B1C_BDS = 1_575_420_000.0         # BDS B1C (new civil, GPS L1 band)
LAMBDA_B1C_BDS = C_LIGHT / F_B1C_BDS
F_B2A_BDS = 1_176_450_000.0         # BDS B2a (shares L5 band)
LAMBDA_B2A_BDS = C_LIGHT / F_B2A_BDS
F_B2B_BDS = 1_207_140_000.0         # BDS B2b (same carrier as Galileo E5b)
LAMBDA_B2B_BDS = C_LIGHT / F_B2B_BDS

# GLONASS L1 is FDMA: f_k = 1602 MHz + k*0.5625 MHz ; k is freq_index
F_L1_GLO_BASE = 1_602_000_000.0
F_L1_GLO_STEP = 562_500.0
# GLONASS L2 is FDMA with a different base: f_k = 1246 MHz + k*0.4375 MHz
# (ratio 7/9 of L1 — preserves the same k encoding across bands).
F_L2_GLO_BASE = 1_246_000_000.0
F_L2_GLO_STEP = 437_500.0

# DIAG "carrier_phase not locked" sentinel on OEM DRE records
CARRIER_PHASE_INVALID = 0x8000000000000000

# Leap seconds applied to GPS→UTC conversion as of 2026.  GPS ahead of UTC
# by this many seconds.  Last IERS insertion: 2017-01-01 (18 s) — no new
# insertions announced through 2026-Q2.  Writer uses GPS time in RINEX
# body records per the RINEX 3.04 spec ("TIME OF FIRST OBS" marker = GPS),
# so this constant is only used when the caller requests UTC epoch labels.
GPS_UTC_LEAP_SECONDS = 18


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RinexHeaderConfig:
    """RINEX 3.04 header metadata.  All fields optional — safe defaults used."""
    marker_name: str = "DIAGGPSD"
    marker_number: str = ""
    observer: str = "UNKNOWN"
    agency: str = "diaggpsd"
    receiver_number: str = ""
    receiver_type: str = "UNKNOWN"
    receiver_version: str = ""
    antenna_number: str = ""
    antenna_type: str = "UNKNOWN"
    antenna_delta_hen: tuple[float, float, float] = (0.0, 0.0, 0.0)
    approx_position_xyz: tuple[float, float, float] = (0.0, 0.0, 0.0)
    program: str = "diaggpsd-rinex"
    run_by: str = "diaggpsd"
    comment_lines: tuple[str, ...] = ()


# Obs code order per constellation — RINEX 3.04 SYS / # / OBS TYPES.
# Keep in sync with the per-SV writer below.
OBS_CODES: dict[str, tuple[str, ...]] = {
    "G": ("C1C", "L1C", "D1C", "S1C"),
    "R": ("C1C", "L1C", "D1C", "S1C"),
    "E": ("C1C", "L1C", "D1C", "S1C"),
    "C": ("C2I", "L2I", "D2I", "S2I"),
}

# Multiband observation codes — one entry per (constellation, band).
# A band is addressable when the extractor emits ``band`` on an SvObs and
# that pair has a row in this table. The header writer expands the active
# bands into a single SYS/#/OBS-TYPES line per constellation by concatenating
# the entries for every band seen in the epoch stream.
#
# Constellation → band → obs codes. RINEX 3.04 names:
#
#   GPS     L5 :  C5Q L5Q D5Q S5Q   (Q tracking-code suffix; I-channel is 5I)
#   GLONASS L2 :  C2C L2C D2C S2C
#   Galileo E5a:  C5Q L5Q D5Q S5Q
#   BeiDou  B2a:  C5P L5P D5P S5P   (B2a pilot component, RINEX 3.04 §5.1)
OBS_CODES_BY_BAND: dict[str, dict[str, tuple[str, ...]]] = {
    "G": {
        "L1": ("C1C", "L1C", "D1C", "S1C"),
        "L5": ("C5Q", "L5Q", "D5Q", "S5Q"),
    },
    "R": {
        "L1": ("C1C", "L1C", "D1C", "S1C"),
        "G1": ("C1C", "L1C", "D1C", "S1C"),
        "L2": ("C2C", "L2C", "D2C", "S2C"),
    },
    "E": {
        "E1": ("C1C", "L1C", "D1C", "S1C"),
        "L1": ("C1C", "L1C", "D1C", "S1C"),
        "E5a": ("C5Q", "L5Q", "D5Q", "S5Q"),
        "L5": ("C5Q", "L5Q", "D5Q", "S5Q"),
        "E5b": ("C7Q", "L7Q", "D7Q", "S7Q"),
        "E6": ("C6B", "L6B", "D6B", "S6B"),
    },
    "C": {
        "B1": ("C2I", "L2I", "D2I", "S2I"),
        "B1I": ("C2I", "L2I", "D2I", "S2I"),
        "B1C": ("C1P", "L1P", "D1P", "S1P"),
        "B2a": ("C5P", "L5P", "D5P", "S5P"),
        "L5": ("C5P", "L5P", "D5P", "S5P"),
        "B2b": ("C7D", "L7D", "D7D", "S7D"),
    },
}

# Band → carrier wavelength lookup, keyed by (system, band).  When an
# extractor yields an SvObs with a recognised band label the writer uses
# this table to compute the Doppler frequency shift from ``fine_speed_mps``.
# Entries for GLONASS are computed at runtime because each freq_index
# slot has its own wavelength.
BAND_WAVELENGTHS: dict[tuple[str, str], float] = {
    ("G", "L1"):  LAMBDA_L1_GPS,
    ("G", "L5"):  LAMBDA_L5_GPS,
    ("E", "E1"):  LAMBDA_E1_GAL,
    ("E", "L1"):  LAMBDA_E1_GAL,
    ("E", "E5a"): LAMBDA_E5A_GAL,
    ("E", "L5"):  LAMBDA_E5A_GAL,
    ("E", "E5b"): LAMBDA_E5B_GAL,
    ("E", "E6"):  LAMBDA_E6_GAL,
    ("C", "B1"):  LAMBDA_B1I_BDS,
    ("C", "B1I"): LAMBDA_B1I_BDS,
    ("C", "B1C"): LAMBDA_B1C_BDS,
    ("C", "B2a"): LAMBDA_B2A_BDS,
    ("C", "L5"):  LAMBDA_B2A_BDS,
    ("C", "B2b"): LAMBDA_B2B_BDS,
}


# ---------------------------------------------------------------------------
# Per-SV observation row (intermediate representation)
# ---------------------------------------------------------------------------

@dataclass
class SvObs:
    """One SV's single-band observation at a single epoch.

    Constellation-agnostic.  ``system`` is the RINEX 3.04 one-letter
    constellation code (G/R/E/C/J/S).  Missing observables are ``None``.

    ``band`` is the signal-band label (e.g. ``"L1"``, ``"L5"``,
    ``"E5a"``, ``"B2a"``, ``"L2"`` for GLONASS). Default ``"L1"`` preserves
    the original single-band behaviour; multiband extractors set it
    per-SV so the writer can pick the matching RINEX obs codes from
    ``OBS_CODES_BY_BAND``. A multiband epoch represents one physical SV
    with two SvObs rows — one per band — sharing the same (system, prn).
    """
    system: str          # 'G' | 'R' | 'E' | 'C'
    prn: int             # 1..32 for G, 1..24 for R, ...
    pseudorange_m: Optional[float] = None
    carrier_phase_cycles: Optional[float] = None
    doppler_hz: Optional[float] = None
    snr_dbhz: Optional[float] = None
    band: str = "L1"     # Signal-band label; see OBS_CODES_BY_BAND


@dataclass
class Epoch:
    """One epoch's worth of observations across all constellations."""
    gps_week: int
    gps_tow_ms: int
    observations: list[SvObs] = field(default_factory=list)

    def epoch_datetime_gps(self) -> datetime:
        """Return the epoch instant in GPS time (no leap-second correction).

        RINEX 3.04 TIME OF FIRST OBS "GPS" designator means body records
        are GPS-time, which is what we emit.
        """
        return GPS_EPOCH + timedelta(
            weeks=self.gps_week, milliseconds=self.gps_tow_ms
        )


# ---------------------------------------------------------------------------
# GLONASS time → GPS time
# ---------------------------------------------------------------------------

MS_PER_WEEK = 604_800_000

# GLONASS system time is UTC(SU) = UTC + 3 h, leap seconds included.
GLONASS_UTC_OFFSET = timedelta(hours=3)

# GPS - UTC in seconds, from each leap second's effective date (UTC).
_GPS_UTC_LEAP_S = (
    (datetime(2006, 1, 1, tzinfo=timezone.utc), 14),
    (datetime(2009, 1, 1, tzinfo=timezone.utc), 15),
    (datetime(2012, 7, 1, tzinfo=timezone.utc), 16),
    (datetime(2015, 7, 1, tzinfo=timezone.utc), 17),
    (datetime(2017, 1, 1, tzinfo=timezone.utc), 18),
)


def _gps_minus_utc_s(utc: datetime) -> Optional[int]:
    """Leap-second count GPS - UTC at ``utc``; None before the table starts."""
    leap = None
    for effective, value in _GPS_UTC_LEAP_S:
        if utc >= effective:
            leap = value
    return leap


def glonass_to_gps_epoch(cycle: int, day: int,
                         ms_of_day: Optional[int]) -> Optional[tuple[int, int]]:
    """Convert a GLONASS (N4, N_T, ms of day) time to ``(gps_week, gps_tow_ms)``.

    ``cycle`` is the four-year interval number N4 (1 starts 1996-01-01) and
    ``day`` the day within it, N_T (1 = 1 January of the cycle's first year),
    as carried in the 0x1480 header. Returns None for a zero or out-of-range
    date, or a date before the leap-second table.
    """
    if not cycle or not day or not (1 <= day <= 1461):
        return None
    if ms_of_day is None or not (0 <= ms_of_day < 86_400_000):
        return None
    start = datetime(1996 + 4 * (cycle - 1), 1, 1, tzinfo=timezone.utc)
    utc = start + timedelta(days=day - 1, milliseconds=ms_of_day) - GLONASS_UTC_OFFSET
    leap = _gps_minus_utc_s(utc)
    if leap is None:
        return None
    delta = utc + timedelta(seconds=leap) - GPS_EPOCH
    total_ms = (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000
    return total_ms // MS_PER_WEEK, total_ms % MS_PER_WEEK


# A receiver that has not resolved time reports GPS week 0xFFFF (and GLONASS
# cycle 255, day 0xFFFF, which ``glonass_to_gps_epoch`` already rejects). Its
# report ms is then a free-running count, not a time of week.
GPS_WEEK_UNKNOWN = 0xFFFF


def _gps_time(rec: dict) -> Optional[tuple[int, int]]:
    """``(gps_week, tow_ms)`` from a record's GPS time fields, if it has a time."""
    week = rec.get("gps_week")
    tow_ms = rec.get("gps_milliseconds", rec.get("gps_ms"))
    if week and tow_ms and week != GPS_WEEK_UNKNOWN:
        return (week, tow_ms)
    return None


def _own_epoch(rec: dict) -> Optional[tuple[int, int]]:
    """The GPS epoch a record states itself, from GPS or GLONASS time fields.

    None when the record carries neither; the caller may then borrow the
    epoch of a GPS-time record at the same DIAG tick.
    """
    gps = _gps_time(rec)
    if gps is not None:
        return gps
    cycle = rec.get("glonass_cycle_number")
    day = rec.get("glonass_days")
    if cycle and day:
        return glonass_to_gps_epoch(cycle, day, rec.get("milliseconds"))
    return None


# Bias-corrected instants of one measurement's GPS and GLONASS reports agree
# to ~5 us (the inter-system clock bias). Their integer ms can still differ
# by 1 when the receiver clock bias is near +/-0.5 ms.
SAME_INSTANT_MS = 0.05


def _time_bias_ms(rec: dict) -> float:
    return rec.get("time_bias", rec.get("gps_time_bias")) or 0.0


def _same_instant(epoch: tuple[int, int], bias_ms: float,
                  ref: tuple[int, int], ref_bias_ms: float) -> bool:
    """True when two report times, each ``ms - time_bias``, are one instant."""
    if epoch == ref:
        return True
    ms = epoch[0] * MS_PER_WEEK + epoch[1]
    ref_ms = ref[0] * MS_PER_WEEK + ref[1]
    if abs(ms - ref_ms) > 1:
        return False
    return abs((ms - bias_ms) - (ref_ms - ref_bias_ms)) < SAME_INSTANT_MS


def _snap_to_gps_epoch(rec: dict, own: tuple[int, int],
                       gps_bias: dict[tuple[int, int], float]) -> tuple[int, int]:
    """Move a GLONASS-time epoch onto the GPS epoch of the same instant.

    GPS-time records keep their own epoch. ``gps_bias`` maps each GPS epoch
    in the stream to its report's time bias.
    """
    if _gps_time(rec) is not None or own in gps_bias:
        return own
    bias = _time_bias_ms(rec)
    week, tow = own
    for ref in ((week, tow - 1), (week, tow + 1)):
        if ref in gps_bias and _same_instant(own, bias, ref, gps_bias[ref]):
            return ref
    return own


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def _pseudorange_ms_to_m(integral_ms: int, fraction_ms: float) -> float:
    """Convert a signal travel time (integer ms + fractional ms) → metres.

    RINEX C1C is the raw pseudorange in metres = travel_ms * c / 1000.
    Feed it a travel time from ``_travel_ms`` / ``TxTimeResolver``, never a
    DIAG ``*_pseudorange_ms_*`` pair directly: those carry the SV transmit
    time (see ``TxTimeResolver``).
    """
    total_ms = integral_ms + fraction_ms
    return total_ms * C_LIGHT / 1000.0


# Signal travel time (ms) from a medium-earth-orbit SV to a ground receiver:
# GLONASS at zenith is ~63.8 ms, Galileo at the horizon ~96.4 ms. The
# receiver clock offset left in a raw pseudorange is common to every SV in an
# epoch and small next to this window once the report's time bias is removed.
TRAVEL_MS_MIN = 60.0
TRAVEL_MS_MAX = 100.0


def _in_travel_window(travel_ms: float) -> bool:
    return TRAVEL_MS_MIN <= travel_ms <= TRAVEL_MS_MAX


def _travel_ms(report_ms: float, time_bias_ms: float,
               tx_integral_ms: int, tx_fraction_ms: float) -> Optional[float]:
    """Signal travel time, or None when it cannot be a real satellite range.

    ``tx_*`` is the SV transmit time in the same ms-of-week (GPS) or
    ms-of-day (GLONASS) scale as ``report_ms``.
    """
    travel = report_ms - time_bias_ms - (tx_integral_ms + tx_fraction_ms)
    return travel if _in_travel_window(travel) else None


def _is_tracked(sv: dict) -> bool:
    """True for an SV slot that carries a live measurement.

    Measurement reports also list idle and searching SVs. Those slots have
    ``cno_db == 0`` and a predicted, not measured, transmit time, which still
    yields a plausible-looking travel time. C/N0 is what separates them: on
    0x1480, ``obs_state`` 1 (F3 ``sv_state`` idle) and the all-zero filler
    slots always have zero C/N0, and every slot with C/N0 > 0 is in a
    tracking state (4, 5 or 7) across SDX20, SDX55, SDX62, MDM9x07, MDM9x30
    and MDM9x50 captures.
    """
    return (sv.get("cno_db") or 0.0) > 0.0


class TxTimeResolver:
    """Turn 0x1477 / 0x1480 per-SV transmit times into travel times.

    ``pseudorange_ms_integral + pseudorange_ms_fraction`` on these reports is
    the SV transmit time in ms of the GPS week (0x1477) or of the GLONASS day
    (0x1480), not a range. Two firmware conventions place it relative to the
    report time:

    - ``BASE``: already propagated to the report epoch, so the travel time is
      ``report_ms - time_bias - tx_ms`` (SDX55 and SDX62 builds).
    - ``LATENCY``: stamped ``latency`` ms before the report, so the travel time
      is that minus ``latency`` (SDX20, MDM9x07, MDM9x30 and MDM9x50 builds).

    Both conventions report version 0, so the convention is learned from the
    stream. A record is decisive when all of its tracked SVs land inside the
    travel window under one rule and at least one does not under the other.
    The rule with more decisive records wins. Until a stream has produced one,
    an SV gets a travel time only when both rules agree on it.

    Keep one resolver per stream. Records of different types are tallied
    separately.
    """

    BASE = "base"
    LATENCY = "latency"

    def __init__(self) -> None:
        self._votes: dict[str, dict[str, int]] = {}

    @staticmethod
    def _candidates(report_ms: float, time_bias: float,
                    sv: dict) -> tuple[float, float]:
        tx = sv.get("pseudorange_ms_integral", 0) + sv.get("pseudorange_ms_fraction", 0.0)
        base = report_ms - time_bias - tx
        return base, base - sv.get("latency", 0)

    def mode(self, rtype: str) -> Optional[str]:
        votes = self._votes.get(rtype)
        if not votes or votes[self.BASE] == votes[self.LATENCY]:
            return None
        return self.BASE if votes[self.BASE] > votes[self.LATENCY] else self.LATENCY

    def observe(self, rtype: str, report_ms: Optional[float],
                time_bias: float, svs: Iterable[dict]) -> None:
        if report_ms is None:
            return
        pairs = [self._candidates(report_ms, time_bias, sv)
                 for sv in svs if _is_tracked(sv)]
        if not pairs:
            return
        base_ok = all(_in_travel_window(b) for b, _ in pairs)
        lat_ok = all(_in_travel_window(lat) for _, lat in pairs)
        if base_ok == lat_ok:
            return
        votes = self._votes.setdefault(rtype, {self.BASE: 0, self.LATENCY: 0})
        votes[self.BASE if base_ok else self.LATENCY] += 1

    def travel_ms(self, rtype: str, report_ms: Optional[float],
                  time_bias: float, sv: dict) -> Optional[float]:
        if report_ms is None:
            return None
        base, lat = self._candidates(report_ms, time_bias, sv)
        mode = self.mode(rtype)
        if mode is None:
            if base == lat and _in_travel_window(base):
                return base
            return None
        travel = base if mode == self.BASE else lat
        return travel if _in_travel_window(travel) else None


def _carrier_phase_diag_to_cycles(integral: int, fraction: int) -> float:
    """Combine DIAG ``carrier_phase_integral`` (i32) + ``_fraction`` (u16/65536).

    Returns total cycles as a float.  Zero means "not tracked" on many
    SDX20-family captures — caller should treat 0.0 as "no observation".
    """
    return integral + fraction / 65536.0


def _fine_speed_to_doppler_hz(fine_speed_mps: float, wavelength_m: float) -> float:
    """Doppler frequency shift (Hz) from line-of-sight speed (m/s).

    RINEX convention: **positive Doppler = approaching satellite**.  DIAG
    ``fine_speed_mps`` on every validated chipset is signed with
    **positive = moving away from SV**, so we negate.
    """
    if wavelength_m == 0.0:
        return 0.0
    return -fine_speed_mps / wavelength_m


def _glonass_sv_id_to_slot(sv_id: int) -> int:
    """Map the DIAG GLONASS sv_id encoding → RINEX slot number (1-24).

    Validated on LM960 captures: GLONASS sv_id starts at 65
    (slot 1).  A few captures use the 0-based offset (73 → slot 9); we
    accept both by treating values >= 65 as offset-encoded.
    """
    if sv_id >= 65:
        return sv_id - 64
    return sv_id


def _glonass_wavelength(freq_index: int, band: str = "L1") -> float:
    """Carrier wavelength (m) for a GLONASS FDMA slot.

    ``band`` defaults to L1 for backwards compatibility; pass ``"L2"`` to
    get the L2-band carrier for the same freq_index slot. GLONASS L2 uses
    a different base frequency and step but the same k encoding.
    """
    if band == "L2":
        f = F_L2_GLO_BASE + freq_index * F_L2_GLO_STEP
    else:
        f = F_L1_GLO_BASE + freq_index * F_L1_GLO_STEP
    return C_LIGHT / f


def _format_obs(val: Optional[float]) -> str:
    """Format one RINEX 3.04 observation field.

    Width 16: F14.3 value + 1 LLI + 1 signal-strength-indicator.  Blank
    when the observable is missing (None, NaN, or exact zero for carrier
    phase — see spec §5.1).
    """
    if val is None:
        return " " * 16
    try:
        fval = float(val)
    except (TypeError, ValueError):
        return " " * 16
    if fval != fval:  # NaN
        return " " * 16
    # F14.3 with blank LLI + SSI suffix
    return f"{fval:14.3f}  "


# ---------------------------------------------------------------------------
# Record → SvObs extractors (one per input dataclass type)
# ---------------------------------------------------------------------------

def _resolved_pseudorange_m(report: dict, sv: dict,
                            resolver: TxTimeResolver,
                            report_ms_key: str) -> Optional[float]:
    travel = resolver.travel_ms(
        report.get("type", ""), report.get(report_ms_key),
        report.get("time_bias") or 0.0, sv,
    )
    return None if travel is None else _pseudorange_ms_to_m(0, travel)


def _observe(report: dict, resolver: TxTimeResolver, report_ms_key: str) -> None:
    resolver.observe(
        report.get("type", ""), report.get(report_ms_key),
        report.get("time_bias") or 0.0, report.get("svs", []),
    )


def _extract_gps(report: dict,
                 resolver: Optional[TxTimeResolver] = None) -> Iterable[SvObs]:
    """Yield SvObs rows from a 0x1477 GPS measurement report dict."""
    resolver = resolver or TxTimeResolver()
    _observe(report, resolver, "gps_milliseconds")
    for sv in report.get("svs", []):
        sv_id = sv.get("sv_id")
        if sv_id is None or not (1 <= sv_id <= 32):
            # SBAS (sv_id 33-64) would need 'S' constellation encoding —
            # skip for Phase 1 to avoid cross-constellation ambiguity.
            continue
        if not _is_tracked(sv):
            continue
        pseudorange_m = _resolved_pseudorange_m(
            report, sv, resolver, "gps_milliseconds")
        cp_cycles = _carrier_phase_diag_to_cycles(
            sv.get("carrier_phase_integral", 0),
            sv.get("carrier_phase_fraction", 0),
        )
        # Empirical: CP==0 means "not locked" on SDX20 V2 — emit blank.
        cp_out = cp_cycles if cp_cycles != 0.0 else None
        doppler = _fine_speed_to_doppler_hz(
            sv.get("fine_speed", sv.get("speed_raw_mps", 0.0)),
            LAMBDA_L1_GPS,
        )
        yield SvObs(
            system="G",
            prn=sv_id,
            pseudorange_m=pseudorange_m,
            carrier_phase_cycles=cp_out,
            doppler_hz=doppler,
            snr_dbhz=sv.get("cno_db"),
        )


def _extract_glonass(report: dict,
                     resolver: Optional[TxTimeResolver] = None) -> Iterable[SvObs]:
    """Yield SvObs rows from a 0x1480 GLONASS measurement report dict.

    ``milliseconds`` is GLONASS ms of day, the same scale as the per-SV
    transmit time.
    """
    resolver = resolver or TxTimeResolver()
    _observe(report, resolver, "milliseconds")
    for sv in report.get("svs", []):
        sv_id = sv.get("sv_id")
        if sv_id is None:
            continue
        slot = _glonass_sv_id_to_slot(sv_id)
        if not (1 <= slot <= 24):
            continue
        if not _is_tracked(sv):
            continue
        freq_index = sv.get("freq_index", 0)
        lam = _glonass_wavelength(freq_index)
        pseudorange_m = _resolved_pseudorange_m(
            report, sv, resolver, "milliseconds")
        cp_cycles = _carrier_phase_diag_to_cycles(
            sv.get("carrier_phase_integral", 0),
            sv.get("carrier_phase_fraction", 0),
        )
        cp_out = cp_cycles if cp_cycles != 0.0 else None
        # GLONASS parser exposes both ``fine_speed`` and ``speed_mps`` — the
        # latter is the parser's canonical field on OEM DRE builds; accept
        # either.
        speed = sv.get("fine_speed", sv.get("speed_mps", sv.get("speed_raw_mps", 0.0)))
        doppler = _fine_speed_to_doppler_hz(speed, lam)
        yield SvObs(
            system="R",
            prn=slot,
            pseudorange_m=pseudorange_m,
            carrier_phase_cycles=cp_out,
            doppler_hz=doppler,
            snr_dbhz=sv.get("cno_db"),
        )


def _extract_galileo(report: dict,
                     resolver: Optional[TxTimeResolver] = None) -> Iterable[SvObs]:
    """Yield SvObs rows from a 0x1886 Galileo measurement report dict.

    No 0x1886 layout the parser decodes today exposes ``sv_id`` or a
    transmit time (its SV slots carry ``prn`` and unlabelled floats), so this
    yields nothing on real records. It reads the field names 0x1477 / 0x1480
    use, and starts producing rows if the parser ever exposes them.
    """
    resolver = resolver or TxTimeResolver()
    _observe(report, resolver, "gps_milliseconds")
    for sv in report.get("svs", []):
        sv_id = sv.get("sv_id")
        if sv_id is None or not (1 <= sv_id <= 36):
            continue
        if not _is_tracked(sv):
            continue
        pseudorange_m = _resolved_pseudorange_m(
            report, sv, resolver, "gps_milliseconds")
        cp_cycles = _carrier_phase_diag_to_cycles(
            sv.get("carrier_phase_integral", 0),
            sv.get("carrier_phase_fraction", 0),
        )
        cp_out = cp_cycles if cp_cycles != 0.0 else None
        doppler = _fine_speed_to_doppler_hz(
            sv.get("fine_speed", sv.get("speed_mps", sv.get("speed_raw_mps", 0.0))),
            LAMBDA_E1_GAL,
        )
        yield SvObs(
            system="E",
            prn=sv_id,
            pseudorange_m=pseudorange_m,
            carrier_phase_cycles=cp_out,
            doppler_hz=doppler,
            snr_dbhz=sv.get("cno_db"),
        )


def _extract_oemdre(report: dict,
                    resolver: Optional[TxTimeResolver] = None) -> Iterable[SvObs]:
    """Yield SvObs rows from an ``OemdreMeasurementReport`` dict.

    OEM DRE exposes a ``constellation`` field (``GPS`` / ``GLONASS`` /
    ``GALILEO`` / ``BEIDOU``) and a ``band`` field. Any band that appears
    in ``OBS_CODES_BY_BAND`` is accepted — L1-family (L1/G1/E1/B1) is the
    Phase 1 path; L5-family (L5/E5a/B2a) and GLONASS L2 unlock through
    ``BAND_WAVELENGTHS`` for the Doppler calculation.
    """
    band = report.get("band", "L1")
    # The diaggrok OEM DRE parser emits 'L5?' for seq 6/7 (un-validated
    # multiband slot hypothesis — see gnss_oemdre.py). Strip the trailing
    # '?' for RINEX output: once the multiband slot emits real SV data, we
    # want it to flow through under the concrete band label.
    if band.endswith("?"):
        band = band.rstrip("?")
    # Case-fold GALILEO variants from upstream parsers.
    constellation_raw = report.get("constellation", "GPS")
    constellation = {
        "Galileo": "GALILEO", "BeiDou": "BEIDOU", "Glonass": "GLONASS",
        "Gps": "GPS",
    }.get(constellation_raw, constellation_raw.upper())
    system_map = {"GPS": "G", "GLONASS": "R", "GALILEO": "E", "BEIDOU": "C"}
    system = system_map.get(constellation)
    if system is None:
        return
    # Reject bands the writer has no obs-code entry for. This also keeps
    # SBAS-only seq slots from sneaking through (they land on 'L1' but
    # the sv_id filter below rejects them as non-GPS).
    if band not in OBS_CODES_BY_BAND.get(system, {}):
        return
    # Select a per-system wavelength for Doppler computation. GLONASS
    # uses a per-SV override below because every FDMA slot has its own
    # carrier, so the top-level ``lam`` is just a fallback.
    lam = BAND_WAVELENGTHS.get((system, band), LAMBDA_L1_GPS)

    for sv in report.get("svs", []):
        sv_id = sv.get("prn", sv.get("sv_id"))
        if sv_id is None:
            continue
        if system == "R":
            slot = _glonass_sv_id_to_slot(sv_id)
            if not (1 <= slot <= 24):
                continue
            prn = slot
            freq_index = sv.get("glo_freq_index", 0)
            # GLONASS L2 uses a different FDMA plan — forward the band
            # label into _glonass_wavelength so the doppler helper sees
            # the right carrier.
            per_sv_lam = _glonass_wavelength(
                freq_index, band="L2" if band == "L2" else "L1",
            )
        else:
            if system == "G" and not (1 <= sv_id <= 32):
                continue
            if system == "E" and not (1 <= sv_id <= 36):
                continue
            if system == "C" and not (1 <= sv_id <= 63):
                continue
            prn = sv_id
            per_sv_lam = lam
        # The *_pseudorange_ms_* pair is the SV transmit time, already at the
        # report epoch (latency 0 on every OEM DRE build seen). GLONASS counts
        # it in GLONASS ms of day, every other system in GPS ms of week. BeiDou
        # time runs 14 s behind GPS, so BeiDou rows fall outside the travel
        # window here and get a blank pseudorange rather than a wrong one.
        if system == "R":
            report_ms, bias = report.get("glo_milliseconds"), report.get("glo_time_bias")
        else:
            report_ms, bias = report.get("gps_milliseconds"), report.get("gps_time_bias")
        travel = None
        if report_ms is not None:
            travel = _travel_ms(
                report_ms, bias or 0.0,
                sv.get("filtered_pseudorange_ms_integral",
                       sv.get("unfiltered_pseudorange_ms_integral", 0)),
                sv.get("filtered_pseudorange_ms_fraction",
                       sv.get("unfiltered_pseudorange_ms_fraction", 0.0)),
            )
        pseudorange_m = None if travel is None else _pseudorange_ms_to_m(0, travel)
        cp_raw = sv.get("carrier_phase", 0)
        if cp_raw == CARRIER_PHASE_INVALID or cp_raw == 0:
            cp_out = None
        else:
            # OEM DRE carrier_phase is already in cycles (×1 units) per
            # the parser's conversion; preserve as float.
            cp_out = float(cp_raw) / 65536.0
        doppler = _fine_speed_to_doppler_hz(
            sv.get("fine_speed_mps", sv.get("filtered_speed_mps", 0.0)),
            per_sv_lam,
        )
        yield SvObs(
            system=system,
            prn=prn,
            pseudorange_m=pseudorange_m,
            carrier_phase_cycles=cp_out,
            doppler_hz=doppler,
            snr_dbhz=sv.get("cno_db"),
            band=band,
        )


_EXTRACTORS = {
    # Parsers emit the Diag0xNNNN class name as ``type``; the older names are
    # still accepted for recorded JSONL.
    "GpsMeasurementReport": _extract_gps,
    "Diag0x1477": _extract_gps,
    "GlonassMeasurementReport": _extract_glonass,
    "Diag0x1480": _extract_glonass,
    "GalileoMeasurementReport": _extract_galileo,
    "Diag0x1886": _extract_galileo,
    # gnss_oemdre.py split; accept both old + new 'type' strings
    "OemdreMeasurementReport": _extract_oemdre,
    "Diag0x14DE": _extract_oemdre,
}


def record_to_svobs(record: dict,
                    resolver: Optional[TxTimeResolver] = None) -> list[SvObs]:
    """Dispatch a single parser-output dict → list[SvObs].

    Unknown record types → empty list (caller filters). Pass the stream's
    ``TxTimeResolver`` so 0x1477 / 0x1480 records share what earlier records
    taught it.
    """
    extractor = _EXTRACTORS.get(record.get("type", ""))
    if extractor is None:
        return []
    return list(extractor(record, resolver))


# ---------------------------------------------------------------------------
# Epoch accumulation
# ---------------------------------------------------------------------------

def group_records_by_epoch(records: Iterable[dict],
                           resolver: Optional[TxTimeResolver] = None) -> list[Epoch]:
    """Group parser-output records by (gps_week, gps_ms) → Epoch list.

    When multiple record types cover the same ``(system, prn)`` at a given
    epoch (e.g. ``GpsMeasurementReport`` and ``OemdreMeasurementReport``
    both publishing GPS L1 from the same 1 Hz fix), the OEM DRE record
    takes precedence because it exposes the filtered pseudorange and
    fine-speed values downstream tools expect.

    GLONASS-time reports are converted to GPS time. Reports with neither a
    GPS nor a GLONASS timestamp, and no GPS-time sibling at the same
    ``log_time``, are dropped.
    """
    # priority: higher = preferred when (system, prn) collides within epoch
    source_priority = {
        # gnss_oemdre.py split; accept both old + new 'type' strings
        "OemdreMeasurementReport": 10,
        "Diag0x14DE": 10,
        # gnss_gps.py split; accept both
        "GpsMeasurementReport": 5,
        "Diag0x1477": 5,
        "GlonassMeasurementReport": 5,
        "Diag0x1480": 5,
        "GalileoMeasurementReport": 5,
        "Diag0x1886": 5,
    }
    # Some record types use the modem's internal constellation-specific
    # time (GLONASS OEM DRE → glonass_cycle_number/days/ms; pure
    # GlonassMeasurementReport → same) and leave GPS fields zeroed.
    # ``_own_epoch`` converts that GLONASS time to GPS time. On SDX62 the
    # 0x1480 and 0x1477 of one measurement are ~1,700 ticks apart, so the
    # conversion is the only way they meet. A record with neither time
    # borrows the GPS epoch of a sibling record logged at the same
    # ``log_time`` (monotonic DIAG tick). Two-pass: pass 1 builds the
    # log_time → epoch map from GPS-time-bearing records; pass 2 resolves
    # the rest.
    records_list = list(records)
    resolver = resolver or TxTimeResolver()
    log_time_to_epoch: dict[int, tuple[int, int]] = {}
    gps_bias: dict[tuple[int, int], float] = {}
    for rec in records_list:
        rtype = rec.get("type", "")
        if rtype not in _EXTRACTORS:
            continue
        gps = _gps_time(rec)
        lt = rec.get("log_time")
        if gps is not None:
            gps_bias.setdefault(gps, _time_bias_ms(rec))
            if lt is not None:
                log_time_to_epoch.setdefault(lt, gps)

    def _resolve_epoch(rec: dict) -> Optional[tuple[int, int]]:
        own = _own_epoch(rec)
        if own is not None:
            return _snap_to_gps_epoch(rec, own, gps_bias)
        # Fall back to sibling GPS-time-bearing record at the same tick.
        lt = rec.get("log_time")
        if lt is not None and lt in log_time_to_epoch:
            return log_time_to_epoch[lt]
        return None

    # epoch_key → {(system, prn, band): (priority, SvObs)}
    # Keying on band alongside (system, prn) means multiband rows don't
    # clobber each other: one physical SV at one epoch can contribute
    # one SvObs per band, each picked up by the writer under its own
    # obs-code group.
    obs_by_epoch: dict[
        tuple[int, int],
        dict[tuple[str, int, str], tuple[int, SvObs]],
    ] = {}
    for rec in records_list:
        rtype = rec.get("type", "")
        if rtype not in _EXTRACTORS:
            continue
        key = _resolve_epoch(rec)
        if key is None:
            continue
        prio = source_priority.get(rtype, 0)
        slot = obs_by_epoch.setdefault(key, {})
        for obs in record_to_svobs(rec, resolver):
            sv_key = (obs.system, obs.prn, obs.band)
            existing = slot.get(sv_key)
            if existing is None or existing[0] < prio:
                slot[sv_key] = (prio, obs)
    out: list[Epoch] = []
    for (week, tow_ms), sv_dict in obs_by_epoch.items():
        ep = Epoch(gps_week=week, gps_tow_ms=tow_ms)
        ep.observations = [obs for _, obs in sv_dict.values()]
        out.append(ep)
    return sorted(out, key=lambda e: (e.gps_week, e.gps_tow_ms))


# ---------------------------------------------------------------------------
# RINEX 3.04 file writer
# ---------------------------------------------------------------------------

class RinexObsWriter:
    """Stream-oriented RINEX 3.04 observation file writer.

    Usage::

        with open("out.23o", "w") as fh:
            w = RinexObsWriter(fh, config=RinexHeaderConfig(...))
            w.write_header(epochs[0])
            for epoch in epochs:
                w.write_epoch(epoch)

    Header must be written before any epoch.  All output is ASCII text;
    the writer does not close the underlying stream.
    """

    def __init__(self, stream: IO[str], config: Optional[RinexHeaderConfig] = None):
        self._fh = stream
        self._cfg = config or RinexHeaderConfig()
        self._header_written = False
        # system → list of (band, obs_codes_tuple) entries in the order they
        # appear on the header's SYS/#/OBS TYPES line. Populated by
        # write_header(); read by write_epoch() to map SvObs.band → column.
        self._bands_per_system: dict[str, list[tuple[str, tuple[str, ...]]]] = {}

    # --- header ----------------------------------------------------------

    def _line(self, body: str, label: str) -> str:
        """One RINEX header line: body padded to col 60, then label to col 80."""
        body = body[:60]
        label = label[:20]
        return f"{body:<60}{label:<20}\n"

    def write_header(self, first_epoch: Epoch,
                     systems: Optional[Iterable[str]] = None,
                     bands_per_system: Optional[dict[str, list[str]]] = None) -> None:
        """Emit the RINEX 3.04 observation-file header.

        ``systems`` defaults to ('G', 'R', 'E', 'C') — constellations for
        which SYS / # / OBS TYPES lines are emitted.  Pass a subset if
        you know the capture only contains e.g. GPS+GLONASS.

        ``bands_per_system`` is optional multiband config: e.g.
        ``{"G": ["L1", "L5"], "R": ["L1", "L2"]}`` emits SYS/#/OBS TYPES
        lines that concatenate the obs codes for each band in order.
        Omitted → single-band L1/E1/B1 codes from ``OBS_CODES`` (Phase 1
        behaviour). Bands must be keys of ``OBS_CODES_BY_BAND[system]``.
        """
        if self._header_written:
            raise RuntimeError("header already written")
        c = self._cfg
        systems = tuple(systems) if systems else ("G", "R", "E", "C")

        # Line 1: RINEX VERSION / TYPE
        # Fixed columns per spec: A9 (version), 11X, A20 (file type starts
        # at col 21 0-indexed=20 — MUST be 'O' for obs), A20 (sys at col 41
        # 0-indexed=40 — MUST be 'M' here for mixed).
        self._fh.write(self._line(
            f"{'3.04':<9}{'':<11}{'OBSERVATION DATA':<20}{'M (MIXED)':<20}",
            "RINEX VERSION / TYPE",
        ))
        # Line 2: PGM / RUN BY / DATE
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y%m%d %H%M%S UTC")
        self._fh.write(self._line(
            f"{c.program:<20}{c.run_by:<20}{date_str:<20}",
            "PGM / RUN BY / DATE",
        ))
        # Optional comments
        for cm in c.comment_lines:
            self._fh.write(self._line(cm, "COMMENT"))
        # MARKER NAME / NUMBER
        self._fh.write(self._line(c.marker_name, "MARKER NAME"))
        if c.marker_number:
            self._fh.write(self._line(c.marker_number, "MARKER NUMBER"))
        # OBSERVER / AGENCY
        self._fh.write(self._line(
            f"{c.observer:<20}{c.agency:<40}",
            "OBSERVER / AGENCY",
        ))
        # REC # / TYPE / VERS
        self._fh.write(self._line(
            f"{c.receiver_number:<20}{c.receiver_type:<20}{c.receiver_version:<20}",
            "REC # / TYPE / VERS",
        ))
        # ANT # / TYPE
        self._fh.write(self._line(
            f"{c.antenna_number:<20}{c.antenna_type:<20}",
            "ANT # / TYPE",
        ))
        # APPROX POSITION XYZ (3 × F14.4)
        x, y, z = c.approx_position_xyz
        self._fh.write(self._line(
            f"{x:14.4f}{y:14.4f}{z:14.4f}",
            "APPROX POSITION XYZ",
        ))
        # ANTENNA: DELTA H/E/N (3 × F14.4)
        h, e, n = c.antenna_delta_hen
        self._fh.write(self._line(
            f"{h:14.4f}{e:14.4f}{n:14.4f}",
            "ANTENNA: DELTA H/E/N",
        ))
        # SYS / # / OBS TYPES — one line per active constellation.
        # When bands_per_system is passed, each system's line concatenates
        # the obs codes for every listed band (in order); otherwise the
        # single-band OBS_CODES table is used.
        for sys_char in systems:
            if bands_per_system and sys_char in bands_per_system:
                band_list = bands_per_system[sys_char]
                codes: tuple[str, ...] = ()
                entries: list[tuple[str, tuple[str, ...]]] = []
                for band in band_list:
                    band_codes = OBS_CODES_BY_BAND.get(sys_char, {}).get(band)
                    if not band_codes:
                        continue
                    codes = codes + band_codes
                    entries.append((band, band_codes))
                self._bands_per_system[sys_char] = entries
            else:
                codes = OBS_CODES.get(sys_char, ())
                # Default single-band mapping for the body writer: label
                # the entire column block as the constellation's native L1
                # variant so multiband-aware body code routes L1/G1/E1/B1
                # SvObs to the only available obs-code group.
                default_band = {
                    "G": "L1", "R": "L1", "E": "E1", "C": "B1",
                }.get(sys_char, "L1")
                self._bands_per_system[sys_char] = (
                    [(default_band, codes)] if codes else []
                )
            if not codes:
                continue
            codes_str = " ".join(f"{c:<3}" for c in codes)
            body = f"{sys_char}  {len(codes):>3} {codes_str}"
            self._fh.write(self._line(body, "SYS / # / OBS TYPES"))
        # TIME OF FIRST OBS (GPS time)
        dt = first_epoch.epoch_datetime_gps()
        body = (
            f"{dt.year:6d}{dt.month:6d}{dt.day:6d}"
            f"{dt.hour:6d}{dt.minute:6d}"
            f"{dt.second + dt.microsecond * 1e-6:13.7f}"
            f"     GPS"
        )
        self._fh.write(self._line(body, "TIME OF FIRST OBS"))
        # END OF HEADER
        self._fh.write(self._line("", "END OF HEADER"))
        self._header_written = True

    # --- body ------------------------------------------------------------

    def write_epoch(self, epoch: Epoch) -> bool:
        """Emit one epoch block (one ``>`` line + per-SV obs lines).

        Returns False, writing nothing, when the epoch has no observations.

        When the header declared multiple bands per system, observations
        sharing (system, prn) but differing in ``band`` are merged onto a
        single SV line with the band-order column layout declared at
        header time. Missing bands emit blank 16-col slots (spec §5.1).
        """
        if not self._header_written:
            raise RuntimeError("write_header() must be called first")
        if not epoch.observations:
            return False
        dt = epoch.epoch_datetime_gps()
        seconds_frac = dt.second + dt.microsecond * 1e-6
        # Group observations by (system, prn) so multiband rows collapse
        # into one per-SV line. Keep a stable inner ordering via band.
        by_sv: dict[tuple[str, int], dict[str, SvObs]] = {}
        for obs in epoch.observations:
            sv_map = by_sv.setdefault((obs.system, obs.prn), {})
            sv_map[obs.band] = obs
        num_sats = len(by_sv)
        header_line = (
            f"> {dt.year:4d} {dt.month:02d} {dt.day:02d}"
            f" {dt.hour:02d} {dt.minute:02d}"
            f"{seconds_frac:11.7f}"
            f"  0{num_sats:3d}"
        )
        self._fh.write(header_line + "\n")
        # Constellation order per spec: G R E C J S I
        sys_order = {"G": 0, "R": 1, "E": 2, "C": 3, "J": 4, "S": 5, "I": 6}
        ordered_svs = sorted(
            by_sv.keys(),
            key=lambda k: (sys_order.get(k[0], 99), k[1]),
        )
        for (system, prn) in ordered_svs:
            bands = self._bands_per_system.get(system)
            if not bands:
                continue
            line = f"{system}{prn:02d}"
            sv_map = by_sv[(system, prn)]
            for band, _codes in bands:
                obs = sv_map.get(band)
                if obs is None:
                    # No observation for this band — blank all 4 slots.
                    line += " " * 64
                    continue
                line += _format_obs(obs.pseudorange_m)
                line += _format_obs(obs.carrier_phase_cycles)
                line += _format_obs(obs.doppler_hz)
                line += _format_obs(obs.snr_dbhz)
            self._fh.write(line + "\n")
        return True


# ---------------------------------------------------------------------------
# High-level entry point used by dlf_to_jsonl.py --rinex-prefix
# ---------------------------------------------------------------------------

def _auto_detect_bands(epochs: Iterable[Epoch]) -> dict[str, list[str]]:
    """Walk an epoch stream and return the ordered band list per system.

    The order is the sort-stable set-union of every ``SvObs.band`` seen
    for that system. Systems with only one band produce a single-element
    list; constellations absent from the stream are absent from the
    result. This is what ``write_rinex_obs_file()`` hands to
    ``write_header()`` so the resulting RINEX file declares exactly the
    bands present in the data (no blank columns for bands that never show
    up, no silently-dropped bands either).
    """
    seen: dict[str, list[str]] = {}
    for ep in epochs:
        for obs in ep.observations:
            lst = seen.setdefault(obs.system, [])
            if obs.band not in lst:
                lst.append(obs.band)
    return seen


def write_rinex_obs_file(path: str, records: Iterable[dict],
                         config: Optional[RinexHeaderConfig] = None,
                         systems: Optional[Iterable[str]] = None,
                         bands_per_system: Optional[dict[str, list[str]]] = None) -> int:
    """Write a RINEX 3.04 obs-file from an iterable of parser dicts.

    Returns the number of epochs written.  Empty capture → header-only
    file with 0 epochs.

    ``bands_per_system`` — explicit per-system band config; omitted →
    auto-detected from the observation stream via ``_auto_detect_bands``.
    """
    epochs = group_records_by_epoch(records)
    if bands_per_system is None:
        bands_per_system = _auto_detect_bands(epochs) or None
    with open(path, "w") as fh:
        writer = RinexObsWriter(fh, config=config)
        if epochs:
            writer.write_header(epochs[0], systems=systems,
                                bands_per_system=bands_per_system)
        else:
            # Need *something* for the header — synthesise epoch zero.
            writer.write_header(Epoch(gps_week=0, gps_tow_ms=0),
                                systems=systems,
                                bands_per_system=bands_per_system)
        written = sum(writer.write_epoch(ep) for ep in epochs)
    return written


# ---------------------------------------------------------------------------
# Live-mode streaming writer — Phase 2
# ---------------------------------------------------------------------------
# The offline path (`write_rinex_obs_file` above) collects ALL records in
# memory, groups them by epoch, then writes the file.  That's fine for
# DLF replay but breaks for long-running `diaggpsd.py` captures where
# records stream in at ~1 Hz for hours or days.
#
# `LiveRinexWriter` is the streaming counterpart:
#   - Buffers records per DIAG log_time tick.
#   - On tick change, groups the buffered records into an Epoch and flushes.
#   - Writes the RINEX header lazily on the first complete epoch (we need
#     a TIME OF FIRST OBS value, which we only have after an epoch closes).
#   - Exposes close() to flush the last in-progress tick and release the file.
#
# Design constraint: records within a tick arrive in rapid succession
# (sub-millisecond), so a change in log_time is a reliable epoch boundary
# on every chipset observed.

class LiveRinexWriter:
    """Streaming RINEX writer for the live `diaggpsd.py` pipeline.

    Use::

        lw = LiveRinexWriter(open('live.obs', 'w'), config=RinexHeaderConfig(...))
        for pkt_dict in stream:
            lw.feed(pkt_dict)
        lw.close()

    The writer accumulates records for the current DIAG tick, emits an
    Epoch on tick change, and writes the RINEX header lazily on the
    first complete epoch.
    """

    def __init__(self, stream: IO[str],
                 config: Optional[RinexHeaderConfig] = None,
                 systems: Optional[Iterable[str]] = None,
                 bands_per_system: Optional[dict[str, list[str]]] = None):
        self._inner = RinexObsWriter(stream, config=config)
        self._systems = systems
        self._bands_per_system = bands_per_system
        self._current_tick: Optional[int] = None
        self._buffer: list[dict] = []
        self._buffer_epoch: Optional[tuple[int, int]] = None
        self._buffer_bias = 0.0
        self._epochs_written = 0
        self._resolver = TxTimeResolver()

    @property
    def epochs_written(self) -> int:
        return self._epochs_written

    def feed(self, record: dict) -> None:
        """Feed one parser-output dict into the live pipeline.

        Records without a ``type`` field that the RINEX extractors
        recognise are ignored silently (same criterion as the offline
        path).  A tick change triggers a flush of the accumulated buffer.
        """
        rtype = record.get("type", "")
        if rtype not in _EXTRACTORS:
            return
        tick = record.get("log_time")
        if tick is None:
            return
        # Tick change → flush current buffer as one epoch, start fresh —
        # unless the record states the same GPS epoch as the buffer. SDX62
        # logs a measurement's 0x1480 ~1,700 ticks after its 0x1477; split,
        # they would be written as two epochs with the same time.
        own = _own_epoch(record)
        bias = _time_bias_ms(record)
        if self._current_tick is not None and tick != self._current_tick:
            if (own is None or self._buffer_epoch is None
                    or not _same_instant(own, bias, self._buffer_epoch,
                                         self._buffer_bias)):
                self._flush_current_tick()
        if self._buffer_epoch is None and own is not None:
            self._buffer_epoch, self._buffer_bias = own, bias
        self._current_tick = tick
        self._buffer.append(record)

    def _flush_current_tick(self) -> None:
        """Emit the buffered tick as one epoch (if it resolves to a GPS
        timestamp).  Writes the RINEX header lazily on the first flush."""
        if not self._buffer:
            return
        epochs = group_records_by_epoch(self._buffer, self._resolver)
        self._buffer = []
        self._buffer_epoch = None
        if not epochs:
            return
        for ep in epochs:
            if not self._inner._header_written:
                # Live mode cannot auto-detect bands across the full stream
                # because records arrive one tick at a time; caller must
                # pre-declare any non-L1 bands via bands_per_system.
                self._inner.write_header(
                    ep, systems=self._systems,
                    bands_per_system=self._bands_per_system,
                )
            if self._inner.write_epoch(ep):
                self._epochs_written += 1

    def close(self) -> None:
        """Flush the final in-progress tick and release any internal
        state.  Safe to call multiple times."""
        if self._current_tick is None and not self._buffer:
            return
        self._flush_current_tick()
        self._current_tick = None
