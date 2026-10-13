# GNSS DIAG Log Types — Parser Completeness per Capture

> **Living doc.** Originally created as a dated snapshot; renamed to
> drop the date suffix in `963037489` and reframed as a refreshable
> baseline. The data tables below reflect the **baseline capture
> analysis** of the two captures listed under "Captures analysed" —
> rerun the procedure in [§ Refreshing this doc](#refreshing-this-doc)
> to overwrite with a newer baseline.

**Source file column**: points at the current `diag_0xCCCC.py`
filenames, after the per-code parser file split. Every GNSS log code now lives in its own
ID-only-named parser file; the old descriptive names (`gnss_nav_db`,
`gnss_demod`, `lte_misc`, etc.) referenced in earlier revisions of this
doc no longer exist as files.

**Our type name column**: reflects the dataclass name **at the time of
the baseline capture**, before a later rename converted every class
to `Diag0xCCCC`. Preserved as historical record of what those classes
were called when the completeness analysis ran. Every entry
here is now `Diag0xCCCC` matching its log code.

## Captures analysed

| Capture | Chipset / FW | Capture dir | Duration | Total records | Distinct codes | Unknown codes |
|---------|--------------|-------------|----------|--------------:|---------------:|---------------|
| **EM7511** | MDM9650 — SWI9X50C_01.08.04.00 | GNSS baseline capture | 5 min (08:14:42→08:19:40 UTC) | 308,257 | 98 | 1 |
| **EM9190** | SDX55   — SWIX55C_03.17.04.00 | `capture-03-aligned`     | 5 min (05:38→05:43 UTC) | 473,104 | 110 | 2 |

## Completeness classes

- **FULL** — Dedicated parser consumes the full payload into structured named fields.
- **HEADER** — Dedicated parser is explicitly header-only (`description="… header-only"`); per-SV / tail data is deliberately not decoded.
- **STUB** — Registered via `_simple_parser()` factory (batch registration); emits only `version`, `config_word`, `data_density`, `payload_size` — actual payload structure is **not** decoded. The `_simple_parser` factory lives in `_simple_parser_factory.py`; do not add new codes to it.
- **PARTIAL** — Dedicated parser with some structured fields PLUS `field_N_u32`-style placeholders in output (RE incomplete).
- **NONE** — Log code observed in capture, **no parser registered**.

## GNSS log-type completeness table

| Code | Our type name (at capture) | Source file | EM7511 raw / parsed / auto | EM9190 raw / parsed / auto | Class |
|------|----------------------------|-------------|----------------------------|----------------------------|-------|
| **0x1384** | `GnssNmeaDiag` | `diag_0x1384.py` | 7025/7025/0 | 6215/6215/0 | **FULL** |
| **0x1455** | `GnssCounter1455` | `diag_0x1455.py` | 296/296/0 | 218/218/0 | **FULL** |
| **0x1456** | `GnssHeartbeat1456` | `diag_0x1456.py` | 295/295/0 | 306/306/0 | **FULL** |
| **0x1476** | `PositionReport` | `diag_0x1476.py` | 888/888/0 | 1213/1213/0 | **FULL** |
| **0x1477** | `GpsMeasurementReport` | `diag_0x1477.py` | 296/296/0 | 300/300/0 | **FULL** |
| **0x1478** | `GnssClockReport` | `diag_0x1478.py` | 886/886/0 | 300/300/0 | **FULL** |
| **0x1479** | `GnssDemodTracking` | `diag_0x1479.py` | 0/0/0 | 60746/60746/0 | **FULL** |
| **0x147B** | `GnssCdDbReport` | `diag_0x147b.py` | 886/886/0 | 310/310/0 | **FULL** |
| **0x147C** | `GnssNavDb147C` | `diag_0x147c.py` | 296/592/0 | 300/600/0 | **HEADER** |
| **0x147D** | `GnssNavDb147C` | `diag_0x147d.py` | 296/592/0 | 300/600/0 | **HEADER** |
| **0x147E** | `GnssPrxRfHwStatusReport` | `diag_0x147e.py` | 278/278/0 | 300/300/0 | **FULL** |
| **0x1480** | `GlonassMeasurementReport` | `diag_0x1480.py` | 296/296/0 | 300/300/0 | **FULL** |
| **0x1482** | `GnssData1482` | `diag_0x1482.py` | 296/296/0 | 222/222/0 | **HEADER** |
| **0x1488** | `GnssConfig1488` | `diag_0x1488.py` | 591/591/0 | 300/300/0 | **FULL** |
| **0x148A** | `GnssSvStatus` | `diag_0x148a.py` | 590/590/0 | 1224/1224/0 | **HEADER** |
| **0x148E** | `GnssStatus148E` | `diag_0x148e.py` | 0/0/0 | 234/234/0 | **FULL** |
| **0x1490** | `GnssState1490` | `diag_0x1490.py` | 592/592/0 | 411/411/0 | **FULL** |
| **0x1494** | `GnssNavData1494` | `diag_0x1494.py` | 296/296/0 | 288/288/0 | **HEADER** |
| **0x14A6** | `GnssData14A6` | `diag_0x14a6.py` | 296/296/0 | 215/215/0 | **FULL** |
| **0x14B0** | `GnssData14B0` | `diag_0x14b0.py` | 296/296/0 | 300/300/0 | **FULL** |
| **0x14D8** | `MePositionFix` | `diag_0x14d8.py` | 193/193/6 | 1804/1804/6 | **PARTIAL** |
| **0x14DE** | `OemdreMeasurementReport` | `diag_0x14de.py` | 0/0/0 | 2700/2700/0 | **FULL** |
| **0x14E0** | `GnssOemDreExt14E0` | `diag_0x14e0.py` | 0/0/0 | 300/300/0 | **HEADER** |
| **0x1509** | `GnssInit1509` | `diag_0x1509.py` | 2/2/0 | 0/0/0 | **FULL** |
| **0x1516** | `GnssInit1516` | `diag_0x1516.py` | 3/3/0 | 3/3/0 | **HEADER** |
| **0x1526** | `GnssNavMeasValidity` | `diag_0x1526.py` | 15576/15576/0 | 20541/20541/0 | **FULL** |
| **0x1544** | `GnssSvAggregate` | `diag_0x1544.py` | 8505/8505/0 | 5296/5296/0 | **HEADER** |
| **0x1587** | `GnssTracking1587` | `diag_0x1587.py` | 888/888/0 | 1283/1283/0 | **HEADER** |
| **0x158C** | `GnssRfStats` | `diag_0x158c.py` | 2320/2320/0 | 9379/9379/0 | **FULL** |
| **0x1596** | `GnssConfig1596` | `diag_0x1596.py` | 296/296/0 | 300/300/0 | **FULL** |
| **0x15BD** | `GnssRare15BD` | `diag_0x15bd.py` | 74/74/0 | 106/106/0 | **HEADER** |
| **0x1634** | `GnssMeas1634` | `diag_0x1634.py` | 0/0/0 | 6134/6134/0 | **FULL** |
| **0x1635** | `GnssSvInfo1635` | `diag_0x1635.py` | 0/0/0 | 253/253/0 | **STUB** |
| **0x1636** | `GnssSvInfo1636` | `diag_0x1636.py` | 1180/1180/0 | 253/253/0 | **STUB** |
| **0x163D** | `GnssSvInfo163D` | `diag_0x163d.py` | 296/296/0 | 300/300/0 | **STUB** |
| **0x163E** | `GnssClockState163E` | `diag_0x163e.py` | 296/296/0 | 300/300/0 | **STUB** |
| **0x163F** | `GnssSvInfo163F` | `diag_0x163f.py` | 296/296/0 | 300/300/0 | **STUB** |
| **0x1646** | `GnssStatus1646` | `diag_0x1646.py` | 111/111/0 | 120/120/0 | **STUB** |
| **0x1756** | `BdsMeasurementReport` | `diag_0x1756.py` | 0/0/0 | 300/300/0 | **HEADER** |
| **0x1837** | `GnssNmea1837` | `diag_0x1837.py` | 296/296/0 | 195/195/0 | **FULL** |
| **0x1886** | `GalileoMeasurementReport` | `diag_0x1886.py` | 0/0/0 | 300/300/0 | **FULL** |
| **0x188E** | — *(then NONE)* | `diag_0x188e.py` *(now registered)* | 296/0/0 | 0/0/0 | **NONE** *(at capture)* |
| **0x1893** | `GnssStatus1893` | `diag_0x1893.py` | 30/30/0 | 137/137/0 | **STUB** |
| **0x1899** | `GnssStatus1899` | `diag_0x1899.py` | 296/296/0 | 193/193/0 | **STUB** |

> **0x188E note**: was unregistered at the baseline snapshot; a parser
> has since been added (`diag_0x188e.py`). The row remains classified
> **NONE** for the baseline snapshot's accuracy — a future refresh will
> reclassify based on the new capture.

## Summary

Counts reflect the baseline snapshot:

- **FULL**: 23 / 44
- **HEADER**: 11 / 44
- **PARTIAL**: 1 / 44
- **STUB**: 8 / 44
- **NONE**: 1 / 44

## Unknown log codes (no registered parser at all)

These codes were present in the raw DIAG stream but didn't match any parser in the diaggrok registry **at the time of the snapshot**. Includes LTE/NR5G codes, not just GNSS.

| Code | EM7511 records | EM9190 records | Payload size | Subsystem guess | Notes |
|------|---------------:|---------------:|--------------|------------------|-------|
| **0x188E** | 296 | 0 | 1868 B | LTE/GNSS (0x18xx) | Documented as `LOG_GNSS_OEMCPE_MEASUREMENT_REPORT_C` in [`diag-gnss-log-codes.md`](diag-gnss-log-codes.md); now parsed by `diag_0x188e.py`. 1868 B ≈ OEM CPE per-SV measurement report, only seen on EM7511 (MDM9650). |
| **0x189E** | 0 | 3 | 50–51 B | LTE/GNSS (0x18xx) | Rare: only 3 records total, 50–51 B. Possibly LTE RRC/PHY event. **Status post-snapshot**: see the diaggrok registry and its parser inventory for current. |
| **0x1C5F** | 0 | 300 | 384–524 B | NR5G | Unknown NR5G log — 1 Hz cadence (300 records / 5 min), variable 384–524 B; likely NR5G meas/status. Only seen on EM9190 (SDX55). **Status post-snapshot**: see the diaggrok registry and its parser inventory for current. |

### Totals (baseline snapshot)

- Distinct log codes across both captures: **143**
- Registered (any class): **140**
- Unregistered: **3**

## Refreshing this doc

This doc is intentionally a living artifact — the parser tree and
capture corpus both grow over time, and the completeness table goes
stale as parsers are upgraded from STUB → HEADER → FULL and as new
captures surface previously-unseen codes. Recommended refresh procedure:

1. Pick a fresh pair of captures (one Cat-12 / MDM-series modem, one
   SDX55+ modem) — the diaggrok capture backlog carries the current
   ground-truth list.
2. Run the existing offline-analysis pipeline against each capture:
   `apps/diaggpsd/dlf_to_jsonl.py <dlf>` produces a parsed JSONL; from
   there a small awk/jq pass over the JSONL is enough to count
   raw-vs-parsed records per log code.
3. Cross-reference each code's current class against the registry:
   `parser_info(0xCCCC).description` carries the "header-only" /
   "PARTIAL" annotation, and `fields_identified` / `fields_parsed`
   exposed via `ParserEntry` distinguish FULL from HEADER quantitatively.
4. Overwrite the data tables above with the new numbers; bump the
   "Living doc" note at the top to reference the new snapshot date(s).
5. **Do not rename the file back to a dated form** — the rename to
   `gnss-parser-completeness.md` is the intentional shape going forward.

The previous baseline is preserved in git history if a side-by-side
diff is ever needed (`git log --follow apps/diaggpsd/docs/gnss-parser-completeness.md`
walks back through the rename).
