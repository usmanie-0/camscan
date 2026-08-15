"""Live proximity meter for hunt mode (Phase 6, Step 3).

Reads the firmware's ``H`` lines through ``reader.read_from_serial`` (so the
CH343 open-discipline is reused, never a naive ``serial.Serial``), smooths the
raw RSSI with an EMA, and shows a live PLAIN-ENGLISH "hot / cold" hunt: how
close you are (RIGHT HERE / CLOSE / WARM / FAR) and, more importantly, which
way you're moving (WARMER / COLDER) as you walk. It's a Geiger counter for one
MAC -- the field step that tells "camera in MY room" from "neighbour's,
bleeding through the wall".

The signal engineering is unchanged and still runs underneath; we just say it
in words instead of dBm. The raw number stays available as a small "details"
line for when you want it.

Why the smoothing/hold/decay shape (validated against a real near->far->near
capture):
  * Raw RSSI jumps several dB sample-to-sample (multipath, body blocking,
    antenna orientation). An EMA turns that jitter into a usable needle.
  * The firmware emits an H line even when the target was SILENT that window
    (frames==0). We must NOT fold that -0 into the average -- it would crash
    the needle every other tick, because a phone's traffic is bursty. On a
    silent window we HOLD the last smoothed value instead.
  * Holding forever would lie: if you walk out of range or the target stops
    transmitting, the meter should say so. After DECAY_MS unheard it reads
    LOST -- honest about the loss, not a frozen "RIGHT HERE".

The direction word (WARMER/COLDER) compares the smoothed value now against a
few seconds ago; it needs a little history to be sure, so it lags proximity by
a beat -- deliberately, since calling every jitter a direction would be worse
than a moment's lag.

NOTE (Phase 6, Step 3): the target MAC comes from ``--mac`` for standalone
testing, and the firmware is still built with a hardcoded HUNT_MAC. Step 4
wires this under ``camscan hunt <mac>`` and makes the firmware target
runtime-selectable so any candidate from a scan can be hunted without a
reflash.
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .parser import HuntLine, DeviceLine
from .reader import read_from_serial, read_from_file, hunt_on_serial, HuntSession
from .consolidate import consolidate


# --- Tunables ---------------------------------------------------------------
EMA_ALPHA = 0.35          # smoothing: higher = snappier/noisier, lower = smoother

# Proximity buckets, in smoothed dBm. Anchored to observed hunt data: the
# target read ~-15 at the board and fell past -55 across the room.
PROX_RIGHT_HERE = -25
PROX_CLOSE = -38
PROX_WARM = -50
PROX_FAR = -65
# below PROX_FAR => "VERY FAR"

# Direction: compare now vs this many samples ago; ignore changes below the
# threshold so jitter isn't reported as motion.
TREND_WINDOW = 8
TREND_MIN_DB = 2.0

# After this long with no frames from the target, the target is "lost".
DECAY_MS = 2000

# Activity flag. We track frames-per-second from the target (smoothed) so we
# can tell a real stream from a faint background trickle. An idle but present
# device (a phone not on a call, a camera beaconing but not streaming) still
# emits a few frames now and then -- the proximity reading off those is REAL,
# but low-confidence, so we SHOW it and FLAG it rather than hide it. Derived
# from real data: a live call ran ~20 frames/s (>=2 frames every 200ms window,
# every window); idle trickle sat well under 3 frames/s with mostly-silent
# windows. WINDOW_S is the firmware's emit period.
WINDOW_S = 0.2
FPS_ALPHA = 0.2           # smoothing for the frames/sec estimate (slow = steady)
FPS_STRONG = 15.0         # >= this, SUSTAINED, is a real stream (full confidence)
FPS_FAINT = 5.0           # <  this: a faint trickle, flag it hard
FPS_SUSTAIN = 5           # windows (~1s) fps must stay >=STRONG before we trust
                          # it -- stops a brief idle sync-burst reading as a
                          # stream. Tuned so a real call clears it (~90%) but
                          # lumpy background idle never does.

BAR_WIDTH = 40
RSSI_FLOOR = -90          # bar scale only
RSSI_CEIL = -10


@dataclass
class MeterState:
    ema: Optional[float] = None
    last_rssi: Optional[int] = None
    lost: bool = False
    n_heard: int = 0
    closest: Optional[int] = None     # least-negative (strongest) seen
    farthest: Optional[int] = None    # most-negative (weakest) seen
    history: Deque[float] = field(default_factory=lambda: deque(maxlen=TREND_WINDOW))
    fps: float = 0.0                  # smoothed frames/sec from the target
    fps_sustain: int = 0              # consecutive windows fps has been >=STRONG


def update(st: MeterState, h: HuntLine) -> None:
    """Fold one H sample in. frames>0 => real reading; frames==0 => hold."""
    # Frames/sec is updated EVERY window -- including silent ones -- so an idle
    # or departed target sees its rate decay toward zero, not freeze. This is
    # what separates "streaming" from "faint trickle" and drives the flag.
    inst_fps = h.frames / WINDOW_S
    st.fps = FPS_ALPHA * inst_fps + (1.0 - FPS_ALPHA) * st.fps
    # Sustained-stream counter: fps must stay high for FPS_SUSTAIN windows
    # before activity_words will call it "strong". A one-window idle burst
    # bumps fps but resets here the moment it drops, so it can't clear FAINT.
    if st.fps >= FPS_STRONG:
        st.fps_sustain += 1
    else:
        st.fps_sustain = 0

    if h.frames > 0:
        st.last_rssi = h.rssi
        st.n_heard += 1
        if st.ema is None:
            st.ema = float(h.rssi)
        else:
            st.ema = EMA_ALPHA * h.rssi + (1.0 - EMA_ALPHA) * st.ema
        st.closest = h.rssi if st.closest is None else max(st.closest, h.rssi)
        st.farthest = h.rssi if st.farthest is None else min(st.farthest, h.rssi)
        st.history.append(st.ema)
        st.lost = False
    else:
        st.lost = h.age_ms >= DECAY_MS


def proximity_words(ema: Optional[float]) -> str:
    if ema is None:
        return "SEARCHING…"
    if ema >= PROX_RIGHT_HERE:
        return "RIGHT HERE"
    if ema >= PROX_CLOSE:
        return "CLOSE"
    if ema >= PROX_WARM:
        return "GETTING WARM"
    if ema >= PROX_FAR:
        return "FAR"
    return "VERY FAR"


def trend_words(st: MeterState) -> str:
    if st.ema is None or len(st.history) < TREND_WINDOW:
        return ""
    delta = st.ema - st.history[0]     # + = less negative = closer = warmer
    if delta >= TREND_MIN_DB:
        return "WARMER  ↑  getting closer"
    if delta <= -TREND_MIN_DB:
        return "COLDER  ↓  moving away"
    return "steady — try another direction"


def activity_words(st: MeterState) -> tuple:
    """Return (label, style) describing how much traffic the reading rides on.

    A strong stream => trust the proximity fully. A faint trickle => the
    proximity is real but low-confidence (could read FAR because the device is
    quiet, not because it's distant), so we say so plainly.
    """
    if st.ema is None:
        return ("", "grey42")
    if st.fps_sustain >= FPS_SUSTAIN:
        return ("● strong signal — solid reading", "green")
    if st.fps >= FPS_FAINT:
        return ("◐ some traffic — fairly reliable", "yellow")
    return ("○ FAINT — barely transmitting, low confidence", "bold orange1")


def _prox_style(ema: Optional[float], lost: bool) -> str:
    if lost or ema is None:
        return "grey42"
    if ema >= PROX_RIGHT_HERE:
        return "bright_red"
    if ema >= PROX_CLOSE:
        return "orange1"
    if ema >= PROX_WARM:
        return "yellow"
    return "green"


def _trend_style(text: str) -> str:
    if text.startswith("WARMER"):
        return "bold bright_green"
    if text.startswith("COLDER"):
        return "bold bright_red"
    return "grey58"


def _fraction(ema: Optional[float]) -> float:
    if ema is None:
        return 0.0
    return max(0.0, min(1.0, (ema - RSSI_FLOOR) / (RSSI_CEIL - RSSI_FLOOR)))


def _render(st: MeterState, h: Optional[HuntLine], target: str) -> Panel:
    color = _prox_style(st.ema, st.lost)

    if st.lost:
        prox = Text(" LOST — can't hear the target ", style="bold grey42")
    else:
        prox = Text(f"  {proximity_words(st.ema)}  ", style=f"bold {color}")

    tr = "" if st.lost else trend_words(st)
    trend = Text(tr, style=_trend_style(tr)) if tr else Text("")

    act_label, act_style = ("", "grey42") if st.lost else activity_words(st)
    activity = Text(act_label, style=act_style) if act_label else Text("")

    frac = _fraction(st.ema)
    filled = int(round(frac * BAR_WIDTH))
    bar = Text()
    bar.append("█" * filled, style=("grey42" if st.lost else color))
    bar.append("░" * (BAR_WIDTH - filled), style="grey30")

    if h is not None and h.frames > 0:
        raw = f"{h.rssi} dBm"
    else:
        raw = "—"
    ema_s = f"{st.ema:.0f}" if st.ema is not None else "—"
    details = Text(
        f"signal {ema_s} dBm   ·   now {raw}   ·   nearest {st.closest if st.closest is not None else '—'}"
        f"   ·   heard {st.n_heard}",
        style="grey42",
    )

    body = Group(
        Text(""),
        Align.center(prox),
        Text(""),
        Align.center(bar),
        Text(""),
        Align.center(trend),
        Align.center(activity),
        Text(""),
        Align.center(details),
    )
    ch = f" · ch {h.channel}" if h is not None else ""
    return Panel(body, title=f"[bold]Hunting[/bold] {target}{ch}",
                 subtitle="[grey42]walk around — follow WARMER · Ctrl-C to stop[/grey42]",
                 border_style=color, padding=(1, 3))


def run_meter(records, target_mac: str, refresh_hz: float = 10.0) -> None:
    st = MeterState()
    console = Console()
    want = target_mac.upper()
    last: Optional[HuntLine] = None

    with Live(_render(st, None, target_mac), console=console,
              refresh_per_second=refresh_hz, screen=False) as live:
        try:
            for rec in records:
                if not isinstance(rec, HuntLine):
                    continue
                if rec.mac.upper() != want:
                    continue
                update(st, rec)
                last = rec
                live.update(_render(st, last, target_mac))
        except KeyboardInterrupt:
            pass

    if st.n_heard:
        console.print(
            f"\n[bold]Done.[/bold] Nearest you got: {st.closest} dBm "
            f"(farthest {st.farthest}). Heard the target in {st.n_heard} windows."
        )
    else:
        console.print(
            f"\n[bold]Done.[/bold] Never heard {target_mac}. "
            f"Is it transmitting, and is the board parked on its channel?"
        )


def hunt_target(mac: str, channel: int, port: Optional[str] = None) -> None:
    """Send the board a hunt target and run the live plain-English meter.

    The single entry point the CLI's `hunt` subcommand calls: opens the port
    once (CH343 discipline via hunt_on_serial), tells the firmware to park on
    ``channel`` and meter ``mac``, and drives the gauge until Ctrl-C. All the
    serial/one-process discipline lives in reader.hunt_on_serial; all the
    smoothing/words/flag live in run_meter -- this just joins them.
    """
    records = hunt_on_serial(mac, channel, port=port)
    run_meter(records, mac)


# --- hunt --all: survey, shape candidates, walk each one --------------------

# A device is "camera-shaped" enough to walk automatically when it's close AND
# was heard with real uplink traffic. Deliberately loose -- this only decides
# the DEFAULT walk order; every device stays visible and huntable. Anchored to
# the same ground truth as scoring (a Pixel at the board read ~-15..-20).
SHAPE_RSSI_NEAR = -55       # closer than this is worth walking
SHAPE_MIN_UP = 1            # any uplink at all (survey windows are short)


def _is_camera_shaped(dev) -> bool:
    """Loosely flag a consolidated device as worth walking by default.

    Close enough to be in/near the room AND seen transmitting (uplink). This
    intentionally also catches phones-on-calls -- we SHOW those and let the
    user skip them, rather than hide a possible camera. rssi_avg==0 is the
    'no uplink seen' sentinel, so it never counts as near.
    """
    near = dev.rssi_avg != 0 and dev.rssi_avg >= SHAPE_RSSI_NEAR
    talks = dev.bytes_up >= SHAPE_MIN_UP
    return near and talks


def _survey_candidates(session, seconds: float, console) -> list:
    """Send SCAN, collect D-lines for a window, return consolidated devices.

    Sorted strongest-first. Reuses the real consolidate() so channels and
    bleed-merging match the scan pipeline exactly.
    """
    console.print(f"[bold]Surveying[/bold] for {int(seconds)}s — "
                  "finding devices and their channels…")
    session.send("SCAN")
    d_lines = [r for r in session.records(seconds=seconds)
               if isinstance(r, DeviceLine)]
    devices = consolidate(d_lines)
    # Strongest first; the 0 sentinel (no uplink) sorts weakest.
    devices.sort(key=lambda d: (float("-inf") if d.rssi_avg == 0 else d.rssi_avg),
                 reverse=True)
    return devices


def _print_candidates(devices, shaped, console) -> None:
    from rich.table import Table
    t = Table(title="Devices heard", show_lines=False)
    t.add_column("#", justify="right", width=3)
    t.add_column("MAC", width=18)
    t.add_column("Ch", justify="right", width=4)
    t.add_column("Signal", justify="right", width=8)
    t.add_column("Up/Down", width=12)
    t.add_column("", width=16)
    for i, d in enumerate(devices):
        is_shaped = d in shaped
        sig = "—" if d.rssi_avg == 0 else f"{d.rssi_avg} dBm"
        updown = f"{d.bytes_up}/{d.bytes_down}"
        tag = "[bold]will walk[/bold]" if is_shaped else "[grey42]also heard[/grey42]"
        num = str(shaped.index(d) + 1) if is_shaped else "·"
        style = "" if is_shaped else "grey42"
        t.add_row(num, d.mac, str(d.channel), sig, updown, tag, style=style)
    console.print(t)


def hunt_all(port: Optional[str] = None, survey_seconds: float = 15.0) -> None:
    """Survey, then walk each camera-shaped candidate one at a time.

    RF-only: everything comes from the board's own survey, so this works
    without being on the LAN. Camera-shaped devices are walked by default;
    every other device is shown too (never hidden) and can be hunted by number.
    """
    from rich.console import Console
    console = Console()

    try:
        with HuntSession(port=port) as session:
            devices = _survey_candidates(session, survey_seconds, console)

            if not devices:
                console.print("[yellow]No devices heard in the survey.[/yellow] "
                              "Try a longer survey or check the board.")
                # Leave the board in survey mode; nothing to hunt.
                return

            shaped = [d for d in devices if _is_camera_shaped(d)]
            _print_candidates(devices, shaped, console)

            if not shaped:
                console.print(
                    "\n[yellow]Nothing looked camera-shaped[/yellow] (close + "
                    "transmitting). You can still hunt any device above with "
                    "[bold]camscan hunt <mac> --channel <ch>[/bold].")
                return

            console.print(
                f"\n[bold]{len(shaped)} device(s) to check.[/bold] For each: "
                "walk the room following WARMER, then press Ctrl-C to move on.\n"
                "[grey42]A low signal that stays low as you walk = not in your "
                "room. Your own phone may appear — skip it.[/grey42]")

            for i, dev in enumerate(shaped, 1):
                console.input(
                    f"\n[bold]› Ready to hunt {dev.mac} on ch {dev.channel} "
                    f"({i}/{len(shaped)})[/bold] — press Enter to start… ")
                session.send(f"HUNT,{dev.mac.lower()},{dev.channel}")
                # Reuse the exact plain-English meter on this session's stream.
                run_meter(session.records(), dev.mac)

            console.print("\n[bold]All candidates checked.[/bold]")
            # Return the board to survey mode so it's ready for the next run.
            session.send("SCAN")
    except KeyboardInterrupt:
        # Ctrl-C during the very first survey (not inside a meter) lands here.
        console.print("\n[bold]Stopped.[/bold]")


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Plain-English proximity meter for one MAC.")
    ap.add_argument("--mac", required=True, help="target MAC, e.g. 46:06:cc:e1:15:b0")
    ap.add_argument("--port", default=None, help="serial port (auto-detect CH343 if omitted)")
    ap.add_argument("--file", default=None, help="replay a captured log instead of live serial")
    args = ap.parse_args(argv)

    records = read_from_file(args.file) if args.file else read_from_serial(port=args.port)
    run_meter(records, args.mac)
    return 0


if __name__ == "__main__":
    sys.exit(main())
