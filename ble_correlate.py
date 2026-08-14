"""Correlate BLE advertisers against a device's power state across cycles.

Single on/off diffs are defeated by MAC randomization: a phone that vanishes
once was just re-randomizing, not powered off. A camera with a stable MAC
vanishes EVERY time you cut power and returns EVERY time you restore it. This
tool captures several labeled windows and scores each MAC by how well its
presence tracks the ON/OFF pattern -- so the camera (if any) rises above the
random churn.

Workflow (the script walks you through it):
  It runs N rounds. Each round = one ON window then one OFF window. Before each
  window it tells you to plug in / unplug the target device and waits for Enter.
  At the end it prints MACs ranked by "tracking score": present in ON windows,
  absent in OFF windows.

  python ble_correlate.py [seconds_per_window] [rounds] [COM5]
  defaults: 20s, 3 rounds, auto-detect port
"""

import sys
import time
from collections import defaultdict

from camscan.rf.reader import read_from_serial, resolve_port
from camscan.rf.parser import BleLine


def capture(port, seconds):
    """Return {mac: hit_count} for one window."""
    hits = defaultdict(int)
    start = time.time()
    for rec in read_from_serial(port):
        if isinstance(rec, BleLine):
            hits[rec.bdaddr] += 1
        if time.time() - start > seconds:
            break
    return hits


def main():
    args = sys.argv[1:]
    seconds = int(args[0]) if len(args) >= 1 else 20
    rounds = int(args[1]) if len(args) >= 2 else 3
    port = args[2] if len(args) >= 3 else None

    try:
        port = resolve_port(port)
    except RuntimeError as e:
        print(e)
        return

    print(f"Port {port}. {rounds} rounds, {seconds}s per window.")
    print("Keep the board STILL the whole time. Only the target device moves.\n")

    # Per-MAC tallies across all ON windows and all OFF windows.
    on_hits = defaultdict(int)
    off_hits = defaultdict(int)
    on_windows = 0
    off_windows = 0
    # Track per-window presence for the tracking score.
    on_present = defaultdict(int)   # in how many ON windows did MAC appear
    off_present = defaultdict(int)  # in how many OFF windows did MAC appear

    for r in range(1, rounds + 1):
        input(f"[Round {r}/{rounds}] Make sure target is POWERED ON, "
              f"then press Enter to scan {seconds}s...")
        h = capture(port, seconds)
        on_windows += 1
        for mac, c in h.items():
            on_hits[mac] += c
            on_present[mac] += 1
        print(f"  ON  window: {len(h)} advertisers, {sum(h.values())} hits")

        input(f"[Round {r}/{rounds}] Now UNPLUG the target, wait ~15s for it to "
              f"age out, then press Enter to scan {seconds}s...")
        h = capture(port, seconds)
        off_windows += 1
        for mac, c in h.items():
            off_hits[mac] += c
            off_present[mac] += 1
        print(f"  OFF window: {len(h)} advertisers, {sum(h.values())} hits\n")

    # Score: a camera is present in most ON windows and absent in most OFF ones.
    # tracking = (fraction of ON windows present) - (fraction of OFF windows present)
    # +1.0 = perfect camera-like tracking; ~0 = random / always-on; <0 = inverse.
    all_macs = set(on_present) | set(off_present)
    scored = []
    for mac in all_macs:
        on_frac = on_present[mac] / on_windows if on_windows else 0
        off_frac = off_present[mac] / off_windows if off_windows else 0
        tracking = on_frac - off_frac
        scored.append((tracking, mac, on_present[mac], off_present[mac],
                       on_hits[mac], off_hits[mac]))

    scored.sort(reverse=True)

    print("=" * 64)
    print("TRACKING SCORE  (+1.0 = present when ON, gone when OFF = camera-like)")
    print("=" * 64)
    print(f"{'score':>6} {'MAC':<20} {'ON win':>6} {'OFF win':>7} "
          f"{'ON hits':>7} {'OFF hits':>8}")
    print("-" * 64)
    for tracking, mac, onp, offp, onh, offh in scored:
        flag = "  <== camera-like" if tracking >= 0.66 and onh > 10 else ""
        print(f"{tracking:>6.2f} {mac:<20} {onp:>6} {offp:>7} "
              f"{onh:>7} {offh:>8}{flag}")

    print()
    print("Read: a MAC scoring near +1.0 with real ON hits, that tracked across")
    print("ALL rounds, is your device. Near 0 = random noise or always-on.")


if __name__ == "__main__":
    main()
