"""Read each phone's screen and start the job it is parked on.

One command for the whole day.  Every attached phone is looked at once, then
handed to the command that already does that job:

- the GO BATTLE LEAGUE card          -> `gbl_day.py`
- a gym defender's feeding screen    -> `scripts/berry.py`
- the friends list                   -> `gift.py`
- an award card, a research CLAIM
  button or an open encounter        -> `catch.py`

A phone on any other screen is left alone and named, so the operator can park
it and run again.  Jobs on different phones run side by side; each command
takes its own phone locks, so nothing here holds one past the look.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from PIL import Image

from . import (
    berry_android,
    berry_ios,
    catch_awarded_android,
    catch_ios,
    claim_research_android,
    config_paths,
    gbl_vision,
    gift_android,
    pokemon_fleet,
)

ROOT = Path(__file__).resolve().parent.parent
GBL, BERRIES, GIFTS, CATCH = "gbl", "berries", "gifts", "catch"
JOB_NAMES = {
    GBL: "GO Battle League (gbl_day.py)",
    BERRIES: "gym berries (scripts/berry.py)",
    GIFTS: "gifts (gift.py)",
    CATCH: "catching (catch.py)",
}
CLAIM_LABEL = re.compile(claim_research_android.DEFAULT_LABEL, re.IGNORECASE)

Frame = tuple[int, int, int, bytes]


def raw_frame(image: Image.Image) -> Frame:
    rgba = image.convert("RGBA")
    return rgba.width, rgba.height, 0, rgba.tobytes()


# Words that name a GBL screen besides its card: the league page and the
# party picker a league opens on.  gbl_day plays on from there.
GBL_WORDS = ("go battle league", "choose your party", "use this party")
# The league page's CLAIM RANK REWARDS! is gbl_day's to press, not research.
RANK_WORD = "rank"
# A friend's profile, and the friends list's own header row.  gift.py recovers
# to the list from a profile.
GIFT_WORDS = ("send gift", "add friend")
# A trade's Pokemon picker carries a name/CP plate and a ball-like disc, so an
# encounter is only one without the word.
TRADE_WORD = "trade"


def classify(image: Image.Image, feeding: Callable[[Frame], bool]) -> str | None:
    """The job `image` asks for, or None when it names none.

    The award card and CLAIM buttons go first: their words are their own.  A
    gym feeding screen is only one with no encounter showing, because an
    encounter passes the feed guard too.  The GBL screens come before the
    encounter, as the party picker has both a ball and name/CP plates.  The
    friends panel needs its word as well as its pale header -- a bright sky
    over an encounter passes the header test on its own.  A trade's Pokemon
    picker names no job.
    """
    if catch_awarded_android.card_from_frame(image) is not None:
        return CATCH
    claims = claim_research_android.claim_buttons(image, CLAIM_LABEL)
    if any(RANK_WORD not in claim.label.lower() for claim in claims):
        return CATCH
    frame = raw_frame(image)
    boxes = gbl_vision.recognize(image)
    text = " | ".join(gbl_vision.normalize(line) for line in gbl_vision.lines(boxes))
    encounter = (
        berry_android.encounter_showing(*frame)
        and claim_research_android.plate_visible(image)
        and TRADE_WORD not in text
    )
    if not encounter and feeding(frame):
        return BERRIES
    if gbl_vision.gbl_card_visible(boxes) or any(word in text for word in GBL_WORDS):
        return GBL
    if encounter:
        return CATCH
    if any(word in text for word in GIFT_WORDS):
        return GIFTS
    if gift_android.is_friends_panel(*frame) and "friends" in text and TRADE_WORD not in text:
        return GIFTS
    return None


def never(_frame: Frame) -> bool:
    return False


async def read_android(spec: pokemon_fleet.DeviceSpec, device) -> tuple[Image.Image, Callable[[Frame], bool]]:
    device.label = spec.name
    # The on-phone config carries this phone's feed-screen bands.
    await berry_android.get_config(device)
    device.display_id = await berry_android.find_display_id(device)
    frame = await berry_android.screencap_raw(device)
    if frame is None:
        raise RuntimeError("the screen could not be read")
    return gift_android.frame_image(frame), lambda f: berry_android.feed_screen_metrics(device, f)[2]


def read_iphone(spec: pokemon_fleet.DeviceSpec) -> tuple[Image.Image, Callable[[Frame], bool]]:
    phone = catch_ios.IOSCatchPhone.connect(spec)
    try:
        image = phone.session._screenshot()
    finally:
        phone.close()
    feeding = never
    if spec.supports("berries"):
        config = berry_ios.load_runtime_config(pokemon_fleet.operation_config_path(spec, "berries"))
        feeding = lambda _f: berry_ios.frame_metrics(SimpleNamespace(config=config), image)[2]
    return image, feeding


async def read_screen(spec: pokemon_fleet.DeviceSpec, androids: dict) -> tuple[str | None, str]:
    """(job, note) for one phone.  A phone another command holds is skipped."""
    try:
        with pokemon_fleet.acquire_device_locks([spec]):
            if spec.platform == "android":
                image, feeding = await read_android(spec, androids[spec.identifier])
            else:
                image, feeding = await asyncio.to_thread(read_iphone, spec)
            job = await asyncio.to_thread(classify, image, feeding)
    except pokemon_fleet.FleetError as exc:
        # The lock names the holder; this run skips it rather than stopping it.
        return None, str(exc).split(";")[0] + "; skipped"
    except Exception as exc:
        return None, f"could not read the screen ({exc})"
    if job is None:
        return None, (
            "not on a battle, berry card, friends or catch screen; left alone "
            "(for berries, open a gym defender first)"
        )
    return job, JOB_NAMES[job]


def command(job: str, specs: list[pokemon_fleet.DeviceSpec], args: argparse.Namespace) -> list[str]:
    names = [spec.name for spec in specs]
    if job == GBL:
        return ["gbl_day.py", "--local", "--devices", *names]
    if job == BERRIES:
        spend = ["--spend", str(args.spend)] if args.spend is not None else []
        return ["scripts/berry.py", "--local", "--devices", *names, *spend]
    if job == GIFTS:
        return ["gift.py", "--local", "--all", "--gifts-only", "--devices", *names]
    # catch.py names iPhones by fleet name and Androids by serial.
    chosen: list[str] = []
    for spec in specs:
        chosen += ["--devices", spec.name if spec.platform == "ios" else spec.identifier]
    return ["catch.py", *chosen]


async def launch(arguments: list[str]) -> int:
    print("Starting: " + " ".join(arguments), flush=True)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-u", *arguments, cwd=str(ROOT)
    )
    return await process.wait()


async def attached_androids() -> dict:
    try:
        return {device.serial: device for device in await berry_android.ClientAsync().devices()}
    except Exception as exc:
        print(f"No Android phones read ({exc})")
        return {}


async def run(args: argparse.Namespace) -> int:
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    wanted = set(args.devices or [])
    specs = [
        spec for spec in fleet.devices.values()
        if spec.enabled and (not wanted or spec.name in wanted)
    ]
    androids = await attached_androids()
    iphones = pokemon_fleet.ios_connected_udids()
    here = [
        spec for spec in specs
        if spec.identifier in (androids if spec.platform == "android" else iphones)
    ]
    if not here:
        print("No fleet phone is attached to this computer")
        return 0
    # An iPhone's look waits on WDA, so say at once which phones this computer
    # has; its lines otherwise land under the other machine's battle output.
    print(f"Reading {len(here)} phone(s): " + ", ".join(spec.name for spec in here), flush=True)

    async def reported(spec: pokemon_fleet.DeviceSpec) -> tuple[str | None, str]:
        job, note = await read_screen(spec, androids)
        print(f"[{spec.name}] {note}", flush=True)
        return job, note

    readings = await asyncio.gather(*(reported(spec) for spec in here))
    groups: dict[str, list[pokemon_fleet.DeviceSpec]] = {}
    for spec, (job, note) in zip(here, readings):
        if job is not None:
            groups.setdefault(job, []).append(spec)
    if args.plan or not groups:
        return 0
    codes = await asyncio.gather(*(launch(command(job, specs, args)) for job, specs in groups.items()))
    return next((code for code in codes if code), 0)


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--devices", nargs="+", help="only look at these fleet device names")
    parser.add_argument("--spend", type=int, help="berry budget for a phone on a gym feeding screen")
    parser.add_argument("--plan", action="store_true", help="read every screen and say what would start; start nothing")
    args = parser.parse_args(arguments)
    if args.spend is not None and args.spend < 1:
        parser.error("--spend must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))
