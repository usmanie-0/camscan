"""Read the ESP32-C5 serial stream and yield parsed records.

This sits on top of ``parser.parse_line``. Its whole job is to be the source of
lines -- from a live serial port in the field, or from a captured text file
during development -- and to hand each parsed record to the caller as it
arrives. Parsing logic lives in ``parser``; I/O and framing live here.

The reader is a GENERATOR. The caller writes::

    for record in read_records(source):
        route(record)          # dispatch by type to scoring, storage, etc.

and never has to know or care whether ``source`` was a file or a COM port --
the two are the same loop with a different line source underneath. That's the
point: every line of test we run against a captured file is exercising the
exact code path that will later run against the board.

Records are the dataclasses from ``parser`` (DeviceLine, ChannelLine,
StatusLine, BleLine, CamHintLine). Unparseable lines (boot-log noise, partial
reads) are silently skipped -- ``parse_line`` already returns ``None`` for
those, and the reader just doesn't yield them.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Optional

from .parser import parse_line


# Default matches CONFIG_ESP_CONSOLE_UART_BAUDRATE in the firmware's sdkconfig.
# On the C5's USB Serial/JTAG console the rate is often effectively ignored,
# but we set the correct value anyway so a classic-UART path also works.
DEFAULT_BAUD = 115200

# How long a blocking serial read waits before returning empty-handed, so the
# loop stays responsive to Ctrl-C and to a board that's gone quiet. Seconds.
DEFAULT_TIMEOUT = 1.0

# The C5 devkit's USB-UART bridge is a WCH CH343: USB VID:PID 1A86:55D3.
# We match on VID:PID so detection doesn't depend on the COM number, which
# isn't stable across replugs/resets on this board.
CH343_VID = 0x1A86
CH343_PID = 0x55D3


def find_port() -> Optional[str]:
    """Return the COM port of the attached C5 board, or ``None`` if not found.

    Matches the CH343 bridge by USB VID:PID rather than by COM number -- the
    number moves when the board re-enumerates, but the chip ID doesn't. If more
    than one CH343 is somehow present, the first is returned.

    Imported lazily so file-only use never needs pyserial installed.
    """
    from serial.tools import list_ports

    for p in list_ports.comports():
        if p.vid == CH343_VID and p.pid == CH343_PID:
            return p.device
    return None


def resolve_port(port: Optional[str]) -> str:
    """Return an explicit port, or auto-detect one, or raise a clear error.

    ``port`` given -> use it verbatim. ``port`` None -> auto-detect the CH343.
    Raises ``RuntimeError`` with an actionable message if nothing is found, so
    the tools fail loudly instead of opening a nonexistent port.
    """
    if port:
        return port
    found = find_port()
    if found is None:
        raise RuntimeError(
            "No C5 board found (looked for a CH343 USB-UART bridge). "
            "Check the cable is seated and is a data cable, then retry. "
            "You can also pass an explicit port, e.g. COM5."
        )
    return found


def read_records(lines: Iterable[str]) -> Iterator[object]:
    """Yield one parsed record per parseable line in ``lines``.

    This is the core loop and the ONLY place parsing is driven from. It takes
    any iterable of strings -- a file object, a list, a generator draining a
    serial port -- and yields the non-``None`` results of ``parse_line``.

    Keeping this decoupled from the line *source* is what lets the same loop
    serve both file replay and live capture.
    """
    for line in lines:
        record = parse_line(line)
        if record is not None:
            yield record


def read_from_file(path: str) -> Iterator[object]:
    """Replay a captured serial log from a text file, yielding records.

    Use this to develop and test the whole pipeline with no board attached:
    point it at a saved monitor capture (like the ones from the lab) and it
    produces the identical record stream the live port would.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        # ``fh`` iterates line by line; hand that straight to the core loop.
        yield from read_records(fh)


def _iter_serial_lines(
    port: str,
    baud: int,
    timeout: float,
) -> Iterator[str]:
    """Yield decoded text lines from a live serial port, forever.

    Imported lazily so the whole module -- and all file-based testing -- works
    even in an environment where pyserial isn't installed. Only this function
    needs it.
    """
    import serial  # pyserial; lazy so file replay never requires it

    # Open WITHOUT resetting the board. On this devkit the CH343's DTR/RTS are
    # wired to EN/IO0, so a normal open pulses them and reboots the ESP32 --
    # which meant short-lived opens kept catching only a boot's tail and then
    # sat through the next boot's silence. The cure (proven by measurement):
    # disable hardware/DTR flow control, force both lines low so the open pulse
    # doesn't trigger a reset, then hold the port open for the whole session.
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = timeout
    ser.dsrdtr = False
    ser.rtscts = False
    ser.xonxoff = False
    try:
        ser.dtr = False
        ser.rts = False
    except Exception:
        pass

    ser.open()

    # Re-assert low after open and never touch these lines again, so nothing
    # mid-session resets the board.
    try:
        ser.setDTR(False)
        ser.setRTS(False)
    except Exception:
        pass

    ser.reset_input_buffer()

    # Read raw lines directly (the approach that streams steadily here). No
    # TextIOWrapper: its block buffering stalled under the flood.
    try:
        while True:
            raw = ser.readline()          # bytes, up to and including b"\n"
            if not raw:
                continue                   # timeout tick, nothing waiting
            yield raw.decode("utf-8", errors="replace")
    finally:
        ser.close()


def read_from_serial(
    port: Optional[str] = None,
    baud: int = DEFAULT_BAUD,
    timeout: float = DEFAULT_TIMEOUT,
) -> Iterator[object]:
    """Stream records from a live serial port until interrupted.

    ``port`` may be an explicit name (e.g. "COM5") or ``None`` to auto-detect
    the attached C5 by its CH343 bridge. Same record stream as
    ``read_from_file``, sourced from the board. The generator runs
    indefinitely; the caller ends it by breaking out of the loop or letting a
    KeyboardInterrupt propagate.
    """
    resolved = resolve_port(port)
    yield from read_records(_iter_serial_lines(resolved, baud, timeout))
