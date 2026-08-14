"""Collect camera-vendor BLE adverts from an L-line stream, for scoring.

The scoring engine needs a compact summary of the BLE camera evidence gathered
during a scan: which advertiser addresses matched a camera vendor, how strong
they were, and how they matched. This module produces that summary from any
source of records (live serial or a replayed capture), reusing the parser,
reader, and vendor matcher already built.

It computes evidence; it does NOT decide scores -- that stays in scoring.py.
The split mirrors the LAN side: collection here, judgement there.

Coverage honesty: this returns only POSITIVE camera matches. A device that
didn't match is simply absent from the result. Absence must never be read by
the caller as "not a camera" -- it's just "no BLE camera evidence", which is
different. The result also carries how long we listened, so scoring can state
the BLE coverage honestly (a short listen proves little).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .parser import BleLine
from .reader import read_from_file, read_from_serial
from .vendors import match_line, VendorMatch


@dataclass
class CameraAdvert:
    """One camera-vendor BLE advertiser seen during a scan."""
    bdaddr: str
    best_rssi: int              # strongest (max) RSSI seen for this advertiser
    hits: int                   # how many adverts from it we saw
    match: VendorMatch          # why it matched (keyword, on name vs company_id)


@dataclass
class BleEvidence:
    """All camera-vendor BLE evidence from one scan window.

    ``by_addr`` maps advertiser address -> CameraAdvert, for the strict MAC
    join in scoring. ``duration_s`` and ``total_adverts`` support the honest
    coverage note ("BLE scanned N seconds, saw M adverts").
    """
    by_addr: dict[str, CameraAdvert] = field(default_factory=dict)
    duration_s: float = 0.0
    total_adverts: int = 0       # all L lines seen, camera or not

    @property
    def any_camera_vendor(self) -> bool:
        """True if ANY camera-vendor advert was seen (the loose signal)."""
        return len(self.by_addr) > 0

    def for_mac(self, mac: Optional[str]) -> Optional[CameraAdvert]:
        """Strict join: return the camera advert whose address == this MAC.

        Addresses are compared case-insensitively. Returns None when there's
        no BLE advert at that exact address -- which is the common case, since
        a device's BLE and Wi-Fi addresses usually differ.
        """
        if not mac:
            return None
        return self.by_addr.get(mac.upper())


def _accumulate(records, seconds: Optional[float]) -> BleEvidence:
    """Drive records through the matcher into a BleEvidence summary.

    Stops after ``seconds`` if given (for live capture); for a finite file it
    runs to the end. Only camera-vendor matches are kept in by_addr.
    """
    ev = BleEvidence()
    start = time.time()
    for rec in records:
        if not isinstance(rec, BleLine):
            continue
        ev.total_adverts += 1

        m = match_line(rec)
        if m.is_camera:
            key = rec.bdaddr.upper()
            existing = ev.by_addr.get(key)
            if existing is None:
                ev.by_addr[key] = CameraAdvert(
                    bdaddr=rec.bdaddr,
                    best_rssi=rec.rssi,
                    hits=1,
                    match=m,
                )
            else:
                existing.hits += 1
                if rec.rssi > existing.best_rssi:
                    existing.best_rssi = rec.rssi

        if seconds is not None and time.time() - start > seconds:
            break

    ev.duration_s = time.time() - start
    return ev


def collect_from_serial(port: Optional[str] = None, seconds: float = 30.0) -> BleEvidence:
    """Listen live for ``seconds`` and summarize camera-vendor BLE evidence."""
    return _accumulate(read_from_serial(port), seconds)


def collect_from_file(path: str, seconds: Optional[float] = None) -> BleEvidence:
    """Summarize camera-vendor BLE evidence from a captured L-line log."""
    return _accumulate(read_from_file(path), seconds)
