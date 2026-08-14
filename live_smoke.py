"""Throwaway live smoke test for Step 1d.

Reads the C5's serial stream for ~15 seconds, prints each BLE (L) record as it
arrives, tallies unique advertisers and company IDs, then stops on its own.

Run from the project root:  python live_smoke.py  [COM5]

This is a diagnostic scratch script, not part of the package. Delete it after.
"""

import sys
import time
from collections import Counter

from camscan.rf.reader import read_from_serial
from camscan.rf.parser import BleLine

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM5"
SECONDS = 15

print(f"Reading {PORT} for {SECONDS}s ... (advertisements will scroll)")

seen_addrs = set()
mfr_counts = Counter()
n = 0
start = time.time()

try:
    for rec in read_from_serial(PORT):
        # Only care about BLE lines for this check.
        if isinstance(rec, BleLine):
            n += 1
            seen_addrs.add(rec.bdaddr)
            mfr_counts[rec.mfr_id] += 1
            print(f"  {rec.bdaddr}  {rec.rssi:>4} dBm  "
                  f"name={rec.name!r}  mfr={rec.mfr_id}")
        if time.time() - start > SECONDS:
            break
except KeyboardInterrupt:
    print("\n(interrupted)")

print()
print(f"--- {n} L records, {len(seen_addrs)} unique advertisers ---")
print("company IDs seen (None = no mfr data):")
for mfr, count in mfr_counts.most_common():
    print(f"  {mfr}: {count}")
