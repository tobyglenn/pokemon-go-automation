#!/usr/bin/env python3
"""Remove friends who have sent no gift, carry no halo and sit at or under a heart count."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import re
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import config_paths, excellent_throw_android, gbl_vision, gift_android

ARTIFACT_ROOT = config_paths.state_dir() / "remove-friends"

# How far up the friendship ladder a friend can be and still be removed.  Zero
# is the default because nothing below it has been built up; each step above
# that is a day-counted level the operator has to ask for by name.
DEFAULT_HEARTS = 0
MAX_HEARTS = 3

# --- Reading the hearts ---
# Every row carries five circles under the avatar.  Measured on the razr
# (1224x2992, rows 391px) on 22 Sep 2026, in the three states a circle can be:
#   level reached  solid red-orange disc (~250,85,45) round a pale yellow heart
#                  (~245,225,160)
#   progress only  an amber ring (~250,145,45) round a white heart -- a partial
#                  arc while the days are counting, a closed ring once they are
#                  in and the level has not yet been awarded
#   nothing yet    a grey ring (~210,210,210) round a white heart
# The closed amber ring is the trap: Boggy2600 carries one solid disc and one
# closed ring and is a one-heart friend, not two.  So a heart is counted from
# the disc's colour and the heart's yellow, never from how much ring is lit.
#
# The circles sat between 0.72 and 0.82 of the row on every full row read, and
# across 0.07-0.23 of the screen; the band is wider on both sides so a phone
# that draws them a little differently still lands inside it.
HEART_BAND_Y = (0.66, 0.90)   # of row height
HEART_BAND_X = (0.02, 0.27)   # of screen width
HEART_CIRCLES = 5
# Five 36px circles on a 40px pitch read 200 wide by 37 tall.  Anything much
# off that shape is not the heart strip: a row cut off by the bottom of the
# screen, or a misread row.
HEART_STRIP_ASPECT = (4.5, 6.5)
# Of each circle's box.  A reached disc measured 0.20 red and 0.12 yellow; a
# progress ring and an empty ring carry neither.
HEART_MIN_RED = 0.10
HEART_MIN_YELLOW = 0.05
# A circle drawn in some colour none of the three states uses.  Only levels up
# to two hearts were on the razr to measure, so a higher level drawn in a new
# colour must read as "cannot tell", never as "not reached": undercounting a
# best friend's hearts is how the wrong friend gets removed.
HEART_MAX_FOREIGN = 0.08

# --- Reading the words ---
# Vision reads the list's names and activity lines cleanly at half the razr's
# width, in 0.14-0.36s a screen.
OCR_WIDTH = 640
OCR_CONFIDENCE = 0.4
# A friend with an unopened gift has "<name> sent you a Gift!" as their row's
# activity line, beside a gift box where the caught Pokemon usually sits.
GIFT_WORD = "gift"
# The name is the top line of the text column; the time ("Today", "2+ days
# ago") shares its line out on the right.
NAME_BAND_Y = (0.0, 0.35)     # of row height
TEXT_BAND_X = (0.25, 0.97)    # of screen width
NAME_MAX_X = 0.70             # of screen width; the name's own centre
# A row is only tapped when its name is clear of the header above the list and
# of the X and sort buttons that float over the bottom of it.
TAP_BAND_Y = (0.10, 0.88)     # of screen height

# --- Removing ---
# The profile opens at the top, with REMOVE FRIEND under the SETTINGS rule a
# drag below.  Found by its words, since how far down it sits depends on how
# long the profile is.
REMOVE_LABEL = "remove friend"
PROFILE_SCROLLS = 3
# A friend with a gift waiting opens the gift, not the profile: the postcard
# with an OPEN button.  A second chance to leave them be if the list's gift line
# was misread.
OPEN_LABEL = "open"
# The dialog names who is about to go: "Are you sure you want to unfriend
# IAmArc3us?".  YES is only pressed when that name is the row's own, so a tap
# that landed on the neighbouring row cannot remove the neighbour.
CONFIRM_WORD = "unfriend"
YES_LABEL = "yes"
NO_LABEL = "no"
NAME_MATCH = 0.8
# How long to wait on each screen of the removal before calling it missing.
SCREEN_WAIT = 6.0
SCREEN_POLL = 0.8
TAP_SETTLE = 2.2
BACK_SETTLE = 2.0

# --- Scrolling ---
# A slow drag, never a fling.  Fast flings on the razr reached the end of the
# list and landed as a tap, which opened killingjoker17's gift on 22 Sep 2026.
SCROLL_FROM = 0.78
SCROLL_TO = 0.40
SCROLL_MS = 900
SCROLL_SETTLE = 1.4


class FriendRemovalError(RuntimeError):
    pass


@dataclass(frozen=True)
class FriendRow:
    """One friend as their row on the list reads, in the phone's own pixels."""

    name: str | None
    point: tuple[int, int] | None
    hearts: int | None
    gift: bool
    halo: bool
    trade: bool
    top: int
    bottom: int

    def keep_reason(self, max_hearts: int) -> str | None:
        """Why this friend stays, or None when they are to be removed."""
        if self.name is None:
            return "name unreadable"
        if self.point is None:
            return "under the header or buttons"
        if self.hearts is None:
            return "hearts unreadable"
        if self.hearts > max_hearts:
            return f"{self.hearts} heart(s)"
        if self.gift:
            return "gift waiting"
        if self.halo:
            return "blue halo"
        if self.trade:
            return "trade in flight"
        return None


def as_frame(image: Image.Image) -> tuple[int, int, int, bytes]:
    """The shape the gift runner's list readers take a screen in."""
    rgba = image.convert("RGBA")
    return rgba.width, rgba.height, 0, rgba.tobytes()


def _red(red: int, green: int, blue: int) -> bool:
    """The solid disc of a reached level."""
    return red >= 220 and 55 <= green <= 125 and blue <= 95


def _yellow(red: int, green: int, blue: int) -> bool:
    """The pale heart inside a reached level's disc."""
    return red >= 230 and 195 <= green <= 245 and 110 <= blue <= 200 and red - blue >= 45


def _amber(red: int, green: int, blue: int) -> bool:
    """A progress ring, lit or closed."""
    return red >= 220 and 125 < green <= 190 and blue <= 100


def _grey(red: int, green: int, blue: int) -> bool:
    """An unlit ring."""
    return max(red, green, blue) - min(red, green, blue) <= 16 and 150 <= red <= 238


def _foreign(red: int, green: int, blue: int) -> bool:
    """A strong colour no heart state is drawn in, and not the halo's glow either."""
    if max(red, green, blue) - min(red, green, blue) < 60:
        return False
    if _red(red, green, blue) or _yellow(red, green, blue) or _amber(red, green, blue):
        return False
    # Anti-aliasing between the disc and the white card: orange-to-white blends
    # keep red on top and blue at the bottom.
    if red >= 220 and green >= blue:
        return False
    return not gift_android._halo_pixel(red, green, blue)


def count_hearts(image: Image.Image, row: tuple[int, int]) -> int | None:
    """How many friendship levels this row shows, or None when it cannot tell.

    Counted as the reached circles from the left.  A reached circle after one
    that is not, a strip the wrong shape, or a circle in an unknown colour all
    read as None -- a friend whose hearts cannot be read is kept.
    """
    pixels = image.convert("RGB").load()
    width = image.width
    top, bottom = row
    height = bottom - top
    y0 = top + int(height * HEART_BAND_Y[0])
    y1 = min(image.height, top + int(height * HEART_BAND_Y[1]))
    x0, x1 = (int(width * f) for f in HEART_BAND_X)

    left = right = upper = lower = None
    for y in range(y0, y1):
        for x in range(x0, x1):
            pixel = pixels[x, y]
            if _grey(*pixel) or _red(*pixel) or _amber(*pixel) or _yellow(*pixel):
                left = x if left is None else min(left, x)
                right = x if right is None else max(right, x)
                upper = y if upper is None else min(upper, y)
                lower = y
    if left is None:
        return None
    span_x = right - left + 1
    span_y = lower - upper + 1
    if not HEART_STRIP_ASPECT[0] <= span_x / span_y <= HEART_STRIP_ASPECT[1]:
        return None

    pitch = span_x / HEART_CIRCLES
    reached: list[bool] = []
    for k in range(HEART_CIRCLES):
        cx0 = left + round(k * pitch)
        cx1 = left + round((k + 1) * pitch)
        red = yellow = foreign = total = 0
        for y in range(upper, lower + 1):
            for x in range(cx0, cx1):
                pixel = pixels[x, y]
                total += 1
                red += _red(*pixel)
                yellow += _yellow(*pixel)
                foreign += _foreign(*pixel)
        if not total or foreign / total > HEART_MAX_FOREIGN:
            return None
        reached.append(red / total >= HEART_MIN_RED and yellow / total >= HEART_MIN_YELLOW)

    hearts = 0
    while hearts < HEART_CIRCLES and reached[hearts]:
        hearts += 1
    if any(reached[hearts:]):
        return None
    return hearts


def ocr(image: Image.Image) -> tuple[list[gbl_vision.OCRBox], float]:
    """Readable text on a downscaled copy, and the factor back to phone pixels."""
    scale = 1.0
    source = image
    if image.width > OCR_WIDTH:
        scale = image.width / OCR_WIDTH
        source = image.resize((OCR_WIDTH, max(1, round(image.height / scale))))
    try:
        boxes = gbl_vision.recognize(source)
    except gbl_vision.VisionOCRError:
        return [], scale
    return [box for box in boxes if box.confidence >= OCR_CONFIDENCE], scale


def read_rows(
    image: Image.Image, boxes: list[gbl_vision.OCRBox], scale: float
) -> list[FriendRow]:
    """Every whole friend row on this screen of the list, top first."""
    frame = as_frame(image)
    width, height = image.size
    text_x0, text_x1 = (width * f for f in TEXT_BAND_X)
    rows: list[FriendRow] = []
    spans = gift_android.friend_rows(frame)
    # The top row's upper rule can be missed and something inside the row
    # taken for it (M4C7R1CK5's avatar, on the razr), which shortens the row
    # and slides the heart band off the circles. The bottom rule holds, so
    # the hearts are looked for a full row's height above it.
    pitch = max((bottom - top for top, bottom in spans), default=0)
    for top, bottom in spans:
        row_height = bottom - top
        inside = [
            box
            for box in boxes
            if top <= box.center_y * scale < bottom
            and text_x0 <= box.center_x * scale <= text_x1
        ]
        name_top = top + row_height * NAME_BAND_Y[0]
        name_bottom = top + row_height * NAME_BAND_Y[1]
        names = sorted(
            (
                box
                for box in inside
                if name_top <= box.center_y * scale <= name_bottom
                and box.center_x * scale <= width * NAME_MAX_X
            ),
            key=lambda box: (box.y, box.x),
        )
        name = names[0].text.strip() if names else None
        point = None
        if names:
            x = round(names[0].center_x * scale)
            y = round(names[0].center_y * scale)
            if height * TAP_BAND_Y[0] <= y <= height * TAP_BAND_Y[1]:
                point = (x, y)
        gift = any(GIFT_WORD in gbl_vision.normalize(box.text) for box in inside)
        rows.append(
            FriendRow(
                name=name,
                point=point,
                hearts=count_hearts(image, (max(0, min(top, bottom - pitch)), bottom)),
                gift=gift,
                halo=gift_android.row_is_highlighted(frame, (top, bottom)),
                trade=gift_android.row_waits_on_a_trade(frame, (top, bottom)),
                top=top,
                bottom=bottom,
            )
        )
    return rows


def friend_count(boxes: list[gbl_vision.OCRBox]) -> int | None:
    """The number printed under the FRIENDS tab."""
    tab = next(
        (box for box in boxes if gbl_vision.normalize(box.text) == "friends"), None
    )
    if tab is None:
        return None
    below = [
        box
        for box in boxes
        if box.y > tab.y
        and abs(box.center_x - tab.center_x) <= tab.width
        and box.text.strip().replace(",", "").isdigit()
    ]
    if not below:
        return None
    nearest = min(below, key=lambda box: box.y)
    return int(nearest.text.strip().replace(",", ""))


def on_friends_list(image: Image.Image) -> bool:
    """Whether this is the FRIENDS tab's list, with rows on it."""
    frame = as_frame(image)
    return gift_android.is_friends_panel(*frame) and bool(gift_android.friend_rows(frame))


def _fold(text: str) -> str:
    """A name as the OCR sees it, with the look-alike glyphs made one."""
    folded = gbl_vision.normalize(text).replace(" ", "")
    return folded.translate(str.maketrans("il10", "llio"))


def same_name(expected: str, seen: str) -> bool:
    want = _fold(expected)
    got = _fold(seen)
    if not want or not got:
        return False
    if want == got or want in got:
        return True
    return difflib.SequenceMatcher(None, want, got).ratio() >= NAME_MATCH


def dialog_name(boxes: list[gbl_vision.OCRBox]) -> str | None:
    """Who the unfriend dialog is asking about, or None when it is not up."""
    ordered = sorted(boxes, key=lambda box: (box.y, box.x))
    text = " ".join(gbl_vision.normalize(box.text) for box in ordered)
    match = re.search(rf"\b{CONFIRM_WORD}\b (.+?)(?: if you remove|$)", text)
    if match is None:
        return None
    return match.group(1)


def find_label(
    boxes: list[gbl_vision.OCRBox], label: str, scale: float
) -> tuple[int, int] | None:
    """The centre of the box whose words are exactly `label`."""
    for box in boxes:
        if gbl_vision.normalize(box.text) == label:
            return round(box.center_x * scale), round(box.center_y * scale)
    return None


async def read_screen(
    device: excellent_throw_android.AndroidDevice,
) -> tuple[Image.Image, list[gbl_vision.OCRBox], float]:
    image, _ts = await excellent_throw_android.capture_frame(device)
    boxes, scale = await asyncio.to_thread(ocr, image)
    return image, boxes, scale


async def press_back(device: excellent_throw_android.AndroidDevice) -> None:
    await device.device.shell("input keyevent KEYCODE_BACK")
    await asyncio.sleep(BACK_SETTLE)


async def back_to_list(device: excellent_throw_android.AndroidDevice, presses: int = 3) -> bool:
    """Back out of a profile, gift or dialog until the list is showing again.

    Back is safe on every screen of this flow and the list keeps its scroll
    position behind it, measured on the razr.  It is never pressed on the list
    itself, where it closes the whole friends panel.
    """
    for _press in range(presses):
        image, _ts = await excellent_throw_android.capture_frame(device)
        if on_friends_list(image):
            return True
        await press_back(device)
    image, _ts = await excellent_throw_android.capture_frame(device)
    return on_friends_list(image)


async def wait_for(device, test, seconds: float = SCREEN_WAIT):
    """Poll the screen until `test(boxes, scale)` answers, or give up with None."""
    deadline = time.monotonic() + seconds
    while True:
        _image, boxes, scale = await read_screen(device)
        answer = test(boxes, scale)
        if answer is not None or time.monotonic() >= deadline:
            return answer
        await asyncio.sleep(SCREEN_POLL)


async def remove_friend(
    device: excellent_throw_android.AndroidDevice, row: FriendRow
) -> tuple[bool, str]:
    """Open this friend and unfriend them.  (removed, what happened)."""
    await device.tap(row.point)
    await asyncio.sleep(TAP_SETTLE)

    width, height = device.viewport
    remove_point = None
    for scroll in range(PROFILE_SCROLLS + 1):
        _image, boxes, scale = await read_screen(device)
        if find_label(boxes, OPEN_LABEL, scale) is not None:
            await back_to_list(device)
            return False, "opened a gift, not a profile; kept"
        remove_point = find_label(boxes, REMOVE_LABEL, scale)
        if remove_point is not None:
            break
        if scroll < PROFILE_SCROLLS:
            await device.input_swipe(
                width // 2, round(height * 0.80), width // 2, round(height * 0.35), 600
            )
            await asyncio.sleep(SCROLL_SETTLE)
    if remove_point is None:
        await back_to_list(device)
        return False, "no REMOVE FRIEND button on the profile"

    await device.tap(remove_point)
    await asyncio.sleep(TAP_SETTLE)
    asked = await wait_for(device, lambda boxes, _scale: dialog_name(boxes))
    if asked is None:
        await back_to_list(device)
        return False, "the unfriend dialog never came up"
    _image, boxes, scale = await read_screen(device)
    if not same_name(row.name, asked):
        no = find_label(boxes, NO_LABEL, scale)
        if no is not None:
            await device.tap(no)
            await asyncio.sleep(TAP_SETTLE)
        await back_to_list(device)
        return False, f"dialog named {asked!r}, not {row.name!r}; pressed NO"
    yes = find_label(boxes, YES_LABEL, scale)
    if yes is None:
        await back_to_list(device)
        return False, "no YES button on the dialog"
    await device.tap(yes)
    await asyncio.sleep(TAP_SETTLE)

    # The unfriend drops the profile and hands the list back.  Waited for, and
    # backed out to if it does not come, because the list is the only screen
    # the next row can be read from.
    deadline = time.monotonic() + SCREEN_WAIT
    while time.monotonic() < deadline:
        image, _ts = await excellent_throw_android.capture_frame(device)
        if on_friends_list(image):
            return True, "removed"
        await asyncio.sleep(SCREEN_POLL)
    if not await back_to_list(device):
        raise FriendRemovalError(f"Pressed YES on {row.name!r} and could not get back to the list")
    return True, "removed"


async def scroll_list(device: excellent_throw_android.AndroidDevice) -> None:
    width, height = device.viewport
    x = round(width * 0.62)
    await device.input_swipe(
        x, round(height * SCROLL_FROM), x, round(height * SCROLL_TO), SCROLL_MS
    )
    await asyncio.sleep(SCROLL_SETTLE)


def _log_removal(path: Path, device_label: str, row: FriendRow) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp}\t{device_label}\t{row.name}\t{row.hearts}\n")


async def run_device(
    device: excellent_throw_android.AndroidDevice, args: argparse.Namespace
) -> int:
    args.artifacts.mkdir(parents=True, exist_ok=True)
    record = args.artifacts / f"{device.label}-removed.tsv"
    removing = args.confirm_remove

    image, boxes, scale = await read_screen(device)
    if not on_friends_list(image):
        print(
            f"[{device.label}] Not on the friends list; open FRIENDS, sort by "
            "friendship level (lowest first) and start again"
        )
        return 1
    start_count = friend_count(boxes)
    if start_count is not None:
        print(f"[{device.label}] {start_count} friends")

    removed: list[str] = []
    listed: list[str] = []
    # Keyed on the folded name: Vision reads "SpanceBebop" and "Spance Bebop"
    # off the same row on different screens.
    passed: set[str] = set()
    shown_names: set[str] = set()
    failures = 0
    scrolls = 0
    last_names: list[str | None] | None = None

    while True:
        rows = read_rows(image, boxes, scale)
        for row in rows:
            if row.name is not None and _fold(row.name) not in shown_names:
                shown_names.add(_fold(row.name))
                reason = row.keep_reason(args.hearts)
                if args.verbose or reason is None:
                    shown = "?" if row.hearts is None else row.hearts
                    verdict = "remove" if reason is None else f"keep ({reason})"
                    print(f"[{device.label}]   {row.name}: {shown} heart(s) -> {verdict}")

        target = next(
            (
                row
                for row in rows
                if row.keep_reason(args.hearts) is None and _fold(row.name) not in passed
            ),
            None,
        )
        if target is not None and not removing:
            listed.append(target.name)
            passed.add(_fold(target.name))
            continue
        if target is not None:
            if len(removed) >= args.limit:
                print(f"[{device.label}] Reached --limit {args.limit}")
                break
            ok, what = await remove_friend(device, target)
            passed.add(_fold(target.name))
            if ok:
                removed.append(target.name)
                failures = 0
                _log_removal(record, device.label, target)
                print(f"[{device.label}] Removed {target.name} ({len(removed)})")
            else:
                failures += 1
                print(f"[{device.label}] Kept {target.name}: {what}")
                if failures >= args.max_failures:
                    print(f"[{device.label}] {failures} removals in a row did not go through; stopping")
                    break
            image, boxes, scale = await read_screen(device)
            if not on_friends_list(image):
                if not await back_to_list(device):
                    raise FriendRemovalError("Lost the friends list after a removal")
                image, boxes, scale = await read_screen(device)
            continue

        # Nothing left to remove on this screen.  The list is sorted lowest
        # friendship first, so a screen whose every readable row is above the
        # limit means everyone further down is too.
        readable = [row for row in rows if row.hearts is not None]
        if not args.whole_list and readable and all(
            row.hearts > args.hearts for row in readable
        ):
            print(f"[{device.label}] Every friend in view has more than {args.hearts} heart(s)")
            break
        names = [row.name for row in rows]
        if names == last_names:
            print(f"[{device.label}] End of the list")
            break
        if scrolls >= args.scrolls:
            print(f"[{device.label}] Scrolled {scrolls} screens; stopping")
            break
        last_names = names
        scrolls += 1
        await scroll_list(device)
        image, boxes, scale = await read_screen(device)
        if not on_friends_list(image):
            if not await back_to_list(device):
                raise FriendRemovalError("Scrolled off the friends list")
            image, boxes, scale = await read_screen(device)

    if not removing:
        print(
            f"[{device.label}] Dry run: {len(listed)} friend(s) would be removed; "
            "add --confirm-remove to remove them"
        )
        return 0
    _image, boxes, _scale = await read_screen(device)
    end_count = friend_count(boxes)
    counts = ""
    if start_count is not None and end_count is not None:
        counts = f"; friends {start_count} -> {end_count}"
    print(f"[{device.label}] Removed {len(removed)} friend(s){counts}")
    if removed:
        print(f"[{device.label}] Names written to {record}")
    return 0 if failures < args.max_failures else 1


async def run_all(args: argparse.Namespace) -> int:
    throw_args = excellent_throw_android.parse_args([])
    throw_args.devices = args.devices
    config = excellent_throw_android.load_config(None, throw_args)
    try:
        devices = await excellent_throw_android.prepare_devices(throw_args, config)
    except excellent_throw_android.AndroidExcellentThrowError as exc:
        if args.allow_empty:
            print(f"No Android device to remove friends on: {exc}")
            return 0
        raise
    from . import catch_awarded_android

    catch_awarded_android._apply_fleet_labels(devices)
    mode = "Removing" if args.confirm_remove else "Dry run, listing"
    print(
        f"{mode} friends with at most {args.hearts} heart(s), no gift and no halo on: "
        f"{', '.join(device.label for device in devices)}"
    )
    tasks = [asyncio.create_task(run_device(device, args)) for device in devices]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    status = 0
    for device, result in zip(devices, results):
        if isinstance(result, BaseException):
            print(f"[{device.label}] {result}")
            status = 1
        elif result:
            status = result
    return status


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devices", action="append", help="only these Android serials or fleet device names")
    parser.add_argument(
        "--hearts", type=int, default=DEFAULT_HEARTS, choices=range(0, MAX_HEARTS + 1),
        help="remove friends with at most this many hearts (default 0)",
    )
    parser.add_argument("--confirm-remove", action="store_true", help="actually remove; without it the run only lists who would go")
    parser.add_argument("--limit", type=int, default=100, help="most friends removed per phone")
    parser.add_argument("--scrolls", type=int, default=400, help="most list screens scrolled per phone")
    parser.add_argument("--max-failures", type=int, default=3, help="removals in a row that did not go through before stopping")
    parser.add_argument("--whole-list", action="store_true", help="scan to the end instead of stopping at the first screen above the limit")
    parser.add_argument("--verbose", action="store_true", help="print every row's reading, kept ones too")
    parser.add_argument("--artifacts", type=Path, default=ARTIFACT_ROOT, help="where the removed-names record goes")
    parser.add_argument("--allow-empty", action="store_true", help="exit 0 when no Android phone is attached")
    args = parser.parse_args(arguments)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.scrolls < 0:
        parser.error("--scrolls cannot be negative")
    from . import catch_awarded_android

    args.devices = catch_awarded_android._resolve_selection(args.devices)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(run_all(args))


if __name__ == "__main__":
    raise SystemExit(main())
