"""Tests for camscan.rf.wifi_evidence. Replays a synthetic capture file -- no
board, no serial -- through the REAL reader -> parser -> consolidate chain.

Run from the project root with the venv active:

    python camscan\\rf\\test_wifi_evidence.py
"""

from __future__ import annotations

import os

from camscan.rf.wifi_evidence import collect_from_file, WifiEvidence
from camscan.rf.consolidate import ConsolidatedDevice

FIXTURE = os.path.join(os.path.dirname(__file__), "_fixture_capture.txt")


def check(desc: str, cond: bool) -> None:
    assert cond, f"FAILED: {desc}"
    print(f"  ok: {desc}")


def test_collect_from_file() -> None:
    print("[step 4] wifi_evidence.collect_from_file over a synthetic capture")
    ev = collect_from_file(FIXTURE)
    check("returns a WifiEvidence", isinstance(ev, WifiEvidence))

    # The fixture has 8 D lines; C/S/L lines and boot/garbage noise are ignored.
    check("counted the 8 raw D lines (ignored C/S/L/noise)",
          ev.total_device_lines == 8)

    # Consolidation: aa bleed(5,6,7)->1, dd non-adjacent(1,11)->2,
    # 11:22:33 dual-band(2G,5G)->2, 99 solo->1  == 6 devices.
    check("8 raw lines consolidated to 6 devices", len(ev.devices) == 6)
    check("raw_to_consolidated reports (8, 6)",
          ev.raw_to_consolidated == (8, 6))

    devices = {(d.mac, d.band): d for d in ev.devices}

    # The bleed cluster merged onto the strongest channel with summed traffic.
    aa = devices[("aa:bb:cc:00:00:01", "2G")]
    check("aa merged onto strongest channel 6", aa.channel == 6)
    check("aa rssi is the strongest (-58)", aa.rssi_avg == -58)
    check("aa frames recombined (4+9+2=15)", aa.frames == 15)
    check("aa bytes_up recombined (2000+8000+1500=11500)", aa.bytes_up == 11500)
    check("aa bled across [5,6,7]", aa.bled_channels == [5, 6, 7])

    # Non-adjacent same-MAC stayed two devices.
    dd = [d for d in ev.devices if d.mac == "dd:ee:ff:00:00:02"]
    check("dd stayed two devices (ch 1 and 11)",
          sorted(d.channel for d in dd) == [1, 11])

    # Dual-band device: one record per band, same MAC.
    dual = [d for d in ev.devices if d.mac == "11:22:33:00:00:03"]
    check("dual-band device is two records",
          sorted(d.band for d in dual) == ["2G", "5G"])


def test_for_mac_lookup() -> None:
    print("[step 4] for_mac() strict join (as scoring will use it)")
    ev = collect_from_file(FIXTURE)

    hit = ev.for_mac("aa:bb:cc:00:00:01")
    check("for_mac finds the consolidated device", hit is not None)
    check("and it's the strongest-channel record (ch 6)",
          hit is not None and hit.channel == 6)

    check("for_mac is case-insensitive",
          ev.for_mac("AA:BB:CC:00:00:01") is not None)
    check("for_mac(None) is None", ev.for_mac(None) is None)
    check("for_mac(unknown) is None",
          ev.for_mac("00:00:00:00:00:00") is None)

    # Dual-band MAC: for_mac returns the stronger-RSSI band (5G @ -48 beats
    # 2G @ -50).
    dual = ev.for_mac("11:22:33:00:00:03")
    check("dual-band for_mac returns the stronger band (5G @ -48)",
          dual is not None and dual.band == "5G")


def test_empty_capture(tmp_path_note: str = "") -> None:
    print("[step 4] an empty/no-D capture yields no devices")
    empty = os.path.join(os.path.dirname(__file__), "_fixture_empty.txt")
    with open(empty, "w", encoding="utf-8") as fh:
        fh.write("I (10) boot: nothing here\nS,1,205744,0\n")
    try:
        ev = collect_from_file(empty)
        check("no D lines -> zero devices", ev.devices == [])
        check("total_device_lines is 0", ev.total_device_lines == 0)
    finally:
        os.remove(empty)


if __name__ == "__main__":
    test_collect_from_file()
    test_for_mac_lookup()
    test_empty_capture()
    print("\nAll wifi_evidence checks passed (step 4).")
