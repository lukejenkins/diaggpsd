# DIAG GNSS Log Code Reference

Comprehensive catalog of all known GNSS-related DIAG log codes and their
`LOG_*` symbolic names, cross-referenced with diaggpsd implementation
status.

---

## 1. Overview: How DIAG Log Codes Work

### Equipment IDs and Base Addresses

DIAG log codes are 16-bit values. The upper 4 bits encode an
**equipment ID** that identifies the technology area:

| Equipment ID | Base Address | Name   | Description                    |
|:-------------|:-------------|:-------|:-------------------------------|
| 0            | `0x0000`     | OEM    | 3rd party OEM (licensee) use   |
| 1            | `0x1000`     | 1X     | Traditional 1X line of products|
| 4            | `0x4000`     | WCDMA  | WCDMA products                 |
| 5            | `0x5000`     | GSM    | GSM products                   |
| 6            | `0x6000`     | LBS    | Location-Based Services        |
| 7            | `0x7000`     | UMTS   | UMTS products                  |
| 11           | `0xB000`     | APPS   | Applications processor         |
| 12           | `0xC000`     | DSP    | DSP subsystem                  |

All GNSS log codes use **equipment ID 1** (`LOG_1X_BASE_C = 0x1000`).
The log code value equals the defined offset plus `0x1000`. For example,
`LOG_GNSS_POSITION_REPORT_C` is defined as `(0x476 + LOG_1X_BASE_C)`,
yielding log code `0x1476`.

### Log Code Subscription

To receive log packets, the host must subscribe to specific log codes via
the DIAG protocol:

1. Send `DIAG_LOG_CONFIG_F` (opcode 115) with operation = SET_MASK
2. The mask is organized as a bitmask per equipment ID
3. For equipment ID 1, each log code offset maps to a bit position
4. The modem streams matching log packets as `DIAG_LOG_F` (opcode 16)

### Log Packet Structure

Every DIAG log packet has a common outer frame:

```
Offset  Size  Field         Description
0       1     pending_msgs  Number of pending messages
1       2     outer_len     Length of inner packet
3       2     inner_len     Length of inner packet (redundant)
5       2     log_type      Log code (e.g., 0x1476)
7       8     log_time      Modem clock ticks (CDMA time)
15      var   log_payload   Log-code-specific data
```

### Naming Conventions

Log code names follow a progression reflecting the chipset vendor's GNSS architecture
evolution:

| Prefix    | Era        | Description                                     |
|:----------|:-----------|:------------------------------------------------|
| `LOG_GPS_`| Pre-2007   | Original standalone GPS engine                  |
| `LOG_CGPS_`| 2007-2011 | Converged GPS (gpsOne Gen 7/8)                  |
| `LOG_GNSS_`| 2011+     | Multi-constellation GNSS (Gen 8+)               |
| `LOG_CONVERGED_GPS_`| 2009 | Transitional converged GPS names          |

The `CGPS_` and `GNSS_` prefixed codes are the ones relevant to modern
chipsets (MDM9607, SDX55, SDX62, etc.).

---

## 2. Log Codes Implemented in diaggpsd

These are the log codes that diaggpsd can parse, with detailed struct
information.

### 2.1 Position Report (0x1476)

| Field             | Value                                        |
|:------------------|:---------------------------------------------|
| **Log code**      | `0x1476`                                     |
| **Symbol**        | `LOG_GNSS_POSITION_REPORT_C`                 |
| **Offset**        | `0x476`                                      |
| **diaggpsd status** | Parsed, subscribed                         |
| **Versions**      | v1/v2 (291 bytes, MDM9607), v13 (2477 bytes, SDX55+) |

**v1/v2 struct** (291 bytes, 83 fields):

```
Header (17 fields, 56 bytes):
  B     version
  I     f_count
  B     pos_source          0=None, 1=WLS, 2=KF, 3=Injected, 4=DB
  I     reserved1
  H     pos_vel_flag
  I     pos_vel_flag2
  B     failure_code
  H     fix_events
  I     fake_align
  H     gps_week
  I     gps_tow_ms
  B     glo_four_year
  H     glo_days
  I     glo_tow_ms
  I     pos_count
  d     lat_rad             IEEE 754 double, RADIANS (not degrees)
  d     lon_rad             IEEE 754 double, RADIANS (not degrees)

Float block (48 floats, 192 bytes):
  f     alt_m               WGS84 height above ellipsoid
  f     heading_rad         radians
  f     heading_unc_rad
  f×3   vel_enu[3]          east/north/up velocity (m/s)
  f×3   vel_sigma[3]
  f×2   clock_bias, sigma
  f×2   ggtb, sigma         GPS-GLONASS time bias (ns)
  f×2   gbtb, sigma         GPS-BDS time bias (ns)
  f×2   bgtb, sigma         BDS-GLONASS time bias
  f×6   filt_*tb            filtered time biases
  f×2   sft_offset, sigma
  f×2   clock_drift, sigma
  f×2   filtered_alt, sigma
  f×2   raw_alt, sigma
  f×14  align[14]           padding / reserved
  f     pdop
  f     hdop
  f     vdop

Tail (18 fields, 43 bytes):
  B     ellipse_confidence
  f×3   ellipse_angle, semi_major, semi_minor
  f     pos_sigma_vertical
  B×2   horiz_reliability, vert_reliability
  H     reserved2
  f×2   gnss_heading_rad, gnss_heading_unc_rad
  I×2   sensor_data_mask, sensor_aid_mask
  B×6   num/total GPS/GLO/BDS SVs
```

**v13 struct** (2477 bytes, SDX55/SDX62):

Same core fields, shifted offsets due to 9 inserted header bytes at
offset 23. Float block extended from 48 to 65 floats (adds TDOP, GDOP).
Appends a 90-slot SV info array (23 bytes per SV). See
`tmp/posreport_v13_analysis.md` for full field map.

### 2.2 GPS Measurement Report (0x1477)

| Field             | Value                                        |
|:------------------|:---------------------------------------------|
| **Log code**      | `0x1477`                                     |
| **Symbol**        | `LOG_GNSS_GPS_MEASUREMENT_REPORT_C`          |
| **Offset**        | `0x477`                                      |
| **diaggpsd status** | Parsed, subscribed                         |

**Header** (9 fields, 25 bytes):

```
  B     version
  I     f_count
  H     week              GPS week number
  I     milliseconds      GPS time of week (ms)
  f     time_bias
  f     clock_time_unc
  f     clock_freq_bias
  f     clock_freq_unc
  B     sv_count
```

**Per-SV record** (26 fields, 76 bytes):

```
  B     sv_id
  B     observation_state
  B     observations
  B     good_observations
  H     parity_error_count
  B     filter_stages
  H     carrier_noise       CNo in 0.1 dB-Hz units
  h     latency
  B     predetect_interval
  H     postdetections
  I     unfiltered_meas_integral
  f     unfiltered_meas_fraction
  f     unfiltered_time_unc
  f     unfiltered_speed
  f     unfiltered_speed_unc
  I     measurement_status
  B     misc_status
  I     multipath_estimate
  f     azimuth             radians
  f     elevation           radians
  h     carrier_phase_integral
  H     carrier_phase_fraction
  f     fine_speed
  f     fine_speed_unc
  B     cycle_slip_count
  I     pad
```

### 2.3 GLONASS Measurement Report (0x1480)

| Field             | Value                                        |
|:------------------|:---------------------------------------------|
| **Log code**      | `0x1480`                                     |
| **Symbol**        | `LOG_GNSS_GLONASS_MEASUREMENT_REPORT_C`      |
| **Offset**        | `0x480`                                      |
| **diaggpsd status** | Parsed, subscribed                         |

**Header** (10 fields, 26 bytes):

Same as GPS measurement header but with additional GLONASS-specific
fields (glonass_cycle_number, glonass_days).

**Per-SV record** (27 fields, 77 bytes):

Same as GPS SV record but with additional `frequency_index` (signed
byte, GLONASS frequency slot).

### 2.4 OEM DRE Measurement Report (0x14DE)

| Field             | Value                                        |
|:------------------|:---------------------------------------------|
| **Log code**      | `0x14DE`                                     |
| **Symbol**        | `LOG_GNSS_OEMDRE_MEASUREMENT_REPORT_C`       |
| **Offset**        | `0x4DE`                                      |
| **diaggpsd status** | Parsed, subscribed                         |

Multi-constellation raw measurement report used for OEM Dead Reckoning
Extended (DRE). Contains all GNSS constellations in a single report with
enhanced measurement fields.

**Header** (34 fields, 113 bytes):

```
  B     version
  B     reason
  B     sv_count
  B×2   seq_num, seq_max
  H     rf_loss
  B     system_rtc_valid
  I     f_count
  I     clock_resets
  Q     system_rtc_time
  B×2   gps_leap_seconds, gps_leap_seconds_unc
  f×2   gps_to_glo_bias_ms, gps_to_glo_bias_ms_unc
  H     gps_week
  I     gps_milliseconds
  I     gps_time_bias
  I     gps_clock_time_unc
  B     gps_clock_source
  B     glo_clock_source
  B     glo_year
  H     glo_day
  I     glo_milliseconds
  f     glo_time_bias
  f     glo_clock_time_unc
  f×2   clock_freq_bias, clock_freq_unc
  B     frequency_source
  I×5   cdma_clock_info[5]
  B     source
```

**Per-SV record** (37 fields, 152 bytes):

```
  B     sv_id               constellation encoded in ID range
  B     unkn
  b     glo_frequency_index (signed)
  I     observation_state
  B×3   observations, good_observations, filter_stages
  B×2   predetect_interval, cycle_slip_count
  H     postdetections
  I×2   measurement_status, measurement_status2
  H×2   carrier_noise, rf_loss
  h     latency
  f     filtered_meas_fraction
  I     filtered_meas_integral
  f×3   filtered_time_unc, filtered_speed, filtered_speed_unc
  f     unfiltered_meas_fraction
  I     unfiltered_meas_integral
  f×3   unfiltered_time_unc, unfiltered_speed, unfiltered_speed_unc
  B     multipath_estimate_valid
  I     multipath_estimate
  B     direction_valid
  f×3   azimuth, elevation, doppler_acceleration
  f×2   fine_speed, fine_speed_unc
  Q     carrier_phase
  I     f_count
  H     parity_error_count
  B     good_parity
```

### 2.5 OEM DRE SV Polynomial Report (0x14E1)

| Field             | Value                                        |
|:------------------|:---------------------------------------------|
| **Log code**      | `0x14E1`                                     |
| **Symbol**        | `LOG_GNSS_OEMDRE_SVPOLY_REPORT_C`            |
| **Offset**        | `0x4E1`                                      |
| **diaggpsd status** | Parsed, subscribed                         |

Satellite polynomial ephemeris data for OEM DRE. One report per SV,
contains position and velocity polynomial coefficients for orbit
prediction.

**Fixed struct** (43 fields, 310 bytes):

```
  B     version
  H     sv_id
  b     frequency_index     signed (negative = GPS)
  B     flags
  H     iode
  d     t0                  reference time
  d×3   xyz0[3]             position at t0 (ECEF meters)
  d×9   xyzN[9]             position polynomial coefficients
  f×4   other[4]            additional coefficients
  f     position_uncertainty
  f     iono_delay
  f     iono_dot
  f     sbas_iono_delay
  f     sbas_iono_dot
  f     tropo_delay
  f     elevation           radians
  f     elevation_dot
  f     elevation_uncertainty
  d×12  velocity_coeff[12]  velocity polynomial coefficients
```

### 2.6 Galileo Measurement Report (0x1886)

| Field             | Value                                        |
|:------------------|:---------------------------------------------|
| **Log code**      | `0x1886`                                     |
| **Symbol**        | `LOG_GNSS_GAL_MEASUREMENT_REPORT_C`          |
| **Offset**        | `0x886`                                      |
| **diaggpsd status** | Partially parsed, subscribed               |

Galileo E1/L1 measurement report. The struct format is not fully
reverse-engineered. Indoor captures produce 30-byte header-only reports
with zero SVs. Known header fields:

```
  B     version             (observed: 0)
  B     unknown1            (observed: 6)
  B     unknown2            (observed: 5)
  I     milliseconds        GPS time of week (ms), increments 1000/s
  ...   (unknown fields)
  B     sv_count            last byte of payload (0 in indoor captures)
```

Full SV measurement data expected with outdoor antenna and Galileo
visibility. Full struct RE is still open.

---

## 3. All Known GNSS Log Codes

Complete catalog of every GNSS-related log code with a known `LOG_*`
symbol. Organized by functional category.

### 3.1 Legacy GPS (Pre-Converged)

These are the oldest GPS log codes, predating the converged GPS (CGPS)
architecture. Most are obsolete on modern chipsets but may still be
present on legacy platforms.

| Hex    | Offset | Symbol                              | Description                          | diaggpsd |
|:-------|:-------|:------------------------------------|:-------------------------------------|:---------|
| 0x1014 | 0x014  | `LOG_GPS_C`                         | Position & speed from GPS receiver   | --       |
| 0x1036 | 0x036  | `LOG_GPS_FWD_MSG_C`                | GPS forward link message             | --       |
| 0x1037 | 0x037  | `LOG_GPS_REV_MSG_C`                | GPS reverse link message             | --       |
| 0x1038 | 0x038  | `LOG_GPS_STATS_MSG_C`              | GPS search session statistics        | --       |
| 0x1039 | 0x039  | `LOG_GPS_SRCH_PEAKS_MSG_C`         | GPS search results                   | --       |
| 0x103B | 0x03B  | `LOG_SRCH_GPS_MULTI_PEAKS_INFO_C`  | GPS multi-peak search info           | --       |
| 0x103C | 0x03C  | `LOG_SRCH_GPS_POST_PROC_C`         | GPS search post-processing           | --       |
| 0x1096 | 0x096  | `LOG_GPS_VISIT_PARAMETERS_C`       | GPS visit parameters                 | --       |
| 0x1097 | 0x097  | `LOG_GPS_MEASUREMENT_C`            | GPS measurement (legacy)             | --       |
| 0x10CA | 0x0CA  | `LOG_GPS_FATPATH_INFO_C`           | GPS fast acquisition/tracking path   | --       |
| 0x10DA | 0x0DA  | `LOG_GPS_BIT_EDGE_RESULTS_C`       | GPS bit edge detection results       | --       |
| 0x10DD | 0x0DD  | `LOG_GPS_SINGLE_PEAK_SRCH_RESULTS_C` | GPS single peak search results     | --       |
| 0x10E1 | 0x0E1  | `LOG_GPS_SCHEDULER_TRACE_C`        | GPS scheduler trace                  | --       |
| 0x117D | 0x17D  | `LOG_SRCH_GPS_MULTI_PEAKS_SIMPLIFIED_INFO_C` | GPS multipeak simplified   | --       |
| 0x117E | 0x17E  | `LOG_SRCH_GPS_MULTI_PEAKS_VERBOSE_INFO_C` | GPS multipeak verbose       | --       |

### 3.2 PDSM — Position Determination Session Manager (Legacy)

Session management and callback logs from the legacy PDSM framework.

| Hex    | Offset | Symbol                                    | Description                         | diaggpsd |
|:-------|:-------|:------------------------------------------|:------------------------------------|:---------|
| 0x11E1 | 0x1E1  | `LOG_PDSM_POSITION_REPORT_CALLBACK_C`     | PDSM position report callback       | --       |
| 0x11E2 | 0x1E2  | `LOG_PDSM_PD_EVENT_CALLBACK_C`            | PDSM PD event callback              | --       |
| 0x11E3 | 0x1E3  | `LOG_PDSM_PA_EVENT_CALLBACK_C`            | PDSM PA event callback              | --       |
| 0x11E4 | 0x1E4  | `LOG_PDSM_NOTIFY_VERIFY_REQUEST_C`        | PDSM notify/verify request          | --       |
| 0x11E5 | 0x1E5  | `LOG_PDSM_RESERVED1_C`                    | PDSM reserved 1                     | --       |
| 0x11E6 | 0x1E6  | `LOG_PDSM_RESERVED2_C`                    | PDSM reserved 2                     | --       |

### 3.3 GPS Demodulation & Tracking

Low-level GPS signal processing logs for demodulation, bit-edge
detection, and subframe decoding.

| Hex    | Offset | Symbol                                 | Description                          | diaggpsd |
|:-------|:-------|:---------------------------------------|:-------------------------------------|:---------|
| 0x1250 | 0x250  | `LOG_GPS_DEMOD_TRACKING_HEADER_C`      | GPS demod tracking header info       | --       |
| 0x1251 | 0x251  | `LOG_GPS_DEMOD_TRACKING_C`             | GPS demod tracking results           | --       |
| 0x1252 | 0x252  | `LOG_GPS_DEMOD_BIT_EDGE_C`            | GPS bit edge from demod tracking     | --       |
| 0x1253 | 0x253  | `LOG_GPS_DEMOD_SOFT_DECISIONS_C`       | GPS demod soft decisions             | --       |
| 0x1254 | 0x254  | `LOG_GPS_DEMOD_TRACKING_POST_PROC_C`   | GPS post-processed demod tracking    | --       |
| 0x1255 | 0x255  | `LOG_GPS_DEMOD_SUBFRAME_C`             | GPS subframe log                     | --       |

### 3.4 GPS Position Engine (PE) Reports

Position engine outputs, including WLS and Kalman filter solutions.

| Hex    | Offset | Symbol                                 | Description                          | diaggpsd |
|:-------|:-------|:---------------------------------------|:-------------------------------------|:---------|
| 0x12E3 | 0x2E3  | `LOG_GPS_PE_POSITION_REPORT_C`         | GPS PE position report               | --       |
| 0x12E4 | 0x2E4  | `LOG_GPS_PE_POSITION_REPORT_EXT_C`     | GPS PE position report extended      | --       |
| 0x12E6 | 0x2E6  | `LOG_GPS_DECODED_EPHEMERIS_C`          | GPS decoded ephemeris                | --       |
| 0x12E7 | 0x2E7  | `LOG_GPS_DECODED_ALMANAC_C`            | GPS decoded almanac                  | --       |
| 0x12E9 | 0x2E9  | `LOG_GPS_POSITION_ENGINE_INFO_C`       | GPS position engine info             | --       |
| 0x1359 | 0x359  | `LOG_GPS_PE_POSITION_REPORT_PART3_C`   | GPS PE position report part 3        | --       |

### 3.5 Converged GPS (CGPS) Core

Core logs from the converged GPS architecture (gpsOne Gen 7/8).

| Hex    | Offset | Symbol                                     | Description                          | diaggpsd |
|:-------|:-------|:-------------------------------------------|:-------------------------------------|:---------|
| 0x136D | 0x36D  | `LOG_GPS_SPECTRAL_INFO_C`                  | GPS spectral information             | --       |
| 0x136F | 0x36F  | `LOG_CONVERGED_GPS_CLOCK_REPORT_C`         | Converged GPS clock report           | --       |
| 0x1370 | 0x370  | `LOG_CONVERGED_GPS_POSITION_REPORT_C`      | Converged GPS position report        | --       |
| 0x1371 | 0x371  | `LOG_CONVERGED_GPS_MEASUREMENT_REPORT_C`   | Converged GPS measurement report     | --       |
| 0x1372 | 0x372  | `LOG_CONVERGED_GPS_RF_STATUS_REPORT_C`     | Converged GPS RF status report       | --       |
| 0x1374 | 0x374  | `LOG_GPS_PRESCRIBED_DWELL_RESULT_C`        | GPS prescribed dwell result          | --       |
| 0x1352 | 0x352  | `LOG_GPS_PROCESSED_PEAK_C`                 | GPS processed peak results           | --       |
| 0x138A | 0x38A  | `LOG_CONVERGED_GPS_IQ_REPORT_C`            | Converged GPS IQ report              | --       |
| 0x13C7 | 0x3C7  | `LOG_CONVERGED_GPS_MEDIUM_GRID_C`          | Converged GPS medium grid            | --       |

### 3.6 CGPS — IPC, Session Management & PDSM

Converged GPS inter-process communication, session management, and
position determination session manager logs.

| Hex    | Offset | Symbol                                          | Description                              | diaggpsd |
|:-------|:-------|:------------------------------------------------|:-----------------------------------------|:---------|
| 0x1375 | 0x375  | `LOG_CGPS_IPC_DATA_C`                           | CGPS IPC data                            | --       |
| 0x1376 | 0x376  | `LOG_CGPS_NON_IPC_DATA_C`                       | CGPS non-IPC data                        | --       |
| 0x1377 | 0x377  | `LOG_CGPS_REP_EVT_LOG_PACKET_C`                 | CGPS session report event                | --       |
| 0x1378 | 0x378  | `LOG_CGPS_PDSM_GET_POSITION_C`                  | CGPS PDSM get position                   | --       |
| 0x1379 | 0x379  | `LOG_CGPS_PDSM_SET_PARAMETERS_C`                | CGPS PDSM set parameters                 | --       |
| 0x137A | 0x37A  | `LOG_CGPS_PDSM_END_SESSION_C`                   | CGPS PDSM end session                    | --       |
| 0x137B | 0x37B  | `LOG_CGPS_PDSM_NOTIFY_VERIFY_RESP_C`            | CGPS PDSM notify/verify response         | --       |
| 0x137C | 0x37C  | `LOG_CGPS_PDSM_POSITION_REPORT_CALLBACK_C`      | CGPS PDSM position report callback       | --       |
| 0x137D | 0x37D  | `LOG_CGPS_PDSM_PD_EVENT_CALLBACK_C`             | CGPS PDSM PD event callback              | --       |
| 0x137E | 0x37E  | `LOG_CGPS_PDSM_PA_EVENT_CALLBACK_C`             | CGPS PDSM PA event callback              | --       |
| 0x137F | 0x37F  | `LOG_CGPS_PDSM_NOTIFY_VERIFY_REQUEST_C`         | CGPS PDSM notify/verify request callback | --       |
| 0x1380 | 0x380  | `LOG_CGPS_PDSM_PD_CMD_ERR_CALLBACK_C`           | CGPS PDSM PD command error callback      | --       |
| 0x1381 | 0x381  | `LOG_CGPS_PDSM_PA_CMD_ERR_CALLBACK_C`           | CGPS PDSM PA command error callback      | --       |
| 0x1382 | 0x382  | `LOG_CGPS_PDSM_POS_ERROR_C`                     | CGPS PDSM position error                 | --       |
| 0x1383 | 0x383  | `LOG_CGPS_PDSM_EXT_STATUS_POS_REPORT_C`         | CGPS PDSM ext status position report     | --       |
| 0x1384 | 0x384  | `LOG_CGPS_PDSM_EXT_STATUS_NMEA_REPORT_C`        | CGPS PDSM ext status NMEA report         | --       |
| 0x1385 | 0x385  | `LOG_CGPS_PDSM_EXT_STATUS_MEAS_REPORT_C`        | CGPS PDSM ext status measurement report  | --       |
| 0x1386 | 0x386  | `LOG_CGPS_REP_SVR_TX_LOG_PACKET_C`              | CGPS report server TX packet             | --       |
| 0x1387 | 0x387  | `LOG_CGPS_REP_SVR_RX_LOG_PACKET_C`              | CGPS report server RX packet             | --       |
| 0x1441 | 0x441  | `LOG_CGPS_PDSM_ON_DEMAND_SESSION_START_C`        | CGPS PDSM on-demand session start        | --       |
| 0x1442 | 0x442  | `LOG_CGPS_PDSM_ON_DEMAND_SESSION_STOP_C`         | CGPS PDSM on-demand session stop         | --       |
| 0x1443 | 0x443  | `LOG_CGPS_PDSM_ON_DEMAND_SESSION_NOT_STARTED_C`  | CGPS PDSM on-demand session not started  | --       |
| 0x1444 | 0x444  | `LOG_CGPS_PDSM_EXTERN_COARSE_POS_INJ_START_C`   | CGPS PDSM external coarse pos injection  | --       |
| 0x145C | 0x45C  | `LOG_CGPS_PDSM_GPS_STATE_INFO_C`                | CGPS PDSM GPS state information          | --       |
| 0x145D | 0x45D  | `LOG_CGPS_PDSM_EXT_STATUS_GPS_STATE_INFO_C`     | CGPS PDSM ext status GPS state info      | --       |
| 0x145F | 0x45F  | `LOG_CGPS_PDSM_XTRA_T_SESSION_C`                | CGPS PDSM XTRA-T session                 | --       |
| 0x1460 | 0x460  | `LOG_CGPS_PDSM_XTRA_T_UPLOAD_C`                 | CGPS PDSM XTRA-T upload                  | --       |
| 0x14D7 | 0x4D7  | `LOG_CGPS_PDSM_EXT_STATUS_POS_INJ_REQ_INFO_C`   | CGPS PDSM ext status pos inject req      | --       |

### 3.7 CGPS Measurement Engine (ME)

Low-level measurement engine logs: search jobs, time transfer, IQ data,
and RF status.

| Hex    | Offset | Symbol                                 | Description                          | diaggpsd |
|:-------|:-------|:---------------------------------------|:-------------------------------------|:---------|
| 0x13BA | 0x3BA  | `LOG_CGPS_ME_JOB_INFO_C`              | CGPS ME job info                     | --       |
| 0x13C2 | 0x3C2  | `LOG_CGPS_ME_TIME_TRANSFER_INFO_C`    | CGPS ME time-transfer info           | --       |
| 0x13C3 | 0x3C3  | `LOG_CGPS_ME_UMTS_TIME_TAGGING_INFO_C`| CGPS ME UMTS time-tagging info       | --       |
| 0x13C4 | 0x3C4  | `LOG_CGPS_ME_TIME_EST_PUT_INFO_C`     | CGPS ME generic time estimate put    | --       |
| 0x13C5 | 0x3C5  | `LOG_CGPS_ME_FREQ_EST_PUT_INFO_C`     | CGPS ME generic freq estimate put    | --       |
| 0x13C6 | 0x3C6  | `LOG_CGPS_SLOW_CLOCK_REPORT_C`        | CGPS slow clock report               | --       |
| 0x13CB | 0x3CB  | `LOG_CGPS_CELLDB_CELL_CHANGE_INFO_C`  | CGPS cell database cell change info  | --       |
| 0x13CE | 0x3CE  | `LOG_CGPS_FREQ_EST_REPORT_C`          | CGPS frequency estimate report       | --       |
| 0x13CF | 0x3CF  | `LOG_GPS_DCME_SRCH_JOB_COMPLETED_C`   | GPS DCME search job completed        | --       |
| 0x13D0 | 0x3D0  | `LOG_CGPS_ME_FASTSCAN_RESULTS_C`      | CGPS ME fast-scan results            | --       |
| 0x13DB | 0x3DB  | `LOG_CGPS_SESSION_EARLY_EXIT_DECISION_C` | CGPS session early exit decision  | --       |
| 0x13DC | 0x3DC  | `LOG_CGPS_ME_RF_LINEARITY_INFO_C`     | CGPS ME RF linearity status          | --       |
| 0x13DD | 0x3DD  | `LOG_CGPS_ME_5MS_IQ_SUMS_C`           | CGPS ME 5ms IQ sums                  | --       |
| 0x1422 | 0x422  | `LOG_CGPS_PERFORMANCE_STATS_C`        | GPS performance statistics           | --       |
| 0x1430 | 0x430  | `LOG_CGPS_MC_FREQ_BIAS_EST_C`         | CGPS MC frequency bias estimate      | --       |
| 0x1440 | 0x440  | `LOG_CGPS_ME_DPO_STATUS_C`            | CGPS ME dynamic power optimization   | --       |
| 0x1455 | 0x455  | `LOG_CGPS_DIAG_SUCCESSFUL_FIX_COUNT_C`| CGPS DIAG successful fix count       | --       |
| 0x1456 | 0x456  | `LOG_CGPS_MC_TRACK_DPO_STATUS_C`      | CGPS MC track DPO status             | --       |
| 0x145E | 0x45E  | `LOG_CGPS_ME_RAPID_SEARCH_REPORT_C`   | CGPS ME rapid search report          | --       |
| 0x148C | 0x48C  | `LOG_CGPS_SNDB_RECORD_C`             | CGPS SNDB record                     | --       |
| 0x148D | 0x48D  | `LOG_CGPS_SNDB_REMOVE_C`             | CGPS SNDB remove                     | --       |
| 0x14A6 | 0x4A6  | `LOG_CGPS_SM_EPH_RANDOMIZATION_INFO_C`| CGPS SM ephemeris randomization info | --       |

### 3.8 CGPS SBAS (WAAS/EGNOS/MSAS)

Satellite-Based Augmentation System demodulation logs.

| Hex    | Offset | Symbol                                 | Description                          | diaggpsd |
|:-------|:-------|:---------------------------------------|:-------------------------------------|:---------|
| 0x1457 | 0x457  | `LOG_CGPS_MC_SBAS_DEMOD_BITS_C`       | CGPS MC SBAS demodulated bits        | --       |
| 0x1458 | 0x458  | `LOG_CGPS_MC_SBAS_DEMOD_SOFT_SYMBOLS_C`| CGPS MC SBAS demod soft symbols     | --       |
| 0x14C7 | 0x4C7  | `LOG_GNSS_SBAS_REPORT_C`              | GNSS SBAS report                     | --       |

### 3.9 GNSS Position & Navigation (Modern)

The modern GNSS-era position and navigation log codes. These are the
primary codes used by diaggpsd and openpilot/qcomgpsd.

| Hex    | Offset | Symbol                                    | Description                           | diaggpsd    |
|:-------|:-------|:------------------------------------------|:--------------------------------------|:------------|
| 0x1476 | 0x476  | `LOG_GNSS_POSITION_REPORT_C`              | GNSS position report (main fix)       | **Parsed**  |
| 0x147C | 0x47C  | `LOG_GNSS_PE_WLS_POSITION_REPORT_C`       | GNSS PE WLS position report           | future      |
| 0x147D | 0x47D  | `LOG_GNSS_PE_KF_POSITION_REPORT_C`        | GNSS PE Kalman filter position report | future      |
| 0x1461 | 0x461  | `LOG_CGPS_WIPER_POSITION_REPORT_C`        | CGPS WiFi-based position report       | --          |

### 3.10 GNSS Measurement Reports (Modern, Per-Constellation)

Per-constellation measurement reports. These are the workhorse log codes
for raw GNSS observables.

| Hex    | Offset | Symbol                                    | Description                           | diaggpsd       |
|:-------|:-------|:------------------------------------------|:--------------------------------------|:---------------|
| 0x1477 | 0x477  | `LOG_GNSS_GPS_MEASUREMENT_REPORT_C`       | GNSS GPS L1 C/A measurement report    | **Parsed**     |
| 0x1480 | 0x480  | `LOG_GNSS_GLONASS_MEASUREMENT_REPORT_C`   | GNSS GLONASS L1 measurement report    | **Parsed**     |
| 0x1481 | 0x481  | `LOG_GNSS_GPS_HBW_RXD_MEASUREMENT_C`      | GNSS GPS HBW RxD measurement          | future         |
| 0x1756 | 0x756  | `LOG_GNSS_BDS_MEASUREMENT_REPORT_C`       | GNSS BeiDou B1 measurement report     | future   |
| 0x1886 | 0x886  | `LOG_GNSS_GAL_MEASUREMENT_REPORT_C`       | GNSS Galileo E1 measurement report    | **Partial** |

### 3.11 OEM DRE (Dead Reckoning Extended)

OEM-level raw measurement and satellite polynomial reports used for
precise positioning and dead reckoning. These carry multi-constellation
data in unified formats.

| Hex    | Offset | Symbol                                    | Description                           | diaggpsd    |
|:-------|:-------|:------------------------------------------|:--------------------------------------|:------------|
| 0x14DE | 0x4DE  | `LOG_GNSS_OEMDRE_MEASUREMENT_REPORT_C`    | OEM DRE multi-GNSS measurement report | **Parsed**  |
| 0x14E0 | 0x4E0  | `LOG_GNSS_OEMDRE_POSITION_REPORT_C`       | OEM DRE position report               | future      |
| 0x14E1 | 0x4E1  | `LOG_GNSS_OEMDRE_SVPOLY_REPORT_C`         | OEM DRE SV polynomial report          | **Parsed**  |
| 0x14E2 | 0x4E2  | `LOG_GNSS_OEMDRSYNC_C`                    | OEM DR sync report                    | future      |
| 0x188E | 0x88E  | `LOG_GNSS_OEMCPE_MEASUREMENT_REPORT_C`    | OEM CPE measurement report            | future      |

### 3.12 GNSS Clock & Time

Clock, time bias, and frequency estimation logs.

| Hex    | Offset | Symbol                                    | Description                           | diaggpsd |
|:-------|:-------|:------------------------------------------|:--------------------------------------|:---------|
| 0x1478 | 0x478  | `LOG_GNSS_CLOCK_REPORT_C`                 | GNSS clock report                     | future   |
| 0x1489 | 0x489  | `LOG_SNS_GPS_TIMESTAMP_C`                 | Sensor GPS timestamp                  | --       |
| 0x14FC | 0x4FC  | `Diag0x14FC` (canonical TBD)              | TLE per-cell time-tag entry (cell PLMN/TAC + GPS time of the tag) — decoded v8, MDM9x07/MDM9x30/SDX20. Not a receiver clock report. | future |
| 0x1567 | 0x567  | `LOG_GNSS_FAST_TCAL_C`                    | GNSS fast time calibration            | --       |

### 3.13 GNSS RF & Spectrum

RF front-end status, spectrum analysis, and jammer detection logs.

| Hex    | Offset | Symbol                                            | Description                              | diaggpsd |
|:-------|:-------|:--------------------------------------------------|:-----------------------------------------|:---------|
| 0x147E | 0x47E  | `LOG_GNSS_PRX_RF_HW_STATUS_REPORT_C`              | GNSS primary RF HW status report         | future   |
| 0x147F | 0x47F  | `LOG_GNSS_DRX_RF_HW_STATUS_REPORT_C`              | GNSS diversity RF HW status report       | future   |
| 0x147A | 0x47A  | `LOG_GNSS_ME_5MS_IQ_SUMS_C`                       | GNSS ME 5ms IQ sums                      | --       |
| 0x1487 | 0x487  | `LOG_CGPS_WB_FFT_STATS_C`                         | CGPS wideband FFT stats                  | --       |
| 0x1488 | 0x488  | `LOG_CGPS_SLOW_CLOCK_CALIB_REPORT_C`               | CGPS slow clock calibration report       | --       |
| 0x1520 | 0x520  | `LOG_GNSS_BP_AMP_INFO_C`                           | GNSS baseband processor amp info         | --       |
| 0x1526 | 0x526  | `LOG_GNSS_BROADBAND_JAMMER_INFO_C`                 | GNSS broadband jammer info               | --       |
| 0x1596 | 0x596  | `LOG_GNSS_WWAN_WLAN_IMD_JAMMER_STATUS_C`           | GNSS WWAN/WLAN IMD jammer status         | --       |
| 0x1634 | 0x634  | `LOG_GNSS_SPECTRUM_ANALYZER_SCAN_PARAMS_C`          | GNSS spectrum analyzer scan parameters   | --       |
| 0x1635 | 0x635  | `LOG_GNSS_SPECTRUM_ANALYZER_JAMMER_MEASUREMENTS_C`  | GNSS spectrum analyzer jammer meas       | --       |
| 0x1636 | 0x636  | `LOG_GNSS_SPECTRUM_ANALYZER_NOTCH_ASSIGNMENT_C`     | GNSS spectrum analyzer notch assignment  | --       |
| 0x15BB | 0x5BB  | `GNSS_SOC_RFDEV_MSGS`                               | GNSS SoC RF device messages              | --       |
| 0x15BC | 0x5BC  | `GNSS_SOC_UBP_MSGS`                                 | GNSS SoC UBP messages                    | --       |

### 3.14 GNSS Search & Acquisition

Search strategy, prescribed dwell, and signal acquisition logs.

| Hex    | Offset | Symbol                                         | Description                            | diaggpsd |
|:-------|:-------|:-----------------------------------------------|:---------------------------------------|:---------|
| 0x148A | 0x48A  | `LOG_GNSS_SEARCH_STRATEGY_TASK_ALLOCATION_C`   | GNSS search strategy task allocation   | --       |
| 0x1479 | 0x479  | `LOG_GNSS_DEMOD_SOFT_DECISIONS_C`              | GNSS demod soft decisions              | --       |
| 0x147B | 0x47B  | `LOG_GNSS_CD_DB_REPORT_C`                      | GNSS code-delay database report        | --       |

### 3.15 GNSS PDSM (Modern)

Modern GNSS-era position determination session manager logs.

| Hex    | Offset | Symbol                                            | Description                              | diaggpsd |
|:-------|:-------|:--------------------------------------------------|:-----------------------------------------|:---------|
| 0x1475 | 0x475  | `LOG_CGPS_PDSM_EXT_STATUS_MEAS_REPORT_PPM_C`     | CGPS PDSM ext status meas report PPM     | --       |
| 0x1482 | 0x482  | `LOG_GNSS_PDSM_POSITION_REPORT_CALLBACK_C`        | GNSS PDSM position report callback       | --       |
| 0x148E | 0x48E  | `LOG_GNSS_CC_PERFORMANCE_STATS_C`                  | GNSS CC performance statistics           | --       |
| 0x148F | 0x48F  | `LOG_GNSS_PDSM_SET_PARAMETERS_C`                   | GNSS PDSM set parameters                 | --       |
| 0x1490 | 0x490  | `LOG_GNSS_PDSM_PD_EVENT_CALLBACK_C`                | GNSS PDSM PD event callback              | --       |
| 0x1491 | 0x491  | `LOG_GNSS_PDSM_PA_EVENT_CALLBACK_C`                | GNSS PDSM PA event callback              | --       |
| 0x1494 | 0x494  | `LOG_GNSS_PDSM_EXT_STATUS_MEAS_REPORT_C`           | GNSS PDSM ext status measurement report  | --       |
| 0x1495 | 0x495  | `LOG_GNSS_SM_ERROR_C`                               | GNSS SM error                            | --       |
| 0x15B1 | 0x5B1  | `LOG_GNSS_PDSM_BEST_AVAIL_POS_INFO_C`              | GNSS PDSM best available position info   | --       |
| 0x15B2 | 0x5B2  | `LOG_GNSS_PDSM_EXT_STATUS_BEST_AVAIL_POS_INFO_C`   | GNSS PDSM ext status best avail pos      | --       |

### 3.16 GNSS PPM (Pilot Phase Measurement)

PPM session management for network-assisted positioning.

| Hex    | Offset | Symbol                                       | Description                          | diaggpsd |
|:-------|:-------|:---------------------------------------------|:-------------------------------------|:---------|
| 0x14E5 | 0x4E5  | `LOG_GNSS_PDSM_PPM_SESSION_BEGIN_C`          | GNSS PDSM PPM session begin          | --       |
| 0x14E6 | 0x4E6  | `LOG_GNSS_PDSM_PPM_SESSION_PPM_SUSPEND_C`   | GNSS PDSM PPM session suspend        | --       |
| 0x14E7 | 0x4E7  | `LOG_GNSS_PDSM_PPM_REPORT_THROTTLED_C`      | GNSS PDSM PPM report throttled       | --       |
| 0x14E8 | 0x4E8  | `LOG_GNSS_PDSM_PPM_REPORT_FIRED_C`          | GNSS PDSM PPM report fired           | --       |
| 0x14E9 | 0x4E9  | `LOG_GNSS_PDSM_PPM_SESSION_END_C`           | GNSS PDSM PPM session end            | --       |

### 3.17 GNSS Sensor Fusion (SLIM, INS, PE)

Sensor fusion logs for inertial navigation integration, heading
filtering, and sensor-aided positioning.

| Hex    | Offset | Symbol                                    | Description                           | diaggpsd |
|:-------|:-------|:------------------------------------------|:--------------------------------------|:---------|
| 0x150D | 0x50D  | `LOG_GNSS_SENSOR_STREAMING_READY_STATUS`  | GNSS sensor streaming ready status    | --       |
| 0x150E | 0x50E  | `LOG_GNSS_TIME_SYNC_REQ`                  | GNSS time sync request                | --       |
| 0x150F | 0x50F  | `LOG_GNSS_INJECT_TIME_SYNC_DATA`          | GNSS inject time sync data            | --       |
| 0x1510 | 0x510  | `LOG_GNSS_INJECT_SENSOR_DATA`             | GNSS inject sensor data               | --       |
| 0x1511 | 0x511  | `LOG_GNSS_GET_SENSOR_CONFIG_RESPONSE`     | GNSS get sensor config response       | --       |
| 0x1512 | 0x512  | `LOG_GNSS_SET_SENSOR_CONFIG`              | GNSS set sensor config                | --       |
| 0x1513 | 0x513  | `LOG_GNSS_PE_HEADING_FILTER`              | GNSS PE heading filter                | --       |
| 0x1514 | 0x514  | `LOG_GNSS_PE_NHC`                         | GNSS PE non-holonomic constraint      | --       |
| 0x1515 | 0x515  | `LOG_GNSS_PE_CRD`                         | GNSS PE CRD (clock rate deviation)    | --       |
| 0x1516 | 0x516  | `LOG_GNSS_CONFIGURATION_STATE_C`          | GNSS configuration state              | --       |
| 0x1519 | 0x519  | `LOG_GNSS_YAW_GYRO_CALIBRATION`           | GNSS yaw gyro calibration             | --       |
| 0x1587 | 0x587  | `LOG_GNSS_PE_EVENTS_C`                    | GNSS PE events                        | --       |
| 0x1589 | 0x589  | `LOG_GNSS_ISAGNAV_SDP_EVENTS_C`           | GNSS ISAGNAV SDP events               | --       |
| 0x1839 | 0x839  | `LOG_GNSS_SLIM_TIME_SYNC_C`               | GNSS SLIM time sync                   | --       |
| 0x1885 | 0x885  | `LOG_GNSS_SLIM_EVENT_C`                   | GNSS SLIM event                       | --       |

### 3.18 GNSS Geofencing

Geofencing engine logs for motion detection and position-based triggers.

| Hex    | Offset | Symbol                                       | Description                          | diaggpsd |
|:-------|:-------|:---------------------------------------------|:-------------------------------------|:---------|
| 0x1833 | 0x833  | `LOG_GNSS_GEOFENCE_MOTION_DETECTION_REPORT_C`| GNSS geofence motion detection       | --       |
| 0x1837 | 0x837  | `LOG_GNSS_GEOFENCE_POSITION_REPORT_C`         | GNSS geofence position report        | --       |
| 0x1883 | 0x883  | `LOG_GNSS_GEOFENCE_WWAN_MOT_CLASS_C`          | GNSS geofence WWAN motion class      | --       |
| 0x1899 | 0x899  | `LOG_GNSS_GEOFENCE_MOTION_WIFI_STATE_C`       | GNSS geofence motion WiFi state      | --       |

### 3.19 GNSS ME (Measurement Engine, Modern)

Modern measurement engine internal logs, DPO, and PQME diagnostics.

| Hex    | Offset | Symbol                                    | Description                           | diaggpsd |
|:-------|:-------|:------------------------------------------|:--------------------------------------|:---------|
| 0x1838 | 0x838  | `LOG_GNSS_ME_DPO_STATUS_C`               | GNSS ME dynamic power optimization    | --       |
| 0x163D | 0x63D  | `LOG_GNSS_ME_PQME1_C`                    | GNSS ME PQME diagnostic 1             | --       |
| 0x163E | 0x63E  | `LOG_GNSS_ME_PQME2_C`                    | GNSS ME PQME diagnostic 2             | --       |
| 0x163F | 0x63F  | `LOG_GNSS_ME_PQME3_C`                    | GNSS ME PQME diagnostic 3             | --       |

### 3.20 GNSS QUIPS & Crowd-Sourced Positioning

Indoor positioning (QUIPS) and crowd-sourced AP database logs.

| Hex    | Offset | Symbol                                    | Description                           | diaggpsd |
|:-------|:-------|:------------------------------------------|:--------------------------------------|:---------|
| 0x155D | 0x55D  | `LOG_GNSS_QUIPS_POSITION_REPORT_C`        | GNSS QUIPS position report            | --       |
| 0x155E | 0x55E  | `LOG_GNSS_QUIPS_AP_MEAS_BLOCKS_C`         | GNSS QUIPS AP measurement blocks      | --       |
| 0x156F | 0x56F  | `LOG_GNSS_QUIPS_IN_USE_AP_DATABASE_C`     | GNSS QUIPS in-use AP database         | --       |

### 3.21 GNSS GTP/TDP (Global Terrestrial Positioning)

Terrestrial-based positioning using cell tower and network measurements.

| Hex    | Offset | Symbol                                    | Description                           | diaggpsd |
|:-------|:-------|:------------------------------------------|:--------------------------------------|:---------|
| 0x18A3 | 0x8A3  | `LOG_GNSS_GTP_TDP_CONFIG_C`              | GNSS GTP TDP configuration            | --       |
| 0x18A4 | 0x8A4  | `LOG_GNSS_TLM_TDP_SCANLIST_C`            | GNSS TLM TDP scan list                | --       |

### 3.22 GNSS Miscellaneous

Other GNSS-related log codes that do not fit neatly into the above
categories.

| Hex    | Offset | Symbol                                    | Description                           | diaggpsd |
|:-------|:-------|:------------------------------------------|:--------------------------------------|:---------|
| 0x1492 | 0x492  | `LOG_CGPS_RESERVED2_C`                    | CGPS reserved 2                       | --       |
| 0x1493 | 0x493  | `LOG_CGPS_RESERVED3_C`                    | CGPS reserved 3                       | --       |
| 0x1806 | 0x806  | `LOG_GNSS_MBM_C`                          | GNSS multi-band management            | --       |
| 0x1807 | 0x807  | `LOG_GNSS_PLE_UPDATE_C`                   | GNSS PLE (position level estimate) update | --  |

---

## 4. Cross-Reference: diaggpsd Support Status

### Summary Table

| Status      | Count | Description                                                   |
|:------------|:------|:--------------------------------------------------------------|
| **Parsed**  | 5     | Full struct parser implemented, dataclass output              |
| **Partial** | 1     | Header parsed, SV struct not yet RE'd                         |
| **Subscribed** | 6  | Included in `ALL_LOG_CODES` subscription list                 |
| **Defined** | 7     | Log code constant defined in `gnss_packets.py`                |
| **Future**  | ~10   | Known useful, tracked as issues                               |
| **--**      | ~120  | Not implemented, no current plans                             |

### Detailed Status

**Parsed and subscribed** (in `ALL_LOG_CODES` and `dispatch()`):

| Log Code | Symbol                             | Parser Function                    | Dataclass               |
|:---------|:-----------------------------------|:-----------------------------------|:------------------------|
| 0x1476   | `LOG_GNSS_POSITION_REPORT_C`      | `parse_position_report()`          | `PositionReport`        |
| 0x1477   | `LOG_GNSS_GPS_MEASUREMENT_REPORT_C`| `parse_gps_measurement_report()`  | `GpsMeasurementReport`  |
| 0x1480   | `LOG_GNSS_GLONASS_MEASUREMENT_REPORT_C` | `parse_glonass_measurement_report()` | `GlonassMeasurementReport` |
| 0x14DE   | `LOG_GNSS_OEMDRE_MEASUREMENT_REPORT_C` | `parse_oemdre_measurement_report()` | `OemdreMeasurementReport` |
| 0x14E1   | `LOG_GNSS_OEMDRE_SVPOLY_REPORT_C` | `parse_oemdre_svpoly_report()`    | `OemdreSvPolyReport`    |
| 0x1886   | `LOG_GNSS_GAL_MEASUREMENT_REPORT_C`| `parse_galileo_measurement_report()` | `GalileoMeasurementReport` |

**Defined but NOT subscribed:**

| Log Code | Symbol                             | Notes                              |
|:---------|:-----------------------------------|:-----------------------------------|
| 0x1756   | `LOG_GNSS_BDS_MEASUREMENT_REPORT_C`| Constant defined, not in `ALL_LOG_CODES`, no parser |

**Priority future work** (tracked issues):

| Log Code | Symbol                                    | Tracked | Priority | Notes                                |
|:---------|:------------------------------------------|:--------|:---------|:-------------------------------------|
| 0x1756   | `LOG_GNSS_BDS_MEASUREMENT_REPORT_C`       | yes     | Medium   | BeiDou B1 measurements         |
| 0x1886   | `LOG_GNSS_GAL_MEASUREMENT_REPORT_C`       | yes     | Medium   | Galileo full SV struct RE      |
| 0x1478   | `LOG_GNSS_CLOCK_REPORT_C`                 | --      | Low      | Clock bias/drift details             |
| 0x14E0   | `LOG_GNSS_OEMDRE_POSITION_REPORT_C`       | --      | Low      | OEM DRE position solution            |
| 0x14E2   | `LOG_GNSS_OEMDRSYNC_C`                    | --      | Low      | OEM DR sync                          |
| 0x147C   | `LOG_GNSS_PE_WLS_POSITION_REPORT_C`       | --      | Low      | WLS position engine report           |
| 0x147D   | `LOG_GNSS_PE_KF_POSITION_REPORT_C`        | --      | Low      | Kalman filter position report        |
| 0x147E   | `LOG_GNSS_PRX_RF_HW_STATUS_REPORT_C`      | --      | Low      | RF front-end health monitoring       |
| 0x147F   | `LOG_GNSS_DRX_RF_HW_STATUS_REPORT_C`      | --      | Low      | Diversity RF health monitoring       |
| 0x188E   | `LOG_GNSS_OEMCPE_MEASUREMENT_REPORT_C`    | --      | Low      | OEM CPE measurements                 |

---

## 5. Citations & Sources

1. **Log code symbols** — the `LOG_*` names are the symbolic names the
   modem firmware uses for each code. Which codes a modem actually emits,
   and their payload sizes, are grounded in captured DIAG streams and in
   the modem's own log-mask responses.

2. **openpilot qcomgpsd** (comma.ai, MIT license) — Early inspiration
   for GNSS DIAG log parsing; it parses log codes 0x1476, 0x1477, 0x1480,
   0x14DE, and 0x14E1. diaggpsd incorporates none of its code: the struct
   definitions `gnss_packets.py` re-exports come from the diaggrok library,
   where they were re-derived clean-room from captured records.

3. **diaggpsd reverse engineering** — v13 PositionReport format (2477
   bytes, SDX55/SDX62) was reverse-engineered from live captures. See
   `tmp/posreport_v13_analysis.md`. Galileo measurement report (0x1886)
   partially RE'd from SDX55 indoor captures.
