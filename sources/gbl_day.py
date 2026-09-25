#!/usr/bin/env python3
"""Play a whole day's Go Battle League allotment with nobody at the desk.

`gbl.py --count N` plays one leg and stops.  That is the right shape for a
supervised run, but it is not enough to spend a day's 25 battles: the loop
gives up on purpose whenever it cannot recognise a screen for `UNKNOWN_LIMIT`
reads, and one misread on the way out of a set ends the run with most of the
allotment unplayed.  Live, that is exactly what happened -- a matchmaking
screen read as a scrolled reward page, the loop stopped after battle 7, and
the remaining 18 battles sat there until someone noticed.

So this supervises instead of trusting a single leg.  Each phone gets its own
chain of legs: play what is left, count what actually got played, and start
another leg if the phone stopped early.  A phone that plays nothing twice in a
row has a problem no retry will fix (empty allotment, a modal nobody mapped, a
disconnected cable), and is left alone rather than tapped at forever.

Every phone runs its own chain concurrently, because a leg is mostly waiting
for battles and two phones have no reason to take turns.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from typing import Awaitable, Callable, Sequence

from . import config_paths, fleet_watchdog, pokemon_fleet


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Not Niantic's cap -- a budget the game is expected to end before it is spent.
# 25 was set here as "five sets of five" and it stopped both phones short: the
# compact-layout device reported 25, this supervisor called the day done, and the game then let
# it straight into five more.  Whatever the real allowance is on a given account
# and day (passes, events, a set left over from a leg that lost count), the phone
# is the only thing that knows it, and it says so by refusing to start another
# battle -- which STALL_LIMIT below turns into an ending.  So the number here is
# deliberately higher than any day can hold.
BATTLES_PER_DAY = 40

# Two legs in a row that played nothing means retrying is not the answer.  One
# empty leg is ordinary (a phone can stop on the reward page it cannot read),
# two is a phone that needs a person.
STALL_LIMIT = 2

# A hard ceiling so a phone that reports one battle per leg forever still ends.
MAX_LEGS = 40

# A day's allotment takes hours, and a Mac that sleeps halfway through drops
# both the USB link to the phones and the Appium session on top of it.
CAFFEINATE = Path("/usr/bin/caffeinate")

# `[android-two] Finished: 7 battle(s) (2 of them interrupted)` -- printed by both
# the Android and the iOS runner at the end of every leg, and the only number
# either of them commits to.  The parenthetical is optional: a leg that read
# every result prints the count alone.
FINISHED_LINE = re.compile(
    r"^\[([^\]]+)\]\s*Finished:\s*(\d+)\s*battle(?:\(s\))?"
    r"(?:\s*\((\d+) of them interrupted\))?"
)
DAILY_CAP_LINE = re.compile(r"Daily battle cap reached")


def parse_cap_reached(output: str) -> bool:
    """Whether any runner reported hitting the game's daily battle allowance."""
    return bool(DAILY_CAP_LINE.search(output))


class GBLDayError(Exception):
    """Something the supervisor cannot work around."""


@dataclass
class DeviceProgress:
    """What one phone has managed so far today."""

    name: str
    allotment: int
    played: int = 0
    legs: int = 0
    stalls: int = 0
    recoveries: int = 0
    last_line: str = ""
    stopped: str = ""
    cap_reached: bool = False

    @property
    def remaining(self) -> int:
        return 0 if self.cap_reached else max(self.allotment - self.played, 0)

    @property
    def done(self) -> bool:
        return self.cap_reached or self.remaining == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "played": self.played,
            "remaining": self.remaining,
            "allotment": self.allotment,
            "legs": self.legs,
            "stalls": self.stalls,
            "recoveries": self.recoveries,
            "last_line": self.last_line,
            "stopped": self.stopped,
            "cap_reached": self.cap_reached,
        }


@dataclass
class LegResult:
    """One `gbl.py` invocation: what it played and how it ended."""

    played: dict[str, int] = field(default_factory=dict)
    status: int = 0
    last_line: str = ""
    # Set when the watchdog stopped this leg rather than the leg ending itself.
    abandoned: str = ""
    cap_reached: bool = False


def parse_played(output: str) -> dict[str, int]:
    """Battles each label reported playing, read from the runners' own output.

    Both runners print one `Finished:` line per phone per leg, so the last such
    line for a label is that leg's count.  Anything else in the output is
    commentary and is ignored on purpose -- the battle counters in the running
    log (`Battle 3/17`) are per-leg attempts, not completions.

    Interrupted battles are subtracted.  A leg writes one off when the screen
    stops looking like a battlefield mid-fight, and the game may never have
    counted it: the android-two reported `Finished: 25 battle(s) (4 of them
    interrupted)`, this supervisor called the day spent, and the phone was left
    standing on the league screen with battles still owed to it.  Crediting only
    the battles that were seen through to a result costs at worst one extra leg,
    which the stall rule below ends when it plays nothing.
    """
    played: dict[str, int] = {}
    for line in output.splitlines():
        match = FINISHED_LINE.match(line.strip())
        if match:
            interrupted = int(match.group(3) or 0)
            played[match.group(1).strip()] = max(int(match.group(2)) - interrupted, 0)
    return played


def leg_command(device: str, count: int, trace: Path | None) -> list[str]:
    """The public command a leg runs, as a person would type it."""
    command = [
        sys.executable,
        "-u",
        str(PROJECT_ROOT / "gbl.py"),
        "--local",
        "--devices",
        device,
        "--count",
        str(count),
    ]
    if trace is not None:
        command.extend(["--trace", str(trace)])
    return command


def supervised_environment(
    environ: dict[str, str] | None = None, pid: int | None = None
) -> dict[str, str]:
    """The leg's environment, naming this process as the one it answers to."""
    return {
        **(os.environ if environ is None else environ),
        "POKEMON_SUPERVISOR_PID": str(os.getpid() if pid is None else pid),
    }


LEG_INTERRUPT_SECONDS = 20.0
LEG_TERMINATE_SECONDS = 5.0

# --- When to give up on a leg that is still running ---
# `STALL_LIMIT` above only judges a leg that *finished*, so it never sees a leg
# that simply never returns. On 2026-08-25 a day run stayed up for 1 day 20
# hours having started 0 battles: android-one looped map recovery -> GBL card -> "Party
# ready, using it" forever, printing a line every few seconds, so the pid was
# alive and the log mtime was always fresh. Nothing in the supervisor was
# watching the only number that mattered.
#
# Two different failures need two different patiences:
#   * silence -- no output at all, which is a wedged `adb exec-out screencap` or
#     a hung Appium call. Five minutes is far longer than any single read.
#   * no progress -- output flowing, but no battle ever starting. This is the
#     44-hour case. A battle plus its result screens runs 2-3 minutes, and
#     matchmaking can be slow, but twenty minutes without one battle beginning
#     is a phone going in circles, not a slow queue.
# Both are deliberately generous: ending a healthy leg early costs a restart,
# and the stall machinery below turns an abandoned leg into an ending anyway.
LEG_SILENCE_SECONDS = 300.0
LEG_PROGRESS_SECONDS = 1200.0

# What counts as a leg getting somewhere. Deliberately not "any output" -- that
# is exactly the signal that lied for two days. Both runners print these.
PROGRESS_LINE = re.compile(r"Battle\s+\d+/\d+\s+started|Finished:\s*\d+\s*battle")

# Legs abandoned by the watchdog before the chain gives up on the phone. A leg
# that wedges, gets killed, plays one battle on the retry and wedges again would
# otherwise reset `stalls` forever and ride `MAX_LEGS` for half a day.
ABANDON_LIMIT = 3

# How long a `connected phones` answer stays good for.
CONNECTION_CACHE_SECONDS = 30.0


def watchdog_verdict(
    now: float,
    last_output: float,
    last_progress: float,
    silence: float = LEG_SILENCE_SECONDS,
    progress: float = LEG_PROGRESS_SECONDS,
) -> str:
    """Why this leg should be abandoned, or "" while it still deserves time."""
    if now - last_output >= silence:
        return f"nothing printed for {(now - last_output) / 60:.0f} minute(s)"
    if now - last_progress >= progress:
        return f"no battle started for {(now - last_progress) / 60:.0f} minute(s)"
    return ""


def next_watchdog_wait(
    now: float,
    last_output: float,
    last_progress: float,
    silence: float = LEG_SILENCE_SECONDS,
    progress: float = LEG_PROGRESS_SECONDS,
) -> float:
    """How long to wait for the next line before re-judging the leg.

    Whichever patience runs out first decides, and the floor keeps a watchdog
    that is already overdue from spinning on a zero-length wait.
    """
    remaining = min(silence - (now - last_output), progress - (now - last_progress))
    return max(remaining, 0.05)


async def stop_leg(device: str, process: asyncio.subprocess.Process) -> None:
    """Stops a leg and everything it started, escalating until it is gone.

    ``process`` is `gbl.py`; the runner holding the phone is its child, and
    caffeinate is a child of that.  Terminating only the first of them orphaned
    the runner: it went on tapping, went on holding the fleet lock, and outlived
    the Ctrl-C meant to stop it.  Signalling the leg's process group reaches all
    of them, and each signal is given time to let the runner close its Appium
    session before the next one takes the choice away.
    """
    if process.returncode is not None:
        return
    try:
        group = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    escalation = (
        (signal.SIGINT, LEG_INTERRUPT_SECONDS, "stopping"),
        (signal.SIGTERM, LEG_TERMINATE_SECONDS, "terminating"),
        (signal.SIGKILL, None, "killing"),
    )
    for number, patience, word in escalation:
        print(f"[gbl-day] {device}: {word} the leg", flush=True)
        try:
            os.killpg(group, number)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), timeout=patience)
            return
        except asyncio.TimeoutError:
            continue


async def run_leg(
    device: str,
    count: int,
    *,
    log_path: Path | None,
    trace: Path | None,
    silence_seconds: float = LEG_SILENCE_SECONDS,
    progress_seconds: float = LEG_PROGRESS_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> LegResult:
    """Run one leg, echoing the runner's output and keeping a copy."""
    command = leg_command(device, count, trace)
    print(f"[gbl-day] {device}: leg of up to {count} battle(s)", flush=True)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        # Nothing here is going to answer a prompt.
        stdin=asyncio.subprocess.DEVNULL,
        # Who to outlive, and no longer than.  `stop_leg` below is the orderly
        # way a leg ends, and it is skipped whenever this supervisor dies
        # without getting to run it -- a SIGKILL, a closed terminal, a Mac that
        # slept.  The leg then taps on alone, holding the phone's fleet lock
        # against the next run; the pid here is how it finds that out.
        env=supervised_environment(),
        # A leg is `gbl.py`, which spawns the runner that actually holds
        # the phone.  Left in this terminal's process group, Ctrl-C reached
        # all three at once and each started its own shutdown, so the runner
        # kept tapping while gbl-day believed it had stopped.  Its own group
        # means one place signals the whole leg, in the order below.
        start_new_session=True,
    )
    lines: list[str] = []
    handle = log_path.open("a", encoding="utf-8") if log_path else None
    abandoned = ""
    # Startup counts as progress: WebDriverAgent can take minutes to build
    # before the first battle, and that is the leg working, not stalling.
    last_output = last_progress = clock()
    try:
        assert process.stdout is not None
        while True:
            verdict = watchdog_verdict(
                clock(), last_output, last_progress, silence_seconds, progress_seconds
            )
            if verdict:
                abandoned = verdict
                print(f"[gbl-day] {device}: {verdict}; abandoning the leg", flush=True)
                await stop_leg(device, process)
                break
            try:
                raw = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=next_watchdog_wait(
                        clock(),
                        last_output,
                        last_progress,
                        silence_seconds,
                        progress_seconds,
                    ),
                )
            except asyncio.TimeoutError:
                # Patience may not be spent yet -- whichever timer was nearer
                # simply came due. The top of the loop decides.
                continue
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\n")
            lines.append(line)
            print(line, flush=True)
            if handle:
                # Stamped only in the file. Two phones' lines are interleaved
                # on the terminal and read as a story; the file is read later,
                # to answer questions like whether the charged move the phone
                # gave up on is the one that went off two seconds afterwards.
                handle.write(f"{datetime.now():%H:%M:%S} {line}\n")
                handle.flush()
            last_output = clock()
            if PROGRESS_LINE.search(line):
                last_progress = last_output
        status = await process.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        await stop_leg(device, process)
        raise
    finally:
        if handle:
            handle.close()
    output = "\n".join(lines)
    return LegResult(
        played=parse_played(output),
        status=status,
        last_line=lines[-1] if lines else "",
        abandoned=abandoned,
        cap_reached=parse_cap_reached(output),
    )


def connected_device_names(adb_binary: str | None = None) -> set[str]:
    """Fleet names whose phone is plugged into this Mac right now.

    Deliberately not `pokemon_fleet.collect_status`: that also asks each Appium
    server whether it is up, over HTTP, and this runs between every leg. Only
    the cable matters here.

    The raising probes, for the same reason `fleet_watchdog.count_attached`
    uses them: this answer stops a phone's chain, and a probe that failed is
    not an answer. The bare `adb` default this once had would have ended every
    Android chain before its first leg, because `adb` is not on PATH.
    """
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    android = pokemon_fleet.adb_states_or_raise(adb_binary)
    ios = pokemon_fleet.ios_connected_udids_or_raise()
    names: set[str] = set()
    for spec in fleet.devices.values():
        plugged = (
            spec.identifier in ios
            if spec.platform == "ios"
            else android.get(spec.identifier) == "device"
        )
        if plugged:
            names.add(spec.name)
    return names


Recover = Callable[[str], Awaitable[bool]]
Connected = Callable[[str], bool]


def _assume_connected(name: str) -> bool:
    """The default for `play_device_day`: ask no questions of the hardware."""
    return True


async def _no_recovery(name: str) -> bool:
    """The default for `play_device_day`: touch no phones.

    Recovery drives a real handset, so the supervisor's own loop defaults to
    doing nothing and `run_day` passes the real walk in.
    """
    return False


async def recover_home_screen(name: str, shots: Path | None = None) -> bool:
    """Walk a phone that stopped on the map back to the GO BATTLE LEAGUE card.

    Best effort by design: this runs between legs to improve the next one's
    chances, and a phone that cannot be recovered is still worth another leg.
    Anything that goes wrong is reported and swallowed rather than ending a day
    that has battles left in it.
    """
    from . import gbl_home_recovery

    where = shots or (config_paths.state_dir() / "home-recovery")
    print(f"[gbl-day] {name}: empty leg; walking the phone back to GBL", flush=True)
    try:
        reached, steps = await gbl_home_recovery.recover_device(name, where)
    except Exception as exc:
        print(f"[gbl-day] {name}: home recovery could not run ({exc})", flush=True)
        return False
    for step in steps:
        mark = "ok" if step.ok else "FAIL"
        print(f"[gbl-day] {name}: recovery {mark} {step.name}: {step.detail}", flush=True)
    return reached


LegRunner = Callable[[str, int], Awaitable[LegResult]]


async def play_device_day(
    progress: DeviceProgress,
    run: LegRunner,
    *,
    on_update: Callable[[], None] = lambda: None,
    recover: Recover = _no_recovery,
    still_connected: Connected = _assume_connected,
) -> DeviceProgress:
    """Keep starting legs on one phone until its allotment is spent."""
    abandoned_legs = 0
    while not progress.done and progress.legs < MAX_LEGS:
        # A phone that has left the USB bus cannot be driven or recovered, and
        # every further leg would spend a minute failing to find it. Both
        # Androids dropped off mid-run on 2026-08-25 and the day kept going.
        if not still_connected(progress.name):
            progress.stopped = "phone disconnected"
            break
        result = await run(progress.name, progress.remaining)
        progress.legs += 1
        # A leg names itself by whatever label the runner uses.  Usually that is
        # the fleet name asked for, but a bare serial counts too: one leg drives
        # one phone, so a single reported count can only be this phone's.
        counts = result.played
        played = counts.get(progress.name)
        if played is None and len(counts) == 1:
            played = next(iter(counts.values()))
        played = played or 0
        progress.played += played
        progress.last_line = result.last_line
        if result.cap_reached:
            progress.cap_reached = True
            progress.stopped = "daily battle cap reached"
            on_update()
            break
        if played:
            progress.stalls = 0
        else:
            progress.stalls += 1
        on_update()
        if progress.done:
            progress.stopped = "allotment played"
            break
        if progress.stalls >= STALL_LIMIT:
            progress.stopped = (
                f"stopped after {progress.stalls} leg(s) that played nothing"
            )
            break
        if result.abandoned:
            abandoned_legs += 1
            if abandoned_legs >= ABANDON_LIMIT:
                progress.stopped = (
                    f"stopped after {abandoned_legs} leg(s) the watchdog had to end "
                    f"({result.abandoned})"
                )
                break
        if not played:
            # An empty leg has usually been dropped back to the map with its
            # runner gone, and the next leg starts from whatever screen the
            # phone was left on -- so it reads the map, fails to recognise it,
            # and plays nothing either.  Two taps put it back on the GBL card.
            if await recover(progress.name):
                progress.recoveries += 1
                print(
                    f"[gbl-day] {progress.name}: walked back to the GBL card",
                    flush=True,
                )
                on_update()
        if result.status != 0:
            # Worth saying out loud, but not worth stopping for: the leg still
            # reported what it played, and the next leg is a fresh read of
            # whatever screen the phone was left on.
            print(
                f"[gbl-day] {progress.name}: leg exited with status "
                f"{result.status}; continuing",
                flush=True,
            )
    if not progress.stopped:
        progress.stopped = (
            "allotment played" if progress.done else f"hit the {MAX_LEGS}-leg ceiling"
        )
    on_update()
    return progress


def write_status(path: Path, devices: Sequence[DeviceProgress]) -> None:
    """Leave a readable note of where the day is, for whoever checks on it."""
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "devices": {device.name: device.as_dict() for device in devices},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


async def run_day(
    device_names: Sequence[str],
    *,
    allotment: int = BATTLES_PER_DAY,
    log_dir: Path | None = None,
    trace_dir: Path | None = None,
    status_path: Path | None = None,
    runner: Callable[[str], LegRunner] | None = None,
    keep_mac_awake: bool = True,
    allow_empty: bool = False,
    recover: Recover | None = None,
    still_connected: Connected | None = None,
) -> list[DeviceProgress]:
    """Spend the day's allotment on every named phone at once."""
    if not device_names:
        if allow_empty:
            print("[gbl-day] No connected GBL devices on this machine", flush=True)
            return []
        raise GBLDayError("Name at least one phone to play on")
    devices = [DeviceProgress(name=name, allotment=allotment) for name in device_names]

    def update() -> None:
        if status_path is not None:
            write_status(status_path, devices)

    def default_runner(name: str) -> LegRunner:
        log_path = log_dir / f"{name}.log" if log_dir else None
        trace = trace_dir / name if trace_dir else None

        async def run(device: str, count: int) -> LegResult:
            return await run_leg(device, count, log_path=log_path, trace=trace)

        return run

    # One `adb devices` + one `xcrun devicectl` answer serves every phone that
    # asks within the cache window, and a check that itself fails is treated as
    # "still there" -- a flaky probe must not end a working day.
    connection_cache: dict[str, object] = {"at": 0.0, "names": set(device_names)}

    def default_connected(name: str) -> bool:
        now = time.monotonic()
        if now - float(connection_cache["at"]) >= CONNECTION_CACHE_SECONDS:
            try:
                connection_cache["names"] = connected_device_names()
            except Exception as error:  # noqa: BLE001 - probe must never be fatal
                print(f"[gbl-day] connection check failed: {error}", flush=True)
                connection_cache["names"] = set(device_names)
            connection_cache["at"] = now
        return name in connection_cache["names"]  # type: ignore[operator]

    recover_shots = (log_dir / "home-recovery") if log_dir is not None else None

    async def default_recover(name: str) -> bool:
        return await recover_home_screen(name, recover_shots)

    make_runner = runner or default_runner
    update()
    keep_awake = await hold_awake() if keep_mac_awake else None
    try:
        await asyncio.gather(
            *(
                play_device_day(
                    device,
                    make_runner(device.name),
                    on_update=update,
                    recover=recover or default_recover,
                    still_connected=still_connected or default_connected,
                )
                for device in devices
            )
        )
    finally:
        if keep_awake is not None and keep_awake.returncode is None:
            keep_awake.terminate()
    update()
    return devices


async def hold_awake() -> asyncio.subprocess.Process | None:
    """Keep the Mac awake for as long as the day lasts, if it can be done."""
    if not CAFFEINATE.exists():
        return None
    try:
        return await asyncio.create_subprocess_exec(
            str(CAFFEINATE),
            "-is",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"[gbl-day] could not hold the Mac awake: {exc}", flush=True)
        return None


def summarise(devices: Sequence[DeviceProgress]) -> str:
    return "\n".join(
        f"[gbl-day] {device.name}: {device.played}/{device.allotment} battle(s) "
        f"over {device.legs} leg(s) -- {device.stopped}"
        for device in devices
    )


def default_log_dir(now: datetime | None = None) -> Path:
    """Where a run keeps its per-phone logs when the caller names no directory.

    A bare `gbl_day.py` used to write to the terminal alone.  When such a run
    went wrong, the only account of it was whatever was still on screen, which
    is why a phone that played the wrong league for an hour could not be
    explained afterwards.  Logging somewhere by default costs nothing and
    leaves the run readable.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return config_paths.state_dir() / "gbl-day" / stamp


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "positional_devices",
        nargs="*",
        default=None,
        metavar="devices",
        help="fleet device names to play on (default: all connected GBL devices)",
    )
    parser.add_argument(
        "--devices",
        nargs="+",
        default=None,
        help="fleet device names to play on (e.g. --devices all, --devices android-two)",
    )
    parser.add_argument(
        "--allotment",
        type=int,
        default=BATTLES_PER_DAY,
        help=f"battles per phone for the day (default {BATTLES_PER_DAY})",
    )
    parser.add_argument(
        "--allmachines",
        action="store_true",
        help="run across all connected devices on all configured hosts",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="exit cleanly if no devices are connected on this host",
    )
    parser.add_argument("--log-dir", type=Path, help="keep a per-phone log here")
    parser.add_argument("--trace", type=Path, help="write decision frames under here")
    parser.add_argument(
        "--status",
        type=Path,
        help="JSON file rewritten after every leg with each phone's progress",
    )
    return parser.parse_args(arguments)


def resolve_device_names(args: argparse.Namespace) -> list[str]:
    positional = args.positional_devices or []
    flag_devices = args.devices or []
    names = list(positional) + list(flag_devices)
    if not names:
        names = ["all"]
    config_file = config_paths.default_config("pokemon-fleet.yaml")
    fleet = pokemon_fleet.load_fleet(config_file)
    selected_specs = pokemon_fleet.select_devices(
        fleet,
        names,
        "gbl",
        allow_empty=getattr(args, "allow_empty", False),
    )
    return [spec.name for spec in selected_specs]


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(arguments)
    if args.allotment < 1:
        raise GBLDayError("--allotment needs to be at least 1")
    log_dir = args.log_dir or default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"[gbl-day] Keeping a log per phone under {log_dir}", flush=True)
    device_names = resolve_device_names(args)
    if not device_names and args.allow_empty:
        print("[gbl-day] No connected devices for GBL day on this machine", flush=True)
        return 0
    print(f"[gbl-day] Target devices ({len(device_names)}): {', '.join(device_names)}", flush=True)
    # Legs are watched one by one below; this watches the supervisor itself,
    # which echoes every leg's output and so is silent only if everything is.
    fleet_watchdog.install()
    devices = asyncio.run(
        run_day(
            device_names,
            allotment=args.allotment,
            log_dir=log_dir,
            trace_dir=args.trace,
            status_path=args.status,
            allow_empty=args.allow_empty,
        )
    )
    if not devices:
        return 0
    print(summarise(devices), flush=True)
    return 0 if all(device.done for device in devices) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped. The phones are left wherever they were.", file=sys.stderr)
        raise SystemExit(130)
    except (GBLDayError, pokemon_fleet.FleetError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
