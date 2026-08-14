"""Unified scoring engine. LAN signals + RF/BLE signals + RF/Wi-Fi (Phase 5)."""

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
    # --- RF / BLE (Phase 5) ---
    "ble_vendor_strict": 30,   # camera-vendor BLE advert AT this host's MAC
    "ble_vendor_loose": 12,    # camera-vendor advert in the environment (amplifies only)
    # --- RF / Wi-Fi (Phase 5) ---
    "wifi_stream_strong": 25,  # up-heavy + CLOSE flow AT this host's MAC (lifts ceiling)
    "wifi_stream_weak": 10,    # up-heavy + NEAR flow AT this host's MAC (amplifies only)
}

LAN_CEILING = 70   # max reportable without RF evidence
FULL_CEILING = 100

# --- Wi-Fi camera-shape thresholds (Phase 5) -------------------------------
# What makes a consolidated Wi-Fi flow look like a camera. The PRIMARY signals
# are up:down asymmetry and RSSI -- both independent of how long the hopping
# sniffer happened to dwell on the device. Absolute volume is only a floor,
# NOT a discriminator, because channel-hopping means captured bytes are a
# fraction of true throughput (an unreliable proxy).
#
# CALIBRATION STATUS:
#   WIFI_RSSI_MIN / WIFI_RSSI_STRONG -- ANCHORED to the Phase 4 Step-7 ground
#     truth: an uploading Pixel fired the camera gate at rssi_avg >= -70 and
#     visibly dropped out as it moved away. -60 marks "close".
#   WIFI_ASYM_RATIO / WIFI_MIN_UP -- PLACEHOLDERS. A real streaming camera's
#     up:down ratio and captured-byte floor still want calibration against an
#     actual Wi-Fi camera (or a DVR uploading over Wi-Fi). The home DVR here is
#     wired/ethernet, so it's invisible to the sniffer and can't calibrate
#     these -- tune them at a deployment that has a Wi-Fi-uploading camera.
WIFI_RSSI_MIN = -70        # weakest RSSI we'll treat as a real nearby uploader
WIFI_RSSI_STRONG = -60     # "close" -- strong enough to lift the ceiling
WIFI_ASYM_RATIO = 3        # PLACEHOLDER: up >= 3x down == strongly up-heavy
WIFI_MIN_UP = 2000         # PLACEHOLDER: bytes floor, to drop trivial/keepalive flows


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


def _rf_points(host, ble):
    """RF/BLE evidence for one host. Returns (points, why, joined).

    ``ble`` is a BleEvidence summary (camscan.rf.rf_evidence), or None when no
    BLE was captured. ``joined`` is True only for a STRICT MAC match -- that's
    real per-host RF evidence and is what lifts the ceiling. The loose signal
    is environment-level: it amplifies a host that already looks camera-shaped
    on the LAN, but never lifts the ceiling and never creates evidence alone.
    """
    if ble is None:
        return 0, [], False

    # Strict join: a camera-vendor advert AT this host's exact address.
    advert = ble.for_mac(host.get("mac"))
    if advert is not None:
        note = (f'BLE: camera-vendor advert at this address '
                f'({advert.match.matched_keyword}, '
                f'{advert.best_rssi} dBm, {advert.hits} adverts)')
        return WEIGHTS["ble_vendor_strict"], [("definitive", note)], True

    # Loose corroboration: camera-vendor adverts exist nearby but not at this
    # MAC. Only meaningful for a host already showing LAN camera evidence --
    # the caller gates this so it amplifies rather than invents.
    if ble.any_camera_vendor:
        n = len(ble.by_addr)
        note = (f'BLE: {n} camera-vendor advert(s) in the environment '
                f'(not at this address)')
        return WEIGHTS["ble_vendor_loose"], [("medium", note)], False

    return 0, [], False


def _wifi_shape(dev):
    """Classify one consolidated Wi-Fi device's camera-shape. Returns a tier.

    Returns "strong", "weak", or "none". The discriminators, in priority:
      1. up-heavy at all      -- bytes_up must exceed bytes_down (a camera
                                 uploads video; a consumer/downloader doesn't);
      2. non-trivial upload   -- bytes_up over a small floor (drop keepalives);
      3. how up-heavy + how close -- the up:down ratio and RSSI decide the tier.

    rssi_avg == 0 is the "no uplink seen" sentinel -> can't confirm proximity,
    so it can never reach "strong"/"weak" (treated as unknown/far).
    """
    up, down, rssi = dev.bytes_up, dev.bytes_down, dev.rssi_avg

    if up <= down:                 # not up-heavy -> not camera-shaped
        return "none"
    if up < WIFI_MIN_UP:           # trivial upload -> ignore
        return "none"

    asym = up / max(down, 1)
    close = rssi != 0 and rssi >= WIFI_RSSI_STRONG   # -60 or better
    near = rssi != 0 and rssi >= WIFI_RSSI_MIN       # -70 or better

    # Strong: strongly up-heavy AND physically close -> real per-host evidence.
    if close and asym >= WIFI_ASYM_RATIO:
        return "strong"
    # Weak: up-heavy and merely nearby -> amplifier, never lifts the ceiling.
    if near and asym >= WIFI_ASYM_RATIO:
        return "weak"
    return "none"


def _wifi_points(host, wifi):
    """RF/Wi-Fi evidence for one host. Returns (points, why, joined).

    ``wifi`` is a WifiEvidence summary (camscan.rf.wifi_evidence), or None when
    no Wi-Fi was captured. Mirrors _rf_points: a STRICT per-host MAC match to a
    camera-shaped flow is ``joined`` (lifts the ceiling); a weaker per-host
    match adds modest points but does NOT lift the ceiling. There is no loose
    environment-level Wi-Fi signal -- an up-heavy flow that isn't at this MAC
    is too noisy to splash onto a host (that would inflate thin rows like a
    vendor-OUI-only Tuya), and the genuinely-important unmatched case (a Wi-Fi
    camera / DVR that maps to no LAN host at all) is a SEPARATE detection that
    needs its own surfacing, not amplification here.
    """
    if wifi is None:
        return 0, [], False

    dev = wifi.for_mac(host.get("mac"))
    if dev is None:
        return 0, [], False

    tier = _wifi_shape(dev)
    if tier == "none":
        return 0, [], False

    asym = dev.bytes_up / max(dev.bytes_down, 1)
    note = (f'Wi-Fi: up-heavy stream at this address '
            f'({dev.bytes_up}B up / {dev.bytes_down}B down, {asym:.0f}x, '
            f'{dev.rssi_avg} dBm, {dev.band} ch {dev.channel})')

    if tier == "strong":
        return WEIGHTS["wifi_stream_strong"], [("high", note)], True
    return WEIGHTS["wifi_stream_weak"], [("medium", note)], False


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


def score(host, con=None, ble=None, wifi=None):
    """Score one host. Returns the host with scoring fields added.

    ``ble`` / ``wifi`` are optional RF evidence summaries. RF evidence adds
    points; a STRICT per-host match (BLE at this MAC, or a close up-heavy Wi-Fi
    flow at this MAC) lifts the ceiling from LAN_CEILING to FULL_CEILING and
    marks has_rf. Loose/weak signals only amplify a host that already looks
    camera-shaped -- they never lift the ceiling and never invent evidence.
    """
    raw, why = _lan_points(host)
    lan_raw = raw  # remember LAN-only points, to gate the loose RF signal

    # --- RF / BLE ---
    ble_pts, ble_why, ble_joined = _rf_points(host, ble)
    if ble_joined:
        # Strict per-host BLE evidence: always counts, lifts the ceiling.
        raw += ble_pts
        why.extend(ble_why)
    elif ble_pts > 0 and lan_raw > 0:
        # Loose environment signal: only credit a host already camera-shaped.
        raw += ble_pts
        why.extend(ble_why)
    # else: loose signal on a zero-LAN host is dropped -- never invent evidence.

    # --- RF / Wi-Fi ---
    wifi_pts, wifi_why, wifi_joined = _wifi_points(host, wifi)
    if wifi_joined:
        # Strict per-host Wi-Fi evidence (close + up-heavy at this MAC): lifts.
        raw += wifi_pts
        why.extend(wifi_why)
    elif wifi_pts > 0:
        # Weak per-host Wi-Fi: it IS at this host's MAC (per-host, not
        # environment noise), so it counts -- but it doesn't lift the ceiling.
        raw += wifi_pts
        why.extend(wifi_why)

    has_rf = ble_joined or wifi_joined
    ceiling = FULL_CEILING if has_rf else LAN_CEILING

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
    host["score"] = min(host["raw_score"], ceiling)
    host["clamped"] = host["raw_score"] > ceiling
    host["reasons"] = why
    host["seen_in"] = seen
    host["scan_total"] = total
    host["has_rf"] = has_rf
    host["ceiling"] = ceiling
    host["ble_seconds"] = ble.duration_s if ble else None
    host["wifi_seconds"] = wifi.duration_s if wifi else None
    host["wifi_matched"] = wifi_joined
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
    # BLE was captured but did not strictly tie an advert to this host. Absence
    # of a match is not proof of absence -- the advert may be absent, at a
    # randomized address, or scan-response-only (a passive scan never sees it).
    if host.get("ble_seconds") is not None and not host.get("has_rf"):
        gaps.append(
            f'BLE scanned {host["ble_seconds"]:.0f}s, no camera-vendor advert '
            f'at this address (absence is not proof)'
        )
    # Wi-Fi was captured but no close up-heavy flow tied to THIS host's MAC.
    # Note the two big blind spots explicitly: a camera on its own network (or
    # a DVR uploading over ethernet) never appears as a Wi-Fi flow here, and a
    # device's Wi-Fi MAC can differ from its LAN MAC (dual-homed or randomized).
    if host.get("wifi_seconds") is not None and not host.get("wifi_matched"):
        gaps.append(
            f'Wi-Fi scanned {host["wifi_seconds"]:.0f}s, no up-heavy stream at '
            f'this address (a Wi-Fi MAC can differ from the LAN MAC, and a '
            f'wired/off-network camera emits no Wi-Fi -- absence is not proof)'
        )
    if host.get("ports_skipped"):
        gaps.append("port scan skipped: run without --fast for full LAN evidence")
    elif host.get("cam_vendor_weight") and not host.get("open_ports"):
        gaps.append("camera-vendor device with no open ports: likely cloud-only")
    return gaps


if __name__ == "__main__":
    from camscan.lan.collect import collect, persist

    con = store.connect()
    scan_id, ctx, hosts = collect(
        con, skip_ports=False, note="scoring test",
        progress=lambda m: print(f"  {m}"),
    )

    # Optional: capture RF evidence once and fold it into scoring. Comment the
    # RF lines to run LAN-only (score(h, con) with ble/wifi defaulting None).
    #
    # NOTE (single-stream): the firmware emits BOTH L (BLE) and D (Wi-Fi) lines
    # on ONE serial stream, so ideally one listen drains the port and builds
    # both evidences together. Wired as two sequential listens here to keep the
    # scoring fold self-contained; unifying them into a single pass (half the
    # scan time, same time window) is the next small step.
    from camscan.rf.rf_evidence import collect_from_serial as collect_ble
    from camscan.rf.wifi_evidence import collect_from_serial as collect_wifi
    ble = collect_ble(seconds=30)
    wifi = collect_wifi(seconds=30)

    for h in hosts:
        score(h, con, ble=ble, wifi=wifi)
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
