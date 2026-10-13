#!/usr/bin/env python3
"""diag_setup — Configure a modem for GNSS DIAG logging.

Performs persistent modem state changes:
  1. Write NV items (e.g., OEM DRE feature mask)
  2. Enable OEM DRE session
  3. Start GNSS engine via AT commands (optional)

Log code subscription is NOT done here — it's per-connection state
that diaggpsd handles automatically on each startup.

Run this BEFORE diaggpsd. Only needs to be run once per modem boot
(NV items and OEM DRE session persist until reboot).

Usage:
  # Set up EG25-G for GNSS DIAG
  python3 diag_setup.py --chipset mdm9607 --diag-port /dev/ttyUSB0 --at-port /dev/ttyUSB2

  # DIAG-only setup (modem already has GNSS running)
  python3 diag_setup.py --chipset sdx62 --diag-port tcp --no-at

  # List available profiles
  python3 diag_setup.py --list-profiles

  # Show profile details and notes
  python3 diag_setup.py --chipset mdm9607 --info
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from diagmunge import DiagClient
from modem_profiles import get_profile, list_profiles

log = logging.getLogger('diag_setup')

# Default verification log code: OEM DRE measurement report. Emitted
# whenever the OEM DRE session is active AND the GNSS engine is tracking
# at least one SV, so its presence within the verify window is strong
# evidence that session-enable actually took effect.
DEFAULT_VERIFY_LOG_CODE = 0x14DE
DEFAULT_VERIFY_WINDOW_S = 10.0
DEFAULT_VERIFY_RETRY_WINDOW_S = 30.0


def parse_diag_port(spec: str) -> tuple[str, str, int]:
    """Parse --diag-port value. Returns (mode, host, port)."""
    if spec.startswith('tcp'):
        parts = spec.split(':')
        host = parts[1] if len(parts) > 1 else '0.0.0.0'
        port = int(parts[2]) if len(parts) > 2 else 2500
        return 'tcp', host, port
    if spec.startswith('udp'):
        return 'udp', '', 0
    return 'serial', spec, 0


def run_diag_setup(
    diag: DiagClient,
    profile: dict,
    *,
    verify: bool = True,
    verify_window_s: float = DEFAULT_VERIFY_WINDOW_S,
    verify_retry_window_s: float = DEFAULT_VERIFY_RETRY_WINDOW_S,
    verify_log_code: int = DEFAULT_VERIFY_LOG_CODE,
    retry_on_silent: bool = True,
    session_enable: bool = True,
) -> dict:
    """Run DIAG-side setup: NV writes, OEM DRE session, verification.

    Pass ``session_enable=False`` to skip the OEM DRE runtime session-enable
    command after NV write. The session-enable can wedge the USB stack on
    some firmwares (RM520N-GL R03A03 confirmed). The NV item
    persists across reboots, so an NV-only invocation is the safe path on
    re-runs.

    Returns a breadcrumb dict describing each step's outcome.
    """
    breadcrumb: dict = {
        "profile": profile.get('name'),
        "steps": {},
    }

    # NV items
    if profile.get('nv_items'):
        log.info("Writing OEM DRE NV item (7165=1)")
        nv_ok = diag.enable_oemdre_nv()
        breadcrumb["steps"]["oemdre_nv_write"] = {
            "acked": nv_ok,
            "status": "ok" if nv_ok else "TIMEOUT",
        }
        if not nv_ok:
            log.error(
                "OEM DRE NV write did NOT get an ACK — NV item 7165 may not be "
                "persisted; OEM DRE logs may stay silent after reboot"
            )
        if profile.get('oemdre_needs_reboot'):
            log.info("Note: NV change requires modem reboot to take effect")

    # OEM DRE session
    if profile.get('oemdre') and not session_enable:
        log.info(
            "OEM DRE session-enable SKIPPED by request (--no-session-enable). "
            "NV item is the persistent piece; runtime session-enable can wedge "
            "the USB stack on some firmwares."
        )
        breadcrumb["steps"]["oemdre_session_enable"] = {
            "acked": None,
            "status": "skipped_by_request",
        }
        breadcrumb["oemdre_session_status"] = "SKIPPED_BY_REQUEST"
    elif profile.get('oemdre'):
        log.info("Enabling OEM DRE session")
        session_ok = diag.enable_oemdre_session()
        breadcrumb["steps"]["oemdre_session_enable"] = {
            "acked": session_ok,
            "status": "ok" if session_ok else "TIMEOUT",
        }

        if verify:
            verify_result = _verify_oemdre(
                diag, verify_log_code, verify_window_s, attempt=1,
            )
            breadcrumb["steps"]["oemdre_session_verify"] = verify_result

            if not verify_result["verified"] and retry_on_silent:
                log.warning(
                    f"OEM DRE log 0x{verify_log_code:04X} not seen in "
                    f"{verify_window_s:.1f}s — retrying session enable with "
                    f"longer window ({verify_retry_window_s:.1f}s)"
                )
                retry_session_ok = diag.enable_oemdre_session()
                retry_verify = _verify_oemdre(
                    diag, verify_log_code, verify_retry_window_s, attempt=2,
                )
                breadcrumb["steps"]["oemdre_session_retry"] = {
                    "acked": retry_session_ok,
                    "verify": retry_verify,
                }
                verify_result = retry_verify

            if not verify_result["verified"]:
                log.error(
                    f"OEM DRE session UNVERIFIED — log code 0x{verify_log_code:04X} "
                    f"never emitted. Capture may be missing 21 gated DIAG log "
                    f"codes across GNSS+LTE. Likely causes: "
                    f"(a) firmware regression, (b) GNSS engine not tracking, "
                    f"(c) LTE not connected, (d) modem does not support OEM DRE."
                )
                breadcrumb["oemdre_session_status"] = "UNVERIFIED"
            else:
                log.info(
                    f"OEM DRE session VERIFIED — log code 0x{verify_log_code:04X} "
                    f"observed ({verify_result['frames_seen']} total frames in "
                    f"{verify_result['duration_s']:.2f}s)"
                )
                breadcrumb["oemdre_session_status"] = "VERIFIED"
        else:
            breadcrumb["oemdre_session_status"] = "NOT_VERIFIED_BY_REQUEST"

    return breadcrumb


def _verify_oemdre(
    diag: DiagClient, log_code: int, window_s: float, attempt: int,
) -> dict:
    """Subscribe to `log_code` and drain frames for `window_s` seconds.

    Returns a dict suitable for breadcrumb emission.
    """
    log.info(
        f"Verifying OEM DRE active (attempt {attempt}): subscribing to "
        f"log code 0x{log_code:04X} for {window_s:.1f}s"
    )
    # Narrow subscription to just the verification code. diaggpsd will
    # re-subscribe to the full ALL_LOG_CODES set on its next startup, so
    # this is a no-op from its perspective.
    diag.subscribe_logs([log_code])
    drain = diag.wait_for_log_codes([log_code], timeout=window_s)
    verified = log_code in drain["observed_codes"]
    return {
        "verified": verified,
        "log_code": f"0x{log_code:04X}",
        "window_s": window_s,
        "attempt": attempt,
        "frames_seen": drain["frames_seen"],
        "duration_s": drain["duration_s"],
        "observed_codes": [f"0x{c:04X}" for c in drain["observed_codes"]],
    }


def run_at_setup(at_port: str, profile: dict, cold_start: bool = False) -> None:
    """Run AT-side setup: start GNSS engine, inject XTRA time."""
    vendor = profile.get('at_vendor')
    if not vendor:
        log.warning("No AT vendor defined for this chipset profile — skipping AT setup")
        log.warning("Start GNSS manually with the correct vendor AT commands")
        return

    from at import create_at_serial
    at = create_at_serial(vendor=vendor, port=at_port)
    log.info(f"AT port {at_port}: starting GPS (vendor={vendor})")
    at.start_gps(cold_start=cold_start)
    at.inject_xtra_time(datetime.now(timezone.utc))
    at.close()
    log.info("AT setup complete")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Configure modem DIAG subsystem for GNSS logging')
    parser.add_argument('--diag-port', default='/dev/ttyUSB0',
                        help='DIAG port (serial path or tcp[:host:port])')
    parser.add_argument('--at-port', default='/dev/ttyUSB2',
                        help='AT command port')
    parser.add_argument('--chipset',
                        help='Chipset or modem name (e.g., mdm9607, eg25-g, sdx62)')
    parser.add_argument('--no-at', action='store_true',
                        help='Skip AT command setup')
    parser.add_argument('--cold-start', action='store_true',
                        help='Delete GNSS assistance data before starting')
    parser.add_argument('--list-profiles', action='store_true',
                        help='List available chipset profiles')
    parser.add_argument('--info', action='store_true',
                        help='Show profile details and notes')
    parser.add_argument('--no-verify', action='store_true',
                        help='Skip OEM DRE session-enable verification')
    parser.add_argument('--no-session-enable', action='store_true',
                        help='Skip OEM DRE runtime session-enable after NV write. '
                             'Use when the session-enable wedges the USB stack '
                             '(e.g. RM520N-GL R03A03). NV write still '
                             'happens; the NV item persists across reboots.')
    parser.add_argument('--verify-window', type=float,
                        default=DEFAULT_VERIFY_WINDOW_S,
                        help='Seconds to wait for OEM DRE log emission (default: 10)')
    parser.add_argument('--verify-retry-window', type=float,
                        default=DEFAULT_VERIFY_RETRY_WINDOW_S,
                        help='Seconds to wait on verify retry (default: 30)')
    parser.add_argument('--verify-log-code', type=lambda s: int(s, 0),
                        default=DEFAULT_VERIFY_LOG_CODE,
                        help='Log code used for verification (default: 0x14DE, OEM DRE meas)')
    parser.add_argument('--no-verify-retry', action='store_true',
                        help='Do not retry OEM DRE enable if first verify window is silent')
    parser.add_argument('--breadcrumb', type=Path, default=None,
                        help='Write setup breadcrumb JSON to this path')
    parser.add_argument('--debug', action='store_true')
    return parser


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    if args.list_profiles:
        print("Available chipset profiles:\n")
        print(list_profiles())
        return

    if not args.chipset:
        parser.error("--chipset is required (use --list-profiles to see options)")

    profile = get_profile(args.chipset)
    if not profile:
        print(f"Unknown chipset: {args.chipset}", file=sys.stderr)
        print(f"\nAvailable profiles:\n{list_profiles()}", file=sys.stderr)
        sys.exit(1)

    if args.info:
        print(f"Profile: {profile['name']}")
        print(f"Modems: {', '.join(profile['modems'])}")
        print(f"NV items: {profile.get('nv_items', {})}")
        print(f"OEM DRE: {profile.get('oemdre', False)}")
        print(f"AT vendor: {profile.get('at_vendor') or 'none (use --no-at)'}")
        print(f"\nNotes:")
        for note in profile.get('notes', []):
            print(f"  - {note}")
        return

    # Print warnings from profile
    for note in profile.get('notes', []):
        if note.startswith('WARNING'):
            log.warning(note)

    # DIAG setup
    mode, host, port = parse_diag_port(args.diag_port)
    if mode == 'udp':
        log.error("UDP mode is receive-only — cannot send DIAG setup commands over UDP")
        log.error("Use serial or TCP mode for setup, then switch to UDP for consumption")
        sys.exit(1)
    elif mode == 'tcp':
        log.info(f"TCP mode: waiting for connection on {host}:{port}")
        diag = DiagClient.from_tcp_server(host=host, port=port)
    else:
        log.info(f"Serial mode: {args.diag_port}")
        diag = DiagClient.from_serial(args.diag_port)

    breadcrumb = {
        "tool": "diag_setup",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "chipset": args.chipset,
        "diag_port": args.diag_port,
        "at_port": args.at_port if not args.no_at else None,
    }
    try:
        diag_breadcrumb = run_diag_setup(
            diag, profile,
            verify=not args.no_verify,
            verify_window_s=args.verify_window,
            verify_retry_window_s=args.verify_retry_window,
            verify_log_code=args.verify_log_code,
            retry_on_silent=not args.no_verify_retry,
            session_enable=not args.no_session_enable,
        )
        breadcrumb["diag"] = diag_breadcrumb
        log.info("DIAG setup complete")
    finally:
        diag.close()

    # AT setup
    if not args.no_at:
        run_at_setup(args.at_port, profile, cold_start=args.cold_start)
        breadcrumb["at"] = {"ran": True, "cold_start": args.cold_start}
    else:
        log.info("AT setup skipped (--no-at)")
        breadcrumb["at"] = {"ran": False}

    breadcrumb["finished_utc"] = datetime.now(timezone.utc).isoformat()

    if args.breadcrumb:
        args.breadcrumb.parent.mkdir(parents=True, exist_ok=True)
        args.breadcrumb.write_text(json.dumps(breadcrumb, indent=2) + "\n")
        log.info(f"Breadcrumb written to {args.breadcrumb}")

    session_status = breadcrumb.get("diag", {}).get("oemdre_session_status")
    if session_status == "UNVERIFIED":
        log.error(
            "Modem configured but OEM DRE session UNVERIFIED — expect silent "
            "log codes. Run diaggpsd anyway or retry diag_setup "
            "with --verify-window 30."
        )
        sys.exit(2)
    log.info("Modem configured — run diaggpsd to start consuming DIAG packets")


if __name__ == '__main__':
    main()
