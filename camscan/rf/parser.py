"""Parse the CSV lines the ESP32-C5 firmware emits over USB serial.

The firmware speaks six line types, each a comma-separated record with a
single-letter (or ``CAM?``) prefix. This module turns ONE raw line into ONE
small structured record, or ``None`` if the line isn't a recognized record.

It does no serial I/O and no vendor matching -- that stays out of here on
purpose (firmware stays dumb, matching lives higher up in Phase 5). This layer
only answers: "what did the board just tell me, as typed Python fields?"

Line formats (pinned from firmware source, not guessed):

    D,<mac>,<band>,<ch>,<rssi_avg>,<frames>,<bytes_up>,<bytes_down>,<h0>,<h1>,<h2>,<h3>,<retries>
    C,<band>,<ch>,<dwell_ms>,<tot_frames>,<tot_bytes>,<stations>,<strongest_rssi>,<max_up>
    S,<uptime_ms>,<free_heap>,<frames_dropped>
    L,<bdaddr>,<rssi>,<name>,<mfr_id>
    CAM?,<mac>,<band>,<ch>,<rssi_avg>,<bytes_up>,<bytes_down>,<h2>,<h3>,<frames>
    H,<mac>,<ch>,<rssi>,<frames>,<age_ms>

Design notes:
  * ``band`` is the literal string "2G" or "5G", never a number.
  * On an ``L`` line, an EMPTY name or mfr_id field is a real, valid state
    ("not seen"), not an error -- passive BLE scans routinely see neither.
    We represent absent as ``None``, present-but-empty is impossible here.
  * mfr_id arrives as 4 bare hex digits (e.g. "004C") with NO "0x" prefix.
    We keep the raw hex string AND provide the parsed int for convenience.
  * An ``H`` line (Phase 6 hunt mode) is a live RSSI meter sample for one
    target MAC. It has no ``band`` field (hunt parks on a known channel), and
    ``frames == 0`` marks a silent window where ``rssi`` is not meaningful.
  * A line that doesn't match a known shape returns ``None`` rather than
    raising -- serial streams contain boot log noise, partial lines, and the
    occasional corrupted record, and the read loop shouldn't crash on those.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# --- Record types -----------------------------------------------------------
# One dataclass per line type. Kept deliberately flat and close to the wire
# format; interpretation (scoring, vendor lookup, aggregation) happens later.


@dataclass
class DeviceLine:
    """A ``D`` line: per-STA aggregate for one focus-dwell window."""
    mac: str
    band: str          # "2G" or "5G"
    channel: int
    rssi_avg: int      # signed dBm; 0 means "no uplink frames seen this window"
    frames: int
    bytes_up: int
    bytes_down: int
    h0: int            # frame-size histogram: <100, <500, <1000, >=1000 bytes
    h1: int
    h2: int
    h3: int
    retries: int


@dataclass
class ChannelLine:
    """A ``C`` line: per-channel survey/dwell summary."""
    band: str
    channel: int
    dwell_ms: int
    tot_frames: int
    tot_bytes: int
    stations: int
    strongest_rssi: int   # signed dBm; 0 means "no uplink rssi seen"
    max_up: int


@dataclass
class StatusLine:
    """An ``S`` line: firmware heartbeat."""
    uptime_ms: int
    free_heap: int
    frames_dropped: int


@dataclass
class BleLine:
    """An ``L`` line: one passive BLE advertisement.

    ``name`` and ``mfr_id`` are ``None`` when the field was empty on the wire.
    ``mfr_id_int`` is the parsed IEEE company ID (e.g. 0x004C) or ``None``.
    """
    bdaddr: str
    rssi: int
    name: Optional[str]
    mfr_id: Optional[str]        # raw 4-hex-digit string, e.g. "004C"
    mfr_id_int: Optional[int]    # parsed int, e.g. 76, or None


@dataclass
class CamHintLine:
    """A ``CAM?`` line: firmware's camera-candidate highlight for a D line.

    This is a diagnostic hint derived from a D line, not an independent
    device record. Scoring should treat it as a nudge, not a source of truth.
    """
    mac: str
    band: str
    channel: int
    rssi_avg: int
    bytes_up: int
    bytes_down: int
    h2: int
    h3: int
    frames: int


@dataclass
class HuntLine:
    """An ``H`` line: live RSSI meter sample for ONE target MAC (Phase 6).

    Emitted every ~200 ms by the firmware's hunt mode while parked on the
    target's channel. ``frames == 0`` means the target was silent this window
    (its ``rssi`` is meaningless then); ``age_ms`` is how long since it was
    last actually heard, so the meter can hold through short gaps and decay
    when the target is genuinely lost.
    """
    mac: str
    channel: int
    rssi: int          # signed dBm; only meaningful when frames > 0
    frames: int        # target frames heard this window; 0 = silent
    age_ms: int        # ms since the target was last heard


# --- Parser -----------------------------------------------------------------


def _empty_to_none(s: str) -> Optional[str]:
    """An empty CSV field means 'absent'. Everything else is kept verbatim."""
    return s if s else None


def parse_line(line: str) -> Optional[object]:
    """Parse one raw serial line into a record, or return ``None``.

    ``None`` is returned for blank lines, boot-log noise, unknown prefixes, and
    records whose field count or numeric fields don't match the expected shape.
    The caller (the serial read loop) simply skips ``None`` and keeps reading.
    """
    if line is None:
        return None

    # Strip trailing CR/LF and surrounding whitespace. Do NOT strip internal
    # commas or fields -- an L line's empty name field is meaningful.
    line = line.strip()
    if not line:
        return None

    # Split on the FIRST comma to get the prefix, so a "CAM?" prefix (which has
    # no comma in it) is handled the same way as single-letter prefixes.
    if "," not in line:
        return None
    prefix, _, rest = line.partition(",")

    try:
        if prefix == "D":
            f = rest.split(",")
            if len(f) != 12:
                return None
            return DeviceLine(
                mac=f[0],
                band=f[1],
                channel=int(f[2]),
                rssi_avg=int(f[3]),
                frames=int(f[4]),
                bytes_up=int(f[5]),
                bytes_down=int(f[6]),
                h0=int(f[7]),
                h1=int(f[8]),
                h2=int(f[9]),
                h3=int(f[10]),
                retries=int(f[11]),
            )

        if prefix == "C":
            f = rest.split(",")
            if len(f) != 8:
                return None
            return ChannelLine(
                band=f[0],
                channel=int(f[1]),
                dwell_ms=int(f[2]),
                tot_frames=int(f[3]),
                tot_bytes=int(f[4]),
                stations=int(f[5]),
                strongest_rssi=int(f[6]),
                max_up=int(f[7]),
            )

        if prefix == "S":
            f = rest.split(",")
            if len(f) != 3:
                return None
            return StatusLine(
                uptime_ms=int(f[0]),
                free_heap=int(f[1]),
                frames_dropped=int(f[2]),
            )

        if prefix == "H":
            # Hunt-mode RSSI meter sample. Five fields, no band (parked on a
            # known channel). frames==0 marks a silent window.
            f = rest.split(",")
            if len(f) != 5:
                return None
            return HuntLine(
                mac=f[0],
                channel=int(f[1]),
                rssi=int(f[2]),
                frames=int(f[3]),
                age_ms=int(f[4]),
            )

        if prefix == "L":
            # Exactly 4 fields after the prefix: bdaddr, rssi, name, mfr_id.
            # name/mfr_id may be empty. The firmware sanitizes name so it can't
            # contain a comma, so a plain split is safe and must yield 4.
            f = rest.split(",")
            if len(f) != 4:
                return None
            mfr_raw = _empty_to_none(f[3])
            mfr_int = int(mfr_raw, 16) if mfr_raw is not None else None
            return BleLine(
                bdaddr=f[0],
                rssi=int(f[1]),
                name=_empty_to_none(f[2]),
                mfr_id=mfr_raw,
                mfr_id_int=mfr_int,
            )

        if prefix == "CAM?":
            f = rest.split(",")
            if len(f) != 9:
                return None
            return CamHintLine(
                mac=f[0],
                band=f[1],
                channel=int(f[2]),
                rssi_avg=int(f[3]),
                bytes_up=int(f[4]),
                bytes_down=int(f[5]),
                h2=int(f[6]),
                h3=int(f[7]),
                frames=int(f[8]),
            )

    except ValueError:
        # A numeric field wasn't numeric -> corrupted/partial line. Skip it.
        return None

    # Unknown prefix (boot log, ESP_LOGI output, etc.)
    return None
