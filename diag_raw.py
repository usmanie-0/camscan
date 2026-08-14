"""Print RAW serial text for 20s so we can SEE what the board sends on open.

Key question: do we see ONE boot banner then a flood (good), or REPEATED boot
banners / download-mode messages (the board is reset-looping on open)?

Also tries the one thing not yet tested: holding the port open and NOT resetting
the input buffer, printing everything verbatim including boot log.

  python diag_raw.py [COM5]
"""
import sys, time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM5"
BAUD = 115200

ser = serial.Serial()
ser.port = PORT
ser.baudrate = BAUD
ser.timeout = 0.2
ser.dsrdtr = False
ser.rtscts = False
try:
    ser.dtr = False
    ser.rts = False
except Exception:
    pass
ser.open()
try:
    ser.setDTR(False); ser.setRTS(False)
except Exception:
    pass

print("=== RAW dump for 20s (watch for repeated 'ESP-ROM' / 'boot:' banners) ===")
t0 = time.time()
buf = b""
boot_banners = 0
l_lines = 0
while time.time() - t0 < 20:
    c = ser.read(4096)
    if not c:
        continue
    buf += c
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        text = line.decode("utf-8", errors="replace").rstrip("\r")
        if "ESP-ROM" in text or "boot:" in text or "rst:" in text:
            boot_banners += 1
            print(f"[BOOT] {text}")
        elif text.startswith("L,"):
            l_lines += 1
            if l_lines <= 3 or l_lines % 50 == 0:
                print(f"[L#{l_lines}] {text}")
        elif "waiting for download" in text.lower() or "DOWNLOAD" in text:
            print(f"[DOWNLOAD-MODE] {text}")
        else:
            # other firmware log lines
            if text.strip():
                print(f"[log] {text[:70]}")
ser.close()
print(f"\n=== boot banners seen: {boot_banners} | L lines: {l_lines} ===")
if boot_banners > 1:
    print("=> Board is RESET-LOOPING on open. The Windows open pulse resets it")
    print("   repeatedly. Fix: prevent the DTR/RTS pulse at the driver level.")
elif boot_banners == 1 and l_lines > 100:
    print("=> ONE boot then flood = healthy. Whatever reset it, it recovered.")
elif l_lines == 0 and boot_banners == 0:
    print("=> Silence: board may be stuck in download mode from prior toggling.")
    print("   Power-cycle (unplug/replug) and rerun.")
