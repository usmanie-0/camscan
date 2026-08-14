"""Collect a scan's Wi-Fi ``D``-line devices, consolidated, ready for scoring.

This is the Wi-Fi mirror of ``rf_evidence.py`` (the BLE side). Same division of
labour: collection lives HERE, judgement lives in ``scoring.py``. It drains a
record stream -- live serial or a replayed capture -- keeps only the
``DeviceLine`` (``D``) records, and runs the adjacent-channel-bleed
consolidation over the whole batch, so downstream scoring sees ONE device per
physical device instead of the same device fragmented across neighbouring
channels.

It computes evidence; it does NOT decide scores.

WHAT "ONE SCAN" MEANS HERE: we gather every ``D`` line seen during the listen
window and consolidate them together at the end. Because ``consolidate`` merges
same-MAC records on adjacent (and identical) channels, a device that reappears
across several firmware focus-windows during the listen is summed into one
record -- i.e. this consolidates across TIME as well as across channels, giving
per-device totals over the window attributed to its strongest channel. That's
the useful default for scoring "how much did this device transmit, and where
does it really live". If you later want per-firmware-window consolidation
instead, delimit windows on the ``C`` lines and call ``consolidate`` per window.

COVERAGE HONESTY: ``WifiEvidence`` reports how long we listened and how many raw
``D`` lines collapsed into how many consolidated devices, so scoring can state
the Wi-Fi coverage honestly and the consolidation stays visible, not hidden.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .parser import DeviceLine
from .reader import read_from_file, read_from_serial
from .consolidate import consolidate, ConsolidatedDevice


def _rssi_key(device: ConsolidatedDevice) -> float:
    """Sort key so the 0-sentinel ('no uplink seen') ranks weakest."""
    return float("-inf") if device.rssi_avg == 0 else float(device.rssi_avg)


@dataclass
class WifiEvidence:
    """All Wi-Fi device evidence from one scan window, bleed-consolidated.

    ``devices`` is the consolidated list (one per physical device per band).
    ``duration_s`` and ``total_device_lines`` support an honest coverage note
    and make the consolidation auditable (N raw lines -> M devices).
    """
    devices: list[ConsolidatedDevice] = field(default_factory=list)
    duration_s: float = 0.0
    total_device_lines: int = 0        # raw D lines seen, BEFORE consolidation

    @property
    def raw_to_consolidated(self) -> "tuple[int, int]":
        """(raw D lines seen, consolidated devices) -- how much bleed merged."""
        return self.total_device_lines, len(self.devices)

    def for_mac(self, mac: Optional[str]) -> Optional[ConsolidatedDevice]:
        """Strict join: the consolidated device at this exact MAC, or ``None``.

        Case-insensitive, mirroring ``BleEvidence.for_mac`` so scoring can look
        up a host's Wi-Fi evidence exactly the way it looks up BLE. A dual-band
        device has one record per band with the same MAC; we return the
        STRONGER-RSSI record (the band where it's most present), with the 0
        sentinel treated as weakest.
        """
        if not mac:
            return None
        want = mac.lower()
        matches = [d for d in self.devices if d.mac.lower() == want]
        if not matches:
            return None
        return max(matches, key=_rssi_key)


def _accumulate(records, seconds: Optional[float]) -> WifiEvidence:
    """Drive records into a consolidated ``WifiEvidence`` summary.

    Collects every ``DeviceLine`` (ignoring ``C``/``S``/``L`` and noise, which
    the reader already filters to typed records), stopping after ``seconds`` if
    given (for live capture) or at end-of-file otherwise, then consolidates the
    whole batch ONCE.
    """
    lines: list[DeviceLine] = []
    start = time.time()
    for rec in records:
        if isinstance(rec, DeviceLine):
            lines.append(rec)
        if seconds is not None and time.time() - start > seconds:
            break
    return WifiEvidence(
        devices=consolidate(lines),
        duration_s=time.time() - start,
        total_device_lines=len(lines),
    )


def collect_from_serial(port: Optional[str] = None,
                        seconds: float = 30.0) -> WifiEvidence:
    """Listen live for ``seconds`` and return bleed-consolidated Wi-Fi devices."""
    return _accumulate(read_from_serial(port), seconds)


def collect_from_file(path: str, seconds: Optional[float] = None) -> WifiEvidence:
    """Consolidate the Wi-Fi devices from a captured ``D``-line log (no board)."""
    return _accumulate(read_from_file(path), seconds)
