"""Unified scoring engine. LAN signals now; RF signals arrive in Phase 5."""

from camscan import store

WEIGHTS = {
    "onvif": 45,
    "rtsp_video": 45,
    "rtsp_auth": 25,
    "rtsp_other": 20,
    "vendor_high": 25,
    "vendor_medium": 10,
    "port_high": 15,
    "port_medium": 8,
    "randomized_penalty": -10,
}

LAN_CEILING = 70   # max reportable without RF evidence
FULL_CEILING = 100


def _lan_points(host):
    pts, why = 0, []

    if host.get("onvif"):
        pts += WEIGHTS["onvif"]
        why.append(("definitive", "ONVIF: self-identifies as a video transmitter"))

    r = host.get("rtsp")
    if r:
        if r.get("has_video_track"):
            pts += WEIGHTS["rtsp_video"]
            why.append(("definitive", "RTSP: serving a video track"))
        elif r.get("needs_auth"):
            pts += WEIGHTS["rtsp_auth"]
            why.append(("high", "RTSP: server present, credentials required"))
        else:
            pts += WEIGHTS["rtsp_other"]
            why.append(("high", f'RTSP: server responded (code {r.get("code")})'))

    w = host.get("cam_vendor_weight")
    if w == "high":
        pts += WEIGHTS["vendor_high"]
        why.append(("high", f'vendor OUI: {host.get("cam_vendor_note")}'))
    elif w == "medium":
        pts += WEIGHTS["vendor_medium"]
        why.append(("medium", f'vendor OUI: {host.get("cam_vendor_note")}'))

    for p in host.get("open_ports", []):
        if p["weight"] == "high":
            pts += WEIGHTS["port_high"]
            why.append(("high", f'port {p["port"]}: {p["note"]}'))
        elif p["weight"] == "medium":
            pts += WEIGHTS["port_medium"]
            why.append(("medium", f'port {p["port"]}: {p["note"]}'))

    if host.get("randomized"):
        pts += WEIGHTS["randomized_penalty"]
        why.append(("negative", "randomized MAC: cameras rarely rotate addresses"))

    return pts, why


def _persistence_factor(seen, total):
    """Amplify existing evidence for devices that stay put. Never creates it."""
    if total < 2:
        return 1.0, None
    ratio = seen / total
    if ratio >= 0.9 and seen >= 2:
        return 1.15, ("medium", f"persistent: present in {seen}/{total} scans")
    if ratio <= 0.34:
        return 0.9, ("negative", f"transient: only {seen}/{total} scans")
    return 1.0, None


def score(host, con=None):
    """Score one host. Returns the host with scoring fields added."""
    raw, why = _lan_points(host)

    seen = total = None
    if con is not None and host.get("mac"):
        seen, total = store.persistence(con, host["mac"])
        total = max(0, total - 1)  # exclude the in-progress scan
        factor, reason = _persistence_factor(seen, total)
        if raw > 0:
            raw = int(round(raw * factor))
        if reason:
            why.append(reason)

    host["raw_score"] = max(0, raw)
    host["score"] = min(host["raw_score"], LAN_CEILING)
    host["clamped"] = host["raw_score"] > LAN_CEILING
    host["reasons"] = why
    host["seen_in"] = seen
    host["scan_total"] = total
    host["has_rf"] = False
    host["ceiling"] = LAN_CEILING
    host["verdict"] = verdict(host["score"])
    host["unchecked"] = unchecked(host)
    return host


def verdict(pts):
    if pts >= 45:
        return "LIKELY CAMERA"
    if pts >= 20:
        return "SUSPICIOUS"
    return "CLEAR"


def unchecked(host):
    """What this scan could NOT rule out. Honesty about coverage."""
    gaps = []
    if not host.get("has_rf"):
        gaps.append("no RF data: cloud-only cameras may serve nothing on the LAN")
    if host.get("cam_vendor_weight") and not host.get("open_ports"):
        gaps.append("camera-vendor device with no open ports: likely cloud-only")
    return gaps


if __name__ == "__main__":
    from camscan.lan.collect import collect, persist

    con = store.connect()
    scan_id, ctx, hosts = collect(
        con, skip_ports=False, note="scoring test",
        progress=lambda m: print(f"  {m}"),
    )

    for h in hosts:
        score(h, con)
    persist(con, scan_id, hosts)

    hosts.sort(key=lambda h: -h["raw_score"])
    print()

    for h in hosts:
        if h["raw_score"] == 0:
            continue
        vendor = h["vendor"] or ("randomized" if h["randomized"] else "unknown")
        clamp = f' (raw {h["raw_score"]})' if h["clamped"] else ""
        print(f'{h["ip"]:<16} {vendor[:38]}')
        print(f'    {h["score"]}/{h["ceiling"]}{clamp}  {h["verdict"]}')
        for level, text in h["reasons"]:
            print(f"      [{level}] {text}")
        for gap in h["unchecked"]:
            print(f"      [gap] {gap}")
        print()

    con.close()

