"""Per-chipset GNSS DIAG setup profiles.

Each profile defines what a chipset needs for GNSS DIAG to work:
- nv_items: NV items to write (id → value) — persistent modem state
- oemdre: whether to send the OEM DRE session command
- at_vendor: which AT adapter to use ('quectel', 'sierra', None)
- notes: human-readable setup notes and caveats

Log code subscription is NOT here — it's per-connection state handled by diaggpsd.
"""

from gnss_packets import ALL_LOG_CODES


PROFILES = {
    'mdm9607': {
        'name': 'MDM9607',
        'modems': ['EG25-G', 'EC25-AF', 'EC25-AU', 'EC25-E'],
        'nv_items': {7165: 1},
        'oemdre': True,
        'oemdre_needs_reboot': True,
        'at_vendor': 'quectel',
        'notes': [
            'NV 7165 requires full modem reboot (AT+CFUN=1,1) to take effect',
            'OEM DRE v2-hybrid format: v2 header with v1 SV records (1839B)',
            'gnssconfig is an ENUM: value 1 = GPS+GLONASS+Galileo all ON; '
            'value 7 = GLONASS OFF / Galileo OFF (per EG25 enum). Galileo IS '
            'supported on MDM9607 (GAGSV NMEA + 0x1886/0x14DE Galileo records, '
            'OEM-DRE grounded 0.89 dB vs LG290P) — use gnssconfig=1',
            'BeiDou: EG25-G A0.301 never ACQUIRES BeiDou under any gnssconfig '
            '(searched but 0 SVs confirmed) despite the enum '
            'nominally enabling BDS',
            'Check carrier profile — some MBN configs restrict to GPS-only',
        ],
    },
    'sdx55': {
        'name': 'SDX55',
        'modems': ['RM500Q-AE', 'FN980m', 'EM9190'],
        'nv_items': {7165: 1},
        'oemdre': True,
        'oemdre_needs_reboot': True,
        'at_vendor': None,  # varies by modem make — use --no-at and configure manually
        'notes': [
            'WARNING: Some SDX55 firmware builds compile out GNSS DIAG handlers entirely',
            'Binary test: scan modem.b* for 0x1477 (77 14 LE) — if zero hits, no GNSS DIAG',
            'AT vendor varies: Quectel=AT+QGPS, Telit=AT$GPSP, Sierra=AT!GPS*',
            'Use --no-at and start GNSS manually with the correct vendor AT commands',
            'OEM DRE v2 full format: 2783B (95B header + 24×112B SV records)',
        ],
    },
    'sdx62': {
        'name': 'SDX62',
        'modems': ['RG520N-NA', 'CFW-3212'],
        'nv_items': {7165: 1},
        'oemdre': True,
        'oemdre_needs_reboot': True,
        'at_vendor': None,  # CFW-3212: GNSS runs autonomously, no AT start needed
        'notes': [
            'OEM DRE session command times out on SDX62 — may need different enablement',
            'PositionReport uses v24 format (1575+B, 104 floats)',
            'DOP values of 500.0 are sentinels (not computed)',
            'CFW-3212: use --no-at — GNSS runs autonomously, AT path unreliable on OpenCPU',
        ],
    },
    'mdm9x30': {
        'name': 'MDM9x30',
        'modems': ['MC7455', 'EM7565', 'MC7411'],
        'nv_items': {7165: 1},
        'oemdre': True,
        'oemdre_needs_reboot': True,
        'at_vendor': 'sierra',
        'notes': [
            'OEM DRE support on MDM9x30 unverified on a live modem WITH this profile applied — '
            'the reference MC7455 (SWI9X30C_02.24.03.00) GNSS-compare capture used '
            '--skip-diag-setup and observed 0 × 0x14DE across 705,846 GNSS DIAG records.',
            'Native GNSS DIAG path works regardless: 0x1476 PositionReport / 0x1477 '
            'GpsMeasurementReport / 0x1480 GlonassMeasurementReport flowed at 1 Hz with '
            'zero parse errors in that capture.',
            'Closure checklist: a future live MC7455/EM7565 run WITH this profile '
            'applied is the only way to confirm whether NV-7165 ACKs on MDM9x30 and whether '
            '0x14DE flows after reboot. If NV-7165 fails to ACK, flip oemdre to False here.',
            'AT vendor: sierra (MC7455 / EM7565 / MC7411 are all Sierra Wireless modems).',
            'Most Sierra modems require AT!ENTERCND="A710" before AT!GPS* commands respond — '
            'handled by the sierra AT adapter.',
        ],
    },
    'sdx20': {
        'name': 'SDX20 (MDM9x50)',
        'modems': ['LM960', 'EM7511', 'T77W968', 'EG12-GT', 'EM120R-GL'],
        'nv_items': {7165: 1},
        'oemdre': True,
        'oemdre_needs_reboot': True,
        'at_vendor': None,  # varies: Telit, Sierra, Quectel, Foxconn
        'notes': [
            'AT vendor varies by modem make — use --no-at and configure manually',
            'EM7511: Sierra AT!GPS* (use --vendor sierra with diaggpsd AT setup)',
            'Check if DIAG GPS subsystem is active before attempting setup',
        ],
    },
    'mdm9x07': {
        'name': 'MDM9207/MDM9307',
        'modems': ['SIM7600NA', 'RC400L', 'R11e-LTE-US', 'BG95-M3'],
        'nv_items': {7165: 1},
        'oemdre': True,
        'oemdre_needs_reboot': True,
        'at_vendor': None,  # varies
        'notes': [
            'MDM9207 is lower-tier — GNSS DIAG support unverified on most modems',
            'RC400L: no GNSS antenna, unlikely to produce measurements',
        ],
    },
}


def get_profile(chipset: str) -> dict | None:
    """Look up a profile by chipset name or modem name (case-insensitive).

    Tries exact match first, then checks modem names.
    """
    key = chipset.lower().replace('-', '').replace('_', '')
    # Exact match on profile key
    for profile_key, profile in PROFILES.items():
        if key == profile_key.replace('-', '').replace('_', ''):
            return profile
    # Match by modem name
    for profile in PROFILES.values():
        for modem in profile.get('modems', []):
            if key == modem.lower().replace('-', '').replace('_', ''):
                return profile
    return None


def list_profiles() -> str:
    """Return a formatted string listing all profiles and their modems."""
    lines = []
    for key, p in PROFILES.items():
        modems = ', '.join(p['modems'])
        lines.append(f"  {key:12s}  {p['name']:30s}  {modems}")
    return '\n'.join(lines)
