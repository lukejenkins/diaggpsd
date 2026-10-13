# diaggpsd

Live GNSS from a Qualcomm modem's **DIAG** stream — served to gpsd clients and
written to RINEX. diaggpsd subscribes to a Qualcomm modem's GNSS DIAG log
records, decodes them, and makes the fixes and raw measurements available three
ways:

- **gpsd clients** — a gpsd-compatible JSON server on TCP 2947, so `gpspipe`,
  `cgps`, `xgps`, `gpsmon`, JOSM, and anything built on `libgps` just work.
- **Raw JSON** — one packet per line on stdout or a plain TCP port, for logging
  and custom tooling without the gpsd protocol overhead.
- **RINEX** — observation/navigation files for post-processing pipelines.

diaggpsd *feeds* gpsd clients by speaking the gpsd protocol; it is not the gpsd
daemon and does not replace it. It reads the modem directly over DIAG, so it
works where no kernel GPS driver or NMEA port is exposed.

## Layout

diaggpsd is a small collection of scripts installed side by side:

| Module | Role |
|--------|------|
| `diaggpsd.py` | the live daemon — argument parsing, DIAG subscription, main loop, gpsd/raw output |
| `gnss_packets.py` | thin re-export shim over the `diaggrok` GNSS parsers |
| `at.py` | AT-command interface for GNSS engine setup (Quectel-oriented) |
| `gpsd_server.py` | the gpsd JSON protocol server (TCP 2947) |
| `diag_setup.py` | DIAG log-mask setup helper |
| `dlf_to_jsonl.py` | offline: decode a DIAG capture to JSONL |
| `dump_pos_report.py` | offline: dump position reports from a live/captured stream |
| `gnss_compare.py` / `gnss_compare_ref_filter.py` | compare decoded fixes against a reference track |
| `rinex_writer.py` | RINEX observation/navigation output |
| `modem_profiles.py` | per-chipset NV/AT setup profiles |

## Dependencies

diaggpsd is first-party Python. At install time it pulls two sibling libraries
that hold the reverse-engineered pieces:

- **[diaggrok](https://github.com/lukejenkins/diaggrok)** — the DIAG GNSS
  log-record parsers (imported through `gnss_packets.py`).
- **[diagmunge](https://github.com/lukejenkins/diagmunge)** — the DIAG transport
  core (`DiagClient`) and replay.

The live AT-port path additionally needs **pyserial**, installed via the
optional `serial` extra. DIAG capture, replay, decode, RINEX, and the gpsd
server are all pyserial-free.

```bash
pip install .            # daemon + gpsd server + offline tooling
pip install '.[serial]'  # + the live AT-port path (pyserial)
```

Requires Python 3.11+.

## Quick start

Two steps: `diaggpsd-diag-setup` prepares the modem once (NV items, the GNSS
session, AT commands), then `diaggpsd` streams. `diaggpsd` itself never sends AT
commands, so the AT options (`--at-port`, `--no-at`, `--cold-start`) belong to
the setup step.

### USB-connected modem

```bash
# One-time setup: enable GNSS logging and start the engine over AT.
diaggpsd-diag-setup --chipset sdx55 --diag-port /dev/ttyUSB0 --at-port /dev/ttyUSB2

# Stream to gpsd clients.
diaggpsd --diag-port /dev/ttyUSB0 --debug

# In another terminal:
gpspipe -w 127.0.0.1
```

`diaggpsd-diag-setup --list-profiles` lists the chipset profiles. Add
`--cold-start` to delete cached assistance, or `--no-at` when the GNSS engine is
already running or the AT path is unavailable. On QuecOpen builds the host-side
AT port is bridged to an SMD device node by an AP-side service; if that bridge
is stopped, AT commands can fail silently, so `--no-at` is the robust choice
there.

### Network-attached modem

A modem that relays its DIAG stream over the network is read without a serial
port. In the two passive modes the device sets the log mask and `diaggpsd`
never writes one.

```bash
# The device pushes a DGE1-framed UDP stream (diagbarf):
diaggpsd --diag-port udp-listen:0.0.0.0:12399 --debug

# The device runs diag_socket_log and connects to this host:
diaggpsd --diag-port tcp --debug
#   on the modem: diag_socket_log -a <host_ip> -p 2500
```

## Transport modes

| Mode | Invocation | Who sets the log mask |
|------|------------|-----------------------|
| USB / serial (default) | `--diag-port /dev/ttyUSB0` | diaggpsd |
| TCP `diag_socket_log` | `--diag-port tcp` or `tcp:0.0.0.0:2500` | diaggpsd |
| UDP, DGE1 framing (diagbarf) | `--diag-port udp-listen` or `udp-listen:0.0.0.0:12399` | the device |
| UDP, QMDL2 chunks (`diag_udp_fwd`) | `--diag-port udp` or `udp:0.0.0.0:2500` | the device |

When diaggpsd sets the log mask, it clears the mask again on exit with one
LOG_CONFIG DISABLE, draining queued frames for up to 10 s while it waits for
the ack. That also happens when the subscribe itself fails, for example when
a slow `diag_socket_log` tunnel backs up until a mask ack never arrives (exit
status 1). If no ack comes, the log says so; power-cycle the modem
(`AT+CFUN=1,1`) and send a zero log mask before the next run.

## Output modes

- **stdout JSON** (default) — one parsed packet per line
  (`PositionReport`, `GpsMeasurementReport`, `GlonassMeasurementReport`,
  `OemdreMeasurementReport`, `OemdreSvPolyReport`). Suppress with `--no-stdout`.
- **Raw TCP** — `--raw-port 5000` broadcasts the JSON lines to connected TCP
  clients.
- **gpsd server** (TCP 2947) — the standard gpsd JSON protocol. Disable with
  `--no-gpsd`, move with `--gpsd-port PORT`.

## Offline decoding: input formats for `dlf_to_jsonl`

How a DIAG capture was produced determines how to decode it. Two on-disk formats
both commonly carry the `.dlf` extension but are **not** interchangeable:

| Source | On-disk format | Invocation |
|--------|----------------|------------|
| QCSuper (`--dlf`) | DLF records (per-record length/type header) | `dlf_to_jsonl capture.dlf out.jsonl` |
| raw HDLC dump | 0x7E-delimited HDLC byte stream (no DLF envelope) | `dlf_to_jsonl --format bin --verify-crc capture.dlf out.jsonl` |

Using DLF mode on a raw-HDLC file yields a near-zero parse rate (the 0x7E
delimiters do not form a valid DLF record header). DLF mode on QCSuper output is
correct and needs no `--verify-crc` (QCSuper validates CRCs at capture time).

## GPS lock notes

After start, the modem needs time to acquire satellites: typically 10–30 s
outdoors with fresh XTRA assistance, 1–10 min on a cold start with clear sky.
The first `PositionReport` with `pos_source=2` (Kalman fix) marks a usable
position; the gpsd server begins pushing TPV at that point.

## gpsd protocol subset

`VERSION` (on connect), `WATCH` (subscribe/unsubscribe), `TPV` (from
`PositionReport`), `SKY` (from GPS/GLONASS measurement reports), and minimal
`DEVICES` / `POLL` stubs for client compatibility. `TPV.mode`: 1 = no fix,
2 = 2D, 3 = 3D (Kalman).

## Acknowledgements

Reverse-engineering of Qualcomm DIAG GNSS log records was pioneered by projects
including openpilot's qcomgpsd, QCSuper, and SCAT. diaggpsd relies on struct
definitions re-derived clean-room in the `diaggrok` library and stands on the
documentation those projects contributed to the community.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
