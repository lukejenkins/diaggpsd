#!/usr/bin/env python3
"""diaggpsd — DIAG GNSS Daemon

Reads raw GNSS log packets from SDX-series modems via the DIAG
protocol and provides:
  • JSON output on stdout (or --raw-port TCP)
  • gpsd-compatible JSON on TCP port 2947 (TPV + SKY)

Run diag_setup.py first to configure the modem (NV items, OEM DRE, AT
commands). diaggpsd handles log subscription and packet consumption.

Transport modes:
  --diag-port /dev/ttyUSB0        USB serial (default)
  --diag-port tcp                 Listen for diag_socket_log on 0.0.0.0:2500
  --diag-port tcp:192.168.1.1:2500  Custom host:port for diag_socket_log
  --diag-port udp                 Receive UDP broadcast DIAG on 0.0.0.0:2500
  --diag-port udp:0.0.0.0:2500   Custom bind for UDP broadcast
  --diag-port udp-listen          Receive a DGE1-framed UDP stream (diagbarf) on 0.0.0.0:12399
  --diag-port udp-listen:0.0.0.0:12509  Custom bind for the DGE1 stream
  --diag-port broker              Subscribe to a diagbroker on 127.0.0.1:2700
  --diag-port broker:HOST:PORT    Custom diagbroker address

Quick start:
  python3 diag_setup.py --chipset mdm9607 --diag-port /dev/ttyUSB0 --at-port /dev/ttyUSB2
  python3 diaggpsd.py --debug
"""

import argparse
import json
import logging
import select
import signal
import socket
import sys
import threading
import time
from struct import pack, unpack_from
import os
# Lazy diaggrok: gnss_packets imports the nine parser modules diaggpsd
# decodes, so the ~1,000-module sweep (about 1.1 s) is skipped. A lookup for any
# other code still triggers the full sweep. The variable is set only for this
# import, so child processes do not inherit it.
_lazy_set = "DIAGGROK_LAZY_PARSERS" not in os.environ
if _lazy_set:
    os.environ["DIAGGROK_LAZY_PARSERS"] = "1"
import gnss_packets
if _lazy_set:
    del os.environ["DIAGGROK_LAZY_PARSERS"]
from diagmunge import DiagClient, DIAG_BAD_CMD_F, DIAG_LOG_CONFIG_F
from diagmunge.framefilter import MULTI_RADIO_WRAPPER_OFFSET, FrameFilter
from diagmunge.transport import BrokerError
from diagmunge import maskstate
from gnss_packets import (
    PositionReport, GpsMeasurementReport,
    GlonassMeasurementReport, GalileoMeasurementReport,
    OemdreMeasurementReport, OemdreSvPolyReport,
    parse_outer_frame, dispatch,
)


def _json_default(o):
    """Hex-encode raw byte fields (``body_raw``, ``pem``, ...) instead of crashing."""
    if isinstance(o, (bytes, bytearray, memoryview)):
        return bytes(o).hex()
    raise TypeError(f'Object of type {o.__class__.__name__} is not JSON serializable')


def encode_packet(pkt_dict: dict) -> str:
    """Serialise one parsed packet for stdout / --record / --raw-port."""
    return json.dumps(pkt_dict, default=_json_default)


def parse_diag_port(spec: str) -> tuple[str, str, int]:
    """Parse --diag-port value.

    Returns (mode, host, port) where mode is 'serial', 'tcp', 'udp',
    'udp-listen' or 'broker'.
    """
    if spec.startswith('broker'):
        parts = spec.split(':')
        host = parts[1] if len(parts) > 1 else '127.0.0.1'
        port = int(parts[2]) if len(parts) > 2 else 2700
        return 'broker', host, port
    if spec.startswith('udp-listen'):
        parts = spec.split(':')
        host = parts[1] if len(parts) > 1 else '0.0.0.0'
        port = int(parts[2]) if len(parts) > 2 else 12399
        return 'udp-listen', host, port
    if spec.startswith('tcp'):
        parts = spec.split(':')
        host = parts[1] if len(parts) > 1 else '0.0.0.0'
        port = int(parts[2]) if len(parts) > 2 else 2500
        return 'tcp', host, port
    if spec.startswith('udp'):
        parts = spec.split(':')
        host = parts[1] if len(parts) > 1 else '0.0.0.0'
        port = int(parts[2]) if len(parts) > 2 else 2500
        return 'udp', host, port
    return 'serial', spec, 0


# Shared streams: the device or a fan-out (diagbarf) owns the log mask, so the
# default --mask for these is 'none'. Everything else defaults to 'own'. 'tcp'
# is a diag_socket_log link that dials in to diaggpsd alone, so it stays 'own'.
PASSIVE_MODES = frozenset({'udp', 'udp-listen', 'broker'})

# --- Log codes per output ---
# Arm, and decode, only what the enabled outputs consume. Subscribing to every
# registered diaggrok code (~1,060) and testing each packet against that set
# was most of diaggpsd's 37 % of a core on a max-mask stream.
TPV_CODES = frozenset({0x1476})                          # PositionReport
SKY_CODES = frozenset({0x1477, 0x1480, 0x14DE})          # gpsd SKY sources
RINEX_CODES = frozenset({0x1476, 0x1477, 0x1480, 0x1886, 0x14DE})
# The JSON stream (stdout / --record / --raw-port) carries every GNSS report
# diaggpsd decodes: the above plus clock, SV health, ME fix and SV poly.
JSON_CODES = TPV_CODES | SKY_CODES | RINEX_CODES | frozenset(
    {0x1478, 0x147B, 0x14D8, 0x14E1})

# A shared stream whose owner did not arm a required code gets one warning
# after this long (spec §5), then diaggpsd keeps running.
MISSING_CODE_WARN_S = 30.0


def required_codes(*, gpsd: bool, json_out: bool, rinex: bool) -> frozenset[int]:
    """Log codes the enabled outputs consume.

    The gpsd server needs TPV and SKY; the JSON stream needs every GNSS report;
    RINEX needs position and the measurement reports. Always includes 0x1476:
    with every output off diaggpsd still tracks position.
    """
    codes = set(TPV_CODES)
    if gpsd:
        codes |= TPV_CODES | SKY_CODES
    if json_out:
        codes |= JSON_CODES
    if rinex:
        codes |= RINEX_CODES
    return frozenset(codes)


def broker_declaration(codes) -> dict:
    """The diagbroker SUB declaration: exactly the codes the enabled
    outputs consume, so the broker arms (and forwards) nothing more."""
    return {"name": "diaggpsd", "log_codes": sorted(codes)}


def default_mask_mode(mode: str) -> str:
    """'none' for shared streams, 'own' for a port diaggpsd reads alone."""
    return 'none' if mode in PASSIVE_MODES else 'own'


def log_frames(diag, ff: FrameFilter):
    """Yield ``payload`` (bytes after the 0x10 opcode) of each wanted LOG frame.

    Reads the raw stream and lets ``ff`` reject unwanted frames from their
    first bytes, so only wanted frames pay unescape + CRC. A 0x98 multi-radio
    wrapper's inner LOG frame is unwrapped. Raises EOFError at end of stream.
    """
    while True:
        chunk = diag.read_raw(timeout=1.0)
        if not chunk:
            continue
        for m in ff.feed(chunk):
            frame = m.frame
            if m.peek.inner_opcode is not None:
                frame = frame[MULTI_RADIO_WRAPPER_OFFSET[frame[0]]:]
            yield frame[1:]

# LOG_CONFIG operation 0 clears every log group in one command.
LOG_CFG_DISABLE = 0
DISARM_TIMEOUT_S = 10.0
DISARM_RECOVERY_HINT = (
    "the modem may still have log masks armed. If the next DIAG client "
    "cannot subscribe, power-cycle the modem (AT+CFUN=1,1) and then send a "
    "zero log mask.")


def _disable_reply_status(opcode: int, body: bytes) -> int | None:
    """Status of a LOG_CONFIG DISABLE reply; None if the frame is not one."""
    if opcode != DIAG_LOG_CONFIG_F or len(body) < 11:
        return None
    operation, status = unpack_from('<3xII', body)
    return status if operation == LOG_CFG_DISABLE else None


def disarm_logs(diag, log: logging.Logger,
                timeout: float = DISARM_TIMEOUT_S) -> bool:
    """Clear every log mask on the modem, waiting at most ``timeout`` seconds.

    A failed subscribe can leave the masks armed with the stream backed up
    behind them. Sending a zero mask per group then waits behind the
    backlog for each ack. DISABLE is one command: once the modem acts on it,
    no new logs are queued, and the log frames read here while waiting for
    the ack drain the backlog. When the modem rejects DISABLE, fall back to
    zero masks. Returns True when the modem confirmed the masks are clear.
    """
    try:
        diag.send(DIAG_LOG_CONFIG_F, pack('<3xI', LOG_CFG_DISABLE))
    except OSError as exc:
        log.warning(f"Could not send LOG_CONFIG DISABLE ({exc}); {DISARM_RECOVERY_HINT}")
        return False
    deadline = time.monotonic() + timeout
    drained = 0
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            opcode, body = diag.recv(timeout=remaining)
        except (TimeoutError, EOFError, OSError):
            break
        if opcode == DIAG_BAD_CMD_F and body[:1] == bytes([DIAG_LOG_CONFIG_F]):
            log.info("Modem rejected LOG_CONFIG DISABLE; clearing masks per group")
            try:
                diag.subscribe_logs([])
                return True
            except Exception as exc:
                log.warning(f"Zero-mask disarm failed ({exc}); {DISARM_RECOVERY_HINT}")
                return False
        status = _disable_reply_status(opcode, body)
        if status is None:
            drained += 1
            continue
        if status == 0:
            log.info(f"Log masks cleared (drained {drained} queued frame(s))")
            return True
        log.warning(f"LOG_CONFIG DISABLE returned status {status}; {DISARM_RECOVERY_HINT}")
        return False
    log.warning(f"No LOG_CONFIG DISABLE ack within {timeout:.0f} s "
                f"(drained {drained} frame(s)); {DISARM_RECOVERY_HINT}")
    return False


def open_diag(mode: str, host: str, port: int, serial_path: str,
              codes=frozenset()) -> DiagClient:
    """Open the DIAG transport selected by parse_diag_port()."""
    log = logging.getLogger('diaggpsd')
    if mode == 'broker':
        log.info(f"diagbroker mode: subscribing to {host}:{port}")
        return DiagClient.from_tcp_client(host, port, broker_sub=broker_declaration(codes))
    if mode == 'udp-listen':
        log.info(f"DGE1 UDP listen mode: listening on {host}:{port}")
        return DiagClient.from_udp_listen(host=host, port=port)
    if mode == 'udp':
        log.info(f"UDP broadcast mode: listening on {host}:{port}")
        return DiagClient.from_udp_broadcast(host=host, port=port)
    if mode == 'tcp':
        log.info(f"TCP mode: waiting for diag_socket_log on {host}:{port}")
        return DiagClient.from_tcp_server(host=host, port=port)
    log.info(f"Serial mode: opening {serial_path}")
    return DiagClient.from_serial(port=serial_path)


class RawServer:
    """Simple TCP server that broadcasts JSON lines to all connected clients."""

    def __init__(self, host: str = '0.0.0.0', port: int = 0):
        self._host = host
        self._port = port
        self._server_sock: socket.socket | None = None
        self._clients: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> None:
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self._host, self._port))
        self._server_sock.listen(8)
        self._server_sock.settimeout(1.0)
        self._running = True
        t = threading.Thread(target=self._accept_loop, daemon=True, name='raw-accept')
        t.start()
        actual = self._server_sock.getsockname()
        logging.getLogger(__name__).info(f"Raw JSON server on {actual[0]}:{actual[1]}")

    def stop(self) -> None:
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
        with self._lock:
            for sock in list(self._clients):
                try:
                    sock.close()
                except Exception:
                    pass
            self._clients.clear()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _ = self._server_sock.accept()
                with self._lock:
                    self._clients.add(conn)
            except socket.timeout:
                continue
            except OSError:
                break

    def broadcast(self, line: str) -> None:
        data = (line.strip() + '\n').encode()
        dead = []
        with self._lock:
            for sock in list(self._clients):
                try:
                    sock.sendall(data)
                except OSError:
                    dead.append(sock)
        if dead:
            with self._lock:
                for sock in dead:
                    self._clients.discard(sock)
                    try:
                        sock.close()
                    except Exception:
                        pass


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        '--diag-port', default='/dev/ttyUSB0', metavar='PATH_OR_tcp',
        help='DIAG transport: serial path, tcp[:host[:port]], udp[:host[:port]] or '
             'udp-listen[:host[:port]] (default: /dev/ttyUSB0)',
    )
    p.add_argument(
        '--gpsd-port', type=int, default=2947, metavar='PORT',
        help='gpsd-compatible server port (default: 2947)',
    )
    p.add_argument(
        '--no-gpsd', action='store_true',
        help='Disable the gpsd server',
    )
    p.add_argument(
        '--raw-port', type=int, default=0, metavar='PORT',
        help='Enable JSON-over-TCP raw output on this port (0 = disabled)',
    )
    p.add_argument(
        '--no-stdout', action='store_true',
        help='Suppress JSON output on stdout',
    )
    p.add_argument(
        '--record', metavar='PATH',
        help='Record all parsed packets to a JSONL file (appends)',
    )
    # --- RINEX live-mode output (Phase 2) ---
    p.add_argument(
        '--rinex-out', metavar='PATH',
        help='Also write a RINEX 3.04 observation file to PATH live as packets stream',
    )
    p.add_argument(
        '--rinex-marker-name', default='DIAGGPSD', metavar='NAME',
        help='RINEX MARKER NAME header value (default: DIAGGPSD)',
    )
    p.add_argument(
        '--rinex-receiver', default='', metavar='TYPE',
        help="RINEX REC # / TYPE / VERS receiver-type value (e.g. 'LM960')",
    )
    p.add_argument(
        '--rinex-antenna', default='', metavar='TYPE',
        help='RINEX ANT # / TYPE antenna-type value',
    )
    p.add_argument(
        '--mask', choices=('own', 'none'), default=None,
        help="Log mask ownership. own = arm only the codes the enabled "
             "outputs need and clear them on exit. none = never write a mask; "
             "another process owns it. Default: none for udp / udp-listen, "
             "own otherwise.",
    )
    p.add_argument(
        '--nmea-out', action='append', default=[], metavar='TARGET',
        help="Basic NMEA (GGA, RMC, GSA) from 0x1476, goal tier 1. "
             "TARGET: '-' (stdout; turns JSON stdout off), file:PATH, "
             "tcp:[HOST:]PORT (gpsd tcp://), pty:LINK (gpsd -n LINK). Repeatable. "
             "Altitude is HAE with geoid separation 0.0 (see nmea.py).",
    )
    p.add_argument(
        '--nmea-talker', default='GN', metavar='XX',
        help='NMEA talker ID (default: GN)',
    )
    p.add_argument(
        '--debug', action='store_true',
        help='Enable DEBUG logging',
    )
    return p


def main() -> None:
    args = build_arg_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    log = logging.getLogger('diaggpsd')

    # --- Resources to clean up on exit ---
    diag = None
    saved_masks = None   # F3 + event masks to put back on exit
    gpsd_server = None
    raw_server = None
    record_file = None
    rinex_file = None
    rinex_writer = None

    if args.record:
        record_file = open(args.record, 'a')
        log.info(f"Recording to {args.record}")

    if args.rinex_out:
        from rinex_writer import LiveRinexWriter, RinexHeaderConfig
        rinex_file = open(args.rinex_out, 'w')
        rinex_writer = LiveRinexWriter(
            rinex_file,
            config=RinexHeaderConfig(
                marker_name=args.rinex_marker_name,
                receiver_type=args.rinex_receiver,
                antenna_type=args.rinex_antenna,
            ),
        )
        log.info(f"RINEX 3.04 obs output: {args.rinex_out}")

    nmea_sinks = []
    if '-' in args.nmea_out and not args.no_stdout:
        log.info("--nmea-out -: NMEA on stdout, so JSON stdout is off")
        args.no_stdout = True
    if len(args.nmea_talker) != 2 or not args.nmea_talker.isalpha():
        log.error(f"--nmea-talker must be two letters, not {args.nmea_talker!r}")
        sys.exit(2)
    for spec in args.nmea_out:
        import nmea
        nmea_sinks.append(nmea.open_sink(spec))
        log.info(f"NMEA output: {spec}")

    mode, net_host, net_port = parse_diag_port(args.diag_port)
    mask_mode = args.mask or default_mask_mode(mode)
    if mode == 'broker' and mask_mode == 'own':
        log.error("--mask own with --diag-port broker: the broker owns the mask; "
                  "diaggpsd declares its codes in the SUB line instead")
        sys.exit(2)
    codes = required_codes(gpsd=not args.no_gpsd,
                           json_out=not args.no_stdout or bool(args.record)
                           or bool(args.raw_port),
                           rinex=bool(args.rinex_out))

    def shutdown(signum=None, frame=None, exit_code: int = 0) -> None:
        log.info("Shutting down ...")
        if diag is not None:
            if mask_mode == 'own':
                disarm_logs(diag, log)
                # After the log disarm, so the F3 / event restore readbacks do
                # not queue behind a GNSS log stream.
                if saved_masks is not None:
                    try:
                        maskstate.restore(diag, saved_masks, who="diaggpsd")
                    except Exception as exc:   # shutdown must finish
                        log.warning(f"F3/event mask restore failed ({exc}); "
                                    "the modem keeps the zeroed masks")
            diag.close()
        if gpsd_server is not None:
            gpsd_server.stop()
        if raw_server is not None:
            raw_server.stop()
        if record_file is not None:
            record_file.close()
        if rinex_writer is not None:
            rinex_writer.close()
            log.info(f"RINEX epochs written: {rinex_writer.epochs_written}")
        if rinex_file is not None:
            rinex_file.close()
        for sink in nmea_sinks:
            sink.close()
        sys.exit(exit_code)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # --- Start optional servers ---
    if not args.no_gpsd:
        from gpsd_server import GpsdServer
        gpsd_server = GpsdServer(port=args.gpsd_port)
        gpsd_server.start()

    if args.raw_port:
        raw_server = RawServer(port=args.raw_port)
        raw_server.start()

    # --- DIAG transport ---
    try:
        diag = open_diag(mode, net_host, net_port, args.diag_port, codes)
    except BrokerError as exc:
        log.error(f"diagbroker {net_host}:{net_port}: {exc}")
        shutdown(exit_code=2)

    # --- DIAG configuration (only when diaggpsd owns the mask) ---
    code_list = ', '.join(f'0x{c:04X}' for c in sorted(codes))
    if mask_mode == 'own':
        # Quiet F3 and events that an earlier tool left armed; both are put back
        # on exit. A mask that cannot be read is left alone.
        saved_masks = maskstate.save_and_zero(diag, who="diaggpsd")
        log.info(f"Subscribing to {len(codes)} GNSS log code(s): {code_list}")
        try:
            diag.subscribe_logs(sorted(codes))
        except (RuntimeError, AssertionError) as exc:
            # Groups acked before the failure stay armed; disarm before exit.
            log.error(f"Log subscription failed: {exc}")
            shutdown(exit_code=1)
    else:
        log.info(f"--mask none: not writing a log mask; using {code_list} "
                 "from whatever the stream owner armed")

    log.info("Listening for GNSS packets ...")
    # Non-LOG opcodes and unwanted log codes never leave the filter, so the
    # loop below sees only frames it will decode.
    ff = FrameFilter(log_codes=codes, verify_crc=not diag.no_crc)
    seen: set[int] = set()
    warn_at = time.monotonic() + MISSING_CODE_WARN_S if mask_mode == 'none' else None
    frames = log_frames(diag, ff)
    # Tier 1 (NMEA only) must not pay for JSON it never emits.
    need_json = (not args.no_stdout or record_file is not None
                 or raw_server is not None or rinex_writer is not None)
    if nmea_sinks:
        from nmea import EpochGate, from_position as nmea_from_position
        nmea_gate = EpochGate()

    # --- Main loop ---
    while True:
        try:
            payload = next(frames)
        except EOFError:
            log.error("DIAG transport closed")
            shutdown()
            return  # unreachable

        # Parse outer frame (lenient in UDP mode — QMDL2 framing has slight length mismatches)
        try:
            pending_msgs, log_type, log_time, log_payload = parse_outer_frame(
                payload, lenient=(mode == 'udp'))
        except ValueError as exc:
            log.debug(f"Malformed outer frame: {exc}")
            continue

        if pending_msgs > 0:
            log.debug(f"{pending_msgs} pending messages in modem queue")

        if warn_at is not None:
            seen.add(log_type)
            if time.monotonic() >= warn_at:
                warn_at = None
                missing = sorted(codes - seen)
                if missing:
                    log.warning(
                        "No frames of %s in %.0f s: the stream owner's mask "
                        "may not include them, or this modem does not emit "
                        "them", ', '.join(
                            f'0x{c:04X}' for c in missing), MISSING_CODE_WARN_S)

        # Dispatch to correct parser
        try:
            packet = dispatch(log_type, log_time, log_payload)
        except (ValueError, Exception) as exc:
            log.debug(f"Parse error for log_type=0x{log_type:04x}: {exc}")
            continue

        if packet is None:
            continue

        # --- Output routing ---
        if (nmea_sinks and isinstance(packet, PositionReport)
                and nmea_gate.accept(packet, time.monotonic())):
            text = ''.join(nmea_from_position(packet, args.nmea_talker))
            for sink in nmea_sinks:
                sink.write(text)

        if gpsd_server is not None:
            if isinstance(packet, PositionReport):
                gpsd_server.push_position(packet)
            elif isinstance(packet, (GpsMeasurementReport, GlonassMeasurementReport)):
                gpsd_server.push_sky(packet)
            elif isinstance(packet, OemdreMeasurementReport) and packet.svs:
                gpsd_server.push_sky(packet)

        if not need_json:
            continue
        pkt_dict = packet.to_dict()
        try:
            json_line = encode_packet(pkt_dict)
        except (TypeError, ValueError) as exc:
            # One unserialisable packet must not end the capture.
            log.debug(f"Unserialisable packet log_type=0x{log_type:04x}: {exc}")
            continue

        if not args.no_stdout:
            print(json_line, flush=True)

        if record_file is not None:
            record_file.write(json_line + '\n')
            record_file.flush()

        if raw_server is not None:
            raw_server.broadcast(json_line)

        if rinex_writer is not None:
            rinex_writer.feed(pkt_dict)


if __name__ == '__main__':
    main()
