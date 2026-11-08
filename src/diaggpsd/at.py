"""AT command interfaces for controlling GNSS engines on cellular modems.

Provides vendor-specific AT command adapters for starting/stopping the GNSS
engine before connecting the DIAG client:

  - QuectelATSerial  — Quectel AT+QGPS* commands (RG520N, EG25-G, RM500Q, etc.)
  - SierraATSerial   — Sierra Wireless AT!GPS* commands (EM7511, MC7455, etc.)
  - TelitATSerial    — Telit AT$GPS*/AT$GNSS* commands (FN980m, LM960, etc.)
  - ATSerial         — Legacy alias for QuectelATSerial (backward compatibility)
  - detect_vendor()  — Auto-detect vendor from ATI response

Factory function:
  - create_at_serial(vendor, port, ...) — Return the correct adapter for a vendor
"""

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class _ATSerialBase:
    """Base class providing serial transport and the cmd() method.

    Subclasses implement vendor-specific GNSS start/stop commands.
    """

    def __init__(self, port: str = '/dev/ttyUSB2', baudrate: int = 115200,
                 timeout: float = 5.0):
        from serial import Serial
        self._serial = Serial(port, baudrate=baudrate, timeout=timeout)
        self._timeout = timeout
        logger.debug(f"Opened AT port {port}")

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:
            pass

    def cmd(self, command: str, timeout: float | None = None) -> str:
        """Send an AT command and return the full response as a string.

        Waits until 'OK', 'ERROR', or '+CME ERROR' terminates the response,
        or until timeout is reached.

        Returns the raw response text (including the echoed command line if
        echo is enabled on the modem).
        """
        if timeout is None:
            timeout = self._timeout

        line = (command.strip() + '\r\n').encode()
        self._serial.reset_input_buffer()
        self._serial.write(line)
        self._serial.flush()

        deadline = time.monotonic() + timeout
        buf = b''
        while time.monotonic() < deadline:
            chunk = self._serial.read(self._serial.in_waiting or 1)
            buf += chunk
            text = buf.decode(errors='replace')
            for terminal in ('OK\r\n', 'ERROR\r\n', '+CME ERROR'):
                if terminal in text:
                    logger.debug(f"AT {command!r} → {text.strip()!r}")
                    return text
            time.sleep(0.01)

        logger.warning(f"AT {command!r} timed out after {timeout}s, partial: {buf!r}")
        return buf.decode(errors='replace')

    # --- GNSS control (abstract interface) ---

    def start_gps(self, cold_start: bool = False) -> None:
        raise NotImplementedError

    def stop_gps(self) -> None:
        raise NotImplementedError

    def inject_xtra_time(self, dt: datetime | None = None) -> None:
        """Inject XTRA/assistance time.  Default is a no-op (not all vendors need this)."""
        pass

    def query_gps_status(self) -> bool:
        raise NotImplementedError


class QuectelATSerial(_ATSerialBase):
    """AT command interface for Quectel modems (AT+QGPS* commands)."""

    def start_gps(self, cold_start: bool = False) -> None:
        """Configure and start the GNSS engine.

        Args:
            cold_start: If True, delete all GNSS assistance data before
                        starting (slower first fix, but avoids stale data).
                        If False, allow hot start using cached assistance.
        """
        # Delete assistance data
        if cold_start:
            logger.info("Cold start: deleting all GNSS assistance data")
            self.cmd("AT+QGPSDEL=0")
        else:
            self.cmd("AT+QGPSDEL=1")  # delete hot-start data only, keep XTRA

        # Disable DPO power savings for maximum accuracy
        self.cmd('AT+QGPSCFG="dpoenable",0')

        # Disable automatic GPS-on-powerup (we control it explicitly)
        self.cmd('AT+QGPSCFG="autogps",0')

        # Enable XTRA assistance
        self.enable_xtra()

        # Start GNSS engine
        resp = self.cmd("AT+QGPS=1")
        if 'ERROR' in resp and 'ALREADY' not in resp.upper():
            logger.warning(f"AT+QGPS=1 returned: {resp.strip()!r}")
        else:
            logger.info("GNSS engine started")

    def stop_gps(self) -> None:
        """Stop the GNSS engine."""
        resp = self.cmd("AT+QGPSEND")
        logger.info(f"GNSS engine stopped: {resp.strip()!r}")

    def enable_xtra(self) -> None:
        """Enable XTRA assistance data download."""
        resp = self.cmd("AT+QGPSXTRA=1")
        logger.debug(f"XTRA enable: {resp.strip()!r}")

    def inject_xtra_time(self, dt: datetime | None = None) -> None:
        """Inject the current UTC time for XTRA validity checking.

        Args:
            dt: UTC datetime to inject.  Defaults to now.
        """
        if dt is None:
            dt = datetime.now(timezone.utc)
        # Format: YYYY/MM/DD,HH:MM:SS
        time_str = dt.strftime("%Y/%m/%d,%H:%M:%S")
        # AT+QGPSXTRATIME=<mode>,<time>,<utcflag>,<confidence>,<tokentime>
        resp = self.cmd(f'AT+QGPSXTRATIME=0,"{time_str}",1,1,1000')
        logger.debug(f"XTRA time injection ({time_str}): {resp.strip()!r}")

    def query_gps_status(self) -> bool:
        """Return True if the GNSS engine is currently running."""
        resp = self.cmd("AT+QGPS?")
        return "+QGPS: 1" in resp


class SierraATSerial(_ATSerialBase):
    """AT command interface for Sierra Wireless modems (AT!GPS* commands).

    Sierra modules (EM7511, MC7455, etc.) use the AT!GPS* command family.
    Engineering mode (AT!ENTERCND) is required before issuing GPS commands.
    """

    def start_gps(self, cold_start: bool = False) -> None:
        """Enter engineering mode and start a standalone GPS fix.

        Args:
            cold_start: Ignored for Sierra — the module manages assistance
                        data internally.  Accepted for interface compatibility.
        """
        if cold_start:
            logger.debug("cold_start ignored for Sierra (module manages assistance data)")

        # Enter engineering mode (required before AT!GPS* commands)
        resp = self.cmd('AT!ENTERCND="A710"')
        if 'ERROR' in resp:
            logger.warning(f"AT!ENTERCND failed: {resp.strip()!r}")
            return
        logger.debug("Entered Sierra engineering mode")

        # Start standalone GPS fix: mode=1 (standalone), timeout=30s, accuracy=100m
        resp = self.cmd("AT!GPSFIX=1,30,100")
        if 'ERROR' in resp:
            logger.warning(f"AT!GPSFIX=1,30,100 returned: {resp.strip()!r}")
        else:
            logger.info("GNSS engine started (Sierra standalone fix)")

    def stop_gps(self) -> None:
        """Stop the GPS engine."""
        resp = self.cmd("AT!GPSEND=0")
        logger.info(f"GNSS engine stopped: {resp.strip()!r}")

    def inject_xtra_time(self, dt: datetime | None = None) -> None:
        """No-op — Sierra modules handle assistance time internally."""
        logger.debug("XTRA time injection skipped (Sierra handles this internally)")

    def query_gps_status(self) -> bool:
        """Return True if the GPS engine is currently running."""
        resp = self.cmd("AT!GPSSTATUS?")
        # AT!GPSSTATUS? returns lines like "Fix Session Status = ACTIVE" when running
        return 'ACTIVE' in resp.upper() or 'SUCCESS' in resp.upper()


class TelitATSerial(_ATSerialBase):
    """AT command interface for Telit modems (AT$GPS*/AT$GNSS* commands).

    Telit modules (FN980m, LM960, etc.) use the AT$GPS* command family.
    GNSS constellations are configured via AT$GNSSCONF (region + mode).
    """

    def start_gps(self, cold_start: bool = False) -> None:
        """Start the GNSS engine via AT$GPSP=1.

        Args:
            cold_start: If True, reset GNSS NV data before starting.
        """
        if cold_start:
            logger.info("Cold start: resetting GNSS NV data")
            self.cmd("AT$GPSNVRAM=15")

        # Set worldwide mode (all constellations enabled for this region)
        self.cmd('AT$GNSSCONF=9,0')  # 9 = GPS+GLO+BDS+GAL, 0 = worldwide

        resp = self.cmd("AT$GPSP=1")
        if 'ERROR' in resp:
            # Try GPSSLSR as alternative (standalone session)
            logger.debug(f"AT$GPSP=1 failed ({resp.strip()!r}), trying AT$GPSSLSR")
            resp = self.cmd("AT$GPSSLSR=2,3,,,,,1,255,0,0,0,1")
            if 'ERROR' in resp:
                logger.warning(f"GNSS start failed: {resp.strip()!r}")
            else:
                logger.info("GNSS engine started (via GPSSLSR)")
        else:
            logger.info("GNSS engine started")

    def stop_gps(self) -> None:
        """Stop the GNSS engine."""
        resp = self.cmd("AT$GPSSTOP")
        if 'ERROR' in resp:
            resp = self.cmd("AT$GPSP=0")
        logger.info(f"GNSS engine stopped: {resp.strip()!r}")

    def inject_xtra_time(self, dt: datetime | None = None) -> None:
        """No-op — Telit handles assistance time via SUPL/AGPS internally."""
        logger.debug("XTRA time injection skipped (Telit handles via SUPL)")

    def query_gps_status(self) -> bool:
        """Return True if the GNSS engine is currently running."""
        resp = self.cmd("AT$GPSP?")
        return "$GPSP: 1" in resp


# Backward-compatible alias — existing code imports ATSerial
ATSerial = QuectelATSerial


def detect_vendor(port: str = '/dev/ttyUSB2', baudrate: int = 115200,
                  timeout: float = 5.0) -> str:
    """Probe a serial port with ATI and return 'quectel', 'sierra', or 'unknown'.

    Opens the port, sends ATI, inspects the response, then closes.
    """
    from serial import Serial
    ser = Serial(port, baudrate=baudrate, timeout=timeout)
    try:
        ser.reset_input_buffer()
        ser.write(b'ATI\r\n')
        ser.flush()

        deadline = time.monotonic() + timeout
        buf = b''
        while time.monotonic() < deadline:
            chunk = ser.read(ser.in_waiting or 1)
            buf += chunk
            text = buf.decode(errors='replace')
            if 'OK\r\n' in text or 'ERROR\r\n' in text:
                break
            time.sleep(0.01)

        text = buf.decode(errors='replace').upper()
        if 'SIERRA' in text:
            return 'sierra'
        if 'QUECTEL' in text:
            return 'quectel'
        if 'TELIT' in text:
            return 'telit'
        return 'unknown'
    finally:
        try:
            ser.close()
        except Exception:
            pass


def create_at_serial(vendor: str, port: str = '/dev/ttyUSB2',
                     baudrate: int = 115200,
                     timeout: float = 5.0) -> _ATSerialBase:
    """Factory: return the correct AT adapter for *vendor*.

    Args:
        vendor: 'quectel', 'sierra', or 'auto' (auto-detect from ATI).
        port:   Serial port path.
        baudrate: Baud rate.
        timeout:  Command timeout in seconds.

    Raises:
        ValueError: If vendor is 'auto' and detection returns 'unknown'.
    """
    if vendor == 'auto':
        detected = detect_vendor(port, baudrate, timeout)
        logger.info(f"Auto-detected modem vendor: {detected}")
        if detected == 'unknown':
            raise ValueError(
                f"Could not auto-detect modem vendor on {port}. "
                "Use --vendor quectel or --vendor sierra."
            )
        vendor = detected

    if vendor == 'sierra':
        return SierraATSerial(port=port, baudrate=baudrate, timeout=timeout)
    if vendor == 'telit':
        return TelitATSerial(port=port, baudrate=baudrate, timeout=timeout)
    # Default to Quectel for backward compatibility
    return QuectelATSerial(port=port, baudrate=baudrate, timeout=timeout)
