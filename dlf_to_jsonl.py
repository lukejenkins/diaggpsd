#!/usr/bin/env python3
"""dlf_to_jsonl — offline replay a DLF or raw HDLC capture through diaggrok.

Supports two input formats:

  **DLF** (default for .dlf files): QCSuper-style record-per-frame files
  with the layout:
      2B  rec_len (total incl header)
      2B  log_code
      8B  timestamp (64-bit, 1.25 ms / tick since GPS epoch)
      rec_len-12  payload

  **Raw HDLC** (.bin files from diaggulp.py): 0x7E-delimited HDLC frames
  with CRC-16 CCITT.  When reading raw input, the HDLC decode pipeline from
  hdlc_to_dlf.py is applied inline so you can go straight from raw capture
  to parsed JSONL in one command.

Usage:
    python3 dlf_to_jsonl.py capture.dlf output.jsonl
    python3 dlf_to_jsonl.py --format bin --verify-crc raw.bin output.jsonl
    python3 dlf_to_jsonl.py - output.jsonl          # read DLF from stdin
    python3 dlf_to_jsonl.py capture.dlf -            # write JSONL to stdout
    diaggulp.py /dev/ttyUSB2 | python3 dlf_to_jsonl.py --format bin --verify-crc - -

The --verify-crc flag only applies to raw HDLC input and validates
CRC-16/CCITT on each frame before parsing.  Frames with bad CRC are
skipped with a warning count in the summary.
"""

import argparse
import sys
from pathlib import Path

# gnss_packets / rinex_writer are diaggpsd-app-local — keep this app dir on path.
# diaggrok / diagmunge resolve via -libs.pth (no source-tree insert needed).
_app_dir = Path(__file__).parent
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

from diagmunge.munge.replay import (  # noqa: E402  # GNSS-free replay core
    UnsupportedFormatError,
    detect_format as _detect_format,
    iter_dlf_records,
    iter_hdlc_records,
    iter_hdlc_records_stream,
    read_chunks,
    replay_to_jsonl,
)
from gnss_packets import ALL_LOG_CODES, dispatch  # noqa: E402
from rinex_writer import (  # noqa: E402
    Epoch,
    RinexHeaderConfig,
    RinexObsWriter,
    group_records_by_epoch,
)

# Back-compat aliases: existing tests reference dlf_to_jsonl._read_chunks.
_read_chunks = read_chunks


# Readers (iter_dlf_records / iter_hdlc_records[_stream] / read_chunks) and the
# "dlf"/"bin" detect_format label mapper are the GNSS-free replay core, imported
# above from diagmunge.munge.replay. This module wires GNSS specifics
# (ALL_LOG_CODES, dispatch, RINEX) into that core.


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dlf_to_jsonl.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input",
        help='Input file: .dlf, .bin, or "-" for stdin',
    )
    parser.add_argument(
        "output",
        help='Output JSONL file or "-" for stdout',
    )
    parser.add_argument(
        "--format",
        choices=["dlf", "bin"],
        default=None,
        help="Input format (default: auto-detect from extension)",
    )
    parser.add_argument(
        "--verify-crc",
        action="store_true",
        help="Validate CRC-16 on raw HDLC frames; skip bad frames (requires --format bin or .bin input)",
    )
    parser.add_argument(
        "--rinex-prefix",
        default=None,
        help="Also write a RINEX 3.04 observation file to PREFIX.obs",
    )
    parser.add_argument(
        "--rinex-marker-name",
        default="DIAGGPSD",
        help="RINEX MARKER NAME header value (default: DIAGGPSD)",
    )
    parser.add_argument(
        "--rinex-receiver",
        default="UNKNOWN",
        help="RINEX REC # / TYPE / VERS receiver-type value (e.g. 'LM960' or 'FN980m')",
    )
    parser.add_argument(
        "--rinex-antenna",
        default="UNKNOWN",
        help="RINEX ANT # / TYPE antenna-type value",
    )
    args = parser.parse_args()

    # Determine input format
    fmt = args.format
    if fmt is None:
        if args.input == "-":
            fmt = "dlf"  # default for stdin
        elif args.input.endswith(".bin"):
            fmt = "bin"
        else:
            fmt = None  # auto-detect after reading data

    if args.verify_crc and fmt != "bin":
        print(
            "warning: --verify-crc only applies to raw HDLC (.bin) input; ignored for DLF",
            file=sys.stderr,
        )

    # Step 2: when the format is known to be raw HDLC *before* reading,
    # decode incrementally from a chunk stream — JSONL is emitted as each
    # frame's terminating 0x7E arrives and memory is bounded to a single
    # in-flight frame, instead of buffering the whole capture then parsing.
    # This is the true low-latency live-decode / FIFO-fan-out path. DLF and
    # auto-detect still read-all: DLF records aren't 0x7E-framed (no frame
    # boundary to stream on), and auto-detect must peek the header first.
    streaming = fmt == "bin"
    if streaming:
        try:
            chunk_source = _read_chunks(args.input)
        except FileNotFoundError:
            print(f"input file not found: {args.input}", file=sys.stderr)
            return 1
        except IsADirectoryError:
            print(f"input is a directory, not a file: {args.input}", file=sys.stderr)
            return 1
        records = iter_hdlc_records_stream(chunk_source, verify_crc=args.verify_crc)
    else:
        # Read input (stdin DLF, .dlf file, or a path needing auto-detect).
        if args.input == "-":
            data = sys.stdin.buffer.read()
        else:
            in_path = Path(args.input)
            # Step 1: gate on exists(), not is_file(), so FIFOs / char
            # devices are accepted; read_bytes() drains to EOF (read-all).
            if not in_path.exists():
                print(f"input file not found: {in_path}", file=sys.stderr)
                return 1
            if in_path.is_dir():
                print(f"input is a directory, not a file: {in_path}", file=sys.stderr)
                return 1
            data = in_path.read_bytes()

        # Auto-detect format if not specified
        if fmt is None:
            fmt = _detect_format(data)
            print(f"auto-detected format: {fmt}", file=sys.stderr)

        # Select record iterator
        if fmt == "bin":
            records = iter_hdlc_records(data, verify_crc=args.verify_crc)
        else:
            records = iter_dlf_records(data)

    gnss_codes = set(ALL_LOG_CODES)

    # Hold parsed dicts for optional RINEX pass (--rinex-prefix)
    rinex_records: list[dict] = []
    on_parsed = rinex_records.append if args.rinex_prefix is not None else None

    # Open output
    if args.output == "-":
        out = sys.stdout
    else:
        out = open(args.output, 'w')

    # Replay through the GNSS-free core, injecting the GNSS dispatch + code set.
    # streaming=True flushes each record so a live JSONL tail / FIFO reader
    # sees it immediately instead of at the OS buffer boundary.
    try:
        stats = replay_to_jsonl(
            records,
            dispatch=dispatch,
            code_filter=gnss_codes,
            out=out,
            streaming=streaming,
            on_parsed=on_parsed,
        )
    finally:
        if args.output != "-":
            out.close()

    if args.rinex_prefix is not None:
        cfg = RinexHeaderConfig(
            marker_name=args.rinex_marker_name,
            receiver_type=args.rinex_receiver,
            antenna_type=args.rinex_antenna,
            program="dlf_to_jsonl",
        )
        epochs = group_records_by_epoch(rinex_records)
        rinex_path = f"{args.rinex_prefix}.obs"
        with open(rinex_path, 'w') as rfh:
            writer = RinexObsWriter(rfh, config=cfg)
            writer.write_header(epochs[0] if epochs else Epoch(0, 0))
            written = sum(writer.write_epoch(ep) for ep in epochs)
        stats['rinex_epochs'] = written
        stats['rinex_path'] = rinex_path

    for k, v in stats.items():
        print(f'{k}: {v}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
