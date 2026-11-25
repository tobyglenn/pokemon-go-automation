"""Let Sol (OpenAI GPT-6.1) retune the Android throw while a catch run goes on.

After every few encounters, send that phone's throw counts, encounter
outcomes, and tuning settings to the `sol` CLI. Apply its suggested values
between encounters on that phone.

Sol only moves the knobs listed in KNOBS, each inside a hard range and by at
most MAX_STEP of its range per review, so a bad answer costs a few balls, not
a run.  Everything it changes is written to the review log with its reason,
and `overrides.json` in the run's directory is the last applied state.
"""
from __future__ import annotations

import dataclasses
import json
import math
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import excellent_throw_android


@dataclass(frozen=True)
class Knob:
    low: float
    high: float
    meaning: str


KNOBS: dict[str, Knob] = {
    "throw_rise": Knob(
        0.30, 0.62,
        "flick length as a share of screen height (longer = throws further)",
    ),
    "reach_step": Knob(
        0.0, 0.25,
        "extra flick length added per miss, cycling over 5 misses",
    ),
    "aim_deadband": Knob(
        0.0, 0.15,
        "plate this close to centre (share of width) gets a straight flick",
    ),
    "aim_max_lean": Knob(
        0.05, 0.35,
        "largest sideways lean of the flick, share of width",
    ),
    "plate_to_body": Knob(
        0.05, 0.30,
        "how far below the name plate the body is assumed, share of height",
    ),
    "throw_ms": Knob(
        80, 260,
        "swipe duration in ms (shorter = faster ball)",
    ),
}

# The most one review may move a knob, as a share of its whole range.
MAX_STEP = 0.15
SOL_TIMEOUT = 240.0
REVIEW_ENCOUNTERS = 12


def current_values(device: excellent_throw_android.AndroidDevice) -> dict[str, float]:
    return {**dataclasses.asdict(device.throw_aim), "throw_ms": device.config.throw_duration_ms}


def clamp_change(name: str, old: float, wanted: float) -> float:
    knob = KNOBS[name]
    step = (knob.high - knob.low) * MAX_STEP
    value = max(old - step, min(old + step, wanted))
    value = max(knob.low, min(knob.high, value))
    return round(value) if name == "throw_ms" else round(value, 4)


def apply_values(device: excellent_throw_android.AndroidDevice, values: dict[str, float]) -> None:
    aim = {}
    for name, value in values.items():
        knob = KNOBS.get(name)
        if (
            knob is None
            or type(value) not in (int, float)
            or not knob.low <= value <= knob.high
        ):
            continue
        if name == "throw_ms":
            device.config = dataclasses.replace(device.config, throw_duration_ms=int(value))
        else:
            aim[name] = value
    if aim:
        device.throw_aim = dataclasses.replace(device.throw_aim, **aim)


def parse_answer(text: str) -> tuple[dict[str, float], str]:
    """The last JSON object in Sol's answer: {"changes": {...}, "reason": "..."}."""
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    for start in (index for index, char in enumerate(text) if char == "{"):
        try:
            data, _end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("changes"), dict):
            found = data
    if found is not None:
        clean = {
            name: value
            for name, value in found["changes"].items()
            if name in KNOBS and (type(value) is int or (type(value) is float and math.isfinite(value)))
        }
        return clean, str(found.get("reason", ""))
    return {}, ""


PROMPT = """You are tuning an automated Pokemon GO throw. Encounters can \
vary and the numeric results cover one Android phone.

The throw is a straight adb swipe from the held ball upward.  Its end point is \
`throw_rise * screen_height` above the ball and grows by a factor set by \
`reach_step` after a miss. It leans toward a name plate that is off centre. \
Use as few balls as possible. An ended encounter means the ball stayed \
gone, which is a proxy for a completed catch. A long encounter can reflect \
range, aim or a breakout.

Knobs you may change (name: current value, allowed range, meaning):
{knobs}

Recent encounter results (newest last):
{journal}

Reply with a short diagnosis, then one JSON object on its own as the last thing:
{{"changes": {{"knob_name": new_value, ...}}, "reason": "one sentence"}}
Change at most two knobs, by small steps; send "changes": {{}} if the throw is \
already working (most encounters caught with 1-2 balls)."""


class SolTuner:
    """Journal encounters, ask Sol every `every` of them, apply what it says."""

    def __init__(self, device: excellent_throw_android.AndroidDevice, run_dir: Path, every: int = 3,
                 runner: Callable[[str], str] | None = None) -> None:
        self.device = device
        self.run_dir = run_dir
        self.every = max(1, every)
        self.runner = runner or run_sol
        self.journal: deque[dict[str, Any]] = deque(maxlen=REVIEW_ENCOUNTERS)
        self.encounters = 0
        self.pending: dict[str, float] | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        run_dir.mkdir(parents=True, exist_ok=True)
        self.review_log = run_dir / "sol-reviews.log"
        self.overrides = run_dir / "overrides.json"
        if self.overrides.exists():
            try:
                saved = json.loads(self.overrides.read_text())
                if isinstance(saved, dict):
                    apply_values(device, saved)
            except (OSError, json.JSONDecodeError):
                pass

    def record(self, ended: bool, balls: int) -> None:
        entry = {
            "ended": ended,
            "balls": balls,
            "settings": current_values(self.device),
        }
        self.journal.append(entry)
        self.encounters += 1
        with (self.run_dir / "journal.jsonl").open("a") as handle:
            handle.write(json.dumps(entry) + "\n")
        if self.encounters % self.every == 0:
            self.review_in_background()

    def apply_pending(self) -> None:
        """Call between encounters, never mid-throw."""
        with self._lock:
            changes, self.pending = self.pending, None
        if changes:
            apply_values(self.device, changes)
            self.overrides.write_text(json.dumps(current_values(self.device), indent=2))
            print(f"[{self.device.label}] Sol retune applied: {changes}")

    def review_in_background(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self.pending is not None:
                return
        prompt = self.build_prompt()
        values = current_values(self.device)
        self._thread = threading.Thread(target=self._review, args=(prompt, values), daemon=True)
        self._thread.start()

    def build_prompt(self) -> str:
        values = current_values(self.device)
        knobs = "\n".join(
            f"- {name}: {values[name]} (range {knob.low}..{knob.high}) -- {knob.meaning}"
            for name, knob in KNOBS.items()
        )
        journal = "\n".join(json.dumps(entry) for entry in self.journal)
        return PROMPT.format(knobs=knobs, journal=journal)

    def _review(self, prompt: str, values: dict[str, float]) -> None:
        try:
            answer = self.runner(prompt)
        except Exception as exc:  # a failed review must never stop the catching
            self._note(f"review failed: {exc}")
            return
        wanted, reason = parse_answer(answer)
        changes = {
            name: clamp_change(name, values[name], value)
            for name, value in list(wanted.items())[:2]
        }
        changes = {name: value for name, value in changes.items() if value != values[name]}
        self._note(f"answer:\n{answer.strip()}\napplied next encounter: {changes} ({reason})")
        if changes:
            with self._lock:
                self.pending = changes

    def _note(self, text: str) -> None:
        with self.review_log.open("a") as handle:
            handle.write(f"--- {time.strftime('%H:%M:%S')} ---\n{text}\n")


def run_sol(prompt: str) -> str:
    binary = shutil.which("sol") or str(Path.home() / ".local/bin/sol")
    result = subprocess.run(
        [binary, "--no-stream", prompt],
        capture_output=True, text=True, timeout=SOL_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-300:] or f"sol exited {result.returncode}")
    return result.stdout
