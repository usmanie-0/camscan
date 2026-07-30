"""MAC vendor resolution and camera-vendor matching."""

from pathlib import Path
from mac_vendor_lookup import MacLookup, VendorNotFoundError

_VENDOR_FILE = Path(__file__).resolve().parents[2] / "data" / "camera_vendors.txt"
_lookup = MacLookup()
_keywords = None


def _load_keywords():
    """Load camera vendor keywords as (keyword, weight, note) tuples."""
    global _keywords
    if _keywords is not None:
        return _keywords
    rows = []
    if _VENDOR_FILE.exists():
        for line in _VENDOR_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",", 2)]
            if len(parts) == 3:
                rows.append((parts[0].lower(), parts[1], parts[2]))
    _keywords = rows
    return rows


def is_randomized(mac):
    """True if the locally-administered bit is set (privacy MAC)."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def vendor(mac):
    """IEEE-registered vendor name, or None."""
    try:
        return _lookup.lookup(mac)
    except (VendorNotFoundError, KeyError, ValueError):
        return None


def camera_match(vendor_name):
    """Return (weight, note) if the vendor matches a camera keyword."""
    if not vendor_name:
        return None
    low = vendor_name.lower()
    for keyword, weight, note in _load_keywords():
        if keyword in low:
            return weight, note
    return None


def annotate(host):
    """Add vendor, randomized, and camera-match fields to a host dict."""
    mac = host["mac"]
    host["randomized"] = is_randomized(mac)
    host["vendor"] = None if host["randomized"] else vendor(mac)
    match = camera_match(host["vendor"])
    host["cam_vendor_weight"] = match[0] if match else None
    host["cam_vendor_note"] = match[1] if match else None
    return host


if __name__ == "__main__":
    from camscan.lan.arp import sweep
    from camscan.lan.iface import detect_active

    name, ip, gw, net = detect_active()
    print(f"sweeping {net}\n")

    for h in sweep(subnet=net, iface=name):
        annotate(h)
        if h["randomized"]:
            desc = "(randomized MAC - privacy address)"
        else:
            desc = h["vendor"] or "(unknown vendor)"
        flag = f'  [CAMERA VENDOR: {h["cam_vendor_note"]}]' if h["cam_vendor_weight"] else ""
        print(f'{h["ip"]:<16} {h["mac"]}  {desc}{flag}')
