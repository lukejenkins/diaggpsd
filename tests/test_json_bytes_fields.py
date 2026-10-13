"""Regression test: diaggpsd crashed with ``TypeError: Object of
type bytes is not JSON serializable`` on the first packet whose ``to_dict()``
carried a raw ``bytes`` field. Hit live on a WNC MDM9650 and on an
RM520N-GL-AP over /dev/mhi_DIAG, where 15 registered parsers
(0x1022, 0x1838, 0x1D15, ...) emit ``body_raw`` / ``pem`` / ``signature``.
"""
from __future__ import annotations

import json

import pytest

import diaggpsd


def test_bytes_fields_are_hex_encoded():
    line = diaggpsd.encode_packet(
        {"log_code": 0x1D15, "pem": b"\x01\xab", "signature": bytearray(b"\xff"),
         "body_raw": memoryview(b"\x00\x10")})
    assert json.loads(line) == {"log_code": 0x1D15, "pem": "01ab",
                                "signature": "ff", "body_raw": "0010"}


def test_nested_bytes_are_hex_encoded():
    line = diaggpsd.encode_packet({"svs": [{"raw": b"\xde\xad"}]})
    assert json.loads(line) == {"svs": [{"raw": "dead"}]}


def test_other_unserialisable_types_still_raise():
    # The daemon loop catches this and skips the packet; the encoder itself
    # must not silently stringify arbitrary objects.
    with pytest.raises(TypeError):
        diaggpsd.encode_packet({"x": object()})


def test_live_parsers_emitting_bytes_now_serialise():
    """Feed a real registered parser that returns body_raw bytes."""
    import gnss_packets
    pkt = gnss_packets.dispatch(0x1022, 0, bytes(68))
    if pkt is None:
        pytest.skip("0x1022 parser rejected the synthetic payload")
    d = pkt.to_dict()
    assert any(isinstance(v, (bytes, bytearray)) for v in d.values()), \
        "positive control: this parser no longer emits bytes; pick another code"
    json.loads(diaggpsd.encode_packet(d))
