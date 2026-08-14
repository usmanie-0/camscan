"""Diagnostic round 3 -- steady trickle (not a cliff) suggests port contention:
another process draining COM5 at the same time. This test:

  1. Reports whether the port opens EXCLUSIVELY.
  2. Reads for 15s in one continuous loop (no reconfigure) and buckets by 5s,
     to see if a single clean reader sustains the flood.

Before running: close EVERY other terminal that touched COM5 -- all IDF
monitors, all other python windows. Ideally UNPLUG and REPLUG the board first
so any zombie handle is dropped, then run this as the ONLY thing using COM5.

Run:  python diag_probe3.py [COM5]
"""
import sys, time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM5"
BAUD = 115200

print(f"Opening {PORT} exclusively ...")
try:
    ser = serial.Serial(PORT, BAUD, timeout=0.2, exclusive=True)
except Exception as e:
    print("  EXCLUSIVE OPEN FAILED:", e)
    print("  -> Something else holds the port. Close it / replug, retry.")
    sys.exit(1)

print("  exclusive open OK -- no other process holds it at open time.")
ser.reset_input_buffer()

t0 = time.time(); bstart = t0
rb = 0; ln = 0; b = 1
total_lines = 0
while time.time() - t0 < 20:
    chunk = ser.read(4096)
    if chunk:
        rb += len(chunk); ln += chunk.count(b"\n"); total_lines += chunk.count(b"\n")
    if time.time() - bstart >= 5:
        print(f"  {b*5:2d}s: {rb:6d} bytes, {ln:4d} lines")
        rb = 0; ln = 0; bstart = time.time(); b += 1
ser.close()
print(f"\nTotal: {total_lines} lines in 20s.")
if total_lines > 400:
    print("=> HEALTHY. A single exclusive reader sustains the flood.")
    print("   The earlier starvation was port contention (another reader).")
elif total_lines < 80:
    print("=> Still starved even exclusively. Not contention; dig further.")
else:
    print("=> Partial. Paste this output.")
