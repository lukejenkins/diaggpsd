# diaggpsd Device Compatibility Matrix

Tracks which devices work with diaggpsd, what GNSS capabilities each chipset provides, and the configuration needed per device.

**Most recent additions:** RM520N-GL-AP measured live over `/dev/mhi_DIAG`; SDX62 PositionReport corrected to v24.

---

## How to Use This Document

- **Confirmed** data comes from live testing with diaggpsd or direct observation (NMEA captures, DIAG logs).
- **Inferred** data comes from chipset datasheets, firmware analysis, or published chipset product briefs.
- Status indicators: ✅ confirmed working, ❌ confirmed not working, ⚠️ partial/issues, ❓ untested

---

## Chipset GNSS Capabilities

GNSS capabilities by chipset generation. Constellations and frequencies listed are the chipset hardware maximum; actual availability depends on firmware configuration and antenna.

| Chipset | Generation | Constellations | Frequencies | GNSS Engine | Devices in Project |
|---------|-----------|----------------|-------------|-------------|-------------------|
| MDM9205 | LPWA (2018) | GPS, GLONASS, BeiDou, Galileo | L1 | Gen 8 Lite | BG95-M3, SIM7080G |
| MDM9207 (MDM9607 SoC) | LTE Cat 4 (2015) | GPS, GLONASS, BeiDou, Galileo | L1 | Gen 8 | EG25-G, EC25-AF, RC400L, R11e-LTE-US, LM1200 (EC25-AF), SIM7600NA-H, EG95NA, EP06-A |
| MDM9230 | LTE Cat 6 (2014) | GPS, GLONASS, BeiDou | L1 | Gen 8 | MC7455 |
| MDM9607 | LTE Cat 4-6 (2016) | GPS, GLONASS, BeiDou, Galileo | L1 | Gen 8 | Same die as MDM9207 entries above |
| MDM9650 / SDX20 | LTE-A Cat 12-18 (2016) | GPS, GLONASS, BeiDou, Galileo, QZSS, NavIC | L1, L5 | Gen 9A | EM7511, T77W968 (DW5821e), LM960A18, EG12-GT, EM120R-GL, MC7411 |
| SDX50M | LTE-A Pro Cat 18 (2018) | GPS, GLONASS, BeiDou, Galileo, QZSS, NavIC | L1, L5 | Gen 9A | MC7411 |
| SDX55 | 5G Sub-6 (2019) | GPS, GLONASS, BeiDou, Galileo, QZSS, NavIC | L1, L5 | Gen 9B | RM500Q-AE, EM9190, FN980m |
| SDX62 | 5G SA/NSA (2021) | GPS, GLONASS, BeiDou, Galileo, QZSS, NavIC | L1, L5 | Gen 9C | CFW-3212 (RG520N-NA), RM520N-GL (-AA USB, -AP PCIe) |

**Notes:**
- L5 support on MDM9650+ requires dual-band antenna and firmware enabling. Not all OEM implementations expose L5.
- Gen 8 engines output PositionReport v1/v2. SDX55 outputs v13 (2477 B fixed). **SDX62 outputs v24 (0x18, variable length)** — not v13 (see the PositionReport table below).
- "Constellations" reflects chipset silicon capability. OEM firmware may restrict to GPS+GLONASS only (common on Quectel modules).

---

## Device Compatibility Matrix

| Device | Chipset | DIAG Transport | AT Commands | diaggpsd Status | PositionReport Ver | Constellations Confirmed | Notes |
|--------|---------|---------------|-------------|-----------------|--------------------|-----------------------|-------|
| **Quectel EG25-G** | MDM9207 | USB serial (ttyUSB0) | AT+QGPS* ✅ | ✅ Working | v1/v2 (291 bytes) | GPS + GLONASS ✅ | Primary test device. 7 GPS + 7 GLONASS SVs confirmed outdoors. |
| **Quectel RM500Q-AE** | SDX55 | USB serial (ttyUSB0) | AT+QGPS* ✅ | ✅ Working | v13 (2477 bytes) | GPS ✅, GLONASS ✅ | USB DM accepts LOG_CONFIG directly. All 4 GNSS packet types + OEM DRE confirmed. 9 GPS + 2 GLONASS SVs used in fix. |
| **Casa CFW-3212 (RG520N-NA)** | SDX62 | TCP (diag_socket_log :2500) | `--no-at` ⚠️ | ✅ Working | v24 (0x18, variable) ✅ | ❓ Untested outdoors | AT path unreliable on OpenCPU. Uses TCP transport, not USB. Emits the same v24 variant as RM520N-GL (DOP identity 884/884). |
| **Quectel RM520N-GL (-AA, USB)** | SDX62 | USB serial (DIAG = `if00`; `ttyUSB1` in the 6-interface composition) | AT+QGPS* ✅ | ✅ Working | v24 (0x18, variable 1245–2895 B) ✅ | GPS ✅, GLONASS ✅, Galileo ✅ (incl. E6); BeiDou ⚠️ B2b only | Live diaggpsd run (no AT setup, socat PTY) plus an offline replay of a 202 MB capture: 0 parse errors. LG290P same-antenna: 616 epochs, **2.59 m** horizontal median. BeiDou B1I never searched on R03A03 (F3 strategy `IDLE`). GLONASS `used > total` parser bug. |
| **Quectel RM520N-GL-AP (PCIe)** | SDX62 | MHI chardev `/dev/mhi_DIAG` (OOT `pcie_mhi`) | AT+QGPS* via `/dev/mhi_DUN` ✅ | ⚠️ Transport + parsing working; no fix yet (no antenna) | **v21 (0x15), 647 B** on R01A08 (62/62 records) ✅ | ❓ No antenna fitted | Live run on a PCIe bench host (R01A08 `_11.200.11.200`). The chardev rejects `tcgetattr`/`tcdrain`; `DiagClient.from_serial` falls back to a bare raw fd and works. 60 s live: 86 GNSS-range codes, 0 parse errors; 90 s daemon run recorded 96,731 packets. All 0x1476 epochs are pos_source=4 cold-start seeds (coarse seed position, gps_week 0xFFFF) — engine on, no fix (`AT+QGPSLOC=2` → 516). The run needed two diaggpsd fixes: the `bytes` crash and gpsd publishing seed epochs as a mode-2 fix. ~90/s `0x98` multi-radio envelopes (radio 1, LTE 0xB1xx only, no GNSS) are dropped. The GL-XE3000 unit (R03A04) has not been run yet. |
| **Sierra EM7511** | MDM9650 | USB serial (ttyUSB0) | Needs AT!GPS* ❓ | ❓ Untested | v1/v2 or v13 ❓ | ❓ | DIAG port available. GPS engine active (loc_launcher running). `--no-at` should work. |
| **Orbic RC400L** | MDM9207 | USB serial (DIAG on if02) | None (no GNSS AT cmds) | ❌ Low priority | v1/v2 (291 bytes) | ❌ No satellites | Hardware RF limitation: cannot acquire L1 GPS/GLONASS signals. No usable GNSS antenna. |
| **Soracom Onyx (EG25-G)** | MDM9207 | USB serial | AT+QGPS* ❓ | ❓ Untested | v1/v2 (291 bytes) | ❓ | Same silicon as EG25-G Device 1. Should work identically. |
| **Foxconn T77W968 (DW5821e)** | SDX20 | USB serial (ttyUSB3, raw os.open) | ❓ Unknown GNSS AT dialect | ❓ Untested | v1/v2 or v13 ❓ | ❓ | DIAG port confirmed. No GNSS antenna in Dell laptop. GPS engine functional per docs. |
| **Foxconn T77W968 Unit 2 (DW5821e)** | SDX20 | USB serial (ttyUSB3) | ❓ | ❓ Untested | v1/v2 or v13 ❓ | ❓ | Same chipset as Unit 1. DIAG port available but locks up after idle. |
| **Netgear LM1200 (EC25-AF)** | MDM9607 | ❓ | AT+QGPS* (inferred) | ❓ Untested | v1/v2 (291 bytes) | ❓ | OpenCPU variant (Netgear firmware). DIAG port access not yet confirmed. |
| **SIMCom SIM7600NA-H** | MDM9607 | USB serial (ttyUSB0) | ❓ SIMCom AT dialect | ❓ Untested | v1/v2 (291 bytes) | ❓ | DIAG port present in USB composition. AT GNSS commands unknown. |
| **MikroTik R11e-LTE-US** | MDM9207 | USB DIAG interface present | ❓ Minimal AT support | ❓ Untested | v1/v2 (291 bytes) | ❓ | WNC custom firmware. No CLAC, no vendor extensions. DIAG interface in USB composition. |
| **Telit LM960A18** | SDX20 | ❓ | AT#GPS* (Telit dialect) | ❌ Likely not possible | v1/v2 or v13 ❓ | Active 2D fix observed | SDX20 platform — GPS DIAG (ID 5) inactive on all tested SDX20 modules. |
| **Sierra EM9190** | SDX55 | ❌ DIAG blocked | AT!GPS* (Sierra dialect) | ❌ Not possible | v13 (2477 bytes) | L1+L5 confirmed (AT!GPSSATINFO) | USB DIAG port non-functional even after AT!USBCOMP change. DIAG-over-QMI under investigation. |
| **Sierra MC7455** | MDM9230 | USB serial (ttyUSB0, DIAG in composition) | AT!GPS* (Sierra dialect) | ❓ Untested | v1/v2 (291 bytes) | ❓ | Older MDM9230 generation. DIAG port in USB composition 0x0000010D. |
| **Sierra MC7411** | SDX50M | USB serial (DIAG in composition) | AT!GPS* (Sierra dialect) | ❓ Untested | v1/v2 or v13 ❓ | ❓ | LTE-A Pro (Cat 18) platform. DIAG available. |
| **Telit FN980m** | SDX55 | USB serial (ttyUSB0) | AT$GPSP=1 ✅ | ✅ Working | v13 (2477 bytes) | GPS ✅, GLONASS ✅, Galileo ✅, BeiDou ✅ | GPS DIAG (ID 5) active. All 4 GNSS packet types confirmed. USB DM rejects LOG_CONFIG — use diag_socket_log. |
| **Quectel BG95-M3** | MDM9205 | ❓ | AT+QGPS* (inferred) | ❓ Untested | ❓ Unknown | ❓ | LPWA module. DIAG availability unknown. May use different GNSS log format. |
| **Quectel EG18-NA** | SDX20 V2 | USB serial (ttyUSB0) | AT+QGPS* ✅ | ✅ Working | **v10** (new) | GPS ✅, GLONASS ⚠️ (DIAG only), Galileo ⚠️ (DIAG only) | First SDX20 with active GPS DIAG. All 9 GNSS log codes confirmed. GLONASS/Galileo in DIAG but not NMEA. C/N0 10x scaled. |
| **Quectel EG12-GT** | SDX20 | USB serial (ttyUSB0) | AT+QGPS* ✅ | ❌ Not possible | — | GPS via NMEA only | GPS DIAG (ID 5) inactive. Only 3 subsystems respond (DIAG, HS_USB, AUDIO). liblocdiagiface.so missing. |
| **Quectel EM120R-GL** | SDX20 | ❓ | AT+QGPS* (inferred) | ❌ Likely not possible | v1/v2 or v13 ❓ | ❓ | SDX20 platform — GPS DIAG (ID 5) inactive on tested SDX20 modules. EG18-NA (SDX20 V2) is the exception. |
| **Quectel EG95NA** | MDM9x07 | ❓ | AT+QGPS* (inferred) | ❓ Untested | v1/v2 (291 bytes) | ❓ | Uncaptured. No survey data. |
| **Quectel EP06-A** | MDM9640 | USB serial (ttyUSB0) | AT+QGPS* ✅ | ⚠️ Likely works | v1/v2 (291 bytes) | ❓ | GPS DIAG (ID 5) active. Same subsystem profile as EG25-G. diaggpsd not yet tested live. |

### Devices on Other Chipset Families (Not Compatible)

| Device | Chipset | Reason |
|--------|---------|--------|
| Fibocom L850-GL | Intel XMM 7360 | Intel baseband. No DIAG protocol. |
| Quectel EG800Q-NA | UNISOC UIS8910DM | UNISOC chipset, not a DIAG-family part. No DIAG GNSS log codes. |
| SIMCom SIM7080G | MDM9205 | LPWA; DIAG GNSS support unconfirmed on MDM9205. Needs investigation. |

---

## GNSS Antenna Paths

| Device | Antenna Type | GNSS Antenna Port | Signal Status | Notes |
|--------|-------------|-------------------|---------------|-------|
| **EG25-G (standalone)** | External | Dedicated GNSS U.FL | ✅ Working | 7 GPS + 7 GLONASS, SNR 29-50 dBHz outdoors. Shared LNA with cellular possible on some boards. |
| **Soracom Onyx (EG25-G)** | Internal (USB dongle) | Internal trace antenna | ❓ Untested | Compact USB form factor; likely reduced GNSS performance vs standalone module. |
| **RM500Q-AE** | Internal (M.2) | M.2 module, no dedicated ext antenna | ✅ Working | 10 GPS + 8 GLONASS SVs tracked (gnssconfig=1). 9 GPS + 2 GLONASS used in fix. |
| **CFW-3212 (RG520N-NA)** | External (outdoor CPE) | Integrated in CPE housing | ✅ Confirmed fix | Outdoor-mounted unit. GNSS fix confirmed via AT+QGPSLOC. |
| **RM520N-GL (-AA)** | External, M.2 GNSS U.FL | Bench antenna via splitter shared with the LG290P (when fitted) | ⚠️ Antenna moves between units | 3D fix (GPS+GAL+GLO, HDOP 0.9) on the 2026-04 LG290P bench. With the bench antenna moved to the EG18-NA, a re-check shows engine on, 0 SVs. `AT+QGPSLOC` → 516 means "engine on, no fix"; 505 means engine off. |
| **RM520N-GL-AP (GL-XE3000 unit, PCIe bench unit)** | None connected | M.2 slot (GL-XE3000 / bench host PCIe `01:00.0`) | ❓ Untested | PCIe-only units. No GNSS antenna recorded on either. Bench unit: engine on, 0 SVs used, `AT+QGPSLOC` → 516. |
| **EM7511** | None connected | M.2 module, no antenna attached | ⚠️ No signal | GPS engine active but clock at epoch, no satellites tracked. Needs external GNSS antenna. |
| **RC400L** | None functional | ❌ No usable GNSS antenna | ❌ No signal | Hardware limitation confirmed. RF front-end cannot acquire L1 signals. SBAS detected at marginal SNR only. |
| **T77W968 (DW5821e)** | None in laptop | M.2 module in Dell laptop | ⚠️ No antenna | GPS engine functional per firmware but no GNSS antenna routed in Dell laptop chassis. |
| **LM960A18** | Unknown | Unknown | ✅ 2D fix observed | Active 2D fix at survey time. Antenna path unknown. |
| **FN980m** | Unknown | M.2 module on a USB bench host | ✅ Working | GPS + GLONASS + Galileo + BeiDou SVs all received via DIAG. Cold start ramp-up 2→5 GPS SVs in 7s. |
| **MC7455** | None connected | Mini PCIe module | ❓ Untested | NMEA port in USB composition. No antenna attached during survey. |
| **MC7411** | None connected | Mini PCIe module | ❓ Untested | NMEA port in USB composition. No antenna attached during survey. |
| **EM9190** | Shared cellular (AUX=L1, MIMO2=L5) | M.2 module | ✅ L1+L5 confirmed | Dual-band GNSS via AT!GPSSATINFO. No dedicated GNSS port — shares RF with cellular. Active antennas forbidden. |
| **LM1200 (EC25-AF)** | Unknown | Unknown | ❓ Untested | Ethernet-only modem. Antenna routing unknown. |
| **SIM7600NA-H** | Unknown | NMEA port in USB composition | ❓ Untested | GNSS capability expected (MDM9607). |
| **R11e-LTE-US** | Unknown | mini-PCIe module | ❓ Untested | WNC custom design. GNSS antenna routing unknown. |

---

## DIAG Log Code Support by Chipset

### PositionReport (0x1476) Versions

| Chipset | PositionReport Version | Payload Size | Status |
|---------|----------------------|--------------|--------|
| MDM9207 / MDM9607 | v1/v2 | 291 bytes | ✅ Confirmed (EG25-G) |
| MDM9230 | v1/v2 | 291 bytes | ❓ Inferred (same DIAG generation) |
| MDM9650 / SDX20 | v1/v2 or v13 | 291 or 2477 bytes | ❓ Untested — may use either format depending on firmware |
| SDX50M | v1/v2 or v13 | Unknown | ❓ Untested |
| SDX55 | v13 | 2477 bytes | ✅ Confirmed (RM500Q-AE) |
| SDX62 | **v24 (0x18)** | Variable, 1245–2895 bytes (N × 30 B SV array) | ✅ Confirmed (RM520N-GL: 1540/1540 records v24; 616 LG290P epochs at 2.59 m median. CFW-3212 emits the same variant.) Early RM520N-GL firmware also emitted a few **v21 (0x15)** records (1547 B fix / 647 B prediction), which decode on the v24 DOP offset @461. **R01A08 (RM520N-GL-AP, `_11.200.11.200`) emits v21 only:** 62/62 records, all 647 B prediction epochs (no antenna). |

### GNSS Log Code Availability

| Log Code | Name | MDM9207 | MDM9230 | MDM9650 | SDX55 | SDX62 |
|----------|------|---------|---------|---------|-------|-------|
| 0x1476 | PositionReport | ✅ | ❓ | ❓ | ✅ | ✅ |
| 0x1477 | GpsMeasurementReport | ✅ | ❓ | ❓ | ✅ (5 SVs confirmed) | ✅ |
| 0x1480 | GlonassMeasurementReport | ✅ | ❓ | ❓ | ✅ (6 SVs confirmed) | ❓ |
| 0x14DE | OemdreMeasurementReport | ❓ | ❓ | ❓ | ✅ (NV 7165 enable) | ❓ |
| 0x14E1 | OemdreSvPolyReport | ❓ | ❓ | ❓ | ✅ | ❓ |
| 0x1756 | BdsMeasurementReport | ❓ | ❓ | ❓ | ❓ Not yet parsed | ❓ |
| 0x1886 | GalMeasurementReport | ❓ | ❓ | ❓ | ✅ (header parsed, SV struct TBD) | ❓ |

### OEM DRE (Dead Reckoning Engine) Support

OEM DRE is enabled via NV item 7165 write (value=1) through DIAG, then activated via subsystem command (opcode 75, subsys GPS=13, cmd 0x64, OEM_CONTROL=202).

| Chipset | OEM DRE | Notes |
|---------|---------|-------|
| MDM9207 | ❓ | Untested |
| MDM9650 / SDX20 | ❓ | Untested |
| SDX55 | ✅ Confirmed | NV write + session enable working on RM500Q-AE |
| SDX62 | ⚠️ Partial | RM520N-GL: NV 7165 = 1 write succeeds, but the OEM DRE session-enable command times out; still unresolved. Same on CFW-3212. |

---

## diaggpsd Configuration per Device

Quick reference for command-line invocation per tested or expected device configuration.

### Tested Configurations

| Device | Host | Command |
|--------|------|---------|
| EG25-G | Raspberry Pi 5 (USB host) | `python3 diaggpsd.py --debug` |
| RM500Q-AE | USB host (laptop) | `python3 diaggpsd.py --diag-port /dev/ttyUSB0 --debug` |
| RM500Q-AE | USB host (bench, other modems enumerated first) | `python3 diaggpsd.py --diag-port /dev/ttyUSB5 --debug` |
| FN980m | USB host | `python3 diaggpsd.py --diag-port /dev/ttyUSB0 --debug` |
| CFW-3212 | any host on the CPE's LAN, passive DGE1 UDP from the on-device diagbarf daemon | `diaggpsd --diag-port udp-listen:0.0.0.0:12509 --gpsd-port 12947 --record out.jsonl` (the unit's DGE1 port; the device owns the log mask. Verified with the published package and its public `diaggrok` / `diagmunge` dependencies: TPV + SKY served, record stream byte-identical to the development tree for every shared record type.) |
| CFW-3212 | any (TCP client) | `diaggpsd --diag-port tcp --debug` (run `diaggpsd-diag-setup --chipset sdx62 --diag-port tcp --no-at` first; `--no-at` is a setup option) |
| RM520N-GL (-AA) | remote host via socat PTY tunnel | `diaggpsd.py --diag-port <pty> --no-gpsd --debug` |
| RM520N-GL (-AA) | any (offline replay) | `.venv/bin/python apps/diaggpsd/dlf_to_jsonl.py --format bin --verify-crc <capture.bin> <out.jsonl>` |
| RM520N-GL-AP | PCIe host, local MHI chardev | `.venv/bin/python apps/diaggpsd/diaggpsd.py --diag-port /dev/mhi_DIAG --gpsd-port 12947 --no-stdout --record out.jsonl` (diaggpsd has no `--no-at`; AT setup is `diag_setup.py`'s job. GNSS engine was already on.) |
| RM520N-GL-AP | PCIe host, `diag_socket_log` over ADB-over-MHI | ❌ `adb -s 127.0.0.1:6555 reverse tcp:2500 tcp:2500` + `diag_socket_log -a 127.0.0.1 -p 2500 -r 3` → `diaggpsd.py --diag-port tcp:127.0.0.1:2500`: connects, SET_MASK groups ack with growing latency, then `RuntimeError: Timed out waiting for SET_MASK response for log type 0xb` (0x7 on a rerun). **Cause:** the ADB-over-MHI tunnel carries a steady **~90 KiB/s** (4 MB synthetic `nc` stream, no stall), and the subscribed GNSS logs alone run at ~100 KB/s, so the modem-side DIAG queue backs up and the next ack never leaves it. The raw stream is clean `0x7E` HDLC, and every ack that arrived was parsed; the missing ack is absent from the bytes. A GNSS-only list (311 codes) fails the same way. **Hazard:** a failed run leaves its log masks armed in diag-router. New `diag_socket_log` instances then fail `Diag_LSM_Init` (`mask sync error`), `/dev/mhi_DIAG` can go silent, and `adb shell` can hang (an `adb disconnect`/`connect` clears that). Recover with `AT+CFUN=1,1` on `/dev/mhi_DUN` (about 20 s), then clear masks over `/dev/mhi_DIAG`. Use the MHI chardev instead. |

### Expected Configurations (Untested)

| Device | Expected Command | Notes |
|--------|-----------------|-------|
| EM7511 | `python3 diaggpsd.py --diag-port /dev/ttyUSB0 --debug` | GPS engine already active (loc_launcher). AT+QGPS is unsupported, so run `diag_setup.py --no-at`. Sierra AT!GPS* not yet implemented. |
| T77W968 (DW5821e) | `python3 diaggpsd.py --diag-port /dev/ttyUSB3 --debug` | DIAG on ttyUSB3 (raw os.open, pyserial may fail). No GNSS antenna in laptop. |
| EM9190 | ❌ Not possible — DIAG port blocked | Investigate QMI-encapsulated DIAG. |
| MC7455 | `python3 diaggpsd.py --diag-port /dev/ttyUSBx --debug` | DIAG in USB composition. Exact index depends on driver enumeration order. |
| MC7411 | `python3 diaggpsd.py --diag-port /dev/ttyUSBx --debug` | Same as MC7455. |
| SIM7600NA-H | `python3 diaggpsd.py --diag-port /dev/ttyUSB0 --debug` | DIAG on ttyUSB0 per USB composition. SIMCom GNSS AT commands unknown. |
| FN980m | `python3 diaggpsd.py --diag-port /dev/ttyUSB0 --debug` | DIAG on ttyUSB0 (if00). USB DM rejects LOG_CONFIG on some units — may need diag_socket_log. Telit AT$GPSP not implemented. |
| RC400L | `python3 diaggpsd.py --diag-port /dev/ttyUSBx --debug` | DIAG on USB interface 2. No GNSS AT commands on this device. No usable antenna. |
| RM520N-GL (-AA, direct USB) | `.venv/bin/python apps/diaggpsd/diaggpsd.py --diag-port /dev/serial/by-id/<…>-if00-port0 --debug` | Direct `if00` DIAG works on R03A03 A0.303 and R03A04 01.203. Use by-id paths: the composition varies (4-port vs 6-interface) and so does the DIAG index. **Do not reuse the CFW-3212 TCP recipe as-is:** `diag_socket_log` over ADB was recorded as pre-framed DLF on this unit. On the RM520N-GL-AP (R01A08) the same tool emits clean HDLC, so check the first bytes before choosing a deframer. |
| RM520N-GL-AP (GL-XE3000) | No validated recipe yet | The GL-XE3000 has no Python. Candidates: diagbarf `--source hdlc-chardev --diag-dev /dev/mhi_DIAG` on the GL-XE3000 feeding diaggpsd's TCP/UDP listener, or a socat tunnel of `/dev/mhi_DIAG`. Neither has been validated. On the -AP, `diag_socket_log` over ADB-over-MHI into `--diag-port tcp:127.0.0.1:2500` connects but SET_MASK for log group 0xB times out (see Tested Configurations). |

### AT Command Dialects

diaggpsd currently supports only Quectel `AT+QGPS*` commands. Other vendors require skipping AT setup (`diag_setup.py --no-at`; diaggpsd itself has no such flag) or new AT dialect support.

| Vendor | AT GNSS Dialect | diaggpsd Support |
|--------|----------------|-----------------|
| Quectel | `AT+QGPS=1` / `AT+QGPSEND` | ✅ Implemented |
| Sierra Wireless | `AT!GPSFIX=` / `AT!GPSSTATUS?` | ❌ Not implemented |
| Telit | `AT$GPSP=1` / `AT#GPS=1` | ❌ Not implemented |
| SIMCom | `AT+CGNSSPWR=1` / `AT+CGNSSINFO` | ❌ Not implemented |
| Foxconn/Dell | Unknown | ❌ Not implemented |
| MikroTik (WNC) | Unknown (minimal AT) | ❌ Not implemented |

---

## Testing Priority

Devices ranked by testing value for diaggpsd development:

| Priority | Device | Chipset | Rationale |
|----------|--------|---------|-----------|
| 1 | Sierra EM7511 | MDM9650 | First non-Quectel test. `--no-at` ready. DIAG + ADB available. GPS engine active. |
| 2 | Foxconn T77W968 | SDX20 | Tests SDX20 PositionReport version (v1/v2 vs v13 question). DIAG confirmed. GPS DIAG likely inactive (SDX20). |
| 3 | Quectel EP06-A | MDM9640 | GPS DIAG (ID 5) active. Same profile as EG25-G. Live diaggpsd test needed. |
| 4 | SIMCom SIM7600NA-H | MDM9607 | Tests SIMCom firmware variant of MDM9607. DIAG on ttyUSB0. |
| 5 | Sierra MC7455 | MDM9230 | Older chipset. Tests MDM9230 log code compatibility. |
| 6 | Sierra MC7411 | SDX50M | Tests SDX50M (between SDX20 and SDX55). |
| 7 | Quectel RM520N-GL-AP | SDX62 | ⚠️ Transport + v21 parsing measured on a PCIe bench host. Remaining: fit a GNSS antenna and compare a fix with `AT+QGPSLOC=2`; run the GL-XE3000 unit (R03A04). |
| — | ~~Quectel RM520N-GL (-AA)~~ | SDX62 | ✅ Tested. v24 PositionReport validated against LG290P (2.59 m). Still open: BeiDou B1I and the GLONASS used-count bug. |
| — | ~~Sierra EM9190~~ | SDX55 | ❌ DIAG blocked. QMI-encapsulated DIAG under investigation. |
| — | ~~Telit FN980m~~ | SDX55 | ✅ Tested. GPS DIAG active, diaggpsd works. |
| — | ~~Telit LM960A18~~ | SDX20 | ❌ SDX20 — GPS DIAG inactive. |
