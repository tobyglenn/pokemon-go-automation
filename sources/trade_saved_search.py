"""Favorite-search controls shared by Pokémon storage and the local trade picker."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageOps

from . import clear_pokemon, config_paths, gbl_vision, starter, trade_local


def _ios_editor_visible(driver):
    from appium.webdriver.common.appiumby import AppiumBy
    return bool(driver.find_elements(AppiumBy.ACCESSIBILITY_ID, "Clear text") or
                driver.find_elements(AppiumBy.IOS_PREDICATE,
                    "(type == 'XCUIElementTypeKeyboard' OR type == 'XCUIElementTypeToolbar') AND visible == 1"))


def _copy_ios_selection(driver):
    """Use the native editing menu when unsolicited hardware Copy is ignored."""
    from appium.webdriver.common.appiumby import AppiumBy
    clear = driver.find_elements(AppiumBy.ACCESSIBILITY_ID, "Clear text")
    if not clear:
        return False
    rect = clear[0].rect
    # The text occupies the same native toolbar immediately left of Clear.
    x = max(6, rect["x"] - 50)
    y = rect["y"] + rect["height"] / 2
    driver.execute_script("mobile: doubleTap", {"x": x, "y": y})
    time.sleep(.3)
    for _ in range(2):
        driver.execute_script("mobile: tap", {"x": max(6, x - 18), "y": y})
        time.sleep(.3)
    for name in ("Select All", "Copy"):
        items = driver.find_elements(AppiumBy.IOS_PREDICATE,
                                     f"type == 'XCUIElementTypeMenuItem' AND name == '{name}' AND visible == 1")
        if not items:
            return False
        items[0].click()
        time.sleep(.35)
    return True


def read_ios_editor(driver):
    """Read the native Unity keyboard input, including text outside its viewport."""
    from appium.webdriver.common.appiumby import AppiumBy
    def unavailable(reason):
        driver._pogo_editor_reason = reason
        return None
    predicate = "(type == 'XCUIElementTypeTextField' OR type == 'XCUIElementTypeTextView' OR type == 'XCUIElementTypeSearchField') AND visible == 1"
    fields = driver.find_elements(AppiumBy.IOS_PREDICATE, predicate)
    values = [f.get_attribute("value") for f in fields if f.is_displayed()]
    if len(values) == 1 and isinstance(values[0], str):
        return values[0]
    # Unity's keyboard toolbar on the SE hides its input from accessibility.
    # Copy the focused selection and require a fresh device pasteboard revision
    # before reading it. This also handles searches wider than the screen.
    udid = driver.capabilities.get("udid") or driver.capabilities.get("appium:udid")
    if not udid or not _ios_editor_visible(driver):
        return unavailable("native keyboard/toolbar unavailable")
    command = ["xcrun", "devicectl", "device", "pasteboard"]
    device = ["--device", udid, "--timeout", "5"]

    def revision():
        data = subprocess.check_output([*command, "info", *device, "--json-output", "-"],
                                       stderr=subprocess.DEVNULL, timeout=6)
        return json.loads(data)["result"]["changeCount"]

    def copy_focused():
        driver.execute_script("mobile: keys", {"keys": [
            {"key": "a", "modifierFlags": 16}, {"key": "c", "modifierFlags": 16},
        ]})

    try:
        clear = driver.find_elements(AppiumBy.ACCESSIBILITY_ID, "Clear text")
        if clear:
            rect = clear[0].rect
            if isinstance(rect, dict):
                driver.execute_script("mobile: tap", {"x": max(5, rect["x"] - 30),
                                      "y": rect["y"] + rect["height"] / 2})
        time.sleep(.45)
        # Copy before inspecting even metadata. CoreDevice refuses metadata
        # for a preexisting Handoff clipboard. Read only the new local game's
        # selection, then require a second confirmed Copy revision.
        copy_focused()
        try:
            before = revision()
        except subprocess.CalledProcessError:
            if not _copy_ios_selection(driver):
                return unavailable("could not establish a local game selection")
            before = revision()
        copy_focused()
        copied = revision()
        if copied == before:
            # The SE may acknowledge the hardware shortcut before UIKit has
            # copied the focused text into the revision CoreDevice reads.
            time.sleep(.25)
            copied = revision()
            if copied == before:
                if not _copy_ios_selection(driver):
                    return unavailable("copy shortcut and Select All menu did not yield a fresh clip")
                copied = revision()
                if copied == before:
                    return unavailable("native Copy left the prior clipboard revision")
        value = subprocess.check_output([*command, "paste", *device, "--quiet"],
                                        stderr=subprocess.DEVNULL, timeout=6).decode()
        return value if revision() == copied else unavailable("clipboard changed during read")
    except (OSError, subprocess.SubprocessError, KeyError, ValueError) as exc:
        return unavailable(f"CoreDevice clipboard unavailable ({type(exc).__name__}: {exc})")


def _paste_ios_editor(driver, term):
    udid = driver.capabilities.get("udid") or driver.capabilities.get("appium:udid")
    if not udid or not _ios_editor_visible(driver):
        return False
    try:
        subprocess.run(["xcrun", "devicectl", "device", "pasteboard", "copy", "--device", udid,
                        "--quiet", "--timeout", "5"], input=term.encode(), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=6, check=True)
        driver.execute_script("mobile: keys", {"keys": [
            {"key": "a", "modifierFlags": 16}, {"key": "v", "modifierFlags": 16},
        ]})
        return read_ios_editor(driver) == term
    except (OSError, subprocess.SubprocessError):
        return False


async def dismiss_ios_return(phone):
    """Dismiss Unity's plain iOS keyboard using its visible Return button."""
    from appium.webdriver.common.appiumby import AppiumBy

    dev = phone.target.device
    if hasattr(dev, "dismiss_ios_return"):
        return await dev.dismiss_ios_return()
    driver = dev.driver
    if not hasattr(driver, "find_elements"):
        return False
    predicate = ("(type == 'XCUIElementTypeButton' OR type == 'XCUIElementTypeKey') "
                 "AND name == 'Return' AND visible == 1")
    # After Copy, the game can replace its toolbar with a plain keyboard.
    # Wait for the native Return control or the keyboard to finish closing.
    for _ in range(8):
        if not await phone.ios_keyboard():
            return True
        matches = await asyncio.to_thread(driver.find_elements, AppiumBy.IOS_PREDICATE, predicate)
        if len(matches) == 1:
            await asyncio.to_thread(matches[0].click)
        await asyncio.sleep(.35)
    return not await phone.ios_keyboard()


@asynccontextmanager
async def _ios_pasteboard_turn():
    """Universal Clipboard is shared across iPhones, even on different Macs."""
    import fcntl

    path = config_paths.state_dir() / "storage-favorites" / "ios-pasteboard.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Take this on the coordinator before dispatching remote read/paste RPCs.
    # A separate open file description also serializes tasks in one process.
    with path.open("a") as guard:
        while True:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(.15)
        try:
            yield
        finally:
            # Give the last local copy time to propagate through Handoff.
            await asyncio.sleep(.55)


async def paste_ios_editor(phone, term):
    """Replace the active Unity search in one paste and verify the complete result."""
    async with _ios_pasteboard_turn():
        dev = phone.target.device
        if hasattr(dev, "paste_editor"):
            return await dev.paste_editor(term)
        if hasattr(dev.driver, "execute_script"):
            return await asyncio.to_thread(_paste_ios_editor, dev.driver, term)
        return False


async def editor_text(phone):
    if not phone.ios:
        return await clear_pokemon._android_edit_text(phone)
    async with _ios_pasteboard_turn():
        dev = phone.target.device
        for attempt in range(2):
            if hasattr(dev, "read_editor"):
                value = await dev.read_editor()
            elif hasattr(dev.driver, "find_elements"):
                value = await asyncio.to_thread(read_ios_editor, dev.driver)
            else:
                return None
            if value is not None:
                return value
            if attempt == 0:
                await asyncio.sleep(.55)
        return None


def name_for(term):
    return "T" + hashlib.sha256(term.encode()).hexdigest()[:7].upper()


def _label_key(label):
    # The game wraps in the middle of a word and omits the space at a line break.
    return "".join(label.casefold().split())


def favorite_tiles(image, boxes, *, scrolled=False):
    """Read each magnifier's own label; whole-frame OCR often merges four labels."""
    points = icons(image, boxes, "favorite", scrolled=scrolled)
    tiles = []
    for x, y in points:
        w = image.width
        rect = (max(0, round(x - .113*w)), round(y + .09*w),
                min(w, round(x + .113*w)), min(image.height, round(y + .23*w)))
        if rect[3] <= rect[1]:
            continue
        lines = sorted(gbl_vision.recognize(image, rect), key=lambda b: (b.center_y, b.x))
        # On a tall iPhone the staging suffix can wrap just below the
        # one-line label cutoff, at almost the same height as numeric counts.
        parts = [line.text.strip() for line in lines if line.text.strip() and
                 (line.center_y < y + .177*w or
                  (line.text.strip().casefold() == "new" and line.center_y < y + .22*w))]
        tiles.append((" ".join(parts), (x, y)))
    return tiles


async def _cancel_name_dialog(phone, image, boxes):
    if not heading(boxes, "set search name"):
        return image, boxes
    # The keyboard may cover the game dialog. Dismiss it first if necessary.
    if not heading(boxes, "ok") or not heading(boxes, "cancel"):
        await (clear_pokemon._ios_tap_done(phone) if phone.ios else trade_local.submit(phone))
        image, boxes = await menu(phone, "set search name")
    ok = heading(boxes, "ok")
    cancel = next((b for b in boxes if ok and b.center_y > ok.center_y
                   and b.text.casefold() == "cancel"), None)
    if not cancel:
        raise starter.StepFailed(f"{phone.name}: could not cancel Set Search Name")
    await phone.tap_box(cancel, image)
    await asyncio.sleep(.6)
    return await phone.read()


async def _dismiss_drawer_keyboard(phone, image, boxes):
    if phone.ios and (heading(boxes, "see more") or heading(boxes, "see less")):
        dev = phone.target.device
        if await asyncio.to_thread(dev.driver.is_keyboard_shown):
            await clear_pokemon._ios_tap_done(phone)
            await asyncio.sleep(.45)
            image, boxes = await phone.read()
    return image, boxes


async def _open_favorites(phone):
    image, boxes = await phone.read()
    if heading(boxes, "set search name"):
        image, boxes = await _cancel_name_dialog(phone, image, boxes)
    image, boxes = await _dismiss_drawer_keyboard(phone, image, boxes)
    if heading(boxes, "see more") or heading(boxes, "see less"):
        return await compact_drawer(phone, image, boxes)
    if starter.storage_open(boxes) and (heading(boxes, "recommended") or heading(boxes, "special")):
        image, boxes = await _top_of_favorites(phone, image, boxes)
        return await compact_drawer(phone, image, boxes)
    if not starter.storage_open(boxes):
        image, boxes = await starter.open_storage(phone)
    field = clear_pokemon._storage_search_field(image, boxes)
    return await favorite_drawer(phone, field, image)


async def _favorite_pages(phone):
    image, boxes = await _open_favorites(phone)
    previous = None
    for page in range(32):
        entries = await asyncio.to_thread(favorite_tiles, image, boxes, scrolled=page > 0)
        yield image, entries
        if heading(boxes, "recommended"):
            return
        signature = tuple((label, round(y / image.width, 2)) for label, (_, y) in entries)
        if signature == previous:
            break
        previous = signature
        image, boxes = await _scroll(phone, image, down=True)
    raise starter.StepFailed(f"{phone.name}: could not reach the end of saved favorites")


async def list_favorites(phone):
    """Return the visible names in Favorite, including wrapped names and clipped query previews."""
    names = {}
    async for _, entries in _favorite_pages(phone):
        for name, _ in entries:
            if name:
                names.setdefault(_label_key(name), name)
    return list(names.values())


async def _find_label(phone, label):
    key = _label_key(label)
    if not key:
        raise ValueError("A favorite needs a nonempty name")
    clipped = []
    async for page, (image, entries) in _numbered_pages(phone):
        for index, (name, point) in enumerate(entries):
            shown = _label_key(name)
            if shown == key:
                return image, point
            # Android OCR can omit an isolated wrapped character, e.g. the last
            # "y" of TradedLucky. The editable native name disambiguates it.
            common = len(os.path.commonprefix((shown, key)))
            if common >= 6 and max(len(shown), len(key)) - common <= 3:
                clipped.append((page, index, name))
    for candidate in clipped:
        located = await _preview_at(phone, *candidate)
        if located is None:
            continue
        image, point = located
        actual = await _read_name_at(phone, image, point)
        if actual and _label_key(actual) == key:
            return await _preview_at(phone, *candidate)
    return None


async def _read_tile(phone, image, point):
    await phone.tap(point, image)
    ok, grid, boxes = await trade_local.settle(phone, lambda i, b: (
        starter.storage_open(b) and not heading(b, "see more") and not heading(b, "see less")
        and clear_pokemon.result_count(b, i.height) is not None), 8)
    if not ok:
        raise starter.StepFailed(f"{phone.name}: the selected favorite did not open "
                                 f"({phone.save(grid, 'favorite-result-unverified')})")
    field = clear_pokemon._storage_search_field(grid, boxes)
    await phone.tap_box(field, grid)
    await asyncio.sleep(.7)
    value = await editor_text(phone)
    if not value and phone.ios:
        # The first tap can open the Unity drawer while leaving its native
        # input unfocused. Retry only on a recognized saved-search drawer.
        drawer, shown = await phone.read()
        if heading(shown, "see more") or heading(shown, "see less"):
            field = clear_pokemon._storage_search_field(drawer, shown)
            await phone.tap([round(.45 * drawer.width), field.center_y], drawer)
            await asyncio.sleep(.7)
            value = await editor_text(phone)
    if value and phone.ios:
        # Releasing the native selection keeps the last rows hittable; Unity
        # can show the drawer underneath an asynchronously reappearing keypad.
        await clear_pokemon._ios_tap_done(phone)
    if not value or value.casefold() == "search":
        reason = getattr(phone.target.device.driver, "_pogo_editor_reason", "") if phone.ios else ""
        raise starter.StepFailed(f"{phone.name}: cannot read the full native favorite query ({reason})")
    return value


async def read_favorite(phone, label):
    """Get the saved filter from the native editor. None means the name is absent."""
    for attempt in range(2):
        located = await _find_label(phone, label)
        if located is None:
            return None
        try:
            return await _read_tile(phone, *located)
        except (starter.StepFailed, clear_pokemon.ClearSafetyError):
            if attempt:
                raise
            # A settling drawer or keypad can consume the first tile tap.
            # Reacquire by name before reading instead of tapping a stale coordinate.
            await asyncio.sleep(.6)


def _picker_or_storage_grid(image, boxes):
    return (
        not any(heading(boxes, h) for h in ("see more", "see less", "recommended", "recent"))
        and (starter.storage_open(boxes) or trade_local.picker(image, list(boxes)))
        and clear_pokemon.result_count(boxes, image.height) is not None
    )


async def _open_named_tile(phone, label, term, image, boxes):
    from .collect_trades import _picker_search_field

    def drawer(labels):
        return any(heading(labels, h) for h in ("see more", "see less", "recommended", "recent", "favorite"))

    for attempt in range(2):
        if not drawer(boxes):
            if attempt:
                break
            field = _picker_search_field(image, boxes)
            if field is None:
                raise starter.StepFailed(f"{phone.name}: no search field for {label!r}")
            image, boxes = await favorite_drawer(phone, field, image)
        else:
            image, boxes = await _top_of_favorites(phone, image, boxes)
            image, boxes = await compact_drawer(phone, image, boxes)
        if not phone.ios and drawer(boxes):
            adb = phone.target._adb
            ime = await asyncio.to_thread(adb, "shell", "dumpsys input_method")
            if re.search(r"\bmInputShown=true\b", ime.stdout or ""):
                # The lower favorite row is covered by the Android input panel
                # even though Unity can report a tile underneath it. Back only
                # when Android confirms its IME is visible, then locate anew.
                await asyncio.to_thread(adb, "shell", "input keyevent KEYCODE_BACK")
                await asyncio.sleep(.5)
                image, boxes = await phone.read()
                if not drawer(boxes):
                    continue
        image, boxes = await _top_of_favorites(phone, image, boxes)
        image, boxes, tile = await find_in_favorites(phone, image, boxes, term, label=label)
        if tile is None:
            raise starter.StepFailed(f"{phone.name}: saved favorite {label!r} is missing")
        await phone.tap_box(tile, image)
        ok, image, boxes = await trade_local.settle(
            phone, lambda i,b: trade_local.expired(b) or _picker_or_storage_grid(i,b), 4)
        if trade_local.expired(boxes):
            break
        if ok:
            return image, boxes
        if not drawer(boxes):
            break
    diagnostic = phone.save(image, "named-picker-unverified")
    raise starter.StepFailed(f"{phone.name}: {label!r} did not open its results ({diagnostic})")


async def _read_storage_results(phone, label, expected_term):
    """Return the native expression and its opened storage grid."""
    value = await read_favorite(phone, label)
    if not _same_query(value, expected_term):
        raise starter.StepFailed(f"{phone.name}: {label!r} did not match its expected saved filter")
    located = await _find_label(phone, label)
    if located is None:
        raise starter.StepFailed(f"{phone.name}: {label!r} disappeared after verification")
    image, point = located
    await phone.tap(point, image)
    ok, image, boxes = await trade_local.settle(phone, _picker_or_storage_grid, 8)
    if not ok:
        raise starter.StepFailed(f"{phone.name}: {label!r} did not return its storage grid")
    return value, image, boxes


async def open_named_storage(phone, label, expected_term):
    """Verify the full saved expression and leave this named storage search open."""
    _, image, boxes = await _read_storage_results(phone, label, expected_term)
    return image, boxes


async def open_named_picker(phone, label, expected_term, image, boxes):
    """Select the existing named trade filter and prove it without changing saved searches."""
    from .collect_trades import _picker_search_field

    if not trade_local.picker(image, list(boxes)) or heading(boxes, "tags") or heading(boxes, "eggs"):
        raise starter.StepFailed(f"{phone.name}: no trade picker to open {label!r}")
    image, boxes = await _open_named_tile(phone, label, expected_term, image, boxes)
    field = _picker_search_field(image, boxes)
    if field is None:
        raise starter.StepFailed(f"{phone.name}: could not read the active {label!r} filter")
    await phone.tap_box(field, image)
    await asyncio.sleep(0.7)
    value = await editor_text(phone)
    if not _same_query(value, expected_term):
        raise starter.StepFailed(f"{phone.name}: {label!r} did not match its expected saved filter")
    if phone.ios:
        await clear_pokemon._ios_tap_done(phone)
    else:
        await trade_local.submit(phone)
    image, boxes = await phone.read()
    if _picker_or_storage_grid(image, boxes) and not heading(boxes, "tags") and not heading(boxes, "eggs"):
        return image, boxes
    return await _open_named_tile(phone, label, expected_term, image, boxes)


def heading(boxes, name):
    return starter.find_box(boxes, name.casefold(), exact=True)


def named_tile(image, boxes, term, *, scrolled=False, label=None):
    """Resolve merged OCR rows by reading each column separately when necessary."""
    if label is not None:
        for shown, (x, y) in favorite_tiles(image, boxes, scrolled=scrolled):
            if _label_key(shown) == _label_key(label):
                return gbl_vision.OCRBox(shown, 1., x - 2, y - 2, 4, 4)
        return None
    alias = name_for(term)
    if not scrolled and not heading(boxes, "favorite"):
        return None
    for box in boxes:
        if alias.casefold() not in box.text.casefold():
            continue
        if box.text.strip().upper() == alias:
            return box
        for left in (0.045, 0.272, 0.500, 0.728):
            crop = (int(image.width * left), box.y - 3,
                    int(image.width * (left + 0.225)), box.y + box.height + 3)
            for label in gbl_vision.recognize(image, crop):
                if label.text.strip().upper() == alias:
                    return label
    return None


def icons(image, boxes, section, *, scrolled=False):
    """Find the large teal magnifier discs between the indicated menu headers."""
    header = heading(boxes, section)
    if not header and not scrolled:
        return []
    top = header.center_y if header else image.height * .205
    bottom = next((b.center_y for b in boxes
                   if b.center_y > top and b.text.casefold() in {"recommended", "special"}), image.height)
    # Connected teal components at thumbnail resolution. Independent of device density.
    w = 160
    h = round(image.height * w / image.width)
    small = image.convert("RGB").resize((w, h))
    pixels = small.load()
    cells = set()
    for y in range(int(top * h / image.height) + 2, int(bottom * h / image.height)):
        for x in range(w):
            r, g, b = pixels[x, y]
            if r < 175 and g - r > 32 and b - r > 31 and abs(g - b) < 45:
                cells.add((x, y))
    discs = []
    while cells:
        first = cells.pop()
        q = deque([first])
        minx = maxx = first[0]
        miny = maxy = first[1]
        area = 0
        while q:
            x, y = q.popleft()
            minx, maxx = min(x, minx), max(x, maxx)
            miny, maxy = min(y, miny), max(y, maxy)
            area += 1
            for pos in ((x-1, y), (x+1, y), (x, y-1), (x, y+1)):
                if pos in cells:
                    cells.remove(pos)
                    q.append(pos)
        if 19 < maxx-minx < 35 and abs((maxx-minx)-(maxy-miny)) <= 4 and area > 240:
            discs.append((round((minx+maxx) * image.width / (2*w)),
                          round((miny+maxy) * image.height / (2*h))))
    return sorted(discs, key=lambda pt: (round(pt[1] / (image.width * .03)), pt[0]))


async def _hold(phone, pt, image, *, ios_seconds=1.5):
    if phone.ios:
        dev = phone.target.device
        logical = dev.logical_point(list(pt), image)
        if hasattr(dev, "touch_and_hold"):
            await dev.touch_and_hold(logical, ios_seconds)
        elif hasattr(dev.driver, "execute_script"):
            await asyncio.to_thread(dev.driver.execute_script, "mobile: touchAndHold",
                                    {"x": logical[0], "y": logical[1], "duration": ios_seconds})
        else:
            await dev.drag(logical, logical, ios_seconds)
    else:
        await phone.swipe(pt, pt, image, seconds=1.25)
    await asyncio.sleep(.6)


async def menu(phone, name, seconds=8):
    ok, image, boxes = await trade_local.settle(
        phone, lambda _i, b: bool(heading(b, name)) or trade_local.expired(b), seconds)
    if not ok or trade_local.expired(boxes):
        diagnostic = phone.save(image, "favorite-dialog-unverified") if hasattr(phone, "save") else ""
        raise starter.StepFailed(f"{phone.name}: did not reach {name} in saved searches ({diagnostic})")
    return image, boxes


async def _toggle(phone, image, boxes, button, after):
    control = heading(boxes, button)
    if control is None:
        raise starter.StepFailed(f"{phone.name}: saved searches has no {button} button")
    await phone.tap_box(control, image)
    return await menu(phone, after)


async def favorite_drawer(phone, field, image):
    """Open the compact drawer with the keyboard dismissed and its tiles hittable."""
    await phone.tap_box(field, image)
    ok, image, boxes = await trade_local.settle(phone, lambda i,b: bool(
        heading(b,"see more") or heading(b,"see less") or heading(b,"recommended")
        or icons(i, b, "favorite", scrolled=True) or trade_local.expired(b)), 8)
    if not ok or trade_local.expired(boxes):
        raise starter.StepFailed(f"{phone.name}: no saved-search drawer")
    if not (heading(boxes, "see more") or heading(boxes, "see less")):
        image, boxes = await _top_of_favorites(phone, image, boxes)
    return await compact_drawer(phone, image, boxes)


async def _android_compact_frame(phone, image, boxes):
    # Unity can draw the last row under Gboard. Only Back an OS-confirmed keyboard.
    adb = phone.target._adb
    for attempt in range(2):
        ime = await asyncio.to_thread(adb, "shell", "dumpsys input_method")
        states = set(re.findall(r"\bmInputShown=(true|false)\b", ime.stdout or ""))
        if states != {"true"} or attempt:
            return image, boxes, states == {"false"}
        await asyncio.to_thread(adb, "shell", "input keyevent KEYCODE_BACK")
        await asyncio.sleep(.5)
        image, boxes = await phone.read()
        if not heading(boxes, "see more"):
            raise starter.StepFailed(f"{phone.name}: compact favorite drawer changed while hiding keyboard")


async def compact_drawer(phone, image, boxes):
    if heading(boxes, "see more"):
        hidden = False
        if not phone.ios:
            image, boxes, hidden = await _android_compact_frame(phone, image, boxes)
        recommended = heading(boxes, "recommended")
        if recommended and recommended.center_y > image.height * .72 and (phone.ios or hidden):
            return await _dismiss_drawer_keyboard(phone, image, boxes)
        # A full compact drawer can push Recommended below the screen.
        if hidden and not recommended and len(icons(image, boxes, "favorite")) == 12:
            return image, boxes
        image, boxes = await _toggle(phone, image, boxes, "see more", "recent")
    image, boxes = await _toggle(phone, image, boxes, "see less", "see more")
    if phone.ios:
        # Unity updates the chevron before the last row finishes moving.
        await asyncio.sleep(.45)
        image, boxes = await phone.read()
        return await _dismiss_drawer_keyboard(phone, image, boxes)
    return image, boxes


async def _scroll(phone, image, *, down):
    start, end = (.78, .49) if down else (.38, .80)
    x = round(image.width * .50)
    await phone.swipe([x, round(image.height * start)], [x, round(image.height * end)],
                      image, seconds=.6, scroll=True)
    return await phone.read()


async def find_in_favorites(phone, image, boxes, term, *, label=None):
    """Look through the compact drawer; its search field stays above the scrolling rows."""
    for page in range(32):
        tile = (named_tile(image, boxes, term, scrolled=page > 0) if label is None else
                await asyncio.to_thread(named_tile, image, boxes, term, scrolled=page > 0, label=label))
        if tile or heading(boxes, "recommended"):
            return image, boxes, tile
        if not icons(image, boxes, "favorite", scrolled=page > 0):
            break
        image, boxes = await _scroll(phone, image, down=True)
    return image, boxes, None


async def _top_of_favorites(phone, image, boxes):
    previous = None
    for _ in range(12):
        if heading(boxes, "see more") or heading(boxes, "see less"):
            return image, boxes
        signature = tuple((b.text, round(b.center_y / image.height, 2)) for b in boxes
                          if image.height * .23 < b.center_y < image.height * .88)
        if signature == previous:
            break
        previous = signature
        image, boxes = await _scroll(phone, image, down=False)
    raise starter.StepFailed(f"{phone.name}: could not reach top of Favorite searches")


def _column(point, image):
    return min(range(4), key=lambda col: abs(point[0] / image.width - (.155 + col * .228)))


def _count_token(text):
    token = text.strip()
    if re.fullmatch(r"(?:0|[1-9]\d{0,4}|[1-9]\d{0,2},\d{3})", token):
        return int(token.replace(",", ""))
    return None


def recent_tile_count(image, boxes, point):
    """Read the count below this magnifier without accepting a neighboring tile's count."""
    x, y = point
    width = image.width
    for box in boxes:
        if abs(x - box.center_x) < width * .09 and y + width * .15 < box.center_y < y + width * .28:
            number = _count_token(box.text)
            if number is not None:
                return number

    # On both phones the row may wrap the query label onto a second line.
    # The lowest ink band contains its count. Full-frame Vision commonly
    # drops a lone digit, especially 7 or 0, between neighboring tile labels.
    tile = ImageOps.grayscale(image.crop((round(x - .095 * width), round(y + .13 * width),
                                         round(x + .095 * width), round(y + .30 * width))))
    mask = tile.point(lambda pixel: 255 if pixel < 180 else 0)
    bands = []
    for row in range(tile.height):
        if mask.crop((0, row, tile.width, row + 1)).getbbox():
            if not bands or row > bands[-1][1] + 2:
                bands.append([row, row])
            else:
                bands[-1][1] = row
    bands = [(a, b) for a, b in bands if b - a > max(3, width * .008)]
    if not bands:
        return None
    top, bottom = bands[-1]
    bounds = mask.crop((0, top, tile.width, bottom + 1)).getbbox()
    if bounds[0] == 0 or bounds[2] == tile.width:
        return None
    digits = ImageOps.autocontrast(tile.crop((bounds[0], top, bounds[2], bottom + 1)))
    digits = digits.resize((max(1, round(digits.width * 64 / digits.height)), 64), Image.Resampling.LANCZOS)
    digits = ImageOps.expand(digits, border=22, fill=255)
    for box in gbl_vision.recognize(digits):
        number = _count_token(box.text)
        if number is not None:
            return number
    binary = shutil.which("tesseract")
    if binary:
        buf = io.BytesIO()
        digits.save(buf, format="PNG")
        try:
            output = subprocess.run(
                [binary, "stdin", "stdout", "--psm", "6", "-c", "tessedit_char_whitelist=0123456789,"],
                input=buf.getvalue(), capture_output=True, timeout=5, check=True,
            )
            return _count_token(output.stdout.decode().strip())
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _empty_field(image, boxes):
    if not starter.storage_open(boxes) or clear_pokemon.result_count(boxes, image.height) is not None:
        return None
    return next((b for b in boxes if .11 * image.height < b.center_y < .26 * image.height
                 and trade_local.norm(b.text).endswith("search")), None)


async def _exit_filtered_storage(phone, field, grid, *, reopen):
    # Recent represents relinquished searches. Editing the still-active search
    # reopens a stale Recent row even when the grid correctly shows a new count.
    if not reopen:
        if phone.ios:
            grid, current = await phone.read()
            if not starter.storage_open(current) and heading(current, "power up"):
                await clear_pokemon.close_detail(phone)
                grid, current = await phone.read()
            if not starter.storage_open(current):
                raise starter.StepFailed(f"{phone.name}: saved-search storage closed before committing Recent")
            field = clear_pokemon._storage_search_field(grid, current)
        # iOS records the submitted search when leaving through the back
        # arrow. Its clear-text X starts another edit of the current query.
        exit_x = .095 if phone.ios else trade_local.PICKER_CLEAR_X
        await phone.tap([round(grid.width * exit_x), field.center_y], grid)
        ok, image, boxes = await trade_local.settle(phone, lambda i,b: bool(_empty_field(i,b)), 4)
        if ok:
            return image, _empty_field(image, boxes)
    image, boxes = await phone.read()
    field = _empty_field(image, boxes)
    if field:
        return image, field
    if starter.storage_open(boxes) and clear_pokemon.result_count(boxes, image.height) is not None:
        # Storage has its own back arrow directly left of the active search.
        # Recent is committed when the filter is exited, even when the clear
        # icon or a reused open_storage call leaves the prior query active.
        active = clear_pokemon._storage_search_field(image, boxes)
        await phone.tap([round(image.width * .095), active.center_y], image)
        ok, image, boxes = await trade_local.settle(phone, lambda i,b: bool(_empty_field(i,b)), 5)
        if ok:
            return image, _empty_field(image, boxes)
    image, boxes = await starter.open_storage(phone)
    field = _empty_field(image, boxes)
    if field is None:
        # The tabs and storage count can appear while the sheet is still
        # sliding in. Its Search control only reaches the header once settled.
        _, image, boxes = await trade_local.settle(phone, lambda i,b: bool(_empty_field(i,b)), 5)
        field = _empty_field(image, boxes)
    if field is None:
        raise starter.StepFailed(f"{phone.name}: storage did not reopen with an empty search "
                                 f"({phone.save(image, 'favorite-clear-unverified')})")
    return image, field


async def _verified_recent(phone, term, count, image, boxes, editor_matches, *, exact_editor=False):
    """Verify the selected Recent's underlying filter before pinning it."""
    for index in range(4 if exact_editor else 1):
        recent = icons(image, boxes, "recent")
        if not recent or index >= len(recent) or abs(recent[index][1] - recent[0][1]) > image.width * .10:
            return None
        if not exact_editor and await asyncio.to_thread(recent_tile_count, image, boxes, recent[index]) != count:
            return None
        await phone.tap(recent[index], image)
        ok, grid, results = await trade_local.settle(phone, lambda i,b: (
            not heading(b, "see more") and not heading(b, "see less")
            and clear_pokemon.result_count(b, i.height) is not None
            and (exact_editor or (clear_pokemon.result_count(b, i.height) == count
                                  and clear_pokemon.search_field_matches_query(b, term, i.height)))), 5)
        if not ok:
            return None
        field = clear_pokemon._storage_search_field(grid, results)
        await phone.tap_box(field, grid)
        await asyncio.sleep(.8)
        image, boxes = await phone.read()
        matched = await editor_matches(phone, term, image, boxes)
        if not matched and not exact_editor:
            return None
        if heading(boxes, "see more"):
            image, boxes = await _toggle(phone, image, boxes, "see more", "recent")
        elif not heading(boxes, "see less"):
            return None
        if matched:
            recent = icons(image, boxes, "recent")
            if index >= len(recent):
                return None
            if not exact_editor and await asyncio.to_thread(recent_tile_count, image, boxes, recent[index]) != count:
                return None
            return recent[index], image, field, grid
        phone.say(f"Recent search {index+1} of {min(len(recent),4)} did not match the native filter")
    return None


async def save_most_recent(phone, term, count, field, grid, *, editor_matches, restore_search, label=None):
    """Pin only a newly submitted, verified search. Never rename a pre-existing tile."""
    for reopen in (False, True):
        if reopen:
            # The failed probe may have opened some older search with the
            # same number of matches. Reapply the intended term before closing
            # storage so the alternate registration path records that term.
            grid, results = await restore_search(phone, term)
            if clear_pokemon.result_count(results, grid.height) != count:
                phone.say(f"reapplied search no longer matches {count} results")
                return False
            field = clear_pokemon._storage_search_field(grid, results)
        blank, empty = await _exit_filtered_storage(phone, field, grid, reopen=reopen)
        image, boxes = await favorite_drawer(phone, empty, blank)
        image, boxes, existing = await find_in_favorites(phone, image, boxes, term, label=label)
        if existing:
            return True
        if not heading(boxes, "recommended"):
            phone.say("could not find the last favorite before adding the Recent search")
            return False
        first_page = heading(boxes, "favorite") is not None
        before = icons(image, boxes, "favorite", scrolled=not first_page)
        last_col = _column(before[-1], image) if before else -1
        image, boxes = await _top_of_favorites(phone, image, boxes)
        image, boxes = await _toggle(phone, image, boxes, "see more", "recent")
        verified = await _verified_recent(phone, term, count, image, boxes, editor_matches, exact_editor=label is not None)
        if verified is not None:
            (x, y), image, field, grid = verified
            break
        phone.say(f"first Recent search did not verify for {label or name_for(term)}")
    else:
        return False
    # Holding Recent past the game’s long-press threshold can repeat the
    # action on iOS and fill the remaining slots with copies of the search.
    await _hold(phone, (x, y), image, ios_seconds=.75)
    image, boxes = await phone.read()
    if heading(boxes, "see less"):
        image, boxes = await _toggle(phone, image, boxes, "see less", "favorite")
    else:
        # WDA drag releases as a tap after the long press, opening that
        # search's grid. Reopen the drawer to locate the newly appended tile.
        image, boxes = await favorite_drawer(phone, field, grid)
    await asyncio.sleep(.65)
    image, boxes = await phone.read()
    image, boxes, existing = await find_in_favorites(phone, image, boxes, term, label=label)
    if existing:
        return True
    if not heading(boxes, "recommended"):
        phone.say("could not find the last favorite after holding the Recent search")
        return False
    after = icons(image, boxes, "favorite", scrolled=True)
    if not after or _column(after[-1], image) != (last_col + 1) % 4:
        phone.say(f"saved-search last column before/after hold: {last_col}/"
                  f"{_column(after[-1], image) if after else None}")
        return False
    if first_page and heading(boxes, "favorite") and len(after) != len(before) + 1:
        phone.say(f"saved-search tile count before/after hold: {len(before)}/{len(after)}")
        return False
    if label is not None:
        if (await _read_tile(phone, image, after[-1])).strip().casefold() != term.strip().casefold():
            raise starter.StepFailed(f"{phone.name}: newly pinned favorite contains a different filter")
        # Reading the native editor opens the drawer with the keyboard. Return
        # to its last row, verifying the just-observed append position.
        image, boxes = await _open_favorites(phone)
        image, boxes, _ = await find_in_favorites(phone, image, boxes, term, label=label)
        final = icons(image, boxes, "favorite", scrolled=heading(boxes, "favorite") is None)
        if not heading(boxes, "recommended") or not final or _column(final[-1], image) != _column(after[-1], image):
            raise starter.StepFailed(f"{phone.name}: appended favorite moved while verifying it")
        return await _rename(phone, final[-1], image, term, label=label)
    return await _rename(phone, after[-1], image, term)


async def _name_editor(phone, point, image, *, focus):
    await _hold(phone, point, image)
    ok, image, boxes = await trade_local.settle(phone, lambda _i, b: bool(
        heading(b, "manage favorite search") or heading(b, "set search name")), 7)
    if not ok:
        raise starter.StepFailed(f"{phone.name}: favorite name dialog did not open "
                                 f"({phone.save(image, 'favorite-rename-unverified')})")
    if phone.ios:
        await asyncio.sleep(.5)
        image, boxes = await phone.read()
    if not heading(boxes, "set search name"):
        rename = heading(boxes, "rename")
        if not rename:
            raise starter.StepFailed(f"{phone.name}: missing Rename in Manage Favorite Search")
        await phone.tap_box(rename, image)
        try:
            image, boxes = await menu(phone, "set search name")
        except starter.StepFailed as exc:
            raise starter.StepFailed(f"{phone.name}: Rename did not open the name dialog: {exc}") from exc
    # On the SE the release from a long press in the bottom row can also
    # activate Rename, landing directly in the same name-entry dialog.
    if phone.ios and focus and not await phone.ios_keyboard():
        image, boxes = await menu(phone, "set search name")
        title, ok = heading(boxes, "set search name"), heading(boxes, "ok")
        cancel = heading(boxes, "cancel")
        if not ok or not cancel or not title.center_y < ok.center_y < cancel.center_y:
            raise starter.StepFailed(f"{phone.name}: could not locate the favorite name field")
        field_y = round(title.center_y + .43 * (ok.center_y - title.center_y))
        await phone.tap([image.width // 2, field_y], image)
        await asyncio.sleep(.55)
        if not await phone.ios_keyboard():
            raise starter.StepFailed(f"{phone.name}: the favorite name field did not open its keyboard")
    return image, boxes


async def read_saved_name(phone, label):
    """Read the persisted full name, including spaces that drawer OCR can invent at wrapping."""
    located = await _find_label(phone, label)
    if located is None:
        raise starter.StepFailed(f"{phone.name}: no native saved search named {label!r}")
    image, point = located
    native = await _read_name_at(phone, image, point)
    if native is None:
        raise starter.StepFailed(f"{phone.name}: no native favorite name for {label!r}")
    return native


async def _read_name_at(phone, image, point):
    await _name_editor(phone, point, image, focus=True)
    native = await editor_text(phone)
    if phone.ios:
        await clear_pokemon._ios_tap_done(phone)
    else:
        await trade_local.submit(phone)
    image, boxes = await menu(phone, "set search name")
    await _cancel_name_dialog(phone, image, boxes)
    return native


async def _rename(phone, point, image, term, *, label=None):
    await _name_editor(phone, point, image, focus=label is not None)
    alias = label or name_for(term)
    verified_input = None
    if label is not None:
        if phone.ios and await paste_ios_editor(phone, alias):
            verified_input = alias
        else:
            current = await editor_text(phone)
            if current == alias:
                verified_input = alias
            elif phone.ios:
                # A paste can take even if iOS briefly hides its input. Clear
                # the field before typing again; names have at most 12 chars.
                await trade_local._clear_ios_native_text(phone)
                await trade_local.clear_field(phone, max(14, len(current or "") + 2))
            elif current:
                await trade_local.clear_field(phone, len(current) + 2)
    if verified_input is None:
        await trade_local.type_text(phone, alias)
        if label is not None:
            verified_input = await editor_text(phone)
    exact_input = verified_input
    if exact_input is not None and exact_input != alias:
        await (clear_pokemon._ios_tap_done(phone) if phone.ios else trade_local.submit(phone))
        await _cancel_name_dialog(phone, *await menu(phone, "set search name"))
        raise starter.StepFailed(f"{phone.name}: the rename field retained {exact_input!r} when setting {alias!r}")
    await (clear_pokemon._ios_tap_done(phone) if phone.ios else trade_local.submit(phone))
    image, boxes = await phone.read()
    if not heading(boxes, "set search name"):
        if exact_input == alias and (heading(boxes, "see more") or heading(boxes, "see less")):
            return await _find_label(phone, alias) is not None
        try:
            image, boxes = await menu(phone, "set search name")
        except starter.StepFailed as exc:
            raise starter.StepFailed(f"{phone.name}: name dialog closed after editing {alias!r}: {exc}") from exc
    ok = heading(boxes, "ok")
    if not ok or (exact_input != alias and not any(_label_key(b.text) == _label_key(alias) for b in boxes)) or not heading(boxes, "cancel"):
        actual = [b.text for b in boxes if .25 < b.center_y / image.height < .55]
        await _cancel_name_dialog(phone, image, boxes)
        raise starter.StepFailed(f"{phone.name}: could not verify {alias!r} in name dialog {actual!r}")
    await phone.tap_box(ok, image)
    await asyncio.sleep(1.0)
    image, boxes = await phone.read()
    if heading(boxes, "see less"):
        image, boxes = await _toggle(phone, image, boxes, "see less", "favorite")
    _, _, tile = await find_in_favorites(phone, image, boxes, term, label=label)
    if tile is None and label is not None:
        return await _find_label(phone, label) is not None
    return tile is not None


async def _remove_at(phone, image, point):
    if phone.ios and point[1] > .68 * image.height:
        # A bottom-row release on SE can immediately activate Rename in the
        # newly opened menu. Move that same row higher before long-pressing.
        boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        entries = await asyncio.to_thread(favorite_tiles, image, boxes, scrolled=True)
        column = _column(point, image)
        def row_at(entries, y, img):
            return tuple((_column(pos, img), _label_key(name)) for name, pos in entries
                         if abs(pos[1] - y) < img.width * .05)
        signature = row_at(entries, point[1], image)
        recommended = heading(boxes, "recommended")
        below = recommended.center_y - point[1] if recommended else None
        source = next((_label_key(label) for label, pos in entries
                       if _column(pos, image) == column and abs(pos[1] - point[1]) < .05 * image.width), None)
        unique = source and sum(_column(pos, image) == column and _label_key(label) == source
                                for label, pos in entries) == 1
        x = image.width // 2
        await phone.swipe([x, round(image.height * .67)], [x, round(image.height * .53)],
                          image, seconds=.9, scroll=True)
        await asyncio.sleep(.45)
        image, boxes = await phone.read()
        entries = await asyncio.to_thread(favorite_tiles, image, boxes, scrolled=True)
        matches = [pos for _, pos in entries if _column(pos, image) == column
                   and signature and row_at(entries, pos[1], image) == signature]
        if len(matches) != 1 and unique:
            matches = [pos for label, pos in entries if _column(pos, image) == column
                       and _label_key(label) == source]
        if len(matches) != 1 and below is not None:
            # Wrapped previews can OCR differently after the scroll. The
            # Recommended heading moves with the list and uniquely anchors
            # the previous tile's column and vertical offset.
            recommended = heading(boxes, "recommended")
            if recommended:
                matches = [pos for pos in icons(image, boxes, "favorite", scrolled=True)
                           if _column(pos, image) == column
                           and abs(recommended.center_y - pos[1] - below) < .035 * image.width]
        if len(matches) != 1:
            raise starter.StepFailed(f"{phone.name}: could not relocate the selected favorite row "
                                     f"({phone.save(image, 'favorite-position-unverified')})")
        point = matches[0]
    await _hold(phone, point, image)
    image, boxes = await menu(phone, "manage favorite search")
    remove = heading(boxes, "remove") or heading(boxes, "delete")
    if not remove or not heading(boxes, "rename"):
        raise starter.StepFailed(f"{phone.name}: no Remove action in Manage Favorite Search")
    await phone.tap_box(remove, image)
    await asyncio.sleep(.6)
    image, boxes = await phone.read()
    if not (heading(boxes, "see more") or heading(boxes, "see less")):
        words = " ".join(b.text.casefold() for b in boxes)
        if "favorite" in words and ("remove" in words or "delete" in words):
            yes = heading(boxes, "yes") or heading(boxes, "remove") or heading(boxes, "delete")
            if yes and not heading(boxes, "rename") and (heading(boxes, "cancel") or heading(boxes, "no")):
                await phone.tap_box(yes, image)
                await asyncio.sleep(.7)


async def remove_favorite(phone, label, *, expected_term=None):
    """Remove one specifically named saved search and verify its disappearance."""
    located = await _find_label(phone, label)
    if not located:
        return False
    if expected_term is not None:
        current = await _read_tile(phone, *located)
        if current.strip().casefold() != expected_term.strip().casefold():
            raise starter.StepFailed(f"{phone.name}: favorite {label!r} changed before removal")
        located = await _find_label(phone, label)
        if not located:
            raise starter.StepFailed(f"{phone.name}: favorite {label!r} moved before removal")
    await _remove_at(phone, *located)
    if await _find_label(phone, label):
        raise starter.StepFailed(f"{phone.name}: {label!r} is still in Favorite searches")
    return True


async def rename_favorite(phone, old_label, new_label, *, expected_term=None):
    term = await read_favorite(phone, old_label)
    if term is None:
        raise starter.StepFailed(f"{phone.name}: no favorite named {old_label!r}")
    if expected_term is not None and term.strip().casefold() != expected_term.strip().casefold():
        raise starter.StepFailed(f"{phone.name}: filter mismatch for {old_label!r}")
    if _label_key(old_label) != _label_key(new_label) and await _find_label(phone, new_label):
        raise starter.StepFailed(f"{phone.name}: another favorite already uses {new_label!r}")
    located = await _find_label(phone, old_label)
    if not located:
        raise starter.StepFailed(f"{phone.name}: favorite moved before renaming")
    image, point = located
    if not await _rename(phone, point, image, term, label=new_label):
        raise starter.StepFailed(f"{phone.name}: rename to {new_label!r} did not verify")
    saved = await read_favorite(phone, new_label)
    if saved is None or saved.strip().casefold() != term.strip().casefold():
        raise starter.StepFailed(f"{phone.name}: underlying query changed during rename")


def _preview_matches(shown, term):
    label = _label_key(shown)
    expression = _label_key(term)
    if label == expression:
        return True
    if not (".." in label or "…" in label):
        return False
    # Wrapped preview text often loses punctuation to OCR. Use it only to
    # locate candidates; the native editor must match the entire expression.
    stem = re.sub(r"[^a-z0-9]", "", re.split(r"\.{2,}|…", label)[0])
    target = re.sub(r"[^a-z0-9]", "", expression)
    return bool(stem) and target.startswith(stem[:4])


async def _preview_at(phone, page, index, shown):
    async for number, (image, entries) in _numbered_pages(phone):
        if number == page:
            if index < len(entries) and _label_key(entries[index][0]) == _label_key(shown):
                return image, entries[index][1]
            return None
    return None


async def _numbered_pages(phone):
    index = 0
    async for image, entries in _favorite_pages(phone):
        yield index, (image, entries)
        index += 1


async def _matching_preview(phone, term, *, try_ambiguous=False):
    candidates, uncertain = [], []
    async for page, (_, entries) in _numbered_pages(phone):
        for index, (shown, _) in enumerate(entries):
            if _preview_matches(shown, term):
                candidates.append((page, index, shown))
            elif try_ambiguous and (".." in shown or "…" in shown):
                # The narrow label can OCR "!98plus" as "198p|us". Exact
                # verification below keeps unrelated previews intact.
                uncertain.append((page, index, shown))
    for candidate in [*candidates, *uncertain]:
        tile = await _preview_at(phone, *candidate)
        if tile is None:
            continue
        try:
            value = await _read_tile(phone, *tile)
        except (starter.StepFailed, clear_pokemon.ClearSafetyError) as exc:
            phone.say(f"Could not verify preview {candidate[2]!r}. Leaving it in place: {exc}")
            continue
        if value.strip().casefold() != term.strip().casefold():
            continue
        tile = await _preview_at(phone, *candidate)
        if tile is None:
            raise starter.StepFailed(f"{phone.name}: matching search preview moved")
        return tile
    return None


async def _recover_preview(phone, label, term, *, try_ambiguous=False):
    tile = await _matching_preview(phone, term, try_ambiguous=try_ambiguous)
    if not tile:
        return False
    image, point = tile
    renamed = await _rename(phone, point, image, term, label=label)
    actual = await read_favorite(phone, label)
    if not _same_query(actual, term):
        message = "renamed preview did not verify" if renamed else "could not rename matching preview"
        raise starter.StepFailed(f"{phone.name}: {message} as {label!r}")
    return True


async def prune_preview_copies(phone, label, term):
    """Remove exact unnamed duplicates only after the named favorite verifies."""
    actual = await read_favorite(phone, label)
    if not actual or actual.strip().casefold() != term.strip().casefold():
        return 0
    removed = 0
    for _ in range(24):
        candidates = []
        async for page, (_, entries) in _numbered_pages(phone):
            candidates.extend((page, index, shown) for index, (shown, _) in enumerate(entries)
                              if _label_key(shown) != _label_key(label) and _preview_matches(shown, term))
        for candidate in candidates:
            located = await _preview_at(phone, *candidate)
            if located is None:
                continue
            source = await _read_tile(phone, *located)
            if source.strip().casefold() != term.strip().casefold():
                continue
            located = await _preview_at(phone, *candidate)
            if located is None:
                raise starter.StepFailed(f"{phone.name}: duplicate search preview moved")
            await _remove_at(phone, *located)
            removed += 1
            break
        else:
            return removed
    raise starter.StepFailed(f"{phone.name}: duplicate previews for {label!r} did not clear")


def _staged_name(label):
    # The old compact-name stages were "FarTrade new" or "New HundoRev".
    # Preserve those keys so an interrupted older installation can resume.
    compact = "".join(label.split())
    if len(compact) <= 8:
        return compact + "New"
    if len(label.split()) > 1:
        words = label.split()
        width = max(1,(9-len(words))//len(words))
        return "New" + "".join(w[:width] for w in words)[:8]
    return "New" + compact[:8]


async def _named_query_grid(phone):
    """Reuse open storage, including its saved-search drawer, before entering a query."""
    image, boxes = await phone.read()
    if heading(boxes, "set search name"):
        image, boxes = await _cancel_name_dialog(phone, image, boxes)
    if not starter.storage_open(boxes):
        return await starter.open_storage(phone)
    drawer = any(heading(boxes, word) for word in ("see more", "see less", "recommended", "recent", "favorite"))
    if not drawer:
        return image, boxes
    image, boxes = await _open_favorites(phone)
    choices = icons(image, boxes, "favorite") or icons(image, boxes, "recommended")
    if choices:
        # Selecting an existing search closes the drawer and yields a regular
        # result grid, keeping the phone inside storage.
        await phone.tap(choices[0], image)
        ok, image, boxes = await trade_local.settle(phone, lambda i,b: (
            starter.storage_open(b) and not heading(b, "see more") and not heading(b, "see less")
            and clear_pokemon.result_count(b, i.height) is not None), 6)
        if ok:
            return image, boxes
    return await starter.open_storage(phone)


async def _ios_named_grid(phone, term, image, boxes):
    """Prove an iOS saved search using its native text and the grid it actually opened."""
    started = time.monotonic()
    await clear_pokemon._enter_query_ios(phone, term, image, boxes, require_exact=True)
    previous = None
    for _ in range(10):
        image, boxes = await phone.read()
        if not starter.storage_open(boxes) and heading(boxes, "power up"):
            await clear_pokemon.close_detail(phone)
            previous = None
            continue
        drawer = any(heading(boxes, word) for word in ("see more", "see less", "recommended", "recent"))
        count = clear_pokemon.result_count(boxes, image.height) if starter.storage_open(boxes) and not drawer else None
        if count is not None and count == previous:
            # _enter_query_ios verified the submitted native value. Leave its
            # grid active so exiting can register it in Recent. Before pinning,
            # _verified_recent reads the selected Recent's full native text.
            return count, image, boxes, time.monotonic() - started
        previous = count
        await asyncio.sleep(.4)
    raise starter.StepFailed(f"{phone.name}: submitted saved search did not return a settled storage count "
                             f"({phone.save(image, 'favorite-submitted-unverified')})")


async def _pin_named(phone, label, term):
    if await _recover_preview(phone, label, term):
        return

    async def apply():
        image, boxes = await _named_query_grid(phone)
        if phone.ios:
            return await _ios_named_grid(phone, term, image, boxes)
        return await clear_pokemon._apply_and_verify_query(phone, term, image, boxes, require_exact=True)

    async def restore(phone, term):
        _, image, boxes, _ = await apply()
        return image, boxes

    async def matches(phone, term, image, boxes):
        value = await editor_text(phone)
        if value is None:
            phone.say(f"could not read the native Recent search for {label!r}")
            return False
        actual, wanted = value.strip().casefold(), term.strip().casefold()
        if actual != wanted:
            phone.say(f"{label!r} expected {len(wanted)} characters, Recent has {len(actual)} "
                      f"(exact prefix: {bool(actual) and wanted.startswith(actual)})")
            return False
        return True

    count, grid, boxes, _ = await apply()
    field = clear_pokemon._storage_search_field(grid, boxes)
    if not await save_most_recent(phone, term, count, field, grid, editor_matches=matches,
                                  restore_search=restore, label=label):
        if await _recover_preview(phone, label, term, try_ambiguous=True):
            await prune_preview_copies(phone, label, term)
            return
        raise starter.StepFailed(f"{phone.name}: could not pin and verify {label!r}")
    saved = await read_favorite(phone, label)
    if saved is None or saved.strip().casefold() != term.strip().casefold():
        raise starter.StepFailed(f"{phone.name}: saved {label!r} query did not verify")


def _same_query(a, b):
    return a is not None and b is not None and a.strip().casefold() == b.strip().casefold()


async def _drawer_full(phone):
    distinct = set()
    async for _, entries in _favorite_pages(phone):
        if len(entries) >= 12:
            return True
        distinct.update(_label_key(shown) for shown, _ in entries if shown)
    return len(distinct) >= 12


def _replacement_path(phone, label):
    handset = getattr(phone, "transport_name", phone.name)
    return (config_paths.state_dir() / "storage-favorites" / quote(handset, safe="") /
            "replacements" / (quote(label.casefold(), safe="") + ".json"))


def _save_replacement(path, record, status):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {**record, "status": status}
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        try:
            json.dump(record, out)
            out.flush()
            os.fsync(out.fileno())
            os.replace(out.name, path)
        finally:
            Path(out.name).unlink(missing_ok=True)


async def _restore_at_capacity(phone, path, record):
    label, old, new = record["label"], record["old"], record["new"]
    current = await read_favorite(phone, label)
    if _same_query(current, new):
        _save_replacement(path, record, "updated")
        return new
    if current is None:
        # A failed long press can leave an unnamed new copy occupying the
        # only slot. The initial full-drawer scan ruled it out before removal.
        if await _drawer_full(phone):
            raw = await _matching_preview(phone, new, try_ambiguous=True)
            if raw is not None:
                image, point = raw
                if await _rename(phone, point, image, new, label=label):
                    confirmed = await read_favorite(phone, label)
                    if _same_query(confirmed, new):
                        _save_replacement(path, record, "updated")
                        return new
                # Re-read the current location after a failed rename.
                raw = await _matching_preview(phone, new, try_ambiguous=True)
                if raw is not None:
                    await _remove_at(phone, *raw)
        await _pin_named(phone, label, old)
        current = await read_favorite(phone, label)
    if not _same_query(current, old):
        raise starter.StepFailed(f"{phone.name}: cannot restore over a different query under {label!r}")
    _save_replacement(path, record, "restored")
    return old


async def _replace_at_capacity(phone, path, record, current):
    label, old, new = record["label"], record["old"], record["new"]
    if _same_query(current, new):
        _save_replacement(path, record, "updated")
        return
    if current is not None and not _same_query(current, old):
        raise starter.StepFailed(f"{phone.name}: {label!r} changed independently. Backup: {path}")
    try:
        if current is not None:
            await remove_favorite(phone, label, expected_term=old)
        await _pin_named(phone, label, new)
        if not _same_query(await read_favorite(phone, label), new):
            raise starter.StepFailed(f"{phone.name}: replacement readback did not match {label!r}")
    except Exception as failure:
        _save_replacement(path, record, "restore pending")
        try:
            recovered = await _restore_at_capacity(phone, path, record)
            if _same_query(recovered, new):
                return
        except Exception as recovery:
            raise starter.StepFailed(f"{phone.name}: replacement of {label!r} failed ({failure}). "
                                     f"Restoration pending at {path}: {recovery}") from recovery
        raise starter.StepFailed(f"{phone.name}: restored {label!r} after replacement failed: {failure}") from failure
    _save_replacement(path, record, "updated")


async def ensure_favorite(phone, label, term):
    """Create or update a named search, retaining the original until its replacement verifies."""
    if not label.strip() or not term.strip():
        raise ValueError("A favorite needs a nonempty name and query")
    if len(label) > 12:
        raise ValueError(f"Favorite names are limited to 12 characters: {label!r}")
    path = _replacement_path(phone, label)
    existing = await read_favorite(phone, label)
    if path.exists():
        record = json.loads(path.read_text())
        if record.get("status") in {"pending", "restore pending"}:
            if record.get("device") != getattr(phone, "transport_name", phone.name) or record.get("label") != label:
                raise starter.StepFailed(f"{phone.name}: replacement backup does not match {label!r}")
            # Complete an interrupted swap before processing a newer definition.
            if record["status"] == "restore pending":
                existing = await _restore_at_capacity(phone, path, record)
            else:
                await _replace_at_capacity(phone, path, record, existing)
                existing = record["new"]
    if _same_query(existing, term):
        return "verified"
    staged = _staged_name(label)
    if existing is None:
        if (await read_favorite(phone, staged) or "").strip().casefold() == term.strip().casefold():
            await rename_favorite(phone, staged, label, expected_term=term)
        elif await _drawer_full(phone):
            if not await _recover_preview(phone, label, term, try_ambiguous=True):
                raise starter.StepFailed(f"{phone.name}: all 12 favorites are occupied and none matches {label!r}")
        else:
            await _pin_named(phone, label, term)
        return "created"
    pending = await read_favorite(phone, staged)
    if pending is None:
        if await _drawer_full(phone):
            if not await _recover_preview(phone, staged, term, try_ambiguous=True):
                record = {"device": getattr(phone, "transport_name", phone.name),
                          "label": label, "old": existing, "new": term}
                _save_replacement(path, record, "pending")
                await _replace_at_capacity(phone, path, record, existing)
                return "updated"
        else:
            await _pin_named(phone, staged, term)
    elif pending.strip().casefold() != term.strip().casefold():
        raise starter.StepFailed(f"{phone.name}: {staged!r} already exists with another filter")
    await remove_favorite(phone, label, expected_term=existing)
    await rename_favorite(phone, staged, label, expected_term=term)
    return "updated"
