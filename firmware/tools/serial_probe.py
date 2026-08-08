"""
serial_probe.py - Phase 3 serial-link proof for camscan firmware.

Opens the ESP32-C5's serial port, reads line-based CSV, and parses the
'S' status records defined by the firmware serial contract:

    S,<uptime_ms>,<free_heap>,<frames_dropped>

This is a THROWAWAY PROBE to prove the laptop can read the firmware's
protocol cleanly. It is NOT the Phase 5 ingest (that lives in camscan/rf/).

Usage:
    py -3.11 serial_probe.py            # defaults to COM5 at 115200
    py -3.11 serial_probe.py COM6       # override port
    py -3.11 serial_probe.py COM5 921600

Press Ctrl+C to stop.

Requires the camscan venv (pyserial is already installed there).
Remember: only ONE program can hold COM5. Close the ESP-IDF monitor first.
"""

import sys
import time

try:
    import serial  # pyserial: installs as 'pyserial', imports as 'serial'
except ImportError:
    print("ERROR: pyserial not found. Activate the camscan venv first:")
    print(r"  D:\projects\camscan\.venv\Scripts\Activate.ps1")
    sys.exit(1)

# The firmware console runs at 115200 baud by default (seen in idf.py monitor).
DEFAULT_PORT = "COM5"
DEFAULT_BAUD = 115200


def parse_s_line(line):
    """Parse one 'S' status record into a dict, or return None if it isn't
    a well-formed S line. We validate rather than trust: the same stream
    carries boot-log lines and, later, other record types."""
    parts = line.split(",")
    if len(parts) != 4 or parts[0] != "S":
        return None
    try:
        return {
            "uptime_ms": int(parts[1]),
            "free_heap": int(parts[2]),
            "frames_dropped": int(parts[3]),
        }
    except ValueError:
        # A line that starts with 'S,' but has non-integer fields is
        # corrupt (or a partial read) - skip it rather than crash.
        return None


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BAUD

    print(f"Opening {port} at {baud} baud... (Ctrl+C to stop)")
    try:
        # timeout=1 => readline() returns at most once per second even if
        # no newline arrives, so Ctrl+C stays responsive.
        ser = serial.Serial(port, baud, timeout=1)
    except serial.SerialException as e:
        print(f"ERROR opening {port}: {e}")
        print("Is the ESP-IDF monitor still open on this port? Close it "
              "(Ctrl+]) and retry.")
        sys.exit(1)

    # Counters so we can report signal-vs-noise at the end.
    total_lines = 0
    s_lines = 0
    other_lines = 0
    last_uptime = None

    try:
        with ser:
            while True:
                # readline() reads bytes up to a newline. decode with
                # errors='replace' so a stray corrupt byte doesn't crash us.
                raw = ser.readline()
                if not raw:
                    continue  # timeout with no data; loop and stay responsive

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                total_lines += 1
                rec = parse_s_line(line)

                if rec is None:
                    # Not an S line: boot log, banner, etc. Show it dimly
                    # so you can see the stream is mixed, as expected.
                    other_lines += 1
                    print(f"  [non-S] {line}")
                    continue

                s_lines += 1

                # Sanity check: uptime should increase monotonically. If it
                # jumps backwards, the board reset (or we lost sync).
                delta = ""
                if last_uptime is not None:
                    d = rec["uptime_ms"] - last_uptime
                    delta = f"  (+{d} ms)"
                    if d < 0:
                        delta = f"  (RESET? uptime went backwards by {-d} ms)"
                last_uptime = rec["uptime_ms"]

                print(
                    f"S  uptime={rec['uptime_ms']:>8} ms  "
                    f"free_heap={rec['free_heap']:>7} B  "
                    f"dropped={rec['frames_dropped']}{delta}"
                )

    except KeyboardInterrupt:
        print("\n--- stopped ---")
        print(f"total lines: {total_lines}   "
              f"S lines: {s_lines}   non-S lines: {other_lines}")
        if s_lines:
            print("Serial link OK: firmware S records parsed successfully.")


if __name__ == "__main__":
    main()
