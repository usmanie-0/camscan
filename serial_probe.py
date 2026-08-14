"""Raw serial probe -- bypasses the reader to test the CH343 link directly.

Opens the port two ways and counts raw lines for 5s each:
  Pass 1: plain open (what the reader does now)
  Pass 2: open + explicit DTR/RTS + reset input buffer (the CH343 fix)

If Pass 2 floods and Pass 1 trickles, the fix belongs in reader.py.

Run:  python serial_probe.py [COM5]
"""
import sys, time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM5"
BAUD = 115200

def count(label, setup):
    ser = serial.Serial()
    ser.port = PORT
    ser.baudrate = BAUD
    ser.timeout = 0.5
    setup(ser)
    ser.open()
    ser.reset_input_buffer()
    n = 0
    start = time.time()
    while time.time() - start < 5:
        raw = ser.readline()
        if raw:
            n += 1
    ser.close()
    print(f"  {label}: {n} lines in 5s")
    return n

def plain(ser):
    pass

def with_lines(ser):
    # CH343 sometimes needs DTR/RTS asserted and buffers reset.
    ser.dtr = True
    ser.rts = True

print(f"Probing {PORT} ...")
p1 = count("plain open        ", plain)
time.sleep(0.5)
p2 = count("DTR/RTS + reset   ", with_lines)
print()
if p2 > p1 * 3 and p2 > 20:
    print("=> Pass 2 much better: the fix is DTR/RTS handling in reader.py")
elif p1 > 20 and p2 > 20:
    print("=> Both fine now: earlier trickle was a transient; board is healthy")
elif p1 < 5 and p2 < 5:
    print("=> Both dead: board/cable/firmware problem, not the Python")
else:
    print("=> Mixed result -- paste this output")
