"""Diagnostic round 5 -- test the reset-on-open theory.

Theory: each pyserial open toggles DTR/RTS (wired to EN/IO0 on the CH343) and
RESETS the ESP32. Short read windows only catch a boot's tail, then sit through
the next boot. The monitor floods because it opens ONCE and holds.

This opens ONCE, suppresses DTR/RTS reset as hard as pyserial allows, and reads
continuously for 30s, bucketed by 5s. If resets were the cause, after the final
open-reset the board boots ~1s in and then floods for the remaining ~29s.

RUN ON A FRESHLY REPLUGGED BOARD.  python diag_probe5.py [COM5]
"""
import sys, time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM5"
BAUD = 115200

# Suppress the auto-reset: set control lines FALSE before open where possible.
ser = serial.Serial()
ser.port = PORT
ser.baudrate = BAUD
ser.timeout = 0.2
ser.dsrdtr = False
ser.rtscts = False
# Pre-set lines low so the open pulse is minimized.
try:
    ser.dtr = False
    ser.rts = False
except Exception:
    pass

ser.open()

# After open, force lines low again and DO NOT touch them further.
try:
    ser.setDTR(False)
    ser.setRTS(False)
except Exception:
    pass

ser.reset_input_buffer()

print("Reading continuously for 30s (board should boot ~1s in, then flood)...")
t0 = time.time(); bstart = t0
rb = 0; ln = 0; b = 1; total = 0
while time.time() - t0 < 30:
    c = ser.read(4096)
    if c:
        rb += len(c); ln += c.count(b"\n"); total += c.count(b"\n")
    if time.time() - bstart >= 5:
        print(f"  {b*5:2d}s: {rb:6d} bytes, {ln:4d} lines")
        rb = 0; ln = 0; bstart = time.time(); b += 1
ser.close()

print(f"\nTotal: {total} lines in 30s.")
if total > 500:
    print("=> HEALTHY held-open stream. Fix = open once, no DTR/RTS, hold it.")
    print("   Later buckets should be full even if the first is sparse (boot).")
else:
    print("=> Still dead held-open. Paste full output; next we probe whether the")
    print("   console is actually on USB-Serial/JTAG, not this CH343 UART at all.")
