#!/usr/bin/env python3
"""Post-process DIAG-vs-LG290P captures filtered by reference-receiver state.

The EM7511 01.08.04.00 vs 01.14.22.00 comparison captures were taken with the
LG290P reference in different fix states (GPS-only vs multi-constellation).
The v1.1.0 §3a reference-receiver gate now prevents this structurally, but
the two historical captures are salvageable: limit the comparison to epochs
where the LG290P was itself in a solid multi-constellation fix.

This tool:
  1. Ingests the LG290P gpsd JSONL and labels each second with
     `reference_multi_constellation` = (TPV mode >= 3 AND uSat >= 8
     AND >= 2 distinct GNSS constellations in the concurrent SKY).
  2. Filters the DIAG PositionReport epochs to those seconds.
  3. Re-emits position-accuracy deltas over the salvaged epoch set,
     one capture at a time, plus a cross-capture comparison table.

Per-signal C/N0 deltas via MSM7 are not computed here; the existing
gnss_compare.py covers that path and can be re-run with the filtered
gpsd JSONL produced by `--emit-filtered-gpsd`.

Usage:
  python3 gnss_compare_ref_filter.py \\
    --capture 01.08.04.00=/path/to/cap_a \\
    --capture 01.14.22.00=/path/to/cap_b \\
    --output-md /path/to/salvaged_report.md

Each capture directory must contain:
  em7511_diag_parsed.jsonl[.bz2]
  em7511_lg290p_gpsd.jsonl[.bz2]
"""
from __future__ import annotations

import argparse
import bz2
import gzip
import io
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Reuse gnss_compare helpers — gps_to_utc + haversine_m + _find_closest.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gnss_compare import gps_to_utc, haversine_m  # noqa: E402


# --- constellation detection ------------------------------------------------

# gpsd gnssid values → canonical name. 1 (SBAS) and 5 (QZSS) are not counted
# as distinct constellations for the multi-constellation gate because they
# are augmentation/regional and can appear as single-SV add-ons to a GPS-only
# fix without actually giving us multi-constellation geometry.
_GNSSID_CORE = {0: 'GPS', 2: 'Galileo', 3: 'BeiDou', 6: 'GLONASS'}
MULTI_CONSTELLATION_MIN = 2
USED_SATS_MIN = 8


def _open_jsonl(path: Path):
    """Open a (possibly bz2/gzip-compressed) JSONL file and yield dict rows."""
    if path.suffix == '.bz2':
        fh = bz2.open(path, 'rt')
    elif path.suffix == '.gz':
        fh = gzip.open(path, 'rt')
    else:
        fh = path.open('r')
    try:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    finally:
        fh.close()


def _locate(capture_dir: Path, basename: str) -> Path:
    """Find `basename` or `basename.bz2`/`.gz` inside capture_dir."""
    for ext in ('', '.bz2', '.gz'):
        p = capture_dir / (basename + ext)
        if p.exists():
            return p
    raise FileNotFoundError(f"{basename}[.bz2|.gz] not found under {capture_dir}")


# --- LG290P epoch labeling --------------------------------------------------

@dataclass
class RefEpoch:
    tpv_mode: int = 0
    used_sats: int = 0
    constellations: frozenset = frozenset()

    @property
    def ref_ok(self) -> bool:
        return (self.tpv_mode >= 3
                and self.used_sats >= USED_SATS_MIN
                and len(self.constellations) >= MULTI_CONSTELLATION_MIN)


def _iso_to_second(iso: str) -> int:
    """Convert an ISO timestamp to a second-resolution UNIX epoch."""
    if iso.endswith('Z'):
        iso = iso[:-1] + '+00:00'
    dt = datetime.fromisoformat(iso)
    return int(dt.timestamp())


def label_reference_epochs(gpsd_path: Path) -> dict[int, RefEpoch]:
    """Build {utc_second: RefEpoch} from the LG290P gpsd JSONL."""
    labels: dict[int, RefEpoch] = defaultdict(RefEpoch)
    for obj in _open_jsonl(gpsd_path):
        cls = obj.get('class')
        time_str = obj.get('time')

        if cls == 'TPV' and time_str:
            sec = _iso_to_second(time_str)
            ep = labels[sec]
            ep.tpv_mode = max(ep.tpv_mode, obj.get('mode', 0))

        elif cls == 'SKY':
            # LG290P SKY is emitted at 1 Hz but often without a `time` field;
            # use uSat + satellites present. Attribute to the most recent TPV
            # second if no time. To keep labeling robust, we re-attribute SKY
            # to whichever second already has a TPV — we stream in order.
            sats = obj.get('satellites') or []
            if not sats:
                continue
            used = sum(1 for s in sats if s.get('used'))
            consts = frozenset(
                _GNSSID_CORE[s['gnssid']]
                for s in sats
                if s.get('gnssid') in _GNSSID_CORE
            )
            target_sec: int | None = None
            if time_str:
                target_sec = _iso_to_second(time_str)
            else:
                # Attach to most recent TPV epoch we've seen.
                if labels:
                    target_sec = max(labels.keys())
            if target_sec is None:
                continue
            ep = labels[target_sec]
            if used > ep.used_sats:
                ep.used_sats = used
            if len(consts) > len(ep.constellations):
                ep.constellations = consts

    return dict(labels)


# --- DIAG position loading --------------------------------------------------

def load_diag_positions(diag_path: Path) -> list[dict]:
    """Load PositionReport rows from a DIAG JSONL and attach a UTC epoch."""
    out: list[dict] = []
    for obj in _open_jsonl(diag_path):
        # Parser renamed to Diag0x1476; accept both for
        # JSON files written before/after the rename.
        if obj.get('type') not in ('PositionReport', 'Diag0x1476'):
            continue
        # Match gnss_compare.py: only pos_source == 2 (Kalman ME) is used
        # for accuracy comparison. Seed fixes (pos_source 1) pull mean up.
        if obj.get('pos_source') != 2:
            continue
        week = obj.get('gps_week')
        tow_ms = obj.get('gps_tow_ms')
        if week is None or tow_ms is None:
            continue
        utc = gps_to_utc(week, tow_ms)
        obj['_utc_second'] = int(utc.timestamp())
        obj['_utc'] = utc
        out.append(obj)
    return out


# --- gpsd TPV loading (for distance calc) -----------------------------------

def load_gpsd_tpv(gpsd_path: Path) -> list[dict]:
    """Load TPV rows with lat/lon/time for distance matching."""
    out: list[dict] = []
    for obj in _open_jsonl(gpsd_path):
        if obj.get('class') != 'TPV' or obj.get('mode', 0) < 2:
            continue
        if 'lat' not in obj or 'lon' not in obj or 'time' not in obj:
            continue
        obj['_utc_second'] = _iso_to_second(obj['time'])
        out.append(obj)
    return out


# --- filtered comparison ----------------------------------------------------

@dataclass
class CaptureResult:
    label: str
    epochs_total: int = 0
    epochs_dropped_no_ref_match: int = 0
    epochs_dropped_ref_single_const: int = 0
    epochs_kept: int = 0
    horizontal_m: list[float] = field(default_factory=list)
    alt_diff_m: list[float] = field(default_factory=list)
    constellation_hist: dict = field(default_factory=lambda: defaultdict(int))

    @property
    def mean_horizontal(self) -> float | None:
        return sum(self.horizontal_m) / len(self.horizontal_m) if self.horizontal_m else None

    @property
    def median_horizontal(self) -> float | None:
        if not self.horizontal_m:
            return None
        s = sorted(self.horizontal_m)
        return s[len(s) // 2]

    @property
    def max_horizontal(self) -> float | None:
        return max(self.horizontal_m) if self.horizontal_m else None

    @property
    def drop_rate(self) -> float:
        return (
            1.0 - (self.epochs_kept / self.epochs_total)
            if self.epochs_total else 0.0
        )


def evaluate_capture(label: str, capture_dir: Path) -> CaptureResult:
    """Filter a capture's DIAG positions to multi-constellation reference epochs."""
    gpsd_path = _locate(capture_dir, 'em7511_lg290p_gpsd.jsonl')
    diag_path = _locate(capture_dir, 'em7511_diag_parsed.jsonl')

    ref_labels = label_reference_epochs(gpsd_path)
    diag_positions = load_diag_positions(diag_path)
    tpvs = load_gpsd_tpv(gpsd_path)

    # Index TPVs by second for O(1) lookup, with ±1s tolerance.
    tpv_by_sec: dict[int, dict] = {}
    for t in tpvs:
        tpv_by_sec.setdefault(t['_utc_second'], t)

    result = CaptureResult(label=label)
    for pos in diag_positions:
        result.epochs_total += 1
        sec = pos['_utc_second']

        tpv = None
        for offset in (0, -1, 1, -2, 2):
            if sec + offset in tpv_by_sec:
                tpv = tpv_by_sec[sec + offset]
                ref_sec = sec + offset
                break
        if tpv is None:
            result.epochs_dropped_no_ref_match += 1
            continue

        ref_ep = ref_labels.get(ref_sec)
        if ref_ep is None or not ref_ep.ref_ok:
            result.epochs_dropped_ref_single_const += 1
            continue

        result.constellation_hist[len(ref_ep.constellations)] += 1

        dist = haversine_m(pos['lat_deg'], pos['lon_deg'], tpv['lat'], tpv['lon'])
        alt_diff = pos.get('alt_m', 0.0) - tpv.get('alt', pos.get('alt_m', 0.0))
        result.horizontal_m.append(dist)
        result.alt_diff_m.append(alt_diff)
        result.epochs_kept += 1

    return result


# --- markdown emission ------------------------------------------------------

def render_markdown(results: list[CaptureResult]) -> str:
    lines = [
        "# Salvaged DIAG-vs-LG290P position accuracy (reference-state filter)",
        "",
        f"Filter rule: LG290P concurrent TPV mode ≥ 3, uSat ≥ {USED_SATS_MIN},",
        f"             and ≥ {MULTI_CONSTELLATION_MIN} core constellations observed.",
        f"Core constellations counted: {', '.join(sorted(_GNSSID_CORE.values()))}.",
        "SBAS and QZSS are excluded from the constellation count (augmentation/regional).",
        "",
        "## Per-capture salvage summary",
        "",
        "| Capture | Epochs total | Dropped (no ref) | Dropped (single-const) | Kept | Drop rate |",
        "|---------|-------------:|-----------------:|-----------------------:|-----:|----------:|",
    ]
    for r in results:
        lines.append(
            f"| {r.label} | {r.epochs_total} | "
            f"{r.epochs_dropped_no_ref_match} | "
            f"{r.epochs_dropped_ref_single_const} | "
            f"{r.epochs_kept} | "
            f"{r.drop_rate*100:.1f}% |"
        )

    lines += [
        "",
        "## Filtered horizontal accuracy",
        "",
        "| Capture | N | Mean (m) | Median (m) | Max (m) |",
        "|---------|--:|---------:|-----------:|--------:|",
    ]
    for r in results:
        if not r.horizontal_m:
            lines.append(f"| {r.label} | 0 | — | — | — |")
            continue
        lines.append(
            f"| {r.label} | {len(r.horizontal_m)} | "
            f"{r.mean_horizontal:.2f} | "
            f"{r.median_horizontal:.2f} | "
            f"{r.max_horizontal:.2f} |"
        )

    lines += ["", "## Constellation-count distribution on kept epochs", ""]
    for r in results:
        parts = ", ".join(
            f"{k} consts: {v}"
            for k, v in sorted(r.constellation_hist.items())
        ) or "(none)"
        lines.append(f"- **{r.label}**: {parts}")

    # Cross-capture delta (only if ≥2 captures).
    if len(results) >= 2 and all(r.mean_horizontal is not None for r in results):
        lines += [
            "",
            "## Cross-capture delta (salvaged)",
            "",
            "| Pair | ΔMean (m) | Notes |",
            "|------|----------:|-------|",
        ]
        for i in range(len(results)):
            for j in range(i + 1, len(results)):
                a, b = results[i], results[j]
                delta = (b.mean_horizontal or 0) - (a.mean_horizontal or 0)
                warn = ""
                if min(a.epochs_kept, b.epochs_kept) < 30:
                    warn = "⚠ low sample size — not statistically meaningful"
                lines.append(
                    f"| {a.label} → {b.label} | {delta:+.2f} | {warn} |"
                )

    lines.append("")
    return "\n".join(lines)


# --- CLI --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument(
        '--capture', action='append', required=True,
        metavar='LABEL=DIR',
        help='Capture to evaluate (repeatable). Example: '
             '--capture 01.08.04.00=/path/to/em7511/capture/outputs',
    )
    ap.add_argument(
        '--output-md', type=Path, required=True,
        help='Path to write the salvaged comparison markdown.',
    )
    args = ap.parse_args()

    captures = []
    for spec in args.capture:
        if '=' not in spec:
            ap.error(f"--capture expects LABEL=DIR, got {spec!r}")
        label, path = spec.split('=', 1)
        captures.append((label, Path(path)))

    results = []
    for label, capture_dir in captures:
        print(f"→ Evaluating {label} ({capture_dir}) ...", file=sys.stderr)
        r = evaluate_capture(label, capture_dir)
        print(
            f"  kept={r.epochs_kept}/{r.epochs_total} "
            f"(dropped {r.epochs_dropped_no_ref_match} no-ref, "
            f"{r.epochs_dropped_ref_single_const} single-const); "
            f"mean={r.mean_horizontal if r.mean_horizontal is None else f'{r.mean_horizontal:.2f}m'}",
            file=sys.stderr,
        )
        results.append(r)

    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(render_markdown(results))
    print(f"Wrote {args.output_md}", file=sys.stderr)


if __name__ == '__main__':
    main()
