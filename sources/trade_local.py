"""Two phones trading each other, found on screen by their words.

One trade, on both phones at once:

    friends list -> SEARCH the partner -> their avatar -> profile
    LOCAL TRADE -> the Pokemon picker -> type the search -> first tile
    NEXT -> both CONFIRM buttons -> received Pokémon detail -> partner profile

Walked by hand on the android-two and android-three (4 Nov 2026) before any of it was
written.  Two things are never pressed: REMOTE TRADE, and a CONFIRM whose
Stardust price is not exactly 100.  A special trade, or one that adds a
Pokedex entry, prices far higher, so that single number is the guard against
both.  A phone that cannot reach the next screen cancels the trade on both.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
import time
from typing import Awaitable, Callable, Sequence

from PIL import Image

from . import config_paths, gbl_vision, trade_approval, trade_history
from .gbl_home_recovery import GAME_PACKAGE, on_map
from .starter import Phone, StepFailed, close_button, find_box, walk_to_friends

OCRBox = gbl_vision.OCRBox
NORMAL_PRICE = 100
POLL = 1.0
# The gift card a friend's row opens: its X sits under OPEN, mid-screen.
GIFT_CLOSE = (0.5, 0.923)
# XXS/XXL "New Weight Record" / "New Height Record" banner X on the post-trade card.
RECORD_CLOSE = (0.5, 0.93)
# The trade screens' exit door, top-left (android-two 75/720, 105/1600).
DOOR = (0.104, 0.066)
SLOW_TIMEOUT = 25.0
PICKER_CLEAR_X = 0.917  # the picker field's (x) clear button, ios-one 750x1334
FRIENDS_SEARCH_CLEAR_X = 0.87  # the friends list search field's teal X clear button
IOS_DONE = (0.735, 0.600)   # the check beside the iPhone's text box


class TradeRefused(StepFailed):
    """The pair stopped before CONFIRM, on purpose."""


class LuckyAvailable(StepFailed):
    """A Lucky Trade is waiting between this pair: it is a person's to spend, so the
    pair stops before any trade (the operator's rule, 4 Nov)."""


class LuckyRequired(TradeRefused):
    """This offer is reserved for a positively identified guaranteed Lucky Trade."""


class TradeOver(StepFailed):
    """Nothing more can trade today: the game's limit, or nothing to offer."""


class DailyLimit(TradeOver):
    """The game reported the general daily trading limit for the pair."""


class TradeOutcomeUnknown(RuntimeError):
    """Both phones must be reconciled before another exchange involving either."""


class TradeCancelled(StepFailed):
    """The game explicitly reported an expired or cancelled trade."""


@dataclass(frozen=True)
class TradeReceipt:
    screenshot: Path
    label: str | None
    lucky: bool | None


def _receipt_fields(image: Image.Image, boxes: list[OCRBox]) -> tuple[str, str | None, bool | None] | None:
    # The offer sheet also shows CP and HP, but those are ranges on two offers.
    if find_box(boxes, "confirm", "next", "waiting", "cancel", "expired"):
        return None
    cp = next((b for b in boxes if b.center_y < image.height * 0.30
               and re.fullmatch(r"cp\s*\d+", norm(b.text))), None)
    hp = next((b for b in boxes if re.fullmatch(r"\s*\d+\s*/\s*\d+\s*HP\s*", b.text, re.I)), None)
    record = find_box(boxes, "new weight record", "new height record", "new size record")
    if not ((cp and hp) or (record and (cp or hp))):
        return None
    label = None
    if hp:
        candidates = [b for b in boxes if 0 < hp.center_y - b.center_y < image.height * 0.12
                      and abs(b.center_x - hp.center_x) < image.width * 0.18
                      and any(c.isalpha() for c in b.text)
                      and norm(b.text) not in {"lucky pokemon", "lucky"}]
        if candidates:
            label = max(candidates, key=lambda b: b.center_y).text.strip()
    lucky_result = True if find_box(boxes, "lucky pokemon", exact=True) else None
    return cp.text if cp else hp.text, label, lucky_result


def _offer_signature(image: Image.Image, boxes: list[OCRBox]) -> tuple | None:
    # Both CP ranges, in their on-screen order, distinguish one exchange from
    # a different offer with the same discounted price.
    ranges = tuple(norm(b.text) for b in sorted(boxes, key=lambda b: b.center_y)
                   if re.fullmatch(r"cp\s*\d+\s*[-–—]\s*\d+", b.text.strip(), re.I))
    cost = price(image, boxes)
    if len(ranges) != 2 or cost is None or lucky(boxes) or trade_warning(boxes):
        return None
    return ranges, cost, special(boxes)


def _pending_files(names: Sequence[str]) -> list[Path]:
    return trade_history.pending_files(names)


def _check_trainer_pending(names):
    from . import trading_accounts
    aliases = trading_accounts.aliases()
    accounts = [aliases.get(name, name) for name in names]
    if len(accounts) != len(set(accounts)):
        raise TradeRefused("A trainer cannot trade with another handset signed into the same account")
    try:
        trade_history.PendingConfirmation(accounts).check()
    except (trade_history.ConfirmationStateError, OSError, ValueError) as exc:
        raise TradeOutcomeUnknown(str(exc)) from exc
    return accounts


def norm(text: str) -> str:
    return gbl_vision.normalize(text)


async def settle(phone: Phone, predicate: Callable[[Image.Image, list[OCRBox]], bool],
                 timeout: float = 15.0) -> tuple[bool, Image.Image, list[OCRBox]]:
    deadline = time.monotonic() + timeout
    while True:
        image, boxes = await phone.read()
        if predicate(image, boxes):
            return True, image, boxes
        notice = gbl_vision.safety_notice_point(boxes, image)
        if notice is not None:
            phone.say("dismissing safety/weather warning")
            await phone.tap(notice, image)
            await asyncio.sleep(1.5)
            continue
        if time.monotonic() >= deadline:
            return False, image, boxes
        await asyncio.sleep(POLL)


# --- typing -------------------------------------------------------------------------

async def type_text(phone: Phone, text: str) -> None:
    if phone.ios:
        dev = phone.target.device
        if hasattr(dev, "wda_keys"):
            await dev.wda_keys(text)
            return
        wda_port = getattr(getattr(getattr(dev, "config", None), "ios_device", {}), "get", lambda *_: None)("wda_local_port")
        if wda_port:
            from . import clear_pokemon
            await asyncio.to_thread(clear_pokemon._post_wda_keys, int(wda_port), text, clear_pokemon.IOS_WDA_KEYS_FREQUENCY)
            return
        await dev.type_text(text)
        return
    # `input text` splits on spaces and the shell would read & ! *, so the
    # term goes in single quotes with spaces as %s. Burst past 120 chars so
    # Android 10 does not drop key events on long filters.
    for pos in range(0, len(text), 120):
        burst = text[pos:pos + 120]
        escaped = burst.replace(" ", "%s").replace("'", "")
        await asyncio.to_thread(phone.target._adb, "shell", f"input text '{escaped}'")
        if pos + 120 < len(text):
            await asyncio.sleep(0.25)


async def clear_field(phone: Phone, keys: int | None = None) -> None:
    """Delete what the field holds; `keys` is enough for its longest text.

    iOS sends deletes one by one, about 0.34 s each: 120 took 41 s on the
    ios-one (4 Nov), long enough for the partner's screens to time out.
    """
    if phone.ios:
        dev = phone.target.device
        if hasattr(dev, "delete_keys"):
            await dev.delete_keys(keys)
            return
        from .raid_scan_ios import CLEAR_KEYS, DELETE_KEY
        await asyncio.to_thread(dev.driver.execute_script,
                                "mobile: keys", {"keys": [DELETE_KEY] * (keys or CLEAR_KEYS)})
        return
    del_count = max(90, int(keys)) if keys else 90
    batch_limit = 120
    first = True
    while del_count > 0:
        step = min(del_count, batch_limit)
        prefix = ["KEYCODE_MOVE_END"] if first else []
        payload = " ".join(prefix + ["KEYCODE_DEL"] * step)
        await asyncio.to_thread(phone.target._adb, "shell", f"input keyevent {payload}")
        del_count -= step
        first = False


async def submit(phone: Phone) -> None:
    """Close the keyboard so the filtered list draws (android-three drew it empty under one)."""
    if phone.ios:
        dev = phone.target.device
        if hasattr(dev, "tap_done"):
            if await dev.tap_done():
                return
        else:
            with contextlib.suppress(Exception):
                from appium.webdriver.common.appiumby import AppiumBy
                for btn in await asyncio.to_thread(dev.driver.find_elements, AppiumBy.ACCESSIBILITY_ID, "Done"):
                    if await asyncio.to_thread(btn.is_displayed):
                        await asyncio.to_thread(btn.click)
                        await asyncio.sleep(2)
                        return
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
                  and len(b.text.strip()) >= 3 and norm(b.text) not in {"online", "offline"}]
    return _clean_name(candidates[0].text.replace(" ", "")) if candidates else None


def _clean_name(name: str) -> str:
    # A lowercase L reads as "|" or "I" in some fonts ("Trainer|Name", SE).
    return name.replace("|", "l")


def search_key(partner: str) -> str:
    """What is typed and matched: the name's first letters, where OCR slips least."""
    return norm(partner).replace(" ", "")[:7]


async def trainer_name(phone: Phone) -> str:
    await leave_previous_trade(phone)
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
        named = any(norm(b.text).replace(" ", "").replace("|", "l").startswith(key) for b in boxes)
        return named and find_box(boxes, "local", exact=True) is not None
    return ready


async def open_partner(phone: Phone, partner: str) -> None:
    """From anywhere the starter can walk home from, to the partner's profile."""
    image, boxes = await phone.read()
    if profile_of(partner)(image, boxes):
        return
    await leave_previous_trade(phone, (image, boxes))
    await walk_to_friends(phone)
    image, boxes = await phone.read()
    search = find_box(boxes, "search", exact=True)
    if search is None:
        stale = next(
            (
                b for b in boxes
                if image.height * 0.18 <= b.center_y < image.height * 0.27
                and norm(b.text) not in {"me", "friends", "social", "add friend"}
                and not re.fullmatch(r"[\(\)\d\s]+", b.text.strip())
            ),
            None,
        )
        if stale is not None:
            await phone.tap([int(image.width * FRIENDS_SEARCH_CLEAR_X), stale.center_y], image)
            await asyncio.sleep(1.0)
            image, boxes = await phone.read()
            search = find_box(boxes, "search", exact=True) or stale
    if search is None:
        raise StepFailed(f"no SEARCH on the friends list (saved {phone.save(image, 'no-friend-search')})")
    await phone.tap_box(search, image)
    await asyncio.sleep(1.5)
    if phone.ios:
        await _clear_ios_native_text(phone)
    else:
        await clear_field(phone, 12)
    key = search_key(partner)
    await type_text(phone, key)
    await submit(phone)

    def row(image: Image.Image, boxes: list[OCRBox]) -> OCRBox | None:
        # The search box itself starts with the key too (ios-one, 4 Nov): a row is
        # longer than what was typed and sits below the box.
        # A name exactly the typed key is a row too, and "<name> sent you a
        # Gift!" under it is not: aiming from that line missed the avatar
        # (observed when searching for a trainer handle).
        # The box holds the key, or the whole name a retry types.
        typed = [
            b for b in boxes
            if norm(b.text).replace(" ", "").replace("|", "l").startswith(key[:4])
            and b.center_y < image.height * 0.27
        ]
        field = min(typed, key=lambda b: b.center_y) if typed else None
        floor = field.center_y + image.height * 0.04 if field is not None else image.height * 0.26
        rows = [
            b for b in boxes
            if norm(b.text).replace(" ", "").replace("|", "l").startswith(key)
            and "sent you" not in norm(b.text)
            and b.center_y > floor
        ]
        return min(rows, key=lambda b: b.center_y) if rows else None

    hit = None
    for attempt in range(3):
        ok, image, boxes = await settle(phone, lambda i, b: row(i, b) is not None, 8)
        hit = row(image, boxes) if ok else None
        if hit is not None:
            break
        # "(1)" over an empty list: the match is found but not drawn (android-four,
        # android-three, 4 Nov).  Typing the search again draws it.
        field = next(
            (
                b for b in boxes
                if norm(b.text).replace(" ", "").replace("|", "l").startswith(key[:4])
                and b.center_y < image.height * 0.27
            ),
            None,
        )
        if field is None:
            # The typing never reached a search box: the SE sat on the plain
            # friends list, SEARCH still showing, and the trade gave up on
            # another trainer (the same search worked on a replay).
            field = find_box(boxes, "search", exact=True)
            if field is None:
                break
        if phone.ios and norm(field.text) != "search":
            await phone.tap([int(image.width * FRIENDS_SEARCH_CLEAR_X), field.center_y], image)
            await asyncio.sleep(1.0)
            image, boxes = await phone.read()
            field = find_box(boxes, "search", exact=True) or field
        await phone.tap_box(field, image)
        await asyncio.sleep(1.2)
        if phone.ios:
            await _clear_ios_native_text(phone)
        else:
            await clear_field(phone, len(partner) + 5)
        await type_text(phone, key if attempt % 2 else partner.lower().replace("|", "l"))
        await submit(phone)
    if hit is None:
        front = getattr(getattr(phone, "target", None), "front_package", None)
        if front and await asyncio.to_thread(front) != GAME_PACKAGE:
            await phone.target.restore()
            raise StepFailed(f"{phone.name}: Pokémon GO left the foreground during partner search; reopened it")
        if not find_box(boxes, "friends", exact=True):
            raise StepFailed(f"{phone.name}: lost the friends screen during partner search "
                             f"(saved {phone.save(image, 'partner-search-interrupted')})")
        raise StepFailed(f"{partner} is not on the friends list (saved {phone.save(image, 'no-partner')})")
    # The avatar, left of the name: the name itself opens their gift first.
    # The avatar's centre sits further under the name on some display sizes.
    # A tap at +0.012 can miss, so each retry taps lower.
    row_y, row_image = hit.center_y, image
    for drop in (0.012, 0.03, 0.045):
        if drop != 0.012:
            image, boxes = await phone.read()
            if (profile_of(partner)(image, boxes) or find_box(boxes, "open", exact=True) is not None
                    or row(image, boxes) is None):
                break
        point = [int(row_image.width * 0.15), row_y + int(row_image.height * drop)]
        phone.say(f"tapping {partner}'s avatar at {point} on a {row_image.width}x{row_image.height} "
                  f"frame (row {hit.text!r} y={row_y}; saved {phone.save(row_image, 'avatar-tap')})")
        await phone.tap(point, row_image)
        ok, image, boxes = await settle(
            phone, lambda i, b: profile_of(partner)(i, b) or find_box(b, "open", exact=True) is not None, 6)
        if ok:
            break
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

def _counter(image: Image.Image, boxes: list[OCRBox]) -> OCRBox | None:
    """The "(7)" result counter under POKEMON."""
    return next((b for b in boxes if re.search(r"\(\d+\)", b.text)
                 and b.center_y < image.height * 0.2), None)


def _pokemon_header(image: Image.Image, boxes: list[OCRBox]) -> bool:
    """The POKEMON heading, even with a stray glyph merged in ("POKÉMON I",
    observed on Android), so long as it sits at the top of the screen."""
    if find_box(boxes, "pokemon", exact=True) is not None:
        return True
    return any(b.center_y < image.height * 0.12 and norm(b.text).split()[:1] == ["pokemon"]
               and len(norm(b.text)) <= len("pokemon") + 2 for b in boxes)


def picker(image: Image.Image, boxes: list[OCRBox]) -> bool:
    # Storage has TAGS and EGGS tabs beside POKEMON; the picker has neither.
    # A search left in its field ("ze", ios-one, 4 Nov) hides both the
    # placeholder and the special-trades line, leaving only the counter.
    return (_pokemon_header(image, boxes)
            and (find_box(boxes, "search", exact=True) is not None
                 or find_box(boxes, "special trades remaining") is not None
                 or find_box(boxes, "show evolutionary line") is not None
                 or (_counter(image, boxes) is not None and find_box(boxes, "tags", exact=True) is None
                     and find_box(boxes, "eggs", exact=True) is None)))


def picker_field(image: Image.Image, boxes: list[OCRBox]) -> OCRBox | None:
    """Whatever text the picker's search field shows, by its place under the counter."""
    counter = _counter(image, boxes)
    if counter is None:
        return None
    below = [b for b in boxes if counter.center_y + image.height * 0.03 < b.center_y < image.height * 0.25
             and "special trades" not in b.text.lower() and "evolutionary" not in b.text.lower()
             and norm(b.text) not in {"recent", "favorite", "favorites", "see less", "see more", "special"}
             and not b.text.upper().startswith("CP")]
    return min(below, key=lambda b: b.center_y, default=None)


def expired(boxes: list[OCRBox]) -> bool:
    if find_box(boxes, "ok", exact=True) is None:
        return False
    joined = " ".join(norm(b.text) for b in boxes)
    return "has expired" in joined or "trade expired" in joined


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


def _quoted_price(image: Image.Image, boxes: list[OCRBox]) -> int | None:
    """Require a single unambiguous numeric price next to the CONFIRM pill."""
    button = find_box(boxes, "confirm", exact=True)
    if button is None:
        return None
    row = [b.text.strip() for b in boxes if b.x > button.x + button.width * 0.6
           and abs(b.center_y - button.center_y) < image.height * 0.03 and b.center_x < image.width * 0.7
           and any(c.isdigit() for c in b.text)]
    if len(row) != 1 or not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)", row[0]):
        return None
    return int(row[0].replace(",", ""))


def lucky(boxes: Sequence[OCRBox]) -> bool:
    """Broad refusal signal for ordinary trades, including an unreadable Lucky badge."""
    return find_box(boxes, "lucky trade", "lucky") is not None


def trainer_identity(name: str) -> str:
    # Vision alternately reads the same vertical glyph as I, lowercase L, or
    # a pipe on the sign-in and friendship cards. Trainer handles have no spaces.
    return norm(name.translate(str.maketrans({"I": "l", "|": "l"}))).replace(" ", "")


def golden_trade_board(image: Image.Image) -> bool:
    """The Lucky trade backdrop at both screen edges, outside the Pokémon sprites."""
    # Lucky selection cards across Android and iPhone returned
    # the same six gold edge colors within 4 RGB levels. Normal trades were blue.
    samples = (
        (0.03, 0.30, (168, 127, 82)), (0.97, 0.30, (172, 125, 76)),
        (0.03, 0.56, (203, 170, 98)), (0.97, 0.56, (148, 110, 80)),
        (0.03, 0.78, (205, 177, 102)), (0.97, 0.78, (212, 190, 116)),
    )
    rgb = image if image.mode in ("RGB", "RGBA") else image.convert("RGB")
    hits = 0
    for x, y, expected in samples:
        pixel = rgb.getpixel((int(x * rgb.width), int(y * rgb.height)))
        if all(abs(actual - wanted) <= 24 for actual, wanted in zip(pixel[:3], expected)):
            hits += 1
    if hits >= 4:
        return True
    # On the smaller iPhone final board the two sprites, cost pill and text
    # panels obscure several edges. Gold still spans all three vertical bands;
    # the blue normal board measured essentially no ochre pixels in any band.
    shares = []
    for top, bottom in ((0.12, 0.37), (0.40, 0.65), (0.68, 0.91)):
        ochre = 0
        for col in range(24):
            for row in range(10):
                x = int(rgb.width * (col + 0.5) / 24)
                y = int(rgb.height * (top + (bottom - top) * (row + 0.5) / 10))
                red, green, blue = rgb.getpixel((x, y))[:3]
                if (110 <= red <= 247 and 75 <= green <= 225 and 35 <= blue <= 175
                        and 8 <= red - green <= 82 and 22 <= green - blue <= 115 and red - blue >= 54):
                    ochre += 1
        shares.append(ochre / 240)
    return hits >= 1 and min(shares) >= 0.48 and sum(shares) / 3 >= 0.68


def guaranteed_profile(image: Image.Image, boxes: list[OCRBox], partner: str) -> bool:
    # Both live friend profiles showed a "• Lucky Friend •" badge directly below
    # the partner's name and buddy, in the upper left of the LOCAL TRADE profile.
    if find_box(boxes, "local", exact=True) is None:
        return False
    name = next((b for b in boxes if trainer_identity(b.text) == trainer_identity(partner)
                 and b.center_y < image.height * 0.22 and b.center_x < image.width * 0.65), None)
    if name is None:
        return False
    badge_area = [b for b in boxes if 0 < b.center_y - name.center_y < image.height * 0.12
                  and b.center_x < image.width * 0.62]
    if any(norm(b.text) == "lucky friend" for b in badge_area):
        return True
    # A whole-frame read on the Moto rendered the orange badge "Lucks Friend".
    # Re-read that line at 3x and require the exact words on the crop.
    for line in badge_area:
        if "friend" not in norm(line.text):
            continue
        dx, dy = int(image.width * 0.018), int(image.height * 0.006)
        crop = image.crop((max(0, line.x - dx), max(0, line.y - dy),
                           min(image.width, line.x + line.width + dx),
                           min(image.height, line.y + line.height + dy)))
        scaled = crop.resize((crop.width * 3, crop.height * 3))
        if find_box(gbl_vision.recognize(scaled), "lucky friend", exact=True):
            return True
    return False


def guaranteed_offer(image: Image.Image, boxes: list[OCRBox], partner: str) -> bool:
    # A Pokémon nickname can literally be "Lucky Trade". The two offer names
    # sit immediately above their CP ranges; never treat either name as a badge.
    cp_rows = [b for b in boxes if re.fullmatch(r"cp\s*\d+\s*[-–—]\s*\d+", b.text.strip(), re.I)]
    if len(cp_rows) != 2 or not confirm_screen(image, boxes):
        return False
    def trainer_from_title(text):
        match = re.fullmatch(r"(.+?)\s+lv\.?\s*\d+\s*", text.strip(), re.I)
        return trainer_identity(match[1]) if match else None
    titles = [b for b in boxes if b.center_y < image.height * 0.16 and trainer_from_title(b.text)]
    expected = trainer_identity(partner)
    named_partner = any(trainer_from_title(b.text) == expected for b in titles)
    if not named_partner:
        # Gold sparkles behind the first letter in the Moto's title can turn M
        # into 1/H. Re-read its complete line; never accept a partial handle.
        for title in titles:
            crop = image.crop((max(0, title.x - 6), max(0, title.y - 3),
                               min(image.width, title.x + title.width + 6),
                               min(image.height, title.y + title.height + 3)))
            lines = gbl_vision.recognize(crop.resize((crop.width * 2, crop.height * 2)))
            if any(trainer_from_title(b.text) == expected for b in lines):
                named_partner = True
                break
    if not named_partner:
        return False
    if golden_trade_board(image):
        return True
    badges = [b for b in boxes if norm(b.text) in {"lucky trade", "guaranteed lucky trade"}]
    return any(all(not (0 < cp.center_y - badge.center_y < image.height * 0.06
                        and abs(cp.center_x - badge.center_x) < image.width * 0.25)
                   for cp in cp_rows) for badge in badges)


def _range_line(boxes, stat, *, current):
    spread = rf"{stat}\s*(\d+)\s*[-–—]\s*(\d+)"
    expr = (rf"{stat}\s*(\d+)\s*(?:->|[>›»→•·&])?\s*" if current else "") + spread
    matches = []
    for box in boxes:
        match = re.fullmatch(expr, box.text.strip(), re.I)
        if match:
            matches.append((box, [int(s) for s in match.groups()]))
        elif current:
            # The game draws current HP, an arrow, and the new range. Vision
            # sometimes returns two boxes and omits the arrow (live iPhone).
            start = re.fullmatch(rf"{stat}\s*(\d+)\s*[/›>]?", box.text.strip(), re.I)
            if start is None:
                continue
            right = [re.fullmatch(r"(?:->|[>›»→•·])?\s*" + spread, peer.text.strip(), re.I) for peer in boxes
                     if peer.x > box.center_x and abs(peer.center_y - box.center_y) < box.height * 0.65]
            ranges = [m for m in right if m is not None]
            if len(ranges) == 1:
                matches.append((box, [int(start[1]), int(ranges[0][1]), int(ranges[0][2])]))
    return sorted(matches, key=lambda item: item[0].center_y)


def _offer_label(image, boxes, cp):
    labels = [b for b in boxes if 0 < cp.center_y - b.center_y < image.height * 0.065
              and abs(cp.x - b.x) < image.width * 0.13 and any(c.isalpha() for c in b.text)]
    return max(labels, key=lambda b: b.center_y).text.strip() if labels else None


def _selected_range(image, boxes, stat):
    parsed = _range_line(boxes, stat, current=True)
    if parsed:
        return parsed
    # Sprites and motion can break OCR on a full-screen read. On a live
    # On Android, 2x OCR of just the row recovered both exact CP labels.
    for row in boxes:
        if not re.match(rf"{stat}\s*\d", row.text, re.I) or not re.search(r"[-–—]", row.text):
            continue
        crop = image.crop((max(0, row.x - 8), max(0, row.y - 5),
                           min(image.width, row.x + row.width + 8), min(image.height, row.y + row.height + 5)))
        recovered = _range_line(gbl_vision.recognize(crop.resize((crop.width * 2, crop.height * 2))),
                                stat, current=True)
        if len(recovered) == 1:
            return [(row, recovered[0][1])]
    return []


def _selected_identity(phone, image, boxes, supplied, trainer):
    cp = _selected_range(image, boxes, "CP")
    hp = _selected_range(image, boxes, "HP")
    date_box = next((b for b in boxes if re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", b.text)), None)
    label = _offer_label(image, boxes, cp[0][0]) if len(cp) == 1 else None
    if (not supplied.get("unique") or not supplied.get("species") or not supplied.get("dex")
            or not detail(image, boxes) or trade_warning(boxes) or len(hp) != 1 or not date_box or not label
            or cp[0][1][0] != supplied.get("cp")):
        raise LuckyRequired(f"{phone.name}: cannot identify a unique selected Pokémon, its CP and catch date "
                            f"(saved {phone.save(image, 'unverified-individual')})")
    # Move names and their power are drawn between HP and the catch location.
    # Capture left-hand names paired with a right-hand power, excluding locality.
    powers = [b for b in boxes if b.center_x > image.width * 0.65 and re.fullmatch(r"\d+", b.text.strip())]
    move_boxes = [b for b in sorted(boxes, key=lambda b: b.center_y)
                  if hp[0][0].center_y < b.center_y < date_box.center_y and b.center_x < image.width * 0.65
                  and any(c.isalpha() for c in b.text)
                  and any(abs(power.center_y - b.center_y) <= image.height * 0.013 for power in powers)]
    last_move = move_boxes[-1].center_y if move_boxes else hp[0][0].center_y
    locality = " ".join(norm(b.text) for b in sorted(boxes, key=lambda b: b.center_y)
                        if last_move < b.center_y < date_box.center_y and b.center_x < image.width * 0.65
                        and any(c.isalpha() for c in b.text))
    moves = [norm(b.text) for b in move_boxes]
    known_moves = [norm(move) for move in supplied.get("moves") or []]
    if (supplied.get("hp") is not None and hp[0][1][0] != supplied["hp"]) or (known_moves and known_moves != moves):
        raise LuckyRequired(f"{phone.name}: selected HP or move text differs from the individually scanned Pokémon "
                            f"(saved {phone.save(image, 'unverified-individual')})")
    return {**supplied, "form": supplied.get("form"), "trainer": trainer, "label": label,
            "hp": hp[0][1][0], "cp_range": cp[0][1][1:], "hp_range": hp[0][1][1:], "moves": moves,
            "locality": locality,
            "caught": re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", date_box.text)[0],
            "detail_screenshot": str(phone.save(image, "guaranteed-individual"))}


def _check_final_individuals(phone, image, boxes, incoming, outgoing):
    cps, hps = _range_line(boxes, "CP", current=False), _range_line(boxes, "HP", current=False)
    if len(cps) == len(hps) == 2:
        for (cp, cr), (_, hr), item in zip(cps, hps, (incoming, outgoing)):
            label = _offer_label(image, boxes, cp)
            if not label or norm(label) != norm(item["label"]) or cr != item["cp_range"] or hr != item["hp_range"]:
                break
        else:
            return
    raise LuckyRequired(f"{phone.name}: final offer does not match both identified individuals "
                        f"(saved {phone.save(image, 'guaranteed-identity-mismatch')})")


def special(boxes: Sequence[OCRBox]) -> bool:
    return any("special trade" in norm(box.text) and "special trades remaining" not in norm(box.text)
               for box in boxes)


def trade_warning(boxes: Sequence[OCRBox]) -> bool:
    """Refuse unexpected pre-confirm trade warnings (Buddy Level, Mega Level, Max Move)."""
    return (
        find_box(
            boxes,
            "are you sure you want to trade",
            "buddy level will be reset",
            "mega level",
            "max move",
        )
        is not None
    )


def max_move_warning(boxes: Sequence[OCRBox]) -> bool:
    """Pre-confirm warning whose only caveat is resetting Max Move levels."""
    joined = " ".join(norm(b.text) for b in boxes)
    return (
        "max move" in joined
        and "buddy level" not in joined
        and "mega level" not in joined
        and find_box(boxes, "yes", exact=True) is not None
    )


def special_confirm_dialog(boxes: Sequence[OCRBox]) -> bool:
    """Match Pokémon GO's post-CONFIRM Special Trade modal even when it wraps across lines."""
    joined = " ".join(norm(b.text) for b in boxes)
    return (
        "make a special trade" in joined
        and find_box(boxes, "yes", exact=True) is not None
        and find_box(boxes, "no", exact=True) is not None
    )


async def start_local(phone: Phone) -> None:
    image, boxes = await phone.read()
    if trade_limit(boxes):
        raise DailyLimit("the daily trading limit is reached")
    local = find_box(boxes, "local", exact=True)
    if local is None:
        raise StepFailed("no LOCAL TRADE on the profile")
    await phone.tap_box(local, image, -0.035)
    for attempt in range(4):
        ok, image, boxes = await settle(
            phone, lambda i, b: picker(i, b) or trade_limit(b) or expired(b)
            or find_box(b, "trading service temporarily") is not None, SLOW_TIMEOUT * 2 if attempt else 12)
        if find_box(boxes, "trading service temporarily"):
            shot = phone.save(image, "trade-service-unavailable")
            dismiss = find_box(boxes, "ok", exact=True)
            if dismiss:
                await phone.tap_box(dismiss, image)
            raise StepFailed(f"{phone.name}: trading service temporarily unavailable (saved {shot})")
        if expired(boxes):
            # One notice per earlier cancelled trade arrives late; on iOS closing
            # it leaves the wait up, while on Android it can drop back to the profile.
            await phone.tap_box(find_box(boxes, "ok", exact=True), image)
            await asyncio.sleep(1.5)
            after_img, after_boxes = await phone.read()
            retry_local = find_box(after_boxes, "local", exact=True)
            if retry_local is not None:
                await phone.tap_box(retry_local, after_img, -0.035)
            continue
        if not ok and attempt < 2:
            retry_local = find_box(boxes, "local", exact=True)
            if retry_local is not None:
                await phone.tap_box(retry_local, image, -0.035)
                continue
            if attempt == 0:
                continue
        break
    if trade_limit(boxes):
        raise DailyLimit("the daily trading limit is reached")
    if not ok:
        raise StepFailed(f"the trade picker did not open (saved {phone.save(image, 'no-picker')})")


@contextlib.asynccontextmanager
async def check_trade_lobby(a, b):
    """From the two open partner profiles, check admission without selecting a Pokémon."""
    await asyncio.to_thread(_check_trainer_pending, [a.name, b.name])
    try:
        await both(start_local(a), start_local(b))
        yield
    except DailyLimit:
        trade_history.record_pair_limit([a.name, b.name])
        raise
    finally:
        await asyncio.gather(cancel(a), cancel(b))


def _storage_field_fallback(image: Image.Image, boxes: list[OCRBox]) -> OCRBox | None:
    """Locate the storage search field from the tab row / CP rows when OCR misses it."""
    from . import raid_scan_android, vision_ocr

    text_boxes = [vision_ocr.TextBox(b.text, b.x, b.y, b.width, b.height) for b in boxes]
    point = raid_scan_android.search_bar_fraction(text_boxes, image)
    if point is None:
        return None
    cx = int(image.width * point[0])
    cy = int(image.height * point[1])
    return OCRBox("search", 1.0, max(0, cx - 20), max(0, cy - 10), 40, 20)


async def _clear_ios_native_text(phone: Phone) -> bool:
    """Tap the native keyboard bar's 'Clear text' button on iOS when exposed."""
    dev = phone.target.device
    if hasattr(dev, "clear_text"):
        return await dev.clear_text()
    try:
        from appium.webdriver.common.appiumby import AppiumBy
        driver = dev.driver
    except Exception:
        return False
    try:
        elements = await asyncio.to_thread(driver.find_elements, AppiumBy.ACCESSIBILITY_ID, "Clear text")
        for elem in elements or ():
            if await asyncio.to_thread(elem.is_displayed):
                await asyncio.to_thread(elem.click)
                await asyncio.sleep(0.8)
                return True
    except Exception:
        return False
    return False


async def search_picker(phone: Phone, term: str, force: bool = False) -> None:
    """Put `term` in the picker's search field, unless it is there already."""
    image, boxes = await phone.read()
    if expired(boxes):
        ok_btn = find_box(boxes, "ok", exact=True)
        if ok_btn is not None:
            await phone.tap_box(ok_btn, image)
        raise StepFailed("the trade expired")
    shown = next((b for b in boxes if "&" in b.text and b.center_y < image.height * 0.25), None)
    on_overlay = (
        shown is None
        and find_box(boxes, "recent") is not None
        and find_box(boxes, "special") is not None
    )
    if force or shown is None or not norm(term).endswith(norm(shown.text)[-14:]):
        # The magnifier reads as a letter: "Q Search" (ios-one).
        # The battle-party picker draws it at 0.302h on the SE (5 Nov).
        field = shown or next((b for b in boxes if norm(b.text).endswith("search")
                               and b.center_y < image.height * 0.34), None)
        # A search left from a hand-made trade ("ze", ios-one, 4 Nov) is
        # neither: find the field by its place under the counter.
        field = field or picker_field(image, boxes) or _storage_field_fallback(image, boxes)
        if field is None:
            raise StepFailed(f"no search field in the picker (saved {phone.save(image, 'no-picker-field')})")
        has_pill = shown is not None or not norm(field.text).endswith("search")
        if (phone.ios or has_pill) and not on_overlay:
            # Deletes do nothing in the picker's field on iOS, and tapping the pill (x)
            # clears long Android queries in one tap without overflowing clear_field.
            await phone.tap([int(image.width * PICKER_CLEAR_X), field.center_y], image)
            await asyncio.sleep(1.0)
            image, boxes = await phone.read()
            # The field does not move when its (x) clears it.  Looking it up
            # again from scratch found the SE's sprite row instead (y 110 of
            # 667, field at 202), so nothing was typed and the battle picker
            # showed no buddy (23-24 Nov); the old place wins over the guesses.
            cleared = OCRBox("search", 1.0, field.x, field.y, field.width, field.height)
            field = (
                next((b for b in boxes if norm(b.text).endswith("search") and b.center_y < image.height * 0.34), None)
                or cleared
            )
            if field is None:
                raise StepFailed(f"no search field in the picker (saved {phone.save(image, 'no-picker-field')})")
        await phone.tap_box(field, image)
        await asyncio.sleep(1.5)
        if phone.ios:
            await _clear_ios_native_text(phone)
        else:
            from . import clear_pokemon
            existing = await clear_pokemon._android_edit_text(phone)
            if existing is None or (existing.strip() and existing.strip().casefold() != "search"):
                await clear_field(phone, max(len(existing or "") + 20, 150))
                await asyncio.sleep(0.4)
                remaining = await clear_pokemon._android_edit_text(phone)
                if remaining and remaining.strip().casefold() != "search":
                    await clear_field(phone, max(len(remaining) + 40, 150))
        await type_text(phone, term)
        if not phone.ios:
            from . import clear_pokemon
            expected = term.strip().casefold()
            wait_end = time.monotonic() + 4.0
            while time.monotonic() < wait_end:
                typed = await clear_pokemon._android_edit_text(phone)
                if typed is None or typed.strip().casefold() == expected:
                    break
                await asyncio.sleep(0.4)
        await submit(phone)


async def choose(phone: Phone, term: str, species: str) -> None:
    """Search the picker and open its first tradeable tile."""
    await search_picker(phone, term)
    wanted = norm(species)

    def tiles_of(image: Image.Image, boxes: list[OCRBox]) -> list[OCRBox]:
        names = [b for b in boxes if norm(b.text) == wanted and b.center_y > image.height * 0.18]
        if not names:
            # The grid draws faded at first and Vision then reads only the CP
            # labels.  The search named the
            # species, so every tile is one: stand a name in under each CP.
            counter = _counter(image, boxes)
            shown = re.search(r"\((\d+)\)", counter.text) if counter else None
            if shown and int(shown.group(1)) > 0:
                names = [OCRBox(wanted, b.confidence, b.x, int(b.y + image.height * 0.085), b.width, b.height)
                         for b in boxes if re.fullmatch(r"cp\s*\d+", norm(b.text))
                         and b.center_y > image.height * 0.18]
        return sorted(names, key=lambda b: (b.center_y // 40, b.center_x))

    # The first search after the picker opens reads (0) for a while before
    # its tiles draw (ios-one, 4 Nov: 0 at 2 s, 11 on the next search).
    ok, image, boxes = await settle(phone, lambda i, b: bool(tiles_of(i, b)), 10)
    if not ok:
        await search_picker(phone, term, force=True)
        ok, image, boxes = await settle(phone, lambda i, b: bool(tiles_of(i, b)), 10)
    for attempt in range(3):
        if attempt:
            image, boxes = await phone.read()
        tiles = tiles_of(image, boxes)
        if not tiles:
            if find_box(boxes, "recent") is not None and find_box(boxes, "special") is not None:
                raise StepFailed(f"search stayed on Recent/Special overlay (saved {phone.save(image, 'search-overlay')})")
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
                timeout: float = SLOW_TIMEOUT, *, allow_lucky: bool = False,
                allow_max_move_reset: bool = False) -> tuple[Image.Image, list[OCRBox]]:
    # White on the green pill, NEXT is missed now and then (ios-one, 4 Nov).
    _, image, boxes = await settle(phone, lambda _i, b: find_box(b, word, exact=True) is not None, 8)
    button = find_box(boxes, word, exact=True)
    if button is None:
        # A whole-frame read can turn it into "EXEN"; the pill's band on its
        # own reads true (ios-one Weedle, 4 Nov).
        band = (0, int(image.height * 0.70), image.width, int(image.height * 0.92))
        button = find_box(await asyncio.to_thread(gbl_vision.recognize, image, band), word, exact=True)
    if button is None:
        raise StepFailed(f"no {word.upper()} (saved {phone.save(image, 'no-' + word)})")
    # NEXT is drawn greyed until the partner has chosen too, and a tap on it
    # then does nothing: tap again while it is still there.
    deadline = time.monotonic() + timeout * 2
    while True:
        if word == "next" and not allow_lucky and (lucky(boxes) or golden_trade_board(image)):
            raise LuckyAvailable(f"{phone.name}: reserved Lucky selection (saved {phone.save(image, 'lucky-available')})")
        await phone.tap_box(button, image)
        ok, image, boxes = await settle(phone, lambda i, b: after(i, b) or trade_warning(b), 8)
        if trade_warning(boxes):
            if word == "next" and allow_max_move_reset and max_move_warning(boxes):
                yes_btn = find_box(boxes, "yes", exact=True)
                if yes_btn is not None:
                    phone.say("accepting Max Move reset warning on donor trade")
                    await phone.tap_box(yes_btn, image)
                    ok, image, boxes = await settle(phone, after, 10)
                    if ok:
                        return image, boxes
                    button = find_box(boxes, word, exact=True)
                    if button is not None and time.monotonic() <= deadline:
                        continue
            no_btn = find_box(boxes, "no", exact=True)
            if no_btn is not None:
                await phone.tap_box(no_btn, image)
                await asyncio.sleep(1.5)
            raise TradeRefused(
                f"{phone.name}: trade warning dialog refused (saved {phone.save(image, 'trade-warning')})"
            )
        if ok:
            return image, boxes
        button = find_box(boxes, word, exact=True)
        if button is None or time.monotonic() > deadline:
            raise StepFailed(f"{word.upper()} led nowhere expected (saved {phone.save(image, 'after-' + word)})")


async def leave_previous_trade(phone: Phone, frame=None) -> None:
    """Exit a stale lobby under the device lock before navigating from the map."""
    image, boxes = frame if frame is not None else await phone.read()
    if find_box(boxes, "tags", exact=True) or find_box(boxes, "eggs", exact=True):
        return
    if (picker(image, boxes) or expired(boxes) or confirm_screen(image, boxes)
            or detail(image, boxes) and (find_box(boxes, "hp") or find_box(boxes, "cp"))):
        await asyncio.to_thread(_check_trainer_pending, [phone.name])
        await cancel(phone)


async def cancel(phone: Phone) -> None:
    """Leave whatever trade screen this is, answering YES to "cancel this trade?"."""
    for _ in range(5):
        image, boxes = await phone.read()
        if (find_box(boxes, "local", exact=True) or find_box(boxes, "add friend")
                or find_box(boxes, "tags", exact=True) or on_map(image)):
            return
        if expired(boxes):
            ok_btn = find_box(boxes, "ok", exact=True)
            if ok_btn is not None:
                await phone.tap_box(ok_btn, image)
                await asyncio.sleep(1.5)
                continue
        if find_box(boxes, "cancel this trade") or find_box(boxes, "won t trade"):
            yes = find_box(boxes, "yes", exact=True)
            if yes is not None:
                await phone.tap_box(yes, image)
                await asyncio.sleep(2)
                continue
        if phone.ios and await phone.ios_keyboard():
            from . import starter as _starter
            await phone.tap(
                [int(image.width * _starter.SEARCH_CANCEL[0]), int(image.height * _starter.SEARCH_CANCEL[1])],
                image,
            )
            await asyncio.sleep(1.0)
            if not await phone.ios_keyboard():
                continue
        if picker(image, boxes) or (detail(image, boxes) and (find_box(boxes, "hp") or find_box(boxes, "cp"))):
            # The detail grid and the picker X occupy the same bottom-centre
            # position on the SE and Moto. Back and the top-left door miss them.
            point = [image.width // 2, round(image.height - image.width * 0.13)]
            if picker(image, boxes):
                point = close_button(image) or point
            await phone.tap(point, image)
            await asyncio.sleep(1.5)
            continue
        if phone.ios or confirm_screen(image, boxes):
            await phone.tap([int(image.width * DOOR[0]), int(image.height * DOOR[1])], image)
        else:
            await asyncio.to_thread(phone.target._adb, "shell", "input keyevent 4")
        await asyncio.sleep(2)


async def finish(phone: Phone, partner: str, special_ok: bool = False,
                 receipts: dict[str, TradeReceipt] | None = None,
                 offer: tuple | None = None) -> str:
    """After CONFIRM: wait out the animation and close what it shows.

    A special trade asks once more after CONFIRM: "Do you want to make a
    Special Trade?" YES/NO. YES only when the
    caller allowed a special trade; otherwise NO, and the trade is off.
    """
    deadline = time.monotonic() + 90
    seen = "?"
    retry_after = time.monotonic() + 3
    repeats = 0
    while time.monotonic() < deadline:
        image, boxes = await phone.read()
        if profile_of(partner)(image, boxes):
            # Only a peer receipt can substantiate this exchange if we missed ours.
            if seen == "?" and receipts is not None:
                phone.save(image, "profile-without-receipt")
            return seen
        if special_confirm_dialog(boxes):
            answer = find_box(boxes, "yes" if special_ok else "no", exact=True)
            if answer is not None:
                await phone.tap_box(answer, image)
                await asyncio.sleep(2)
                if not special_ok:
                    raise TradeRefused("the game asked to make a special trade; answered NO")
                continue
        if trade_limit(boxes):
            raise DailyLimit("the daily trading limit is reached")
        if expired(boxes) or find_box(boxes, "trade was canceled", "trade was cancelled"):
            raise TradeCancelled(f"{phone.name}: the game cancelled the trade")
        if offer and seen == "?" and repeats < 2 and time.monotonic() >= retry_after:
            confirm = find_box(boxes, "confirm", exact=True)
            if confirm and _offer_signature(image, boxes) == offer:
                await phone.tap_box(confirm, image)
                repeats += 1
                retry_after = time.monotonic() + 4
                await asyncio.sleep(POLL)
                continue
        fields = _receipt_fields(image, boxes)
        if fields:
            seen, label, lucky_result = fields
            if receipts is not None and phone.name not in receipts:
                shot = phone.save(image, f"trade-received-{date.today():%Y%m%d}")
                receipts[phone.name] = TradeReceipt(shot, label, lucky_result)
                print(f"[{phone.name}] received {label or 'Pokémon'} ({seen}): {shot}", flush=True)
            point = close_button(image)
            if point is None and find_box(boxes, "new weight record", "new height record", "new size record"):
                point = [int(image.width * RECORD_CLOSE[0]), int(image.height * RECORD_CLOSE[1])]
            if point is not None:
                await phone.tap(point, image)
            else:
                dismiss = find_box(boxes, "ok", "close", exact=True)
                if dismiss is not None:
                    await phone.tap_box(dismiss, image)
        await asyncio.sleep(2)
    if seen != "?":
        return seen
    raise TradeOutcomeUnknown(f"{phone.name}: result unreadable (saved {phone.save(image, 'unfinished')})")


async def both(*jobs: Awaitable) -> list:
    """Both phones' steps; when one fails the other is stopped before the raise,
    so it does not keep tapping while the other phone cancels the trade."""
    tasks = [asyncio.ensure_future(job) for job in jobs]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def trade_once(a: Phone, b: Phone, names: dict[str, str], terms: dict[str, str],
                     species: dict[str, str], lucky_only: bool = False,
                     max_special_price: int | None = None,
                     choosers: dict[str, Callable[[Phone], Awaitable[None]]] | None = None,
                     on_confirmed: Callable[[dict[str, TradeReceipt]], None] | None = None,
                     require_guaranteed_lucky: bool = False,
                     guaranteed_details: dict[str, dict] | None = None, propose_special: bool = False,
                     propose_only: bool = False, reselect_approval_id: str | None = None,
                     allow_max_move_reset: Sequence[str] | set[str] = (),
                     daily_cap: int | None = None) -> dict[str, TradeReceipt]:
    """Exchange one pair, returning the observed post-trade Pokémon sheets.

    A return to the partner profile without a receipt from either phone leaves
    the pair pending reconciliation. A history callback runs while both phones
    are still marked pending, before the next exchange can start.

    `max_special_price` allows a special trade (special_trade.py) at a price up
    to that much Stardust; left out, only a normal 100-Stardust trade confirms.
    `choosers` replaces a phone's pick of its tile (special_trade.py's lowest CP).
    """
    if require_guaranteed_lucky and max_special_price is not None:
        raise ValueError("a guaranteed offer must use the specifically quoted price")
    if propose_only and reselect_approval_id:
        raise ValueError("proposal-only mode cannot submit an approved exchange")
    if (propose_special or propose_only or reselect_approval_id) and not require_guaranteed_lucky:
        raise ValueError("a guaranteed proposal requires Lucky verification")
    accounts = await asyncio.to_thread(_check_trainer_pending, (a.name, b.name))
    partner = {a.name: names[b.name], b.name: names[a.name]}
    pending = _pending_files(accounts)
    confirming = False
    reservation = None
    reserved = False

    async def pick(phone: Phone) -> None:
        chooser = (choosers or {}).get(phone.name)
        if chooser is not None:
            await chooser(phone)
        else:
            await choose(phone, terms[phone.name], species[phone.name])

    try:
        await both(open_partner(a, partner[a.name]), open_partner(b, partner[b.name]))
        if require_guaranteed_lucky:
            async def check_friend(phone: Phone):
                peer = partner[phone.name]
                ok, image, _ = await settle(phone, lambda i, b: guaranteed_profile(i, b, peer), timeout=5)
                if not ok:
                    raise LuckyRequired(f"{phone.name}: could not verify Lucky Friend status for "
                                        f"{peer} (saved {phone.save(image, 'lucky-profile-unverified')})")
                phone.save(image, "verified-lucky-profile")
            await both(check_friend(a), check_friend(b))
        else:
            for phone in (a, b):
                image, boxes = await phone.read()
                if lucky(boxes) or guaranteed_profile(image, boxes, partner[phone.name]):
                    raise LuckyAvailable(f"{phone.name}'s profile of {partner[phone.name]} offers a Lucky Trade "
                                         f"(saved {phone.save(image, 'lucky-available')})")
        await both(start_local(a), start_local(b))
        await both(pick(a), pick(b))
        individuals = {}
        if require_guaranteed_lucky:
            async def identify(phone):
                supplied = (guaranteed_details or {}).get(phone.name, {})
                for attempt in range(3):
                    image, boxes = await phone.read()
                    try:
                        individuals[phone.name] = _selected_identity(phone, image, boxes, supplied, names[phone.name])
                        return
                    except LuckyRequired:
                        if attempt == 2 or not detail(image, boxes) or trade_warning(boxes):
                            raise
                        await asyncio.sleep(0.45)
            await both(identify(a), identify(b))
        allowed_max_move = set(allow_max_move_reset)
        next_frames = await both(
            press(a, "next", confirm_screen, allow_lucky=require_guaranteed_lucky,
                  allow_max_move_reset=a.name in allowed_max_move),
            press(b, "next", confirm_screen, allow_lucky=require_guaranteed_lucky,
                  allow_max_move_reset=b.name in allowed_max_move),
        )
        if require_guaranteed_lucky:
            for phone, frame in zip((a, b), next_frames):
                if frame is not None:
                    phone.save(frame[0], "guaranteed-quote-screen")
        costs = {}
        async def guarded_button(phone: Phone) -> tuple[OCRBox, Image.Image, tuple | None]:
            ok, image, boxes = await settle(
                phone,
                lambda i, b: expired(b) or trade_warning(b) or (not require_guaranteed_lucky and lucky(b))
                or (confirm_screen(i, b) and price(i, b) is not None),
                timeout=8,
            )
            if expired(boxes):
                raise TradeCancelled(f"{phone.name}: the trade expired before confirmation")
            if require_guaranteed_lucky:
                if not guaranteed_offer(image, boxes, partner[phone.name]):
                    raise LuckyRequired(f"{phone.name}: no verified Lucky Trade guarantee on this offer "
                                        f"(saved {phone.save(image, 'lucky-offer-unverified')})")
                peer = b if phone is a else a
                _check_final_individuals(phone, image, boxes, individuals[peer.name], individuals[phone.name])
                individuals[phone.name]["offer_screenshot"] = str(phone.save(image, "verified-lucky-offer"))
            elif lucky(boxes) or golden_trade_board(image):
                raise LuckyAvailable(f"{phone.name}'s trade screen says this would be a Lucky Trade "
                                     f"(saved {phone.save(image, 'lucky-available')})")
            if trade_warning(boxes):
                raise TradeRefused(f"{phone.name}: trade warning (saved {phone.save(image, 'trade-warning')})")
            button = find_box(boxes, "confirm", exact=True)
            cost = _quoted_price(image, boxes) if require_guaranteed_lucky else price(image, boxes)
            if not ok or button is None or cost is None:
                raise StepFailed(f"{phone.name}: could not read CONFIRM and its price "
                                 f"(saved {phone.save(image, 'no-confirm')})")
            budget = NORMAL_PRICE if max_special_price is None else max_special_price
            allow_quoted_special = require_guaranteed_lucky and propose_special
            if (cost < 1 or (cost > budget and not allow_quoted_special)
                    or (special(boxes) and max_special_price is None and not allow_quoted_special)):
                raise TradeRefused(f"{phone.name}: refused cost {cost} or special trade "
                                   f"(saved {phone.save(image, 'refused-confirm')})")
            costs[phone.name] = cost
            repeat_offer = None if require_guaranteed_lucky else _offer_signature(image, boxes)
            return button, image, repeat_offer

        await both(guarded_button(a), guarded_button(b))
        if lucky_only:
            raise TradeRefused("a few-copies trade is a suggestion only; never made automatically")
        intent = {"pair": [a.name, b.name], "offered": species, "at": time.time(),
                  "guaranteed_lucky": require_guaranteed_lucky}
        if require_guaranteed_lucky:
            offer = {"participants": individuals, "costs": costs.copy(), "special": propose_special}
            token = reselect_approval_id or await trade_approval.request(offer, propose_only=propose_only)
            intent["guaranteed_approval_id"] = token
        reservation = trade_history.PendingConfirmation(accounts, intent)
        await asyncio.to_thread(reservation.reserve)
        reserved = True
        if daily_cap is not None and not await asyncio.to_thread(trade_history.has_budget, accounts, daily_cap):
            raise TradeOver(f"shared trade count reached the configured daily cap of {daily_cap}")
        # The lobby can expire while waiting for approval or a peer journal.
        buttons = await both(guarded_button(a), guarded_button(b))
        if require_guaranteed_lucky:
            trade_approval.consume(token, {**offer, "costs": costs.copy()}, reselected=bool(reselect_approval_id))
        confirming = True
        # Dispatch both already validated buttons together. A second full-screen
        # OCR between taps was losing the other phone during the transition.
        await both(a.tap_box(*buttons[0][:2]), b.tap_box(*buttons[1][:2]))
        receipts: dict[str, TradeReceipt] = {}
        special_ok = max_special_price is not None or (require_guaranteed_lucky and propose_special)
        results = await asyncio.gather(finish(a, partner[a.name], special_ok, receipts, buttons[0][2]),
                                       finish(b, partner[b.name], special_ok, receipts, buttons[1][2]),
                                       return_exceptions=True)
        declined = next((r for r in results if isinstance(r, (TradeRefused, TradeCancelled, TradeOver))), None)
        if not receipts and declined and all(isinstance(r, (TradeRefused, TradeCancelled, TradeOver)) for r in results):
            await asyncio.to_thread(reservation.release)
            reserved = False
            confirming = False
            raise declined
        # Leave the exact evidence on disk if recording history or releasing
        # one of the two phones fails. Never clear a marker to retry a trade.
        observed = {
            device: {"label": receipt.label, "lucky": receipt.lucky, "screenshot": str(receipt.screenshot)}
            for device, receipt in receipts.items()
        }
        if observed:
            await asyncio.to_thread(reservation.update, receipts=observed)
        if not receipts or declined:
            observations = ", ".join(str(r) for r in results)
            raise TradeOutcomeUnknown(f"cannot verify the exchange between {a.name} and {b.name}: {observations}; "
                                      f"reconcile {pending[0]}")
        if require_guaranteed_lucky and any(receipt.lucky is False for receipt in receipts.values()):
            raise TradeOutcomeUnknown(f"received Pokémon conflicts with the Lucky guarantee; reconcile {pending[0]}")
        with reservation.recording():
            if on_confirmed is not None:
                on_confirmed(receipts)
            else:
                trade_history.append_event({"pair": intent["pair"], "give": species,
                                            "reason": "trade", "receipts": observed})
        if require_guaranteed_lucky:
            trade_approval.completed(intent["guaranteed_approval_id"], observed)
        await asyncio.to_thread(reservation.release)
        return receipts
    except trade_approval.ProposalReady:
        await asyncio.gather(cancel(a), cancel(b), return_exceptions=True)
        raise
    except BaseException as exc:
        if confirming:
            if isinstance(exc, (TradeOutcomeUnknown, KeyboardInterrupt, asyncio.CancelledError)):
                raise
            raise TradeOutcomeUnknown(f"confirmation interrupted; reconcile {pending[0]}: {exc}") from exc
        if reserved:
            try:
                await asyncio.to_thread(reservation.release)
            except Exception as release_error:
                raise TradeOutcomeUnknown(f"could not release unused confirmation at {pending[0]}: {release_error}") from exc
        if isinstance(exc, trade_history.ConfirmationStateError):
            raise TradeOutcomeUnknown(str(exc)) from exc
        if isinstance(exc, DailyLimit):
            trade_history.record_pair_limit([a.name, b.name])
        # Both screens, before cancelling: the failure is often the other phone's.
        for phone in (a, b):
            with contextlib.suppress(Exception):
                print(f"[{phone.name}] at the failure: {phone.save(await phone.look(), 'trade-failed')}", flush=True)
        await asyncio.gather(cancel(a), cancel(b), return_exceptions=True)
        raise
