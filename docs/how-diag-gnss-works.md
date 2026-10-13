# How the DIAG GNSS Subsystem Works

Technical reference for extracting raw GNSS observables from MDM/SDX-series cellular modems via the DIAG diagnostic protocol, and how `diaggpsd` implements this.

---

## 1. Introduction

MDM/SDX-series cellular modems (MDM9607, SDX55, SDX62, etc.) contain a full GNSS receiver alongside the cellular baseband. The modem's GNSS engine produces two types of output:

- **NMEA sentences** -- standard text strings like `$GPGGA`, `$GPRMC`, delivered over an AT command interface or dedicated NMEA port.
- **DIAG log packets** -- binary packets containing the raw measurements that the GNSS engine uses to compute its NMEA output. These include pseudorange, carrier phase, Doppler, and per-satellite carrier-to-noise ratio.

NMEA is sufficient for basic positioning. DIAG is necessary when you need raw observables -- for example, to feed a PPP (Precise Point Positioning) or RTK (Real-Time Kinematic) solver, or to access GLONASS/Galileo/BeiDou measurement data that the modem's NMEA output may not include.

`diaggpsd` is a Python daemon that connects to a modem's DIAG port, subscribes to GNSS log codes, parses the binary packets, and outputs the results as gpsd-compatible JSON on TCP port 2947.

---

## 2. DIAG Protocol Fundamentals

The DIAG protocol is a binary diagnostic interface exposed over USB serial (typically `/dev/ttyUSB0`) or via a TCP relay (`diag_socket_log`). Every interaction follows the same framing and addressing rules.

### 2.1. HDLC Framing

All DIAG packets are HDLC-framed:

```
[payload + CRC16] → byte-stuff → append 0x7E delimiter
```

**Frame delimiter:** `0x7E` marks the end of every frame. A `0x7E` byte also appears between consecutive frames (functioning as both the end of one frame and separator before the next).

**Byte stuffing:** Two byte values must be escaped within the payload:
- `0x7D` (the escape character itself) becomes `0x7D 0x5D`
- `0x7E` (the delimiter) becomes `0x7D 0x5E`

On the receiving side, un-stuffing is applied in reverse order.

**CRC-16/MCRF4XX:** A 2-byte CRC is appended to the payload before byte-stuffing. The CRC uses:
- Reflected polynomial `0x8408` (bit-reversed `0x1021`)
- Initial value `0xFFFF`
- Final XOR `0xFFFF`
- Little-endian byte order

This is the same CRC as HDLC/PPP/X.25, sometimes called CRC-16/CCITT with reflected bit ordering. The implementation in `diag.py` uses a 256-entry lookup table:

```python
# From diag.py — CRC-16/MCRF4XX
def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC16_TABLE[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFF
```

**Example:** To send a Version Request (opcode `0x00`), the raw wire bytes are:

```
Payload:  00
CRC-16:   crc16(b'\x00') = 0x2017 → bytes: 17 20
Stuffed:  00 17 20       (no bytes need escaping)
Framed:   00 17 20 7E
```

**TCP mode exception:** When using `diag_socket_log` as a relay (the modem's kernel DIAG driver provides raw packets to `diag_socket_log`, which wraps them with `0x7E` delimiters and byte-stuffing), the CRC appended by `diag_socket_log` is not a valid MCRF4XX CRC. `diaggpsd` handles this by stripping the 2-byte trailer without verification when `no_crc=True`.

### 2.2. Key DIAG Opcodes

Every DIAG packet begins with a 1-byte opcode. The opcodes relevant to GNSS work are:

| Opcode | Hex    | Name              | Direction | Purpose |
|:-------|:-------|:------------------|:----------|:--------|
| 16     | `0x10` | `DIAG_LOG_F`      | Modem->Host | Delivers a log packet (unsolicited) |
| 38     | `0x26` | `DIAG_NV_READ_F`  | Host<->Modem | Read a 128-byte NV item by index |
| 39     | `0x27` | `DIAG_NV_WRITE_F` | Host<->Modem | Write a 128-byte NV item |
| 75     | `0x4B` | `DIAG_SUBSYS_CMD_F` | Host<->Modem | Dispatch command to a subsystem |
| 115    | `0x73` | `DIAG_LOG_CONFIG_F` | Host<->Modem | Configure which log codes are delivered |

The modem continuously pushes `DIAG_LOG_F` (opcode 16) packets for all subscribed log codes. These are unsolicited -- the host does not poll for them.

### 2.3. Log Subscription Handshake

Before the modem will deliver any log packets, the host must subscribe to specific log codes. This is a two-step process using opcode `0x73` (`DIAG_LOG_CONFIG_F`):

**Step 1: Retrieve bitmask ranges**

The host sends a `RETRIEVE_RANGES` request (operation=1). The modem responds with the bitmask size (in bits) for each of the 16 log type groups (0x0 through 0xF). GNSS log codes fall in group 0x1 (the `LOG_1X` group, codes `0x1000`-`0x1FFF`).

```
Request:  [0x73] [3 pad bytes] [uint32 operation=1]
Response: [0x73] [3 pad bytes] [uint32 operation=1] [uint32 status=0]
          [uint32 bitsize_group0] [uint32 bitsize_group1] ... [uint32 bitsize_group15]
```

**Step 2: Set the enable bitmask**

For each group containing desired log codes, the host sends a `SET_MASK` request (operation=3) with a bitmask where bit N enables log code `(group << 12) | N`.

For example, to subscribe to log code `0x1476` (PositionReport):
- Group = `0x1` (upper nibble of `0x1476`)
- Bit index = `0x476` (lower 12 bits)
- Set bit 0x476 in the group 1 bitmask

```
Request:  [0x73] [3 pad bytes] [uint32 operation=3]
          [uint32 log_type=1] [uint32 bitsize]
          [bitmask bytes...]
Response: [0x73] [3 pad bytes] [uint32 operation=3] [uint32 status=0]
```

After a successful `SET_MASK`, the modem begins pushing `DIAG_LOG_F` packets for the subscribed codes. `diaggpsd` subscribes to all GNSS codes in a single call to `DiagClient.subscribe_logs()`.

---

## 3. GNSS Data Flow

### 3.1. From DSP to Host

The GNSS data path inside the modem looks like this:

```
GNSS RF Front-End
  → DSP (search, acquisition, tracking)
    → Position Engine (WLS, Kalman filter)
      → DIAG log buffer (binary structs)
        → DIAG transport (USB serial or diag_socket_log)
          → Host (diaggpsd)
```

The GNSS DSP produces raw measurements (pseudorange, carrier phase, Doppler) for each tracked satellite. The Position Engine consumes these measurements and outputs a position fix. Both the raw measurements and the position fix are written to the DIAG log buffer as structured binary packets, each tagged with a log code.

### 3.2. Relationship Between AT+QGPS and DIAG Log Delivery

The GNSS engine must be running for DIAG log packets to appear. On Quectel modems, starting the engine requires the AT command:

```
AT+QGPS=1
```

This is handled by `at.py` (the `ATSerial` class). Once the GNSS engine is running, DIAG log packets flow continuously at approximately 1 Hz for position reports and measurement reports.

For modems where the GPS engine is already running (e.g., an OpenCPU build with an application controlling the engine), pass `--no-at` to `diag_setup.py` so it skips the AT setup and only configures DIAG; `diaggpsd.py` itself never sends AT commands.

### 3.3. OEM DRE Enablement

OEM DRE (Dead Reckoning Engine) is a modem firmware feature that provides extended measurement data including multi-constellation raw observables and SV polynomial orbit data. Enabling it requires two steps:

**Step 1: NV write (persistent)**

Write NV item 7165 (`NV_GNSS_OEM_FEATURE_MASK`) with value 1 via `DIAG_NV_WRITE_F` (opcode `0x27`):

```
Request:  [0x27] [uint16 item=7165] [uint32 value=1] [zeros to pad to 128 bytes]
```

This persists across reboots. `diaggpsd` calls `DiagClient.enable_oemdre_nv()` for this step.

**Step 2: Session start (per-boot)**

Send a subsystem command via `DIAG_SUBSYS_CMD_F` (opcode `0x4B`) targeting subsystem 13 (GPS):

```
Request:  [0x4B]
          [uint8  subsys_id=13]        # DIAG_SUBSYS_GPS
          [uint16 subcmd=0x0064]       # CGPS_DIAG_PDAPI_CMD
          [uint8  cmd=202]             # CGPS_OEM_CONTROL
          [uint8  version=0]
          [uint32 feature=1]           # GPSDIAG_OEMFEATURE_DRE
          [uint32 action=1]            # DRE ON
          [uint32 reserved=0]
          [uint32 reserved=0]
```

After both steps succeed, the modem begins emitting OEM DRE log codes (0x14DE and 0x14E1) in addition to the standard GPS/GLONASS measurement reports.

---

## 4. GNSS Log Codes

All GNSS log codes reside in the `LOG_1X` group (base `0x1000`). The relevant codes are offsets from `LOG_1X_BASE_C`:

| Log Code | Offset | Name in Header | diaggpsd Parser |
|:---------|:-------|:---------------|:----------------|
| `0x1476` | `0x476` | `LOG_GNSS_POSITION_REPORT_C` | `parse_position_report()` |
| `0x1477` | `0x477` | `LOG_GNSS_GPS_MEASUREMENT_REPORT_C` | `parse_gps_measurement_report()` |
| `0x1480` | `0x480` | `LOG_GNSS_GLONASS_MEASUREMENT_REPORT_C` | `parse_glonass_measurement_report()` |
| `0x14DE` | `0x4DE` | `LOG_GNSS_OEMDRE_MEASUREMENT_REPORT_C` | `parse_oemdre_measurement_report()` |
| `0x14E1` | `0x4E1` | `LOG_GNSS_OEMDRE_SVPOLY_REPORT_C` | `parse_oemdre_svpoly_report()` |
| `0x1756` | `0x756` | `LOG_GNSS_BDS_MEASUREMENT_REPORT_C` | defined, not yet parsed |
| `0x1886` | `0x886` | `LOG_GNSS_GAL_MEASUREMENT_REPORT_C` | `parse_galileo_measurement_report()` (partial) |

### 4.1. DIAG Log Frame Structure

Every log packet arrives inside a `DIAG_LOG_F` (opcode 16) wrapper. The payload has two layers:

```
Outer frame (opcode 16 payload):
  [0]       uint8   pending_msgs    — messages queued behind this one
  [1:3]     uint16  outer_len       — length of inner packet
  [3:]      inner packet

Inner packet:
  [0:2]     uint16  inner_len       — self-referential length
  [2:4]     uint16  log_type        — the log code (e.g. 0x1476)
  [4:12]    uint64  log_time        — modem clock ticks (not GPS time)
  [12:]     log_payload             — parsed by gnss_packets.py
```

The `log_type` field determines which parser to invoke. The `log_time` field is an opaque modem timestamp, not directly convertible to UTC.

### 4.2. 0x1476 -- PositionReport

The Position Report contains the GNSS engine's computed position fix. Two struct versions exist:

**v1/v2 format (291 bytes) -- MDM9607 and earlier:**

| Offset | Type | Field | Notes |
|:-------|:-----|:------|:------|
| 0 | uint8 | version | 1 or 2 |
| 1 | uint32 | f_count | Monotonic frame counter |
| 5 | uint8 | pos_source | 0=None, 1=WLS, 2=Kalman, 3=Injected, 4=Database |
| 17 | uint16 | fix_events | Bitmask of fix event flags |
| 23 | uint16 | gps_week | GPS week number |
| 25 | uint32 | gps_tow_ms | GPS time of week in milliseconds |
| 40 | double | lat_rad | Latitude in **radians** (not degrees) |
| 48 | double | lon_rad | Longitude in **radians** |
| 56 | 48 floats | float block | Altitude, velocity, DOP, clock biases |
| ... | mixed | tail | Reliability, SV counts |

Format string: `'<BIBIHIBHIHIBHIIdd' + 'f'*48 + 'BffffBBHffIIBBBBBB'`

**v13 format (2477 bytes) -- SDX55/SDX62 and later:**

The v13 format was reverse-engineered from RM500Q-AE captures. Key differences from v1/v2:

| Aspect | v1/v2 | v13 |
|:-------|:------|:----|
| Total size | 291 bytes | 2477 bytes |
| Header | 56 bytes | 65 bytes (+9 inserted at offset 23) |
| lat_rad offset | 40 | 49 |
| lon_rad offset | 48 | 57 |
| Float count | 48 | 65 (adds TDOP, GDOP) |
| Tail | 43 bytes | 82 bytes |
| Per-SV array | absent | 90 slots x 23 bytes |

The 9 inserted bytes at offset 23 are consistently zero in indoor captures. Their purpose remains unidentified; they likely contain extended validity masks that populate only with outdoor multi-constellation fixes.

The `pos_source` field indicates fix quality:

| Value | Meaning |
|:------|:--------|
| 0 | No position |
| 1 | WLS (Weighted Least Squares) |
| 2 | Kalman filter (best fix) |
| 3 | Externally injected |
| 4 | Database/cell |
| 8 | Sensor-aided (v13 only) |

Latitude and longitude are IEEE 754 doubles in **radians** in both versions. The vendor SDK API layer converts to degrees, but the DIAG log packet uses radians internally. `diaggpsd` converts to degrees in the `to_dict()` method.

The v13 per-SV array contains 90 fixed slots of 23 bytes each. Each record includes SV ID, constellation-specific frequency slot, C/N0 (in 0.5 dB-Hz units), azimuth and elevation (floats, radians), and a measurement value with uncertainty.

`diaggpsd` detects the version from byte 0 and packet size, routing to the appropriate parser automatically.

### 4.3. 0x1477 -- GPS Measurement Report

Per-satellite raw measurements for GPS (L1 C/A). Emitted at approximately 1 Hz.

**Header (25 bytes):**

| Offset | Type | Field | Notes |
|:-------|:-----|:------|:------|
| 0 | uint8 | version | Struct version |
| 1 | uint32 | f_count | Frame counter |
| 5 | uint16 | week | GPS week number |
| 7 | uint32 | milliseconds | GPS time of week in ms |
| 11 | float | time_bias | Receiver clock bias (ms) |
| 15 | float | clock_time_unc | Clock uncertainty (ms) |
| 19 | float | clock_freq_bias | Frequency bias (m/s) |
| 23 | float | clock_freq_unc | Frequency uncertainty (m/s) |
| 24 | uint8 | sv_count | Number of SV records following |

**Per-SV record (74 bytes):** Repeated `sv_count` times.

Key fields per SV:

| Field | Type | Notes |
|:------|:-----|:------|
| sv_id | uint8 | GPS PRN (1-32) |
| observation_state | uint8 | Tracking state |
| carrier_noise | uint16 | C/N0 in 0.1 dB-Hz units |
| latency | int16 | Measurement latency (ms) |
| unfiltered_meas_integral | uint32 | Pseudorange integer part |
| unfiltered_meas_fraction | float | Pseudorange fractional part |
| unfiltered_time_unc | float | Pseudorange uncertainty (m) |
| unfiltered_speed | float | Pseudorange rate / Doppler (m/s) |
| unfiltered_speed_unc | float | Doppler uncertainty (m/s) |
| measurement_status | uint32 | Validity bitmask |
| azimuth | float | Azimuth from north (radians) |
| elevation | float | Elevation above horizon (radians) |
| carrier_phase_integral | uint16 | Carrier phase cycles (integer) |
| carrier_phase_fraction | float | Carrier phase cycles (fractional) |
| fine_speed | float | Fine Doppler (m/s) |
| fine_speed_unc | float | Fine Doppler uncertainty (m/s) |
| cycle_slip_count | uint8 | Number of detected cycle slips |

The `measurement_status` bitmask indicates which fields are valid. The pseudorange is reconstructed from the integral and fractional parts.

### 4.4. 0x1480 -- GLONASS Measurement Report

Structurally similar to the GPS Measurement Report but with GLONASS-specific fields:

**Header (26 bytes):** Same as GPS but adds `glonass_cycle_number` (uint8) and `glonass_days` (uint16) for GLONASS time reference.

**Per-SV record (75 bytes):** Same as GPS but adds `frequency_index` (int8, signed) -- the GLONASS frequency slot number (-7 to +6). This field is critical because GLONASS uses FDMA (each satellite transmits on a different frequency), so the slot number is needed to compute the correct carrier frequency.

SV IDs for GLONASS in the DIAG numbering are offset by 64: GLONASS slot 1 = SV ID 65, slot 24 = SV ID 88.

### 4.5. 0x1886 -- Galileo Measurement Report

Symbol: `LOG_GNSS_GAL_MEASUREMENT_REPORT_C`. The struct format is not fully reverse-engineered. Indoor captures produce 30-byte header-only reports with `sv_count=0`.

Known header fields:
- Byte 0: version (observed: 0)
- Bytes 3-6: uint32 millisecond counter (increments by 1000/s)
- Last byte: sv_count

Full per-SV parsing requires outdoor captures with Galileo constellation visibility. The parser preserves raw hex for future analysis.

### 4.6. 0x14DE -- OEM DRE Measurement Report

Multi-constellation raw measurement data, available when OEM DRE is enabled (see Section 3.3). Contains more detailed fields than the standard GPS/GLONASS reports.

**Header (99 bytes):** Includes GPS and GLONASS time references, GPS-to-GLONASS bias, system RTC time, leap seconds, and clock source indicators.

**Per-SV record (149 bytes):** Contains both filtered and unfiltered measurements, dual measurement status words, multipath estimate, direction (azimuth/elevation), Doppler acceleration, carrier phase, and fine speed with uncertainty.

The OEM DRE report can contain SVs from multiple constellations in a single packet, distinguished by SV ID ranges and the `glo_frequency_index` field.

### 4.7. 0x14E1 -- OEM DRE SV Polynomial Report

Orbit prediction coefficients for a single satellite. These polynomials allow the receiver (or an external processor) to compute SV position and velocity at any time within the validity window.

**Structure (310 bytes):**

| Field | Type | Notes |
|:------|:-----|:------|
| sv_id | uint16 | Satellite identifier |
| frequency_index | int8 | GLONASS slot (negative = GPS) |
| flags | uint8 | Validity flags |
| iode | uint16 | Issue of Data, Ephemeris |
| t0 | double | Reference epoch (seconds) |
| xyz0[3] | double[3] | SV position at t0 (ECEF meters) |
| xyzN[9] | double[9] | Position polynomial coefficients |
| other[4] | float[4] | Additional coefficients |
| position_uncertainty | float | Position uncertainty (meters) |
| iono_delay | float | Ionospheric delay (meters) |
| tropo_delay | float | Tropospheric delay (meters) |
| elevation | float | SV elevation (radians) |
| velocity_coeff[12] | double[12] | Velocity polynomial coefficients |

---

## 5. Why DIAG Over NMEA

NMEA and DIAG serve fundamentally different use cases:

| Capability | NMEA | DIAG |
|:-----------|:-----|:-----|
| Position (lat/lon/alt) | Yes ($GPGGA) | Yes (0x1476) |
| Velocity/heading | Yes ($GPRMC) | Yes (0x1476) |
| SV azimuth/elevation | Yes ($GPGSV) | Yes (0x1477/0x1480) |
| C/N0 per SV | Integer only ($GPGSV) | 0.1 dB-Hz resolution |
| Pseudorange | **No** | Yes (unfiltered_meas) |
| Carrier phase | **No** | Yes (carrier_phase) |
| Doppler (per-SV) | **No** | Yes (fine_speed) |
| Clock bias/drift | **No** | Yes |
| Measurement uncertainty | **No** | Yes (per-field) |
| GLONASS frequency slot | **No** | Yes |
| Orbit polynomials | **No** | Yes (0x14E1) |

**Key practical differences:**

1. **PPP/RTK require raw observables.** NMEA provides only the computed position. To run your own positioning solution (e.g., RTKLIB, CSRS-PPP), you need pseudorange, carrier phase, and Doppler for each satellite. These are only available through DIAG.

2. **GLONASS data gaps in NMEA.** On the RM500Q-AE, GLONASS satellites appear in DIAG measurement reports (0x1480) but are absent from NMEA $GLGSV sentences. This was confirmed during testing -- DIAG showed 6 GLONASS SVs while NMEA reported none. The modem's NMEA generator may suppress constellations based on `AT+QGPSCFG` settings or firmware configuration, but the DIAG log buffer receives all measurements regardless.

3. **Measurement-level C/N0.** NMEA `$GPGSV` provides SNR as an integer (0-99). DIAG provides C/N0 with 0.1 dB-Hz resolution, which is important for signal quality analysis and weighting in positioning algorithms.

4. **Clock information.** DIAG reports include receiver clock bias, clock drift, and clock uncertainty -- essential for precise timing applications and for resolving integer ambiguities in carrier-phase positioning.

---

## 6. diaggpsd Architecture

### 6.1. Component Overview

```
                         +-----------+
                         |  at.py    |  AT+QGPS=1 (start GNSS engine)
                         |  ATSerial |  AT+QGPSEND (stop)
                         +-----+-----+
                               |  serial (/dev/ttyUSB2)
                               v
+----------+           +-------+-------+          +----------------+
| Modem    |  HDLC     |   diag.py     |  parsed  | gnss_packets.py|
| DIAG     | --------> |   DiagClient  | -------> | dispatch()     |
| interface|  serial/  |               |  log     | parse_*()      |
+----------+  TCP      +-------+-------+  frames  +-------+--------+
                               |                          |
                      subscribe_logs()                    | typed dataclasses
                      enable_oemdre_nv()                  v
                      enable_oemdre_session()     +-------+--------+
                                                  | diaggpsd.py    |
                                                  | main loop      |
                                                  +---+----+---+---+
                                                      |    |   |
                                          +-----------+    |   +----------+
                                          v                v              v
                                    stdout JSON     gpsd_server.py   RawServer
                                    (JSON lines)    TCP :2947        TCP :PORT
                                                    (TPV, SKY)       (JSON lines)
```

### 6.2. Transport Modes

**Serial mode** (`--diag-port /dev/ttyUSB0`):

Direct USB connection. The modem's DIAG/DM port is typically the first `ttyUSB` device in the USB composition. `DiagClient.from_serial()` opens the port with pyserial; if the USB driver does not support DTR/RTS ioctls (common with `option` and `qcserial` drivers), it falls back to raw `termios` configuration:

```python
# Raw termios fallback (from diag.py)
fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
```

Full HDLC CRC verification is used in serial mode.

**TCP mode** (`--diag-port tcp` or `--diag-port tcp:host:port`):

For network-connected modems or modems where `diag_socket_log` relays DIAG data over TCP. `diaggpsd` listens on the specified address (default `0.0.0.0:2500`) and waits for `diag_socket_log` to connect. On the modem side:

```
diag_socket_log -a <host_ip> -p 2500
```

In TCP mode, the kernel's `/dev/ffs-diag` interface provides packets to `diag_socket_log` without HDLC CRC, so `diaggpsd` uses `no_crc=True` and strips the 2-byte trailer without verification.

### 6.3. Startup Sequence

Modem configuration happens beforehand in `diag_setup.py`: AT command setup (unless `--no-at`) opens the AT port, sends `AT+QGPS=1` to start the GNSS engine and injects UTC time via `AT+QGPSXTRATIME`; it also enables OEM DRE by writing NV item 7165 and sending the GPS subsystem session-start command.

When `diaggpsd` starts, it then executes this sequence:

1. **Start output servers** -- gpsd server on port 2947 (unless `--no-gpsd`), optional raw JSON server on `--raw-port`.

2. **Open DIAG transport** -- Serial or TCP, depending on `--diag-port`.

3. **Subscribe to log codes** -- Execute the `RETRIEVE_RANGES` / `SET_MASK` handshake for all 6 GNSS log codes:
   - `0x1476` PositionReport
   - `0x1477` GPS Measurement
   - `0x1480` GLONASS Measurement
   - `0x14DE` OEM DRE Measurement
   - `0x14E1` OEM DRE SV Polynomial
   - `0x1886` Galileo Measurement

4. **Main loop** -- Block on `diag.recv()`, filter for `DIAG_LOG_F` (opcode 16), parse the outer frame, dispatch to the correct parser based on `log_type`, route the result to stdout/gpsd/raw outputs.

### 6.4. Version-Aware Parsing

The `parse_position_report()` function in `gnss_packets.py` handles both v1/v2 and v13 formats:

```python
def parse_position_report(log_time: int, data: bytes) -> PositionReport:
    version = data[0]
    if version >= 13 and len(data) >= _POS_V13_MIN_SZ:
        return _parse_position_report_v13(log_time, data)
    # Fall back to v1/v2 format
    ...
```

The v13 parser extracts fields at their shifted offsets (all offsets +9 after the inserted header bytes) rather than using a single `struct.unpack_from()` call, because the v13 format has not been fully mapped to a contiguous format string.

### 6.5. Output Routing

Each parsed packet produces a typed dataclass (`PositionReport`, `GpsMeasurementReport`, etc.) with a `to_dict()` method that converts to a JSON-serializable dictionary. The main loop routes output to three destinations:

- **stdout** -- JSON line per packet (unless `--no-stdout`)
- **gpsd server** -- `PositionReport` becomes a TPV message; `GpsMeasurementReport`/`GlonassMeasurementReport` become SKY messages
- **raw TCP server** -- JSON line broadcast to all connected clients (if `--raw-port` set)

---

## 7. Quick Start Guide

### 7.1. USB Serial Mode (Quectel EG25-G, RM500Q-AE)

Identify the DIAG (DM) and AT ports. On most Quectel modems with the default USB composition:

| Port | Function |
|:-----|:---------|
| `/dev/ttyUSB0` | DIAG/DM |
| `/dev/ttyUSB2` | AT commands |

For the RM500Q-AE (SDX55), the mapping differs:

| Port | Function |
|:-----|:---------|
| `/dev/ttyUSB5` | DIAG/DM |
| `/dev/ttyUSB7` | AT commands |

```bash
# Install dependency
pip install pyserial

# EG25-G (default ports)
python diaggpsd.py --debug

# RM500Q-AE (non-default ports)
python diaggpsd.py --diag-port /dev/ttyUSB5 --at-port /dev/ttyUSB7 --debug

# In another terminal, verify with gpsd clients
gpspipe -w 127.0.0.1
cgps 127.0.0.1
```

Expected output on startup:

```
INFO diaggpsd: AT port /dev/ttyUSB2: starting GPS
INFO diag: Opened serial DIAG port /dev/ttyUSB0
INFO diaggpsd: Enabling OEM DRE NV item ...
INFO diaggpsd: Enabling OEM DRE session ...
INFO diaggpsd: Subscribing to 6 GNSS log codes ...
INFO diag: Log subscription complete for 6 code(s)
INFO diaggpsd: Listening for GNSS packets ...
```

JSON output begins within seconds (measurement reports first, position reports once the engine has a fix):

```json
{"type": "GpsMeasurementReport", "log_time": 12345678, "week": 2410, "milliseconds": 106413002, "time_bias": 0.0, "svs": [{"sv_id": 15, "az_deg": 120.5, "el_deg": 45.2, "cno_db": 35.0, ...}]}
{"type": "PositionReport", "log_time": 12345680, "version": 2, "pos_source": 2, "lat_deg": 12.3456, "lon_deg": -98.7654, "alt_m": 1407.05, ...}
```

### 7.2. TCP Mode (CFW-3212 via diag_socket_log)

For the Quectel RG520N-NA OpenCPU (CFW-3212) or any modem where you use `diag_socket_log` to relay DIAG data over the network:

```bash
# On the host (your PC):
python diag_setup.py --chipset sdx62 --diag-port tcp --no-at
python diaggpsd.py --diag-port tcp --debug

# On the modem (via SSH):
diag_socket_log -a <host_ip> -p 2500
```

The `diag_setup.py --no-at` flag is strongly recommended for OpenCPU builds because the AT command path through `port_bridge` and `/dev/smd7` is unreliable -- `ql_nw_service` can claim `smd7`, silently breaking AT commands without any error indication.

If the GPS engine is not already running on the modem, start it separately before launching `diag_socket_log`:

```bash
# On the modem, start GPS via the local AT interface:
echo -e "AT+QGPS=1\r" > /dev/smd7
```

### 7.3. Verifying Output

**gpspipe** (comes with gpsd):

```bash
# Stream TPV/SKY JSON from diaggpsd's gpsd server
gpspipe -w 127.0.0.1

# Example TPV output:
{"class":"TPV","mode":3,"time":"2026-03-15T12:34:56.000Z","lat":12.3456,"lon":-98.7654,"alt":1407.05,"speed":0.0,"track":0.0,"hdop":2.8,"vdop":1.0}
```

**cgps** (curses GPS monitor):

```bash
cgps 127.0.0.1
```

Displays a continuously updating position, speed, and satellite view in the terminal.

**Raw JSON logging:**

```bash
python diaggpsd.py --no-gpsd --raw-port 5000 --debug | tee gnss_capture.jsonl
```

Saves every parsed packet as a JSON line for offline analysis.

---

## References

- `diag.py` -- HDLC transport, CRC-16, log subscription, OEM DRE enablement
- `gnss_packets.py` -- All struct definitions, field names, parsers
- `diaggpsd.py` -- Main loop, output routing
- `diag_setup.py` -- Modem configuration: AT setup, NV items, OEM DRE
- `../../docs/howto/qualcomm-diag-dump.md` -- DIAG protocol basics, NV items, subsystem IDs
- `../../tmp/posreport_v13_analysis.md` -- PositionReport v13 reverse engineering analysis
