"""Rename an existing collection tag in place and verify its grid."""

from __future__ import annotations

import asyncio
import re

from . import clear_pokemon, starter, trade_local, trade_saved_search as saved


def _key(text):
    return re.sub(r"[^\w%]", "", text).casefold()


def _title(image, boxes, tag):
    return next(
        (
            box
            for box in boxes
            if box.center_y < 0.15 * image.height and _key(box.text) == _key(tag)
        ),
        None,
    )


def _cp_grid(boxes):
    return sorted(
        re.sub(r"\s", "", box.text).upper()
        for box in boxes
        if re.fullmatch(r"CP\s*\d+", box.text, re.IGNORECASE)
    )


async def _form(phone, image, boxes, old):
    """Open the pencil on a proven tag grid. Allow for the iOS status bar."""
    if not _title(image, boxes, old):
        raise starter.StepFailed(f"{phone.name}: expected the {old!r} tag grid")
    for y in (0.106, 0.084, 0.097):
        await phone.tap([round(image.width * 0.915), round(image.height * y)], image)
        await asyncio.sleep(0.45)
        image, boxes = await phone.read()
        if all(
            saved.heading(boxes, title)
            for title in ("edit tag", "name", "color", "delete tag", "done")
        ):
            return image, boxes
        if not _title(image, boxes, old):
            break
    raise starter.StepFailed(f"{phone.name}: could not open Edit Tag for {old!r}")


async def _edit_value(phone, image, boxes):
    name, color = saved.heading(boxes, "name"), saved.heading(boxes, "color")
    if not name or not color or not saved.heading(boxes, "edit tag"):
        raise starter.StepFailed(f"{phone.name}: tag name editor is not open")
    await phone.tap(
        [
            image.width // 2,
            round(name.center_y + 0.38 * (color.center_y - name.center_y)),
        ],
        image,
    )
    await asyncio.sleep(0.6)
    return await saved.editor_text(phone)


async def _close_field(phone):
    if phone.ios:
        await clear_pokemon._ios_tap_done(phone)
    else:
        await trade_local.submit(phone)
    return await saved.menu(phone, "edit tag")


async def _tap_done(phone, image, boxes):
    done = saved.heading(boxes, "done")
    if (
        not done
        or not saved.heading(boxes, "delete tag")
        or not saved.heading(boxes, "edit tag")
    ):
        raise starter.StepFailed(f"{phone.name}: missing the Edit Tag Done control")
    await phone.tap_box(done, image)
    await asyncio.sleep(0.8)


async def close_unchanged(phone, image, boxes):
    """Unchanged Edit Tag disables Done. Use the sheet's own close button."""
    if not all(saved.heading(boxes, x) for x in ("edit tag", "delete tag", "done")):
        raise starter.StepFailed(f"{phone.name}: unrecognized unchanged tag editor")
    await phone.tap(
        [image.width // 2, round(image.height * (0.924 if phone.ios else 0.942))], image
    )
    await asyncio.sleep(0.7)


async def set_tag_name(phone, target):
    # Unity can briefly hide the field after Paste. A verified existing value
    # prevents appending the label when a paste actually succeeded.
    actual = await saved.editor_text(phone)
    if actual == target:
        return
    if phone.ios and not hasattr(phone.target.device, "delete_keys"):
        from appium.webdriver.common.appiumby import AppiumBy

        driver = phone.target.device.driver
        image, boxes = await phone.read()
        name, color = saved.heading(boxes, "name"), saved.heading(boxes, "color")
        if not name or not color:
            raise starter.StepFailed(f"{phone.name}: tag editor disappeared")
        field_y = round(name.center_y + 0.38 * (color.center_y - name.center_y))
        await phone.tap([round(image.width * 0.80), field_y], image)
        await asyncio.sleep(0.3)
        keys = await asyncio.to_thread(
            driver.find_elements,
            AppiumBy.IOS_PREDICATE,
            "name == 'delete' AND visible == 1",
        )
        if len(keys) != 1:
            raise starter.StepFailed(
                f"{phone.name}: missing native keyboard delete key"
            )
        for _ in range(min(34, len(actual or "") + 3)):
            await asyncio.to_thread(keys[0].click)
    else:
        await trade_local.clear_field(phone, max(32, len(actual or "") + 2))
    await trade_local.type_text(phone, target)
    actual = await saved.editor_text(phone)
    if actual != target:
        raise starter.StepFailed(
            f"{phone.name}: native tag input {actual!r} did not equal {target!r}"
        )


async def rename_grid(phone, image, boxes, expected_name, expected_count, folder):
    """Rename only the verified tag, verify its unchanged card count and visible members.

    Callers inventory the full Tags drawer first to rule out a distinct destination.
    The native source spelling determines which characters are preserved.
    """
    original_members = _cp_grid(boxes)
    folder.mkdir(parents=True, exist_ok=True)
    image.save(folder / f"{_key(expected_name)}-before.png")
    image, boxes = await _form(phone, image, boxes, expected_name)
    original = await _edit_value(phone, image, boxes)
    if (
        not original
        or "".join(original.split()).casefold()
        != "".join(expected_name.split()).casefold()
    ):
        raise starter.StepFailed(
            f"{phone.name}: native tag name {original!r} did not match {expected_name!r}"
        )
    target = "".join(original.split())
    if target != original:
        await set_tag_name(phone, target)
    image, boxes = await _close_field(phone)
    if target != original:
        await _tap_done(phone, image, boxes)
    else:
        await close_unchanged(phone, image, boxes)
    ok, image, boxes = await trade_local.settle(
        phone, lambda i, b: bool(_title(i, b, target)), 8
    )
    if not ok:
        raise starter.StepFailed(
            f"{phone.name}: renamed tag did not return to its grid"
        )
    if original_members and _cp_grid(boxes) != original_members:
        ok, image, boxes = await trade_local.settle(
            phone,
            lambda i, b: bool(_title(i, b, target)) and _cp_grid(b) == original_members,
            4,
        )
        if not ok:
            raise starter.StepFailed(
                f"{phone.name}: changed member grid for {target!r}"
            )
    image.save(folder / f"{_key(target)}-after.png")
    # Reopen this same existing tag and read its actual name, including whitespace.
    image, boxes = await _form(phone, image, boxes, target)
    confirmed = await _edit_value(phone, image, boxes)
    if confirmed != target:
        raise starter.StepFailed(
            f"{phone.name}: saved native tag is {confirmed!r}, expected {target!r}"
        )
    image, boxes = await _close_field(phone)
    await close_unchanged(phone, image, boxes)
    return {
        "old": original,
        "new": confirmed,
        "expected_count": expected_count,
        "visible_members_cp": original_members,
        "before": str(folder / f"{_key(expected_name)}-before.png"),
        "after": str(folder / f"{_key(target)}-after.png"),
    }
