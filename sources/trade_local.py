"""Two phones trading each other, found on screen by their words.

One trade, on both phones at once:

    friends list -> SEARCH the partner -> their avatar -> profile
    LOCAL TRADE -> the Pokemon picker -> type the search -> first tile
    NEXT -> the confirm screen -> CONFIRM -> back to the profile

Walked by hand on the android-two and android-three (4 Nov 2026) before any of it was
written.  Two things are never pressed: REMOTE TRADE, and a CONFIRM whose
Stardust price is not exactly 100.  A special trade, or one that adds a
Pokedex entry, prices far higher, so that single number is the guard against
both.  A phone that cannot reach the next screen cancels the trade on both.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Awaitable, Callable, Sequence

from PIL import Image

from . import gbl_vision
from .starter import Phone, StepFailed, close_button, find_box, walk_to_friends

OCRBox = gbl_vision.OCRBox
NORMAL_PRICE = 100
POLL = 1.0
# The gift card a friend's row opens: its X sits under OPEN, mid-screen.
GIFT_CLOSE = (0.5, 0.923)
# The trade screens' exit door, top-left (android-two 75/720, 105/1600).
DOOR = (0.104, 0.066)
SLOW_TIMEOUT = 25.0
IOS_DONE = (0.735, 0.600)   # the check beside the iPhone's text box


class TradeRefused(StepFailed):
    """The pair stopped before CONFIRM, on purpose."""


class LuckyAvailable(StepFailed):
    """A Lucky Trade is waiting between this pair: it is a person's to spend, so the
    pair stops before any trade (the operator's rule, 4 Nov)."""


class TradeOver(StepFailed):
    """Nothing more can trade today: the game's limit, or nothing to offer."""


def norm(text: str) -> str:
    return gbl_vision.normalize(text)


async def settle(phone: Phone, predicate: Callable[[Image.Image, list[OCRBox]], bool],
                 timeout: float = 15.0) -> tuple[bool, Image.Image, list[OCRBox]]:
    deadline = time.monotonic() + timeout
    while True:
        image, boxes = await phone.read()
        if predicate(image, boxes):
            return True, image, boxes
        if time.monotonic() >= deadline:
            return False, image, boxes
        await asyncio.sleep(POLL)


# --- typing -------------------------------------------------------------------------

async def type_text(phone: Phone, text: str) -> None:
    if phone.ios:
        await phone.target.device.type_text(text)
        return
    # `input text` splits on spaces and the shell would read & ! *, so the
    # term goes in single quotes with spaces as %s.
    escaped = text.replace(" ", "%s").replace("'", "")
    await asyncio.to_thread(phone.target._adb, "shell", f"input text '{escaped}'")


async def clear_field(phone: Phone) -> None:
    if phone.ios:
        from .raid_scan_ios import CLEAR_KEYS, DELETE_KEY
        await asyncio.to_thread(phone.target.device.driver.execute_script,
                                "mobile: keys", {"keys": [DELETE_KEY] * CLEAR_KEYS})
        return
    keys = " ".join(["KEYCODE_MOVE_END"] + ["KEYCODE_DEL"] * 90)
    await asyncio.to_thread(phone.target._adb, "shell", f"input keyevent {keys}")


async def submit(phone: Phone) -> None:
    """Close the keyboard so the filtered list draws (android-three drew it empty under one)."""
    if phone.ios:
        # The game's native text box has a blue check beside it; that, not a
        # newline, closes the keyboard (ios-one, 375x667: 246, 400).
        for _ in range(3):
            await asyncio.sleep(1.2)   # pressed while the keys were still landing, it did nothing
            if not await phone.ios_keyboard():
                break
            image = await phone.look()
            await phone.tap([int(image.width * IOS_DONE[0]), int(image.height * IOS_DONE[1])], image)
    else:
        await asyncio.to_thread(phone.target._adb, "shell", "input keyevent 66")
    await asyncio.sleep(2)


# --- who is who ---------------------------------------------------------------------

def own_name(image: Image.Image, boxes: Sequence[OCRBox]) -> str | None:
    """The trainer name under this account's avatar, top-left of the friends list."""
    add = find_box(boxes, "add friend")
    if add is None:
        return None
    # OCR can fold the name into the label beside it: "ExampleTrainer ADD FRIEND" (android-four).
    merged = re.match(r"\s*(\S{3,})\s+add\s+friend\s*$", add.text, re.IGNORECASE)
    if merged:
        return _clean_name(merged.group(1))
    candidates = [b for b in boxes
                  if b.center_x < add.x and abs(b.center_y - add.center_y) < image.height * 0.03
                  and len(b.text.strip()) >= 3]
    return _clean_name(candidates[0].text.replace(" ", "")) if candidates else None


def _clean_name(name: str) -> str:
    # A lowercase L reads as "|" or "I" in some fonts ("Trainer|Name", SE).
    return name.replace("|", "l")


def search_key(partner: str) -> str:
    """What is typed and matched: the name's first letters, where OCR slips least."""
    return norm(partner).replace(" ", "")[:7]


async def trainer_name(phone: Phone) -> str:
    await walk_to_friends(phone)
    image, boxes = await phone.read()
    name = own_name(image, boxes)
    if not name:
        raise StepFailed(f"could not read the trainer name (saved {phone.save(image, 'no-name')})")
    return name


# --- the partner's profile --------------------------------------------------------------

def profile_of(partner: str) -> Callable[[Image.Image, list[OCRBox]], bool]:
    key = search_key(partner)
    def ready(_image: Image.Image, boxes: list[OCRBox]) -> bool:
        named = any(norm(b.text).replace(" ", "").startswith(key) for b in boxes)
        return named and find_box(boxes, "local", exact=True) is not None
    return ready


async def open_partner(phone: Phone, partner: str) -> None:
    """From anywhere the starter can walk home from, to the partner's profile."""
    image, boxes = await phone.read()
    if profile_of(partner)(image, boxes):
        return
    await walk_to_friends(phone)
    image, boxes = await phone.read()
    search = find_box(boxes, "search", exact=True)
    if search is None:
        raise StepFailed(f"no SEARCH on the friends list (saved {phone.save(image, 'no-friend-search')})")
    await phone.tap_box(search, image)
    await asyncio.sleep(1.5)
    await clear_field(phone)
    key = search_key(partner)
    await type_text(phone, key)
    await submit(phone)

    def row(image: Image.Image, boxes: list[OCRBox]) -> OCRBox | None:
        # The search box itself starts with the key too (ios-one, 4 Nov): a row is
        # longer than what was typed and sits below the box.
        field = next((b for b in boxes if norm(b.text).replace(" ", "") == key), None)
        floor = field.center_y + image.height * 0.04 if field is not None else image.height * 0.2
        rows = [b for b in boxes if norm(b.text).replace(" ", "").startswith(key)
                and len(norm(b.text).replace(" ", "")) > len(key) and b.center_y > floor]
        return min(rows, key=lambda b: b.center_y) if rows else None

    hit = None
    for attempt in range(3):
        ok, image, boxes = await settle(phone, lambda i, b: row(i, b) is not None, 8)
        hit = row(image, boxes) if ok else None
        if hit is not None:
            break
        # "(1)" over an empty list: the match is found but not drawn (android-four,
        # android-three, 4 Nov).  Typing the search again draws it.
        field = next((b for b in boxes if norm(b.text).replace(" ", "").startswith(key[:4])
                      and b.center_y < image.height * 0.25), None)
        if field is None:
            break
        await phone.tap_box(field, image)
        await asyncio.sleep(1.2)
        await clear_field(phone)
        await type_text(phone, key if attempt % 2 else partner.lower().replace("|", "l"))
        await submit(phone)
    if hit is None:
        raise StepFailed(f"{partner} is not on the friends list (saved {phone.save(image, 'no-partner')})")
    # The avatar, left of the name: the name itself opens their gift first.
    await phone.tap([int(image.width * 0.15), hit.center_y + int(image.height * 0.012)], image)
    for _ in range(3):
        ok, image, boxes = await settle(
            phone, lambda i, b: profile_of(partner)(i, b) or find_box(b, "open", exact=True) is not None, 10)
        if profile_of(partner)(image, boxes):
            return
        if find_box(boxes, "open", exact=True) is not None:
            # Their gift, full screen.  Closed, never opened here.
            point = close_button(image) or [int(image.width * GIFT_CLOSE[0]), int(image.height * GIFT_CLOSE[1])]
            await phone.tap(point, image)
            await asyncio.sleep(2)
    raise StepFailed(f"{partner}'s profile did not open (saved {phone.save(image, 'no-profile')})")


# --- inside a trade -------------------------------------------------------------------

def picker(_image: Image.Image, boxes: list[OCRBox]) -> bool:
    return (find_box(boxes, "pokemon", exact=True) is not None
            and (find_box(boxes, "search", exact=True) is not None
                 or find_box(boxes, "special trades remaining") is not None
                 or find_box(boxes, "show evolutionary line") is not None))


def detail(_image: Image.Image, boxes: list[OCRBox]) -> bool:
    return find_box(boxes, "next", exact=True) is not None


def confirm_screen(_image: Image.Image, boxes: list[OCRBox]) -> bool:
    return find_box(boxes, "confirm", exact=True) is not None


def trade_limit(boxes: Sequence[OCRBox]) -> bool:
    return find_box(boxes, "trading limit", "trade limit", "traded the maximum") is not None


def price(image: Image.Image, boxes: Sequence[OCRBox]) -> int | None:
    """The Stardust figure printed right of CONFIRM, on its row."""
    confirm = find_box(boxes, "confirm", exact=True)
    if confirm is None:
        return None
    for box in sorted(boxes, key=lambda b: b.x):
        label = box.text.replace(",", "").replace(".", "").strip()
        digits = re.sub(r"[^\d]", "", label)
        if (box.x > confirm.x + confirm.width * 0.6 and abs(box.center_y - confirm.center_y) < image.height * 0.03
                and digits and box.center_x < image.width * 0.7):
            return int(digits)
    return None


def lucky(boxes: Sequence[OCRBox]) -> bool:
    """A Lucky Trade announces itself on the trade screen.  Not yet seen live:
    the word is the only thing relied on."""
    return find_box(boxes, "lucky trade", "lucky") is not None


def special(boxes: Sequence[OCRBox]) -> bool:
    return find_box(boxes, "special trade") is not None and find_box(boxes, "special trades remaining") is None


async def start_local(phone: Phone) -> None:
    image, boxes = await phone.read()
    local = find_box(boxes, "local", exact=True)
    if local is None:
        raise StepFailed("no LOCAL TRADE on the profile")
    if trade_limit(boxes):
        raise TradeOver("the daily trading limit is reached")
    await phone.tap_box(local, image, 0.012)
    ok, image, boxes = await settle(phone, lambda i, b: picker(i, b) or trade_limit(b), SLOW_TIMEOUT * 2)
    if trade_limit(boxes):
        raise TradeOver("the daily trading limit is reached")
    if not ok:
        raise StepFailed(f"the trade picker did not open (saved {phone.save(image, 'no-picker')})")


async def choose(phone: Phone, term: str, species: str) -> None:
    """Search the picker and open its first tradeable tile."""
    image, boxes = await phone.read()
    shown = next((b for b in boxes if "&" in b.text), None)
    if shown is None or not norm(term).endswith(norm(shown.text)[-14:]):
        # The magnifier reads as a letter: "Q Search" (ios-one).
        field = shown or next((b for b in boxes if norm(b.text).endswith("search")
                               and b.center_y < image.height * 0.3), None)
        if field is None:
            raise StepFailed(f"no search field in the picker (saved {phone.save(image, 'no-picker-field')})")
        await phone.tap_box(field, image)
        await asyncio.sleep(1.5)
        await clear_field(phone)
        await type_text(phone, term)
        await submit(phone)
    wanted = norm(species)
    for attempt in range(3):
        image, boxes = await phone.read()
        tiles = sorted((b for b in boxes if norm(b.text) == wanted
                        and b.center_y > image.height * 0.18), key=lambda b: (b.center_y // 40, b.center_x))
        if not tiles:
            raise TradeOver(f"no {species} left to offer (saved {phone.save(image, 'none-left')})")
        tile = tiles[min(attempt, len(tiles) - 1)]
        # The sprite, just above its name.
        await phone.tap([tile.center_x, tile.center_y - int(image.height * 0.03)], image)
        ok, image, boxes = await settle(phone, detail, 8)
        if ok:
            return
        # A greyed tile opens nothing: it cannot be traded.
    raise TradeOver(f"the {species} left cannot be traded (saved {phone.save(image, 'untradeable')})")


async def press(phone: Phone, word: str, after: Callable[[Image.Image, list[OCRBox]], bool],
                timeout: float = SLOW_TIMEOUT) -> tuple[Image.Image, list[OCRBox]]:
    image, boxes = await phone.read()
    button = find_box(boxes, word, exact=True)
    if button is None:
        raise StepFailed(f"no {word.upper()} (saved {phone.save(image, 'no-' + word)})")
    await phone.tap_box(button, image)
    ok, image, boxes = await settle(phone, after, timeout)
    if not ok:
        raise StepFailed(f"{word.upper()} led nowhere expected (saved {phone.save(image, 'after-' + word)})")
    return image, boxes


async def cancel(phone: Phone) -> None:
    """Leave whatever trade screen this is, answering YES to "cancel this trade?"."""
    for _ in range(5):
        image, boxes = await phone.read()
        if find_box(boxes, "local", exact=True) or find_box(boxes, "add friend"):
            return
        if find_box(boxes, "cancel this trade") or find_box(boxes, "won t trade"):
            yes = find_box(boxes, "yes", exact=True)
            if yes is not None:
                await phone.tap_box(yes, image)
                await asyncio.sleep(2)
                continue
        if phone.ios or confirm_screen(image, boxes):
            await phone.tap([int(image.width * DOOR[0]), int(image.height * DOOR[1])], image)
        else:
            await asyncio.to_thread(phone.target._adb, "shell", "input keyevent 4")
        await asyncio.sleep(2)


async def finish(phone: Phone, partner: str) -> str:
    """After CONFIRM: wait out the animation and close what it shows."""
    deadline = time.monotonic() + 90
    seen = "?"
    while time.monotonic() < deadline:
        image, boxes = await phone.read()
        if profile_of(partner)(image, boxes):
            return seen
        if trade_limit(boxes):
            raise TradeOver("the daily trading limit is reached")
        if find_box(boxes, "expired") is not None:
            ok = find_box(boxes, "ok", exact=True)
            if ok is not None:
                await phone.tap_box(ok, image)
            raise StepFailed("the trade expired")
        cp = next((b for b in boxes if re.fullmatch(r"cp\s*\d+", norm(b.text))), None)
        if cp is not None:
            seen = cp.text
        point = close_button(image)
        if point is not None and find_box(boxes, "confirm", exact=True) is None:
            await phone.tap(point, image)
            await asyncio.sleep(2)
            continue
        dismiss = find_box(boxes, "ok", exact=True) or find_box(boxes, "close", exact=True)
        if dismiss is not None:
            await phone.tap_box(dismiss, image)
        await asyncio.sleep(2)
    raise StepFailed(f"the trade never finished (saved {phone.save(image, 'unfinished')})")


async def both(*jobs: Awaitable) -> list:
    return list(await asyncio.gather(*jobs))


async def trade_once(a: Phone, b: Phone, names: dict[str, str], terms: dict[str, str],
                     species: dict[str, str], lucky_only: bool = False) -> None:
    """One trade, each phone giving its own species, or an exception and both cancelled."""
    partner = {a.name: names[b.name], b.name: names[a.name]}
    try:
        await both(open_partner(a, partner[a.name]), open_partner(b, partner[b.name]))
        for phone in (a, b):
            image, boxes = await phone.read()
            if lucky(boxes):
                raise LuckyAvailable(f"{phone.name}'s profile of {partner[phone.name]} offers a Lucky Trade "
                                     f"(saved {phone.save(image, 'lucky-available')})")
        await both(start_local(a), start_local(b))
        await both(choose(a, terms[a.name], species[a.name]), choose(b, terms[b.name], species[b.name]))
        await both(press(a, "next", confirm_screen), press(b, "next", confirm_screen))
        checks = []
        for phone in (a, b):
            image, boxes = await phone.read()
            cost = price(image, boxes)
            if lucky(boxes):
                raise LuckyAvailable(f"{phone.name}'s trade screen says this would be a Lucky Trade "
                                     f"(saved {phone.save(image, 'lucky-available')})")
            if special(boxes) or cost != NORMAL_PRICE:
                checks.append(f"{phone.name}: {'special trade' if special(boxes) else f'costs {cost} Stardust'}"
                              f" (saved {phone.save(image, 'refused-confirm')})")
        if checks:
            raise TradeRefused("not a normal 100-Stardust trade -- " + "; ".join(checks))
        if lucky_only:
            raise TradeRefused("a few-copies trade is a suggestion only; never made automatically")
        for phone in (a, b):
            image, boxes = await phone.read()
            await phone.tap_box(find_box(boxes, "confirm", exact=True), image)
        await both(finish(a, partner[a.name]), finish(b, partner[b.name]))
    except BaseException:
        await asyncio.gather(cancel(a), cancel(b), return_exceptions=True)
        raise
