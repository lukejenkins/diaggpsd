"""Regression test for ``apps/diaggpsd/gnss_packets.py`` import surface.

Guards the compatibility-shim layer against silent breakage when a
diaggrok parser module is renamed or split (e.g. the gnss_oemdre →
diag_0x14de + diag_0x14e1 split in commit 76c23e902).

Failure mode this catches: a from-import of a module that no longer
exists in ``diaggrok.parsers`` raises ``ModuleNotFoundError`` at import
time, taking down every scan / post-capture script that imports the
shim. Without this test the breakage only surfaces at field
deployment. (Originally introduced and fixed in commit 05880c1cf.)

The diaggrok library itself has its own per-parser tests — those rename
in lockstep with the parser module, so they cannot detect a stale
re-export in the diaggpsd shim. This test specifically exercises the
shim's import surface.
"""
from __future__ import annotations


def test_gnss_packets_imports_without_error():
    """The shim must import. Any ModuleNotFoundError here means a
    diaggrok parser was renamed/split without updating gnss_packets.py."""
    import gnss_packets  # noqa: F401


def test_gnss_packets_registers_expected_parsers():
    """ALL_LOG_CODES must be populated and contain the specific codes
    most recently affected by the parser-module renames.

    Spot-checks rather than enumerating every code: any future split
    will break the import itself (caught by the test above), and
    enumerating here would force test churn on every harmless rename.
    """
    import gnss_packets as g

    assert g.ALL_LOG_CODES, (
        "gnss_packets must register at least one parser — "
        "an empty ALL_LOG_CODES likely means the diaggrok registry "
        "auto-discovery failed."
    )

    # 0x14DE / 0x14E1: gnss_oemdre split in commit 76c23e902.
    assert 0x14DE in g.ALL_LOG_CODES, "diag_0x14de OemdreMeasurementReport missing"
    assert 0x14E1 in g.ALL_LOG_CODES, "diag_0x14e1 OemdreSvPolyReport missing"

    # 0x1476 / 0x14D8: gnss_position split.
    assert 0x1476 in g.ALL_LOG_CODES, "diag_0x1476 PositionReport missing"
    assert 0x14D8 in g.ALL_LOG_CODES, "diag_0x14d8 MePositionFix missing"

    # 0x1477 / 0x1478 / 0x147B: gnss_gps split.
    assert 0x1477 in g.ALL_LOG_CODES, "diag_0x1477 GpsMeasurementReport missing"
    assert 0x1478 in g.ALL_LOG_CODES, "diag_0x1478 GnssClockReport missing"
    assert 0x147B in g.ALL_LOG_CODES, "diag_0x147b GnssCdDbReport missing"


def test_gnss_packets_backcompat_aliases_resolve():
    """The retired legacy descriptive-name aliases must still
    resolve to the new Diag0xCCCC classes. Existing diaggpsd code uses
    these in isinstance() checks; the shim's whole purpose is to keep
    those callers working transparently across the rename campaign."""
    import gnss_packets as g

    # Each aliased class must equal the new Diag0xCCCC symbol.
    assert g.OemdreMeasurementReport is g.Diag0x14DE
    assert g.OemdreSvPolyReport is g.Diag0x14E1
    assert g.PositionReport is g.Diag0x1476
    assert g.MePositionFix is g.Diag0x14D8
    assert g.GpsMeasurementReport is g.Diag0x1477
    assert g.GnssClockReport is g.Diag0x1478
    assert g.GnssCdDbReport is g.Diag0x147B

    # And the paired parse_* function aliases.
    assert g.parse_oemdre_measurement_report is g.parse_0x14de
    assert g.parse_oemdre_svpoly_report is g.parse_0x14e1
    assert g.parse_position_report is g.parse_0x1476
    assert g.parse_gps_measurement_report is g.parse_0x1477
