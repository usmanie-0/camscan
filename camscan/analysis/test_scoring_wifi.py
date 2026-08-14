"""Logic tests for the Wi-Fi scoring fold in scoring.py.

These pin the BRANCHES deterministically (things a live capture can't reliably
produce on demand: the rssi==0 sentinel, a clean down-heavy control, the exact
ceiling-lift math). Real threshold VALIDATION is a live Wi-Fi-camera capture --
this only proves the wiring. Run from project root:

    python camscan\\analysis\\test_scoring_wifi.py
"""

from __future__ import annotations

from camscan.analysis import scoring
from camscan.rf.consolidate import ConsolidatedDevice
from camscan.rf.wifi_evidence import WifiEvidence


def check(desc: str, cond: bool) -> None:
    assert cond, f"FAILED: {desc}"
    print(f"  ok: {desc}")


def cdev(mac, *, band="2G", channel=6, rssi, up, down,
         frames=10, h0=0, h1=0, h2=0, h3=0, retries=0) -> ConsolidatedDevice:
    return ConsolidatedDevice(
        mac=mac, band=band, channel=channel, rssi_avg=rssi,
        frames=frames, bytes_up=up, bytes_down=down,
        h0=h0, h1=h1, h2=h2, h3=h3, retries=retries, bled_channels=[channel],
    )


def wifi_with(*devices, duration=30.0) -> WifiEvidence:
    return WifiEvidence(devices=list(devices), duration_s=duration,
                        total_device_lines=len(devices))


# --- _wifi_shape: the camera-shape classifier ------------------------------

def test_wifi_shape() -> None:
    print("[wifi] _wifi_shape tiers")
    # Close (-55) + strongly up-heavy (50k up / 500 down = 100x) -> strong.
    check("close + strongly up-heavy -> strong",
          scoring._wifi_shape(cdev("aa", rssi=-55, up=50000, down=500)) == "strong")
    # Near (-68) + up-heavy -> weak (nearby, not close).
    check("near + up-heavy -> weak",
          scoring._wifi_shape(cdev("aa", rssi=-68, up=50000, down=500)) == "weak")
    # Down-heavy consumer (a big downloader) -> none, however strong the RSSI.
    check("down-heavy consumer -> none",
          scoring._wifi_shape(cdev("aa", rssi=-40, up=500, down=90000)) == "none")
    # rssi==0 sentinel (no uplink seen) -> can't confirm proximity -> none.
    check("rssi==0 sentinel -> none",
          scoring._wifi_shape(cdev("aa", rssi=0, up=50000, down=100)) == "none")
    # Up-heavy but far (-80) -> none.
    check("far (-80) -> none",
          scoring._wifi_shape(cdev("aa", rssi=-80, up=50000, down=100)) == "none")
    # Up-heavy but trivial volume (below floor) -> none.
    check("below volume floor -> none",
          scoring._wifi_shape(cdev("aa", rssi=-50, up=100, down=0)) == "none")
    # Only mildly up-heavy (2x, below asym ratio) at close range -> none.
    check("weakly up-heavy (2x) -> none",
          scoring._wifi_shape(cdev("aa", rssi=-50, up=20000, down=10000)) == "none")


# --- score(): ceiling behaviour, the whole point of RF ---------------------

def test_strict_wifi_lifts_ceiling() -> None:
    print("[wifi] a strict Wi-Fi match lifts a Hikvision-style row past 70")
    # A Hikvision-shaped LAN host: raw LAN points ~86, clamped to 70 alone.
    host = {
        "mac": "aa:bb:cc:00:00:07",
        "rtsp": {"code": 404},                       # rtsp_other 20
        "cam_vendor_weight": "high", "cam_vendor_note": "IP camera maker",  # 25
        "open_ports": [
            {"port": 554, "weight": "high", "note": "RTSP"},   # 15
            {"port": 8000, "weight": "high", "note": "SDK"},   # 15
        ],
    }
    # Without Wi-Fi: LAN-only, capped at 70.
    lan_only = scoring.score(dict(host))
    check("LAN-only caps at 70", lan_only["score"] == 70)
    check("LAN-only ceiling is 70", lan_only["ceiling"] == 70)
    check("LAN-only clamped flag set", lan_only["clamped"] is True)
    check("LAN-only shows 'no RF data' gap",
          any("no RF data" in g for g in lan_only["unchecked"]))

    # With a close up-heavy Wi-Fi flow at the SAME mac: ceiling lifts to 100.
    wifi = wifi_with(cdev("aa:bb:cc:00:00:07", rssi=-52, up=80000, down=600))
    lifted = scoring.score(dict(host), wifi=wifi)
    check("strict Wi-Fi match sets has_rf", lifted["has_rf"] is True)
    check("ceiling lifts to 100", lifted["ceiling"] == 100)
    check("score now exceeds 70 (RF unlocked it)", lifted["score"] > 70)
    check("Wi-Fi reason recorded",
          any("Wi-Fi: up-heavy stream" in t for _lvl, t in lifted["reasons"]))
    check("'no RF data' gap is gone",
          not any("no RF data" in g for g in lifted["unchecked"]))


def test_weak_wifi_does_not_lift() -> None:
    print("[wifi] a weak (nearby) Wi-Fi match adds points but keeps ceiling 70")
    host = {"mac": "aa:bb:cc:00:00:07",
            "cam_vendor_weight": "medium", "cam_vendor_note": "smart home"}  # 10
    wifi = wifi_with(cdev("aa:bb:cc:00:00:07", rssi=-67, up=40000, down=300))
    out = scoring.score(dict(host), wifi=wifi)
    check("weak match does NOT set has_rf", out["has_rf"] is False)
    check("ceiling stays 70", out["ceiling"] == 70)
    check("points still added (10 vendor + 10 wifi_weak = 20)",
          out["raw_score"] == 20)
    check("verdict SUSPICIOUS from the amplified total",
          out["verdict"] == "SUSPICIOUS")


def test_tuya_row_not_inflated_by_unmatched_wifi() -> None:
    print("[wifi] the Tuya trap: an unmatched up-heavy flow can't inflate a row")
    # Tuya-style: vendor-OUI only (10), no ports. A camera-shaped Wi-Fi flow
    # exists in the scan but at a DIFFERENT mac -> must NOT touch this row.
    tuya = {"mac": "11:22:33:00:00:14",
            "cam_vendor_weight": "medium", "cam_vendor_note": "smart home"}
    wifi = wifi_with(cdev("ff:ff:ff:99:99:99", rssi=-50, up=90000, down=300))
    out = scoring.score(dict(tuya), wifi=wifi)
    check("Tuya row unchanged by an unmatched flow (raw stays 10)",
          out["raw_score"] == 10)
    check("Tuya ceiling stays 70 (no RF join)", out["ceiling"] == 70)
    check("Tuya has no Wi-Fi reason",
          not any("Wi-Fi" in t for _lvl, t in out["reasons"]))


def test_wifi_absence_note() -> None:
    print("[wifi] honest coverage note when Wi-Fi ran but didn't match")
    host = {"mac": "aa:bb:cc:00:00:07", "cam_vendor_weight": "high",
            "cam_vendor_note": "IP camera maker"}
    wifi = wifi_with(cdev("00:00:00:00:00:01", rssi=-50, up=90000, down=100))
    out = scoring.score(dict(host), wifi=wifi)
    check("wifi_matched is False (no flow at this mac)",
          out["wifi_matched"] is False)
    check("wifi_seconds recorded", out["wifi_seconds"] == 30.0)
    check("unchecked notes the Wi-Fi/LAN MAC + wired-camera blind spots",
          any("Wi-Fi scanned" in g and "MAC can differ" in g
              for g in out["unchecked"]))


def test_no_wifi_is_backward_compatible() -> None:
    print("[wifi] wifi=None leaves the old LAN+BLE behaviour intact")
    host = {"mac": "aa", "cam_vendor_weight": "high", "cam_vendor_note": "x"}
    out = scoring.score(dict(host))          # no ble, no wifi
    check("no crash, LAN-only score", out["score"] == 25)
    check("wifi_seconds is None", out["wifi_seconds"] is None)
    check("wifi_matched is False", out["wifi_matched"] is False)
    check("ceiling is LAN ceiling", out["ceiling"] == 70)


if __name__ == "__main__":
    test_wifi_shape()
    test_strict_wifi_lifts_ceiling()
    test_weak_wifi_does_not_lift()
    test_tuya_row_not_inflated_by_unmatched_wifi()
    test_wifi_absence_note()
    test_no_wifi_is_backward_compatible()
    print("\nAll Wi-Fi scoring-fold checks passed.")
