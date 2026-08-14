"""Diagnostic round 2 -- the board is healthy (monitor floods 30s+), so the
stall is on the pyserial reading side. Prime suspect: flow control leaving the
CH343 gated after the initial buffer drains.

Tests three open configurations, 15s each, bucketed by 5s, so we see which
keeps the stream alive past the ~5s cliff.

Run:  python diag_probe2.py [COM5]
"""
import sys, time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM5"
BAUD = 115200

def run(label, configure):
    print(f"=== {label} ===")
    ser = serial.Serial()
    ser.port = PORT
    ser.baudrate = BAUD
    ser.timeout = 0.2
    configure(ser)
    ser.open()
    ser.reset_input_buffer()
    t0 = time.time()
    bstart = t0
    rb = 0; ln = 0; b = 1
    while time.time() - t0 < 15:
        chunk = ser.read(4096)
        if chunk:
            rb += len(chunk); ln += chunk.count(b"\n")
        if time.time() - bstart >= 5:
            print(f"  {b*5:2d}s: {rb:6d} bytes, {ln:4d} lines")
            rb = 0; ln = 0; bstart = time.time(); b += 1
    ser.close()
    print()

def cfg_flow_off(ser):
    # Explicitly disable ALL flow control, hold lines in run state.
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.rts = True
    ser.dtr = True

def cfg_flow_off_lines_low(ser):
    # Flow control off, but DTR/RTS deasserted (some CH343 setups need this).
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.rts = False
    ser.dtr = False

def cfg_bigbuf(ser):
    # Flow off + a large driver buffer request, in case it's buffer size.
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.rts = True
    ser.dtr = True
    try:
        ser.set_buffer_size(rx_size=65536, tx_size=4096)
    except Exception as e:
        print("  (set_buffer_size not supported:", e, ")")

run("Test 1: flow control OFF, DTR/RTS high", cfg_flow_off)
run("Test 2: flow control OFF, DTR/RTS low", cfg_flow_off_lines_low)
run("Test 3: flow off + 64KB rx buffer", cfg_bigbuf)
print("If any test streams steadily past 10s, that config is the fix.")
