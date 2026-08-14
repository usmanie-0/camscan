"""Diagnostic round 4 -- the decisive test.

Hypothesis: the console only streams after a specific DTR/RTS TRANSITION
sequence at open (what the IDF monitor does), not any static level. A fresh
replug clears this; a plain pyserial open never re-arms it.

This reproduces esptool/idf_monitor's classic reset sequence, then reads.

RUN THIS ON A FRESHLY REPLUGGED BOARD (no monitor opened since replug), so we
know the arming comes from THIS script, not a prior monitor.

  python diag_probe4.py [COM5]
"""
import sys, time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM5"
BAUD = 115200

def read_burst(ser, secs, tag):
    ser.reset_input_buffer()
    t0 = time.time(); n = 0
    while time.time() - t0 < secs:
        c = ser.read(4096)
        if c: n += c.count(b"\n")
    print(f"    [{tag}] {n} lines in {secs}s")
    return n

ser = serial.Serial()
ser.port = PORT
ser.baudrate = BAUD
ser.timeout = 0.2
ser.open()

print("Baseline read (no handshake):")
read_burst(ser, 5, "baseline")

# --- Classic ESP reset handshake (esptool "classic_reset") ---
# DTR->EN(reset), RTS->GPIO0(boot). Toggle to reset into RUN (not download).
print("Performing classic reset-into-run handshake ...")
ser.setDTR(False); ser.setRTS(True)   # EN low via inverter, IO0 held
time.sleep(0.1)
ser.setDTR(True);  ser.setRTS(False)  # release into run
time.sleep(0.1)
ser.setDTR(False)                     # settle
time.sleep(0.5)  # let it boot

print("Read AFTER handshake (expect boot banner + L flood if this is it):")
n = read_burst(ser, 8, "post-reset")

ser.close()
print()
if n > 100:
    print("=> FIX FOUND: the reset handshake arms the console. Bake it into reader.py.")
elif n > 10:
    print("=> Partial arming. Paste output.")
else:
    print("=> Handshake didn't arm it. Paste output; we try the USB-JTAG path next.")
