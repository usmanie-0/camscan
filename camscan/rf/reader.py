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
    send_first: Optional[str] = None,
) -> Iterator[str]:
    """Yield decoded text lines from a live serial port, forever.

    Imported lazily so the whole module -- and all file-based testing -- works
    even in an environment where pyserial isn't installed. Only this function
    needs it.

    ``send_first``: if given, this text is written to the board ONCE, right
    after the port is open and settled, before the read loop begins. It's how
    ``camscan hunt`` hands the firmware a ``HUNT,<mac>,<ch>`` target over the
    SAME held-open handle -- honouring the one-process-holds-COM5 rule, since
    the write and the read share this single open port. A trailing newline is
    added if absent (the firmware reads whole lines with fgets).
    """
    import serial  # pyserial; lazy so file replay never requires it

    ser = _open_board(port, baud, timeout)

    # Hand the board its command (e.g. a HUNT target) on this same open handle,
    # BEFORE the read loop, so we don't need a second process/opener. We flush
    # the OUTPUT buffer but deliberately do NOT reset the INPUT buffer again --
    # that would discard the board's first replies to the command.
    if send_first is not None:
        line = send_first if send_first.endswith("\n") else send_first + "\n"
        ser.write(line.encode("ascii", errors="replace"))
        ser.flush()

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


def _open_board(port: str, baud: int, timeout: float):
    """Open the C5 without resetting it, returning the held serial handle.

    One source of truth for the CH343 open-discipline. On this devkit the
    CH343's DTR/RTS are wired to EN/IO0, so a normal open pulses them and
    reboots the ESP32 -- short-lived opens kept catching only a boot's tail and
    then sat through the next boot's silence. The cure (proven by measurement):
    disable hardware/DTR flow control, force both lines low so the open pulse
    doesn't trigger a reset, then hold the port open for the whole session.
    Caller owns the returned handle and must close it.
    """
    import serial  # pyserial; lazy so file replay never requires it

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
    return ser




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


def hunt_on_serial(
    mac: str,
    channel: int,
    port: Optional[str] = None,
    baud: int = DEFAULT_BAUD,
    timeout: float = DEFAULT_TIMEOUT,
) -> Iterator[object]:
    """Send the firmware a hunt target, then stream records from the same port.

    Opens the board once (reusing the CH343 open-discipline), writes a single
    ``HUNT,<mac>,<ch>`` command so the firmware parks on ``channel`` and meters
    ``mac``, then yields the record stream (H lines and the occasional S) over
    that same held-open handle. One process, one open port -- the write and the
    read never contend, so the one-process-holds-COM5 rule is respected.

    ``mac`` is normalised to lower-case colon form; the firmware parser accepts
    any case but we keep the wire format tidy. ``channel`` is 1..165; the
    firmware infers band (<=14 => 2G, else 5G).
    """
    resolved = resolve_port(port)
    command = f"HUNT,{mac.lower()},{int(channel)}"
    yield from read_records(
        _iter_serial_lines(resolved, baud, timeout, send_first=command)
    )


class HuntSession:
    """One held-open serial session for multi-command orchestration.

    ``camscan hunt --all`` needs to drive the board through several phases over
    ONE open port: send ``SCAN``, collect D-lines for a while, then send
    ``HUNT,<mac>,<ch>`` for each candidate in turn. Reopening between phases
    would reset nothing (CH343 discipline) but would repeatedly contend for the
    single COM handle; holding one session avoids that and matches the
    one-process-holds-COM5 rule. Use as a context manager::

        with HuntSession() as s:
            s.send("SCAN")
            for rec in s.records(seconds=15): ...
            s.send("HUNT,<mac>,<ch>")
            for rec in s.records(): ...   # unbounded until break/KeyboardInterrupt

    The parsing is identical to the generator path -- same parse_line, same
    skip-None behaviour -- so records are the familiar dataclasses.
    """

    def __init__(self, port: Optional[str] = None,
                 baud: int = DEFAULT_BAUD, timeout: float = DEFAULT_TIMEOUT):
        self._port = resolve_port(port)
        self._baud = baud
        self._timeout = timeout
        self._ser = None

    def __enter__(self):
        self._ser = _open_board(self._port, self._baud, self._timeout)
        return self

    def __exit__(self, *exc):
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None
        return False

    def send(self, command: str) -> None:
        """Write one command line to the board (newline added if absent)."""
        line = command if command.endswith("\n") else command + "\n"
        self._ser.write(line.encode("ascii", errors="replace"))
        self._ser.flush()

    def _lines(self):
        while True:
            raw = self._ser.readline()
            if not raw:
                yield None            # timeout tick -- lets bounded reads exit
                continue
            yield raw.decode("utf-8", errors="replace")

    def records(self, seconds: Optional[float] = None):
        """Yield parsed records, optionally stopping after ``seconds``.

        ``seconds=None`` streams until the caller breaks out (or Ctrl-C). A
        finite value bounds the phase -- used to collect a survey window before
        moving on. Timeout ticks (empty reads) advance the clock but yield
        nothing, so a quiet board still lets a bounded read finish on schedule.
        """
        import time
        deadline = None if seconds is None else time.monotonic() + seconds
        for line in self._lines():
            if deadline is not None and time.monotonic() >= deadline:
                return
            if line is None:
                continue              # timeout tick; clock already advanced
            rec = parse_line(line)
            if rec is not None:
                yield rec
