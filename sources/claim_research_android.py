#!/usr/bin/env python3
"""Press every orange CLAIM button on a research screen and catch what it pays.  Reached through `catch.py`."""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import catch_awarded_android, excellent_throw_android, gbl_vision

# What a claim button says.  Matched against the words inside the orange band
# rather than the whole screen: "Time left to claim rewards:" is printed on the
# GO Pass page in plain black text, and a screen-wide search would call that a
# button and press the middle of a paragraph.
DEFAULT_LABEL = r"\bclaim\b"

# Pokemon GO draws its claim pills in one flat orange.  Measured off the razr's
# GO Battle League Timed Research (1/6) card on 10 Sep 2026: (255, 174, 76) in
# the body, (255, 177, 82) at the rounded edge.  The bounds are wide enough for
# the gradient the button carries on other screens and still exclude the teal
# tabs, the green REWARDS strip and the white card.
ORANGE_MIN_RED = 200
ORANGE_GREEN_RANGE = (110, 215)
ORANGE_MAX_BLUE = 140
ORANGE_RED_OVER_BLUE = 90
ORANGE_GREEN_OVER_BLUE = 35

# A button is a band, not a speck.  The claim pill spans the research card, so
# a row of it is over a third of the frame wide; the "?" encounter medallion,
# the orange dot on the GO PASS tab and an item's own orange artwork are all
# narrower than this and never reach button height.
BUTTON_MIN_WIDTH = 0.30
BUTTON_MIN_HEIGHT = 0.015

# The band scan and the OCR both run on one downscaled copy.  Vision read all
# six of the razr's CLAIM REWARD labels at 1.00 confidence on a 480px-wide
# frame, which is a twelfth of the pixels of its native 1224x2992.
OCR_WIDTH = 480
OCR_CONFIDENCE = 0.5

# A claimed reward plays an animation before the screen underneath is real
# again, and a list that has just been scrolled is still gliding.
CLAIM_SETTLE = 2.4
SCREEN_SETTLE = 1.2
CLAIM_POLL = 1.5

# How long an encounter is still allowed to turn up after the button itself has
# gone.  The row stops being orange the moment the claim registers, but the
# Pokemon is not on screen for several seconds after that: razr claimed a GO
# Battle League step at 10:30 on 10 Sep 2026, was told the button was gone, and
# left a Fearow standing there because it stopped watching two seconds in.
ENCOUNTER_GRACE = 14.0

# How many frames the encounter guard looks at before it lets a scroll through.
# Nothing else on this screen costs anything to get wrong; a scroll on an
# encounter screen is a thrown ball, and both halves of the guard can miss on a
# single frame -- the ball merges with a Pokemon standing over it, and the plate
# is grey on grass.
SCROLL_GUARD_READS = 3

# How much of the tapped band has to be orange again for the button to count as
# still there.  A claim that took leaves the row a reward tile instead.
BAND_OVERLAP = 0.5


# The name/CP plate an encounter draws over the Pokemon.  Searched across the
# whole frame, unlike `gbl_vision.encounter_plate`, which bands it to the middle
# of a GBL screen: the plate rides with the Pokemon, and razr's Fearow held it
# at 0.40 of the screen while the bird was flying and 0.06 once it settled.
ENCOUNTER_PLATE = re.compile(r"\bcp ?\d+\b")


class ResearchClaimError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimButton:
    """One orange button, in the phone's own coordinates."""

    label: str
    point: list[int]
    top: int
    bottom: int

    def overlaps(self, other: "ClaimButton") -> bool:
        span = min(self.bottom, other.bottom) - max(self.top, other.top)
        height = min(self.bottom - self.top, other.bottom - other.top)
        return height > 0 and span / height >= BAND_OVERLAP


def is_orange(pixel: tuple[int, ...]) -> bool:
    red, green, blue = pixel[0], pixel[1], pixel[2]
    return (
        red >= ORANGE_MIN_RED
        and ORANGE_GREEN_RANGE[0] <= green <= ORANGE_GREEN_RANGE[1]
        and blue <= ORANGE_MAX_BLUE
        and red - blue >= ORANGE_RED_OVER_BLUE
        and green - blue >= ORANGE_GREEN_OVER_BLUE
    )


def orange_bands(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Every wide orange horizontal band in this frame, top first.

    Returned as (top, bottom, left, right) in the frame's own pixels.  The band
    is found by colour alone -- what it says is checked afterwards, because the
    reward tiles on the GO Pass page are the same orange as a claim button and
    only their words tell them apart.
    """
    pixels = image.convert("RGB").load()
    width, height = image.size
    rows: list[tuple[int, int, int]] = []
    for y in range(height):
        count = 0
        left = width
        right = -1
        for x in range(width):
            if is_orange(pixels[x, y]):
                count += 1
                left = min(left, x)
                right = max(right, x)
        rows.append((count, left, right))

    bands: list[tuple[int, int, int, int]] = []
    start: int | None = None
    for y in range(height + 1):
        wide = y < height and rows[y][0] >= BUTTON_MIN_WIDTH * width
        if wide and start is None:
            start = y
        elif not wide and start is not None:
            if y - start >= BUTTON_MIN_HEIGHT * height:
                _count, left, right = rows[(start + y) // 2]
                bands.append((start, y, left, right))
            start = None
    return bands


def claim_buttons(image: Image.Image, label: re.Pattern[str]) -> list[ClaimButton]:
    """The pressable claim buttons on this frame, topmost first.

    A band only becomes a button when a word inside it matches, so this reads
    the same on the Today, Events and Special research screens without knowing
    which one is up, and stays off the orange reward tiles that carry an item's
    name instead.
    """
    scale = 1.0
    source = image
    if image.width > OCR_WIDTH:
        scale = image.width / OCR_WIDTH
        source = image.resize((OCR_WIDTH, max(1, round(image.height / scale))))

    bands = orange_bands(source)
    if not bands:
        return []
    try:
        boxes = gbl_vision.recognize(source)
    except gbl_vision.VisionOCRError:
        return []
    readable = [box for box in boxes if box.confidence >= OCR_CONFIDENCE]

    buttons: list[ClaimButton] = []
    for top, bottom, left, right in bands:
        inside = [
            box
            for box in readable
            if top <= box.center_y <= bottom and left <= box.center_x <= right
        ]
        named = next(
            (box for box in inside if label.search(gbl_vision.normalize(box.text))),
            None,
        )
        if named is None:
            continue
        buttons.append(
            ClaimButton(
                label=named.text.strip(),
                # The label, not the band's middle: the encounter medallion sits
                # inside the pill on the right and the whole row is the button,
                # so pressing the words is both correct and the safer half.
                point=[round(named.center_x * scale), round(named.center_y * scale)],
                top=round(top * scale),
                bottom=round(bottom * scale),
            )
        )
    return buttons


def plate_visible(image: Image.Image) -> bool:
    """Whether this frame carries a Pokemon's name and CP."""
    source = image
    if image.width > OCR_WIDTH:
        source = image.resize(
            (OCR_WIDTH, max(1, round(image.height * OCR_WIDTH / image.width)))
        )
    try:
        boxes = gbl_vision.recognize(source)
    except gbl_vision.VisionOCRError:
        return False
    return any(
        box.confidence >= OCR_CONFIDENCE
        and ENCOUNTER_PLATE.search(gbl_vision.normalize(box.text))
        for box in boxes
    )


async def encounter_on_screen(
    device: excellent_throw_android.AndroidDevice,
    reads: int = 1,
) -> bool:
    """Whether a Pokemon is standing in front of this phone right now.

    Wider than the throw loop's own ball test, deliberately.  Every gesture this
    module makes is either a claim tap or a scroll, and on an encounter screen a
    swipe is a thrown ball: razr threw three Great Balls sideways at a Fearow on
    10 Sep 2026 because the ball was in flight at the moment of the read, so the
    frame looked like no encounter at all and the loop scrolled for buttons.
    The plate stays put while the ball moves, so both are asked.

    `reads` is for the callers a wrong answer costs a ball.  Neither test is
    certain on one frame -- the ball merges with a Pokemon standing over it, and
    the plate is grey-on-grass that the OCR sometimes misses -- but both failing
    on several frames running means the screen really is something else.
    """
    for _read in range(max(1, reads)):
        image, _ts = await catch_awarded_android.capture_frame(device)
        if catch_awarded_android.ball_visible(device, image):
            return True
        if await asyncio.to_thread(plate_visible, image):
            return True
    return False


async def plate_on_screen(
    device: excellent_throw_android.AndroidDevice, reads: int = 1
) -> bool:
    """Whether a Pokemon's name and CP are on screen right now.

    Narrower than `encounter_on_screen` and the one to ask before throwing.  A
    round disc at the bottom of the screen is not proof of a Pokemon: the GO
    Snapshot screen razr wandered onto after a catch on 10 Sep 2026 draws its
    camera shutter exactly where the ball sits, and the run threw at that for
    eleven attempts.  Only a Pokemon has a plate over it.
    """
    for _read in range(max(1, reads)):
        image, _ts = await catch_awarded_android.capture_frame(device)
        if await asyncio.to_thread(plate_visible, image):
            return True
    return False


async def press_back(device: excellent_throw_android.AndroidDevice) -> None:
    """Leave whatever sub-screen the app has wandered onto."""
    if catch_awarded_android.is_ios(device):
        await device.press_back()
        return
    await device.device.shell("input keyevent KEYCODE_BACK")


async def read_claim_buttons(
    device: excellent_throw_android.AndroidDevice, label: re.Pattern[str]
) -> list[ClaimButton]:
    image, _ts = await catch_awarded_android.capture_frame(device)
    return await asyncio.to_thread(claim_buttons, image, label)


async def scroll_list(device: excellent_throw_android.AndroidDevice) -> None:
    """Drag the research list up by about a third of a screen.

    One swipe, like the throw: each `input` costs most of a second, so a stepped
    path would turn a scroll into a slow drag the list reads as a fling.
    """
    width, height = device.viewport
    await device.input_swipe(
        width // 2, round(height * 0.72), width // 2, round(height * 0.38), 320
    )
    await asyncio.sleep(SCREEN_SETTLE)


async def claim_one(
    device: excellent_throw_android.AndroidDevice,
    button: ClaimButton,
    args: argparse.Namespace,
    artifact_dir: Path,
) -> tuple[bool, bool]:
    """Press one claim button and see its reward through.  (claimed, caught).

    A research reward is either items or a Pokemon, and which one is behind a
    given button is not written on it -- the razr's six all showed the same "?"
    medallion.  So both endings are waited for: an encounter is thrown at, and
    anything else is pressed through with the same pass a catch uses, which is
    what knows the XP card and the caught Pokemon's page.

    The button going away is not the end of the claim.  It is the start of one:
    the row stops being orange as soon as the tap registers, and an encounter
    takes several more seconds to draw, so the watch carries on through
    ENCOUNTER_GRACE before an item payout is believed.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    await device.tap(button.point)
    await asyncio.sleep(CLAIM_SETTLE)

    deadline = time.monotonic() + args.claim_wait
    gone = False
    while True:
        if await encounter_on_screen(device):
            caught = await catch_awarded_android.catch_one(
                device, args, artifact_dir, lone_ball_reading=True
            )
            await catch_awarded_android.clear_catch_screens(device, artifact_dir)
            return True, caught

        # An item payout draws its own card over the list, and a berry or ball
        # sheet can be left standing over an encounter that has not opened yet.
        await catch_awarded_android.clear_catch_screens(device, artifact_dir)
        await catch_awarded_android.close_item_sheet(device)

        still = await read_claim_buttons(device, args.label)
        if not gone and not any(button.overlaps(other) for other in still):
            gone = True
            deadline = min(deadline, time.monotonic() + ENCOUNTER_GRACE)
        if time.monotonic() >= deadline:
            return gone, False
        await asyncio.sleep(CLAIM_POLL)


async def run_device(
    device: excellent_throw_android.AndroidDevice, args: argparse.Namespace
) -> int:
    base = args.artifacts / device.label
    base.mkdir(parents=True, exist_ok=True)
    claimed = 0
    caught = 0
    failures = 0
    scrolls = 0

    for cycle in range(1, args.max_claims + 1):
        buttons = await read_claim_buttons(device, args.label)
        if not buttons:
            # A payout that turned into an encounter can arrive after its claim
            # gave up waiting, and an encounter must be thrown at before
            # anything else happens on this phone: a swipe here is a thrown
            # ball, not a scroll, which is how razr spent three Great Balls
            # sideways at a Fearow on 10 Sep 2026.
            if not await plate_on_screen(device, reads=SCROLL_GUARD_READS):
                if await encounter_on_screen(device):
                    # Something ball-shaped, with no Pokemon named over it.  Not
                    # a screen to throw at and not a screen to swipe on either,
                    # so back out of it and look again.
                    print(f"[{device.label}] A screen with no Pokemon on it; backing out")
                    await press_back(device)
                    await asyncio.sleep(SCREEN_SETTLE)
                    failures += 1
                    if failures >= args.max_failures:
                        print(
                            f"[{device.label}] Gave up on a screen that is neither "
                            f"a claim nor a catch; {claimed} claimed, {caught} caught"
                        )
                        return 1
                    continue
            else:
                print(f"[{device.label}] An encounter is open; throwing at it")
                stray = base / f"stray-{cycle:02d}"
                stray.mkdir(parents=True, exist_ok=True)
                if await catch_awarded_android.catch_one(
                    device, args, stray, lone_ball_reading=True
                ):
                    caught += 1
                    failures = 0
                    scrolls = 0
                else:
                    # The encounter is up but nothing could be thrown at it,
                    # and the screen it is on has no claim button either.  Left
                    # to itself this retries the same standing Pokemon until
                    # `max_claims` runs out, which is how razr burned a whole
                    # run on one Fearow on 10 Sep 2026.
                    failures += 1
                    print(
                        f"[{device.label}] Encounter would not take a throw "
                        f"({failures}/{args.max_failures})"
                    )
                    if failures >= args.max_failures:
                        print(
                            f"[{device.label}] Gave up on an encounter that will "
                            f"not take a throw; {claimed} claimed, {caught} caught"
                        )
                        return 1
                await catch_awarded_android.clear_catch_screens(device, stray)
                continue

            # Nothing orange in view.  The rest of a research list lives below
            # the fold, so the screen is worth another look further down before
            # the run calls it finished.
            if scrolls >= args.scrolls:
                break
            scrolls += 1
            print(
                f"[{device.label}] No claim button in view; "
                f"scrolling ({scrolls}/{args.scrolls})"
            )
            await scroll_list(device)
            continue
        # A screenful with buttons on it earns the scroll budget back, so a long
        # list is worked all the way down.
        scrolls = 0

        if args.dry_run:
            print(f"[{device.label}] Dry-run: {len(buttons)} claim button(s) in view")
            for button in buttons:
                print(f"[{device.label}]   {button.label!r} at {button.point}")
            return 0

        button = buttons[0]
        print(
            f"[{device.label}] Claim {cycle}: {button.label!r} at {button.point} "
            f"({len(buttons)} in view)"
        )
        took, mon = await claim_one(device, button, args, base / f"claim-{cycle:02d}")
        if took:
            claimed += 1
            failures = 0
            if mon:
                caught += 1
            print(
                f"[{device.label}] Claimed {claimed}"
                f"{'; caught it' if mon else ''}"
            )
            continue

        # The button is still orange and still sitting there, which means the
        # tap never took.  Retried a bounded number of times, because a phone
        # that has stopped answering taps will not start on the next one.
        failures += 1
        print(f"[{device.label}] Claim did not take ({failures}/{args.max_failures})")
        if failures >= args.max_failures:
            print(
                f"[{device.label}] Gave up after {failures} claims in a row that "
                f"did not take; {claimed} claimed, {caught} caught"
            )
            return 1

    print(f"[{device.label}] Nothing left to claim; {claimed} claimed, {caught} caught")
    return 0


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    """The merged `catch.py` options; this loop reads its share of them."""
    from . import catch_rewards

    return catch_rewards.parse_args(arguments)
