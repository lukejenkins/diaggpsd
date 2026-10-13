#!/usr/bin/env python3
"""Diagnostic: dump raw PositionReport (0x1476) payloads from the modem.

Subscribes to GNSS log codes, captures the first N PositionReport packets,
and hex-dumps the log_payload bytes.

Also scans for IEEE 754 doubles at every 2-byte boundary that fall in
plausible lat/lon ranges, to identify where the coordinates live in the
version 24 struct.

Usage:
    # USB serial (direct connection to modem DIAG port)
    python dump_pos_report.py --diag-port /dev/serial/by-id/…-if00-port0

    # TCP from on-modem diag_socket_log (on modem: diag_socket_log -a <host_ip> -p 2500)
    python dump_pos_report.py --diag-port tcp:0.0.0.0:2500 --count 5
"""

import argparse
import struct
import sys
import os

# Allow importing diag.py from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from diagmunge import DiagClient, DIAG_LOG_F
from diag_setup import parse_diag_port
from gnss_packets import ALL_LOG_CODES, parse_outer_frame, LOG_GNSS_POSITION_REPORT


def hexdump(data: bytes, indent: str = '  ') -> None:
    """Print a hex + ASCII dump of data."""
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        asc_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        print(f"{indent}{i:04x}  {hex_part:<48}  {asc_part}")


def scan_doubles(data: bytes) -> list[tuple[int, float]]:
    """Return list of (offset, value) for doubles at every 2-byte boundary."""
    results = []
    for off in range(0, len(data) - 7, 2):
        val, = struct.unpack_from('<d', data, off)
        results.append((off, val))
    return results


def find_latlon_candidates(data: bytes) -> None:
    """Print offsets where doubles look like plausible lat or lon in radians."""
    print("  Scanning for lat/lon candidates (doubles at 2-byte boundary offsets):")
    found = False
    for off, val in scan_doubles(data):
        is_lat = 0.0 < val < 1.58   # 0° to ~90° N in radians
        is_lon = -3.15 < val < 3.15  # ±180° in radians (broad)
        if is_lat or is_lon:
            lat_deg = val * 180.0 / 3.14159265358979
            marker = ''
            if is_lat and 0.1 < val < 1.57:
                marker = f'  ← possible LAT ({lat_deg:.4f}°)'
            elif is_lon:
                marker = f'  ← possible LON ({lat_deg:.4f}°)'
            print(f"    offset {off:3d} (0x{off:02x}): {val:+.9f}{marker}")
            found = True
    if not found:
        print("    (none found)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--diag-port', default='tcp:0.0.0.0:2500',
                    help='DIAG port (serial path or tcp[:host:port]; default: tcp:0.0.0.0:2500)')
    ap.add_argument('--count', type=int, default=3,
                    help='Number of PositionReport packets to capture (default: 3)')
    args = ap.parse_args()

    mode, host, port = parse_diag_port(args.diag_port)
    if mode == 'tcp':
        print(f"Waiting for diag_socket_log on {host}:{port} ...")
        diag = DiagClient.from_tcp_server(host=host, port=port)
    elif mode == 'udp':
        print("Error: UDP mode is receive-only and cannot subscribe to log codes.", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Opening DIAG serial port {args.diag_port} ...")
        diag = DiagClient.from_serial(args.diag_port)
    print("Connected. Subscribing to GNSS log codes ...")
    diag.subscribe_logs(ALL_LOG_CODES)
    print(f"Subscribed. Waiting for {args.count} PositionReport (0x1476) packets ...\n")

    captured = 0
    while captured < args.count:
        try:
            opcode, payload = diag.recv()
        except EOFError:
            print("DIAG transport closed.")
            break

        if opcode != DIAG_LOG_F:
            continue

        try:
            pending_msgs, log_type, log_time, log_payload = parse_outer_frame(payload)
        except ValueError:
            continue

        if log_type != LOG_GNSS_POSITION_REPORT:
            continue

        captured += 1
        version = log_payload[0] if log_payload else -1
        print(f"=== PositionReport #{captured} ===")
        print(f"  log_time   : {log_time}")
        print(f"  log_payload: {len(log_payload)} bytes")
        print(f"  version    : {version} (0x{version:02x})")
        print()

        # Show the first 96 bytes of header region in detail
        hdr = log_payload[:min(96, len(log_payload))]
        print("  Hex dump (first 96 bytes):")
        hexdump(hdr)
        print()

        # Show full dump if packet is small enough
        if len(log_payload) <= 512:
            print("  Full hex dump:")
            hexdump(log_payload)
            print()

        # Scan for lat/lon-like doubles
        find_latlon_candidates(log_payload[:160])
        print()

        # Also show what the current _POS_FMT interprets at the lat/lon offsets
        # (offset 40 = two doubles lat/lon from the reference struct)
        if len(log_payload) >= 56:
            lat_ref, = struct.unpack_from('<d', log_payload, 40)
            lon_ref, = struct.unpack_from('<d', log_payload, 48)
            print(f"  Reference offset 40→lat: {lat_ref:.6e}  ({lat_ref * 180/3.14159:.4f}°)")
            print(f"  Reference offset 48→lon: {lon_ref:.6e}  ({lon_ref * 180/3.14159:.4f}°)")
        print()

    diag.close()
    print("Done.")


if __name__ == '__main__':
    main()
