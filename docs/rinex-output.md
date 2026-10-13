# diaggpsd RINEX output — Phase 1

`dlf_to_jsonl.py --rinex-prefix PREFIX` produces a RINEX 3.04
observation file at `PREFIX.obs` alongside the usual `.jsonl` output.
One epoch is emitted per modem measurement report (1 Hz on every
validated chipset).

## Usage

```bash
# Offline replay of a DLF capture → JSONL + RINEX
python3 apps/diaggpsd/dlf_to_jsonl.py \
    lm960.dlf \
    /tmp/lm960.jsonl \
    --rinex-prefix /tmp/lm960 \
    --rinex-receiver LM960 \
    --rinex-marker-name "TEST-SITE-1"
# → produces /tmp/lm960.jsonl and /tmp/lm960.obs
```

Validate with any compliant RINEX reader:

```python
import georinex
ds = georinex.load('/tmp/lm960.obs')
print(ds.sv.values, list(ds.data_vars))
```

## Supported observables (Phase 1)

| System  | Code | C-code | L-code | D-code | S-code |
|---------|------|--------|--------|--------|--------|
| GPS     | G    | C1C    | L1C    | D1C    | S1C    |
| GLONASS | R    | C1C    | L1C    | D1C    | S1C    |
| Galileo | E    | C1C    | L1C    | D1C    | S1C    |
| BeiDou  | C    | C2I    | L2I    | D2I    | S2I    |

The writer consumes these diaggrok parser outputs:

- `Diag0x1477` GPS measurement report
- `Diag0x1480` GLONASS measurement report
- `Diag0x1886` Galileo measurement report. No layout the parser decodes
  today exposes a transmit time, so it yields no rows yet.
- `Diag0x14DE` OEM DRE measurement report, preferred when available

The older descriptive names (`GpsMeasurementReport` and so on) are still
accepted for recorded JSONL.

When both a chipset-specific report and an OEM DRE report cover the
same `(system, prn)` at a given epoch, the OEM DRE record takes
precedence because it exposes filtered pseudorange and fine-speed.

## Data conversions

- **Epoch time** — GPS time. A `Diag0x1480` header carries GLONASS time
  (four-year cycle, day in cycle, ms of day in UTC(SU) = UTC + 3 h), which
  is converted to GPS time with the leap-second count for its date. Some
  firmware (SDX62, for one) logs a measurement's GLONASS and GPS reports on
  different DIAG ticks, so the tick alone cannot pair them. When the
  receiver clock bias is near ±0.5 ms the two reports can round to integer
  ms one apart; a GLONASS epoch joins the neighbouring GPS epoch when
  `ms - time_bias` agrees to within 50 µs. A GLONASS report with no time
  fields falls back to the GPS epoch logged at the same tick.
  A receiver that has not resolved time reports GPS week `0xFFFF` and
  GLONASS cycle 255, day `0xFFFF` (with `clock_time_unc` near 3e18 ms);
  its report ms is a free-running count, so those reports get no epoch
  and are not written.
- **Pseudorange (C\*)** — `travel_ms * c / 1000`, in metres.
  `pseudorange_ms_integral + _fraction` is the SV **transmit time** in ms
  of the GPS week (GLONASS: ms of the day), not a range; written directly
  it gave ~1.6e14 m and overflowed the F14.3 field. The travel time is
  `report_ms - time_bias - transmit_ms`, minus the SV's `latency` on
  firmware that stamps the transmit time before the report (SDX20,
  MDM9x07, MDM9x30, MDM9x50). SDX55 and SDX62 builds have already moved
  it to the report time. Both report version 0, so `TxTimeResolver` learns
  the convention from the stream. A value outside 60–100 ms, the travel
  time of a medium-earth-orbit satellite, is written as blank, not guessed.
  OEM DRE uses the same rule with no latency term. SV slots with C/N0 0
  (idle or searching) are not written.
- **Carrier phase (L\*)** — `carrier_phase_integral + carrier_phase_fraction / 65536`.
  Units: cycles.  When CP is exactly 0 (SDX20-family "not locked"
  indicator) or equals the OEM DRE `0x8000_0000_0000_0000` sentinel,
  the observation field is emitted as blank (spec-compliant "missing"
  marker) rather than literal `0.000`.
- **Doppler (D\*)** — `-fine_speed_mps / wavelength`.  Sign flipped
  because DIAG reports positive speed = moving away from SV, while
  RINEX positive Doppler = approaching SV.  GLONASS uses per-SV FDMA
  wavelength `C_LIGHT / (1602 MHz + freq_index * 0.5625 MHz)`.
- **SNR (S\*)** — `cno_db` as-is (dB-Hz).

## Time base

RINEX 3.04 `TIME OF FIRST OBS` is labelled `GPS`, meaning body epoch
records are in GPS time (no leap-second correction).  `dlf_to_jsonl.py`
uses the `gps_week + gps_milliseconds` fields from the measurement
reports directly.  Post-processors that need UTC should subtract
18 seconds (the current GPS−UTC leap-second count).

## Limitations (Phase 1)

- **L1 only** — L2/L5 multiband output is gated on the multiband
  parser work being completed.  BeiDou B1I observations are flagged `C2I` per RINEX 3.04
  (BDS-2 B1I is the "historical" B1 signal and uses obs code `2I`).
- **No SBAS** — GPS `sv_id` 33-64 is skipped; RINEX 3.04 would encode
  those under system `S` but we don't yet validate the offsets.
- **No `.nav` file** — broadcast ephemeris decode is Phase 3, blocked
  on 0x147C/0x147D RE.  Downstream tools should use external IGS
  broadcast nav (`brdc*.rnx`) from NASA CDDIS.
- **Static approximate position** — `APPROX POSITION XYZ` defaults to
  0,0,0.  Pass `--rinex-receiver` / `--rinex-antenna` / `--rinex-marker-name`
  to populate the textual header fields; XYZ population is not yet
  wired through the CLI (easy to add — pull from the first `MePositionFix`
  in the stream).
- **Static receiver info** — receiver version / serial number aren't
  auto-derived from `ATI` output yet.  Future enhancement.

## Edge cases locked in regression tests

`tests/test_rinex_writer.py`:

- Pseudorange ms→m conversion preserves full double-precision
  arithmetic (1 ms = 299792.458 m exactly).
- Doppler sign flipped (approaching SV → positive D-code).
- GLONASS SV-ID offset encoding: `sv_id=73` (DIAG) → slot 9 (RINEX).
- Carrier-phase zero → blank field, not `0.000` (spec §5.1).
- OEM DRE `0x8000_0000_0000_0000` CP sentinel → blank field.
- Per-epoch dedup: OEM DRE preferred over constellation-specific
  report for the same `(system, prn)`.
- `georinex.load()` round-trip success for both synthetic and real
  LM960 capture input.

## Roadmap

- **Phase 2 (live mode):** `diaggpsd.py --rinex-out PATH` writes
  observations live with hourly file rotation.  Same writer, different
  driver.
- **Phase 3 (.nav support):** decode 0x147C / 0x147D GNSS Nav DB and
  0x14D0 / 0x14E1 OEM DRE SV polynomial → RINEX `.23n` files.  Blocked
  on parser work.
