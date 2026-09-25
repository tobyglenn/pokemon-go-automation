#!/usr/bin/env python3
"""Stop a run that has gone quiet, or whose phones have gone away.

`gbl_day.py` supervises the legs it starts, so a stalled leg there is caught.
Nothing supervises a script somebody started by hand -- and those are the runs
that get forgotten, because there is no parent process printing a summary at
the end to remind anyone they exist.  Live, a run held two phones for 44 hours
after its last useful output.

This is the floor under all of them.  Every public command installs it, and it
ends the run when any of the three things that make a run pointless happens:
nothing has been printed for a long time, every phone has left the USB bus, or
the supervisor that started this run has gone.

It ends the run with SIGINT first, deliberately.  Each script already handles
KeyboardInterrupt by putting the phone back on the map screen; killing it
outright leaves Pokemon GO mid-battle, which is worse than the stall.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import signal
import sys
import threading
import time
from typing import Callable, TextIO


# Generous on purpose.  A GBL leg prints once per battle and a berry run once
# per feed, so half an hour of complete silence is already far outside normal.
IDLE_SECONDS = float(os.environ.get("POKEMON_IDLE_TIMEOUT", "1800"))

# The bus is only checked this often -- `adb devices` is not free, and a phone
# that has genuinely gone is not coming back within a minute.
POLL_SECONDS = 60.0

# One empty reading is not a disconnect: `adb devices` returns nothing at all
# while its daemon restarts, which it does on its own.  Three readings a minute
# apart costs two extra minutes on a genuinely dead run, against a false stop
# that costs a live trade -- which is exactly what happened on 2026-08-27.
EMPTY_READINGS_BEFORE_STOPPING = 3

# The whole guard, off.  Wanted when a run is expected to sit silent with the
# phones deliberately unplugged, and as the escape hatch when this thing is
# wrong -- it must always be possible to say "leave my run alone".
ENABLED = os.environ.get("POKEMON_WATCHDOG", "1") != "0"

# --- Outliving the supervisor ---
# A leg is started with `start_new_session=True`, which is what lets `gbl_day`
# signal the whole leg on purpose rather than having the terminal signal all of
# it by accident.  The cost is that a leg no longer shares the supervisor's
# fate: on 2026-09-21 a `gbl.py --devices razr` leg was still tapping the phone
# 19 minutes after its supervisor was gone, holding the fleet lock, so the next
# day run died with "razr is already controlled by another fleet command".
# Nothing was left to notice, because the noticing lived in the supervisor.
#
# So the leg carries the supervisor's pid and checks it is still there.  The
# environment variable, rather than `os.getppid() == 1`: a run deliberately
# detached by hand (`nohup ... &`, terminal closed) is reparented to launchd
# too, and that run is meant to keep going.  Only a leg that was handed a
# supervisor gets held to one.
SUPERVISOR_PID = os.environ.get("POKEMON_SUPERVISOR_PID", "")

# Signal 0 checks the pid without touching it.  A pid can in principle be
# reused between checks, and being wrong that way only means missing one
# orphan -- the lock error that follows names the pid, so it stays findable.
def supervisor_gone(pid_text: str = "", *, alive: Callable[[int], bool] | None = None) -> bool:
    """Whether the supervisor that started this run has since exited."""
    text = pid_text or SUPERVISOR_PID
    try:
        pid = int(text)
    except (TypeError, ValueError):
        return False  # Nobody is supervising this run; it answers to nobody.
    if pid <= 1:
        return False
    return not (alive or _pid_alive)(pid)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Alive, just not ours to signal.
    return True


@dataclass
class Activity:
    """Last time this run printed anything.  Written from every thread."""

    at: float
    lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self, now: float) -> None:
        with self.lock:
            self.at = now

    def last(self) -> float:
        with self.lock:
            return self.at


class WatchedStream:
    """A stdout that records when it was last written to.

    Wrapping rather than subclassing: the thing being wrapped may be a real
    file, a pipe from nohup, or a test's StringIO, and only `write` matters.
    """

    def __init__(self, stream: TextIO, activity: Activity, clock: Callable[[], float]):
        self._stream = stream
        self._activity = activity
        self._clock = clock

    def write(self, text: str) -> int:
        self._activity.touch(self._clock())
        return self._stream.write(text)

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def idle_verdict(
    now: float,
    last_output: float,
    empty_readings: int,
    *,
    idle_seconds: float = IDLE_SECONDS,
    empty_limit: int = EMPTY_READINGS_BEFORE_STOPPING,
) -> str:
    """Why this run should stop, or "" while it still deserves to live."""
    if empty_readings >= empty_limit:
        return "no phones are attached any more"
    if idle_seconds > 0 and now - last_output >= idle_seconds:
        return f"nothing printed for {(now - last_output) / 60:.0f} minute(s)"
    return ""


def count_attached(adb_binary: str | None = None) -> int:
    """How many fleet phones this Mac can currently see.

    Both probes are the raising kind on purpose.  The lenient ones report a
    failed probe as an empty bus, and this caller kills runs: on 2026-08-27 it
    used the lenient adb probe with a bare `adb` that is not on PATH, read the
    resulting `{}` as "the phones are gone", and stopped a trade mid-trade with
    both phones plugged in.  A probe that cannot answer must say so.

    Imported late: this module is installed by every command, and pulling the
    fleet config in at import time would make `--help` read YAML.
    """
    from . import pokemon_fleet

    android = sum(
        1
        for state in pokemon_fleet.adb_states_or_raise(adb_binary).values()
        if state == "device"
    )
    return android + len(pokemon_fleet.ios_connected_udids_or_raise())


def _stop_this_run(reason: str) -> None:
    print(f"[watchdog] {reason}; stopping this run", file=sys.stderr, flush=True)
    os.kill(os.getpid(), signal.SIGINT)
    # A script that ignores the interrupt still has to let go of the phones.
    time.sleep(30.0)
    print("[watchdog] interrupt ignored; terminating", file=sys.stderr, flush=True)
    os.kill(os.getpid(), signal.SIGTERM)


def _watch(
    activity: Activity,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    attached: Callable[[], int],
    stop: Callable[[str], None],
    poll_seconds: float,
    idle_seconds: float,
    rounds: int | None,
    orphaned: Callable[[], bool],
) -> None:
    empty_readings = 0
    completed = 0
    while rounds is None or completed < rounds:
        sleep(poll_seconds)
        completed += 1
        # Checked before the bus, and on its own: an orphaned leg is holding a
        # phone the next run is entitled to, whatever the phones look like.
        if orphaned():
            stop("the supervisor that started this run has gone")
            return
        try:
            empty_readings = empty_readings + 1 if attached() == 0 else 0
        except Exception:  # noqa: BLE001 - a failed probe is not a disconnect
            empty_readings = 0
        verdict = idle_verdict(
            clock(), activity.last(), empty_readings, idle_seconds=idle_seconds
        )
        if verdict:
            stop(verdict)
            return


def install(
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    attached: Callable[[], int] = count_attached,
    stop: Callable[[str], None] = _stop_this_run,
    poll_seconds: float = POLL_SECONDS,
    idle_seconds: float = IDLE_SECONDS,
    rounds: int | None = None,
    orphaned: Callable[[], bool] = supervisor_gone,
) -> threading.Thread | None:
    """Watch this process from a daemon thread.  Returns it, or None if off.

    `POKEMON_IDLE_TIMEOUT=0` turns the idle half off for a run that is expected
    to sit silent; the disconnect half stays, because no phone means no work
    however patient the operator is.  `POKEMON_WATCHDOG=0` turns off all three,
    orphan check included -- a leg started outside a supervisor never had one
    to lose, so only a deliberately supervised run is affected either way.
    """
    if not ENABLED:
        return None
    activity = Activity(at=clock())
    sys.stdout = WatchedStream(sys.stdout, activity, clock)  # type: ignore[assignment]
    thread = threading.Thread(
        target=_watch,
        args=(activity,),
        kwargs={
            "clock": clock,
            "sleep": sleep,
            "attached": attached,
            "stop": stop,
            "poll_seconds": poll_seconds,
            "idle_seconds": idle_seconds,
            "rounds": rounds,
            "orphaned": orphaned,
        },
        name="fleet-watchdog",
        daemon=True,
    )
    thread.start()
    return thread
