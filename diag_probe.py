"""Instrumented serial diagnostic -- measure the stream, don't theorize.

Opens the port the SAME way reader.py does (DTR/RTS set), then reports raw
byte and line counts in 5-second buckets for 30s. This shows whether the board
is silent early (reset-on-open) and wakes up later, or genuinely starved.

Run:  python diag_probe.py [COM5]
"""
import sys, time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM5"
BAUD = 115200

# --- Test A: open WITHOUT touching DTR/RTS (avoid triggering a board reset) ---
print("=== Test A: open with DTR/RTS LEFT ALONE (no reset) ===")
ser = serial.Serial()
ser.port = PORT
ser.baudrate = BAUD
ser.timeout = 0.2
# Explicitly DO NOT set dtr/rts before open. On some drivers you must set them
# to False to avoid the reset pulse; we try 'leave default' first.
ser.open()
# Some boards need DTR/RTS deasserted to run instead of holding in reset:
ser.setDTR(False)
ser.setRTS(False)
ser.reset_input_buffer()

t0 = time.time()
bucket_start = t0
raw_bytes = 0
lines = 0
bucket = 1
while time.time() - t0 < 30:
    chunk = ser.read(4096)  # raw bytes, not line-oriented
    if chunk:
        raw_bytes += len(chunk)
        lines += chunk.count(b"\n")
    if time.time() - bucket_start >= 5:
        print(f"  {bucket*5:2d}s: {raw_bytes:6d} bytes, {lines:4d} lines "
              f"(this 5s window)")
        raw_bytes = 0
        lines = 0
        bucket_start = time.time()
        bucket += 1
ser.close()
print("Test A done.\n")
