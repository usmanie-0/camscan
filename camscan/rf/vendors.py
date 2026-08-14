"""Match a BLE advertisement against known camera vendors.

Two signals, both taken from an ``L`` line (a ``BleLine`` from the parser):

  1. the advertised NAME, when present, and
  2. the vendor NAME that the advertisement's company-ID resolves to.

Either one hitting a camera keyword makes the advert "camera-shaped". This
follows the project's stated philosophy: match on vendor name/keyword, not a
brittle list of raw company-ID numbers. Company IDs are first resolved to a
vendor name (via ``company_ids.txt``), then matched like any other name
(against ``camera_vendors.txt``).

Coverage honesty -- the point that matters most here: a match ADDS evidence; a
non-match proves NOTHING. Apple/Microsoft/absent advertisers, and IDs we can't
resolve, simply produce no BLE camera signal. They are not "cleared". Absence
of evidence is not evidence of absence -- the scoring layer must treat a
no-match as zero contribution, never as a negative.

Data lives in two editable text files next to this module, so updating the
vendor list or the ID table needs no code change and no firmware reflash.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# Files sit alongside this module.
_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDORS_FILE = os.path.join(_HERE, "camera_vendors.txt")
_COMPANY_IDS_FILE = os.path.join(_HERE, "company_ids.txt")


def _load_keywords(path: str) -> list[str]:
    """Read a keyword-per-line file, dropping blanks, comments, inline #..."""
    out: list[str] = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            # Strip inline comments, then whitespace.
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line.lower())
    return out


def _load_company_ids(path: str) -> dict[str, str]:
    """Read '<hex4>,<vendor name>' lines into {upper-hex: vendor name}."""
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line or "," not in line:
                continue
            hexpart, name = line.split(",", 1)
            hexpart = hexpart.strip().upper()
            name = name.strip()
            if hexpart and name:
                out[hexpart] = name
    return out


# Loaded once at import. Call reload() if the files change during a session.
_CAMERA_KEYWORDS = _load_keywords(_VENDORS_FILE)
_COMPANY_IDS = _load_company_ids(_COMPANY_IDS_FILE)


def reload() -> None:
    """Re-read both data files (after editing them mid-session)."""
    global _CAMERA_KEYWORDS, _COMPANY_IDS
    _CAMERA_KEYWORDS = _load_keywords(_VENDORS_FILE)
    _COMPANY_IDS = _load_company_ids(_COMPANY_IDS_FILE)


def resolve_company_id(mfr_id: Optional[str]) -> Optional[str]:
    """Return the vendor name for a 4-hex-digit company ID, or None.

    ``mfr_id`` is the raw string from the L line (e.g. "004C"), or None when
    the advert carried no manufacturer data. An ID we don't have in the table
    returns None -- unknown, not an error.
    """
    if not mfr_id:
        return None
    return _COMPANY_IDS.get(mfr_id.strip().upper())


def _first_keyword_hit(text: Optional[str]) -> Optional[str]:
    """Return the first camera keyword that appears in text, or None."""
    if not text:
        return None
    low = text.lower()
    for kw in _CAMERA_KEYWORDS:
        if kw in low:
            return kw
    return None


@dataclass
class VendorMatch:
    """The outcome of matching one BLE advert against camera vendors.

    ``is_camera`` is True when either the advertised name or the resolved
    company-ID vendor name hit a camera keyword. The other fields explain WHY,
    which the scoring layer and any coverage/debug output can surface.
    """
    is_camera: bool
    matched_keyword: Optional[str] = None   # which keyword fired
    matched_on: Optional[str] = None        # "name" or "company_id"
    vendor_name: Optional[str] = None       # resolved company-ID vendor, if any


def match_ble(name: Optional[str], mfr_id: Optional[str]) -> VendorMatch:
    """Match a BLE advert's name and company-ID against camera vendors.

    Checks the advertised name first, then the resolved company-ID vendor
    name. First hit wins and is reported. No hit -> is_camera False with the
    resolved vendor name still filled in when we had one (useful context).
    """
    # 1) advertised name (strong, direct signal)
    hit = _first_keyword_hit(name)
    if hit is not None:
        return VendorMatch(
            is_camera=True,
            matched_keyword=hit,
            matched_on="name",
            vendor_name=resolve_company_id(mfr_id),
        )

    # 2) company-ID -> vendor name -> keyword
    vendor = resolve_company_id(mfr_id)
    hit = _first_keyword_hit(vendor)
    if hit is not None:
        return VendorMatch(
            is_camera=True,
            matched_keyword=hit,
            matched_on="company_id",
            vendor_name=vendor,
        )

    # No camera signal. NOT a clearance -- just no BLE evidence either way.
    return VendorMatch(is_camera=False, vendor_name=vendor)


def match_line(line) -> VendorMatch:
    """Convenience: match a BleLine record directly."""
    return match_ble(line.name, line.mfr_id)
