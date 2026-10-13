"""Input-gate regression tests for ``dlf_to_jsonl.main``.

The reader used to gate input on ``Path.is_file()``, which is False for a
FIFO — so the documented named-pipe fan-out (one ``diaggulp`` feeding a
``dlf_to_jsonl`` consumer through a named pipe) was rejected with
``input file not found`` even though the FIFO existed. The fix widened the
gate to ``Path.exists()`` so FIFOs / char devices are accepted, while still
rejecting a genuinely-missing path and a directory with distinct messages.

These tests lock in:

1. A FIFO is accepted and parsed (rc=0), producing output identical to the
   stdin (``-``) path fed the same bytes — i.e. the FIFO is a true
   read-all-to-EOF source, not a special case.
2. A missing path still returns 1 with ``input file not found``.
3. A directory returns 1 with the ``is a directory`` message (previously it
   fell into the same ``is_file()``-False bucket as a missing file).
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dlf_to_jsonl  # noqa: E402

# A tiny, well-formed raw-HDLC capture: two 0x7E-delimited frames. The exact
# log payloads don't matter — the test asserts FIFO/stdin parse-equivalence
# and the gate's accept/reject decisions, not record semantics. Bytes are an
# empty-body frame plus a short one so iter_hdlc_records has something to walk.
_HDLC_CAPTURE = bytes.fromhex("7e10001000eeff7e") + bytes.fromhex("7e7300abcd1234ffff7e")


def _run_main(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["dlf_to_jsonl.py", *argv])
    return dlf_to_jsonl.main()


def test_fifo_accepted_and_matches_regular_file(tmp_path, monkeypatch):
    import os

    cap = tmp_path / "cap.bin"
    cap.write_bytes(_HDLC_CAPTURE)

    # Baseline: a plain regular file (the path that always worked).
    file_out = tmp_path / "file.jsonl"
    rc_file = _run_main(monkeypatch, ["--format", "bin", str(cap), str(file_out)])
    assert rc_file == 0

    # FIFO: a writer thread pushes the same bytes through a named pipe while
    # main() reads it. Before the fix this returned 1 ("input file not found")
    # because is_file() is False for a FIFO.
    fifo = tmp_path / "diag.fifo"
    os.mkfifo(fifo)
    writer = threading.Thread(
        target=lambda: fifo.write_bytes(_HDLC_CAPTURE), daemon=True
    )
    writer.start()
    fifo_out = tmp_path / "fifo.jsonl"
    rc_fifo = _run_main(monkeypatch, ["--format", "bin", str(fifo), str(fifo_out)])
    writer.join(timeout=5)
    assert rc_fifo == 0

    # A FIFO is a true read-all-to-EOF source: identical output to the file.
    assert fifo_out.read_bytes() == file_out.read_bytes()


def test_missing_path_still_rejected(tmp_path, monkeypatch, capsys):
    rc = _run_main(
        monkeypatch, ["--format", "bin", str(tmp_path / "nope.bin"), str(tmp_path / "o.jsonl")]
    )
    assert rc == 1
    assert "input file not found" in capsys.readouterr().err


def test_directory_rejected_with_distinct_message(tmp_path, monkeypatch, capsys):
    rc = _run_main(
        monkeypatch, ["--format", "bin", str(tmp_path), str(tmp_path / "o.jsonl")]
    )
    assert rc == 1
    assert "is a directory" in capsys.readouterr().err
