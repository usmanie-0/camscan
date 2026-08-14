"""Capture a fixed BLE window and print a stable per-MAC summary.

Built for the power-cycle identification test: run it with a camera ON, run it
again with that camera OFF, and diff the two summaries. The MAC that vanishes
(or drops to near-zero hits) when a specific camera loses power IS that camera.

Unlike live_smoke.py, this doesn't scroll -- it collects for N seconds, then
prints one line per advertiser sorted by strongest RSSI, so two runs line up
visually and by MAC.

Run from the project root:

    python ble_survey.py [seconds] [COM5] [--save label]

Examples:
    python ble_survey.py 20                 # 20s survey on COM5
    python ble_survey.py 20 COM5 --save on  # also writes survey_on.txt
    python ble_survey.py 20 COM5 --save off # (after unplugging a camera)
"""

import sys
import time
from collections import defaultdict

from camscan.rf.reader import read_from_serial, resolve_port
from camscan.rf.parser import BleLine


def parse_args(argv):
    seconds = 20
    port = None          # None -> auto-detect the CH343
    label = None
    positional = []
    i = 0
    while i < len(argv):
        if argv[i] == "--save":
            label = argv[i + 1]
            i += 2
        else:
            positional.append(argv[i])
            i += 1
    if len(positional) >= 1:
        seconds = int(positional[0])
    if len(positional) >= 2:
        port = positional[1]
    return seconds, port, label


def main():
    seconds, port, label = parse_args(sys.argv[1:])

    # Resolve now (auto-detect if not given) so we can show the port and fail
    # loudly before starting a 20s timer against a nonexistent device.
    try:
        port = resolve_port(port)
    except RuntimeError as e:
        print(e)
        return

    print(f"Surveying {port} for {seconds}s ... hold still, don't move the board.")

    # Per-MAC accumulator: hit count, best (max) RSSI, last name, last mfr.
    stats = defaultdict(lambda: {"hits": 0, "best": -999, "name": None, "mfr": None})

    start = time.time()
    try:
        for rec in read_from_serial(port):
            if isinstance(rec, BleLine):
                s = stats[rec.bdaddr]
                s["hits"] += 1
                if rec.rssi > s["best"]:
                    s["best"] = rec.rssi
                if rec.name is not None:
                    s["name"] = rec.name
                if rec.mfr_id is not None:
                    s["mfr"] = rec.mfr_id
            if time.time() - start > seconds:
                break
    except KeyboardInterrupt:
        print("\n(interrupted early)")

    # Sort by strongest RSSI first -- closest things at the top.
    rows = sorted(stats.items(), key=lambda kv: kv[1]["best"], reverse=True)

    lines = []
    header = f"{'MAC':<20} {'best':>5} {'hits':>5}  {'mfr':<5} name"
    lines.append(header)
    lines.append("-" * len(header))
    for mac, s in rows:
        mfr = s["mfr"] or "-"
        name = s["name"] or ""
        lines.append(f"{mac:<20} {s['best']:>5} {s['hits']:>5}  {mfr:<5} {name}")

    out = "\n".join(lines)
    print()
    print(out)
    print(f"\n{len(rows)} advertisers in {seconds}s.")

    if label:
        fname = f"survey_{label}.txt"
        with open(fname, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
        print(f"saved -> {fname}")


if __name__ == "__main__":
    main()
