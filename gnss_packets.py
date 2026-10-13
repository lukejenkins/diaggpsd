"""GNSS log packet definitions and parsers — compatibility shim.

This module re-exports everything from the diaggrok library so that
existing diaggpsd code (diaggpsd.py, gpsd_server.py, modem_profiles.py,
dump_pos_report.py) continues to work without changes.

All parsing logic now lives in the diaggrok package.
"""


# Re-export from diaggrok
from diaggrok.codes import *                       # noqa: F401,F403 — LOG_GNSS_* constants
from diaggrok.frame import parse_outer_frame       # noqa: F401
# gnss_gps.py was split into per-code modules:
from diaggrok.parsers.diag_0x1477 import *         # noqa: F401,F403
from diaggrok.parsers.diag_0x1478 import *         # noqa: F401,F403
from diaggrok.parsers.diag_0x147b import *         # noqa: F401,F403
# gnss_glonass.py was renamed to its per-code module:
from diaggrok.parsers.diag_0x1480 import *         # noqa: F401,F403
# gnss_oemdre.py was split into per-code modules in commit 76c23e902:
from diaggrok.parsers.diag_0x14de import *         # noqa: F401,F403
from diaggrok.parsers.diag_0x14e1 import *         # noqa: F401,F403
# gnss_position.py was split into per-code modules:
from diaggrok.parsers.diag_0x1476 import *         # noqa: F401,F403
from diaggrok.parsers.diag_0x14d8 import *         # noqa: F401,F403
# gnss_galileo.py was renamed to its per-code module in commit 8e9152ef5:
from diaggrok.parsers.diag_0x1886 import *         # noqa: F401,F403
import diaggrok
from diaggrok.registry import parse as dispatch    # noqa: F401



def __getattr__(name):
    """``ALL_LOG_CODES`` is computed on first use, not at import.

    Enumerating the registry runs diaggrok's full parser sweep. That cost
    should fall only on the offline tools that use the list (dlf_to_jsonl,
    dump_pos_report), not on diaggpsd, which imports this module in lazy mode
    and decodes only the codes above.
    """
    if name == "ALL_LOG_CODES":
        codes = diaggrok.registered_codes()
        globals()["ALL_LOG_CODES"] = codes
        return codes
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Backward-compat aliases for the retired descriptive class names.
# Lets diaggpsd's isinstance() checks keep working transparently.
# Note: JSON ``to_dict()['type']`` field emits the new ``Diag0x<code>``
# string regardless; type-string-based dispatchers must accept both.
OemdreMeasurementReport = Diag0x14DE                # noqa: F405
OemdreSvPolyReport = Diag0x14E1                     # noqa: F405
parse_oemdre_measurement_report = parse_0x14de      # noqa: F405
parse_oemdre_svpoly_report = parse_0x14e1           # noqa: F405
# gnss_position.py split (tier-2 batch 4):
PositionReport = Diag0x1476                         # noqa: F405
MePositionFix = Diag0x14D8                          # noqa: F405
parse_position_report = parse_0x1476                # noqa: F405
parse_me_position_fix = parse_0x14d8                # noqa: F405
# gnss_gps.py split (tier-2 batch 5). The legacy 0x1478/0x147B names are
# themselves aliases — openpilot's qcomgpsd corrected them to
# GnssClockReport / GnssCdDbReport, and the codebase carried both. The
# new Diag0xCCCC names supersede everything; the older labels are kept
# here so existing isinstance() callers keep working.
GpsMeasurementReport = Diag0x1477                   # noqa: F405
GnssClockReport = Diag0x1478                        # noqa: F405
GpsRfStatus = Diag0x1478                            # noqa: F405
GnssCdDbReport = Diag0x147B                         # noqa: F405
GpsSvHealth = Diag0x147B                            # noqa: F405
parse_gps_measurement_report = parse_0x1477         # noqa: F405
parse_gnss_clock_report = parse_0x1478              # noqa: F405
parse_gps_rf_status = parse_0x1478                  # noqa: F405
parse_gnss_cd_db_report = parse_0x147b              # noqa: F405
parse_gps_sv_health = parse_0x147b                  # noqa: F405
# gnss_glonass.py rename (tier-1 batch 5):
GlonassMeasurementReport = Diag0x1480               # noqa: F405
parse_glonass_measurement_report = parse_0x1480     # noqa: F405
# gnss_galileo.py rename (tier-1 batch 10):
GalileoMeasurementReport = Diag0x1886               # noqa: F405
parse_galileo_measurement_report = parse_0x1886     # noqa: F405
