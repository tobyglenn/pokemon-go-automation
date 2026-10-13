#!/usr/bin/env python3
"""Read an iPhone's Pokémon GO storage for the raid candy planner.

This is the iOS hands for `raid_inventory`, which owns the per-species
sequence and every parser, so the two platforms cannot measure the game
differently.  What is iOS's own is the navigation:

- There is no BACK key, so nothing here sends one.  A detail page is left by
  its round close disc, found on the screenshot by its colour (the game
  publishes it to neither OCR nor the accessibility tree); a labelled control
  or a close label read off the screen is the fallback, and a screen offering
  none is reported, never swiped at.
- The game edits its search field through a native text box over the keyboard.
  That box ignores the delete-key barrage -- every search was appended to the
  last -- so it is emptied with its own "Clear text" button.
- The game is never launched, terminated or force-quit.  The only dialogs
  cleared unattended are the two the owner authorised, imported from
  `raid_scan_android` rather than restated.

Proven on the iPhone SE (3rd gen, iOS 26.7.1, 750x1334) on 12 Oct 2026: search
confirmation, the 3*/4*/shadow filters, the detail-page read and the close disc.

Nothing here changes game state.  No transfer, no power up, no purchase.
"""

from __future__ import annotations

import argparse
import io
import json
import time
from pathlib import Path
from typing import Any, Sequence

from PIL import Image

from . import config_paths, ios_wda_cleanup, pokemon_fleet, vision_ocr

# Imported, not reimplemented: the two halves measure Pokémon GO the same way.
from . import raid_inventory
from .raid_scan_android import (
    AUTHORISED_DISMISSALS,
    GridEntry,
    RaidScanError,
    _button_label,
    authorised_dismissal,
    blocking_screen,
    classify,
    looks_like_loading,
    normalise,
    parse_grid_entries,
    parse_result_count,
    recovery_tap,
    search_bar_fraction,
)

__all__ = [
    "AUTHORISED_DISMISSALS",
    "GridEntry",
    "IOSPhone",
    "IOSStorageScanner",
    "RaidScanError",
    "RaidScanIOSError",
    "authorised_dialog_tap",
    "build_capabilities",
    "close_button_point",
    "detail_close_point",
    "grid_cell_point",
    "logical_point",
    "main",
    "navigation_tap",
    "resolve_device",
    "scan_device",
    "search_field_point",
    "storage_grid_visible",
]

# `mobile: queryAppState` codes: 0 unknown, 1 not running, 2 suspended,
# 3 backgrounded, 4 frontmost.
IOS_APP_NOT_RUNNING = 1
IOS_APP_FRONTMOST = 4

# XCUITest's delete keycode. `mobile: keys` types into whatever is focused, so
# the storage search field does not have to be an XCUIElement to be emptied --
# which matters, because the game draws that field itself and often publishes
# nothing for it.
DELETE_KEY = chr(0xE003)
# Android clears the field with 48 DELs; the iPhone SE keyboard is shorter than
# any species name in the roster, so the same run is enough here too.
CLEAR_KEYS = 48

# A cell may be tapped between these two fractions of the screen height.  The
# band is wide because the scan types with the keyboard still up, and the
# keyboard-down layout would reject every cell.
CELL_TOP = 0.12
CELL_BOTTOM = 0.90
# The CP sort control sits in the bottom-right corner.  Tapping it opens a sort
# menu: a popup with no way out this workflow can recognise, and where the
# Android scan kept wandering off to.
SORT_CORNER_X = 0.72
SORT_CORNER_Y = 0.82

# Labels the game's own controls carry, in the order a control is worth
# trusting.  These are read from the accessibility tree, never from a coordinate.
SEARCH_FIELD_LABELS = ("search", "Search Pokémon")
CLEAR_TEXT_LABELS = ("Clear text",)
CLOSE_CONTROL_LABELS = ("close", "back", "cancel", "dismiss", "not now")
MENU_CONTROL_LABELS = ("menu", "open menu", "main menu", "Poké Ball", "pokeball")

# A bare "x" is only ever trusted as the detail page's close button, and only on
# a screen `classify` already called a detail page.  Anywhere else a lone x is
# as likely to be a multiplier or a rating.
BARE_CLOSE = "x"


class RaidScanIOSError(RaidScanError):
    """The iPhone could not be read, or was not where the scan needs it.

    Subclasses the Android worker's error so a caller that drives both halves
    can catch one type, while iOS-specific handling stays possible.
    """


# --- reading a screen ------------------------------------------------------


def storage_grid_visible(
    boxes: Sequence[vision_ocr.TextBox], image: Image.Image
) -> bool:
    """True on the storage grid, including one holding no results at all.

    `classify` needs three CP tokens before it will call a screen a grid, so a
    filter that matched nothing reads as "unknown" -- and a worker that treats
    unknown as "not where I asked to be" would start navigating away from the
    very screen it needs to read next.  The POKÉMON tab row and the
    parenthesised counter are what identify an empty grid, and both are on it.
    """
    if classify(boxes) == "grid":
        return True
    has_tabs = any(
        normalise(box.text).strip().lower() == "pokemon" for box in boxes
    )
    return has_tabs and parse_result_count(boxes, image.height) is not None


def search_field_point(
    boxes: Sequence[vision_ocr.TextBox], image: Image.Image
) -> tuple[float, float] | None:
    """Where the storage search field is, as a screen fraction.

    Derived from what is on screen -- the tab row above it and the first CP row
    below -- because the same control sits at a different fraction of the height
    on a 750x1334 iPhone SE than on a 720x1600 Android handset, the game laying
    it out in density-independent units either way.

    This is the fallback for when the game publishes no XCUIElement for the
    field.  A located control, not an invented coordinate: with no tab row and
    no CP row there is nothing to anchor between and the answer is None.
    """
    return search_bar_fraction(boxes, image)


def grid_cell_point(
    cp: int, image: Image.Image, entries: Sequence[GridEntry]
) -> tuple[float, float] | None:
    """The cell showing `cp`, as a screen fraction, or None.

    Anchored on the recognised CP token rather than a fixed column, so a phone
    whose grid packs a different number of columns still works, and the
    bottom-right corner is excluded because that is where the sort control is.
    """
    for entry in entries:
        if entry.cp != cp:
            continue
        point = (entry.x / image.width, entry.y / image.height)
        if not CELL_TOP <= point[1] <= CELL_BOTTOM:
            continue
        if point[1] > SORT_CORNER_Y and point[0] > SORT_CORNER_X:
            continue
        return point
    return None


def _label_point(
    boxes: Sequence[vision_ocr.TextBox], image: Image.Image, wanted: str
) -> tuple[float, float] | None:
    """Centre of a box whose normalised text equals `wanted`."""
    for box in boxes:
        if normalise(box.text).strip().lower() == wanted:
            return box.cx / image.width, box.cy / image.height
    return None


def detail_close_point(
    boxes: Sequence[vision_ocr.TextBox], image: Image.Image
) -> tuple[float, float] | None:
    """A control that leaves a storage detail page, as a screen fraction.

    The detail page is a full-screen card, so its only way out is the close
    control at the top of it.  An XCUIElement for it is preferred and is tried
    first on the device; this is the OCR fallback for a build that publishes
    one, and it returns None rather than a guess when the screen offers no
    labelled way out at all.

    A bare "x" is accepted only on a screen already identified as a detail
    page: anywhere else a lone x on this screen is as likely to be a multiplier
    or a rating, and tapping it does something to the account.
    """
    for candidate in CLOSE_CONTROL_LABELS:
        point = _label_point(boxes, image, candidate)
        if point is not None:
            return point
    if classify(boxes) == "detail":
        return _label_point(boxes, image, BARE_CLOSE)
    return None


# The detail page's only exit is a dark-teal disc with a white x, drawn at the
# bottom centre and published to neither OCR nor the accessibility tree.
CLOSE_DISC_RGB = (29, 134, 150)
CLOSE_DISC_TOLERANCE = 14


def close_button_point(image: Image.Image) -> tuple[float, float] | None:
    """The detail page's round close button, found by its colour.

    Only the bottom-centre band is searched, which keeps the menu disc in the
    bottom-right corner out of it.  The caller has already identified the
    screen as a detail page: the storage grid carries a disc of the same colour
    that leaves storage altogether.
    """
    width, height = image.size
    step = max(1, width // 375)
    hits = [
        (x, y)
        for y in range(int(height * 0.80), int(height * 0.995), step)
        for x in range(int(width * 0.35), int(width * 0.65), step)
        if all(abs(a - b) <= CLOSE_DISC_TOLERANCE
               for a, b in zip(image.getpixel((x, y)), CLOSE_DISC_RGB))
    ]
    if len(hits) < 20:
        return None
    cx = sum(x for x, _ in hits) / len(hits)
    cy = sum(y for _, y in hits) / len(hits)
    return cx / width, cy / height


def navigation_tap(
    boxes: Sequence[vision_ocr.TextBox], image: Image.Image
) -> tuple[float, float] | None:
    """A visible affordance to tap to get back toward storage, if there is one.

    `raid_scan_android.recovery_tap`, shared because the decision it makes is
    the same one: a target located on the screen, never a BACK.  None means
    nothing recognisable is showing, which is the signal to report rather than
    act on.
    """
    return recovery_tap(boxes, image)


def authorised_dialog_tap(
    boxes: Sequence[vision_ocr.TextBox], image: Image.Image
) -> tuple[tuple[float, float], str] | None:
    """A tap this workflow is permitted to make on its own, if one is showing.

    `raid_scan_android.authorised_dismissal`, over the same imported
    `AUTHORISED_DISMISSALS`: the weekly adventure sync card, whose only exit is
    "CLAIM REWARD!", and the Continue Party Play prompt, where "REJOIN PARTY"
    restores the session the account was already in.  Sharing the list is the
    point -- a second copy here is how one half would come to clear a dialog the
    owner never authorised.
    """
    return authorised_dismissal(boxes, image)


def logical_point(
    pixel: tuple[float, float], image: Image.Image, viewport: tuple[int, int]
) -> tuple[int, int]:
    """Turn a pixel read off a screenshot into a point WebDriverAgent accepts.

    A screenshot is in device pixels and `mobile: tap` wants logical points, so
    anything located by looking at the picture has to come back through here:
    on a 2x phone a pixel tapped as a point lands at half the height it was
    found at.  The iPhone SE (3rd gen) is 750x1334 pixels over 375x667 points.
    """
    return (
        round(pixel[0] * viewport[0] / image.width),
        round(pixel[1] * viewport[1] / image.height),
    )


# --- the phone -------------------------------------------------------------


def build_capabilities(device: dict[str, Any]) -> dict[str, Any]:
    """Appium capabilities for one registered iPhone.

    `inventory_scanner_ios`'s capabilities, which is the bootstrap this repo
    already proves against this fleet.  `noReset` and `shouldTerminateApp` are
    the load-bearing pair here: they are what keeps Appium from clearing the
    game's data or closing it when a session starts or ends, which on this
    account would mean losing whatever it was in the middle of.

    `newCommandTimeout` is raised because this worker is slow by design: every
    screen is a screenshot plus an OCR pass, and the stock 60s timeout would cut
    a session off in the middle of a species.
    """
    capabilities: dict[str, Any] = {
        "platformName": "iOS",
        "appium:automationName": "XCUITest",
        "appium:udid": device["udid"],
        "appium:deviceName": device.get("name", "iPhone"),
        "appium:bundleId": device.get("bundle_id", "com.nianticlabs.pokemongo"),
        "appium:noReset": True,
        "appium:shouldTerminateApp": False,
        "appium:xcodeOrgId": device["team_id"],
        "appium:xcodeSigningId": device.get("xcode_signing_id", "Apple Development"),
        "appium:updatedWDABundleId": device["wda_bundle_id"],
        "appium:useNewWDA": False,
        "appium:newCommandTimeout": 3600,
    }
    if isinstance(device.get("platform_version"), str):
        capabilities["appium:platformVersion"] = device["platform_version"]
    if isinstance(device.get("wda_local_port"), int):
        capabilities["appium:wdaLocalPort"] = device["wda_local_port"]
    if isinstance(device.get("mjpeg_server_port"), int):
        capabilities["appium:mjpegServerPort"] = device["mjpeg_server_port"]
    if isinstance(device.get("derived_data_path"), str):
        capabilities["appium:derivedDataPath"] = config_paths.expand_user_path(
            device["derived_data_path"]
        )
    if device.get("use_preinstalled_wda"):
        capabilities["appium:usePreinstalledWDA"] = True
    return capabilities


def _locators(labels: Sequence[str]) -> list[tuple[str, str]]:
    """Every way worth asking for a control by name, most trustworthy first.

    The game is inconsistent about which of these it publishes for its own
    buttons, so all three are tried and "not found" means not on this screen.
    The Appium client is an optional dependency, so it is imported here rather
    than at module scope: this worker has to stay importable for its tests.
    """
    try:
        from appium.webdriver.common.appiumby import AppiumBy
    except ModuleNotFoundError as exc:  # reported at connect time
        raise RaidScanIOSError(
            "The Appium Python client is missing; install the ios extra"
        ) from exc
    found: list[tuple[str, str]] = []
    for label in labels:
        found.append((AppiumBy.ACCESSIBILITY_ID, label))
        found.append((
            AppiumBy.IOS_PREDICATE,
            f'name == "{label}" OR label == "{label}" OR value == "{label}"',
        ))
        found.append((
            AppiumBy.XPATH,
            f'//*[@name="{label}" or @label="{label}" or @value="{label}"]',
        ))
    return found


class IOSPhone:
    """One iPhone, driven over Appium/XCUITest.

    The session is the one `inventory_scanner_ios` already builds for this
    fleet; what is added here is the screenshot/OCR read and the rule that
    every point leaving this class was located on the screen first.
    """

    platform = "ios"

    def __init__(
        self,
        driver: Any,
        label: str = "",
        viewport: tuple[int, int] | None = None,
        bundle_id: str = "com.nianticlabs.pokemongo",
    ) -> None:
        self.driver = driver
        self.label = label or "iPhone"
        self.bundle_id = bundle_id
        if viewport is None:
            rect = driver.get_window_rect()
            viewport = (int(rect["width"]), int(rect["height"]))
        self.viewport = (int(viewport[0]), int(viewport[1]))

    @classmethod
    def connect(cls, spec: pokemon_fleet.DeviceSpec, *, label: str = "") -> "IOSPhone":
        """Open a session on a registered iPhone, or fail without touching the game.

        A phone that cannot be reached ends the run here.  Grinding through the
        remaining roster with a dead session would spend an hour producing
        identical errors.
        """
        name = label or spec.name
        profile = pokemon_fleet.load_appium_profile(spec)
        device = profile["device"]

        # A dark screen cannot run WebDriverAgent, and the failure it produces
        # names Xcode rather than the phone, four minutes later.
        asleep = ios_wda_cleanup.asleep_message(
            device["udid"], device.get("name", "The iPhone")
        )
        if asleep:
            raise RaidScanIOSError(asleep)

        try:
            from appium import webdriver
            from appium.options.ios import XCUITestOptions
        except ModuleNotFoundError as exc:
            raise RaidScanIOSError(
                "The Appium Python client is missing; install the ios extra"
            ) from exc

        options = XCUITestOptions().load_capabilities(build_capabilities(device))
        try:
            driver = webdriver.Remote(
                command_executor=profile["server_url"], options=options
            )
        except Exception as exc:
            raise RaidScanIOSError(
                f"{name}: could not start an Appium session on "
                f"{device.get('name', 'the iPhone')} ({exc})"
            ) from exc

        phone = cls(
            driver,
            label=name,
            bundle_id=device.get("bundle_id", "com.nianticlabs.pokemongo"),
        )
        phone.ensure_foreground()
        return phone

    # --- screen -------------------------------------------------------------

    def screenshot(self) -> Image.Image:
        try:
            png = self.driver.get_screenshot_as_png()
        except Exception as exc:
            raise RaidScanIOSError(f"{self.label}: could not read the screen") from exc
        if not png:
            raise RaidScanIOSError(f"{self.label}: empty screenshot")
        try:
            return Image.open(io.BytesIO(png)).convert("RGB")
        except OSError as exc:
            raise RaidScanIOSError(f"{self.label}: unreadable screenshot") from exc

    def read(self) -> tuple[Image.Image, list[vision_ocr.TextBox]]:
        image = self.screenshot()
        return image, vision_ocr.ocr(image)

    # --- input --------------------------------------------------------------

    def point(
        self, fraction: tuple[float, float], image: Image.Image
    ) -> tuple[int, int]:
        """A screen fraction, on the frame that was measured, as logical points."""
        return logical_point(
            (fraction[0] * image.width, fraction[1] * image.height),
            image,
            self.viewport,
        )

    def tap(
        self,
        fraction: tuple[float, float],
        image: Image.Image,
        settle: float = 1.1,
    ) -> None:
        """Tap a point that was located on the screen just read."""
        x, y = self.point(fraction, image)
        self._touch(x, y, settle)

    def _touch(self, x: int, y: int, settle: float) -> None:
        try:
            self.driver.execute_script("mobile: tap", {"x": x, "y": y})
        except Exception as exc:
            raise RaidScanIOSError(f"{self.label}: tap at ({x}, {y}) refused") from exc
        time.sleep(settle)

    def find_control(self, labels: Sequence[str], *, quick: bool = False) -> Any | None:
        """A displayed XCUIElement whose name, label or value matches `labels`.

        None means "this screen does not publish that control", which is a
        normal answer here rather than a failure: the game draws most of these
        screens itself and exposes only some of them.
        """
        locators = _locators(labels)
        if quick:
            # Accessibility id only: one round trip per label instead of three.
            locators = locators[::3]
        for by, locator in locators:
            try:
                found = self.driver.find_elements(by, locator)
            except Exception:
                # A locator the XCUITest driver rejects is one answer about that
                # strategy, not about the screen. Try the next.
                continue
            for element in found or ():
                try:
                    if element.is_displayed():
                        return element
                except Exception:
                    continue
        return None

    def tap_control(self, control: Any, settle: float = 1.2) -> None:
        """Click a control that was found on the screen, rather than a point."""
        try:
            control.click()
        except Exception as exc:
            raise RaidScanIOSError(
                f"{self.label}: could not use the control this screen shows"
            ) from exc
        time.sleep(settle)

    def clear_field(self, field: Any | None = None, settle: float = 0.8) -> None:
        """Empty the focused search field.

        A real element is cleared the way iOS expects.  Failing that -- and the
        game's field is usually not an element -- the delete key is sent
        straight at whatever has focus, which cannot miss the way a tap on the
        circular clear glyph can: that glyph is a second thing whose position
        differs between screen sizes.
        """
        if field is not None:
            try:
                field.clear()
                time.sleep(settle)
                return
            except Exception as exc:
                raise RaidScanIOSError(
                    f"{self.label}: the search field refused to clear"
                ) from exc
        # The game edits its field through a native text box drawn over the
        # keyboard, and that box publishes a real "Clear text" button.  The
        # delete-key run below does nothing to it on iOS 26 -- every search
        # was appended to the last one -- so the button comes first.
        clear = self.find_control(CLEAR_TEXT_LABELS, quick=True)
        if clear is not None:
            self.tap_control(clear, settle=settle)
            return
        try:
            self.driver.execute_script(
                "mobile: keys", {"keys": [DELETE_KEY] * CLEAR_KEYS}
            )
        except Exception as exc:
            raise RaidScanIOSError(
                f"{self.label}: could not clear the search field"
            ) from exc
        time.sleep(settle)

    def type_text(self, text: str, field: Any | None = None, settle: float = 1.4) -> None:
        """Type a search term into the focused field."""
        term = text.strip()
        if field is not None:
            try:
                field.send_keys(term)
            except Exception as exc:
                raise RaidScanIOSError(f"{self.label}: could not type '{term}'") from exc
            time.sleep(settle)
            return
        # The search field is custom-drawn and often has no discoverable
        # XCUIElement; XCUITest's application-level keyboard input targets the
        # focused field without needing one.  `gbl_ios` types its picker search
        # the same way.
        try:
            self.driver.execute_script("mobile: keys", {"keys": list(term)})
        except Exception as exc:
            raise RaidScanIOSError(f"{self.label}: could not type '{term}'") from exc
        time.sleep(settle)

    def drag(self, start: tuple[float, float], end: tuple[float, float],
             image: Image.Image, settle: float = 1.4) -> None:
        """Drag between two points located on `image`, with no press-and-hold.

        `dragFromToForDuration` holds the press before moving, and a held press
        is a tap to whatever is under it: that is how a scroll once opened the
        Mega Level page.  This presses and moves at once.
        """
        x0, y0 = self.point(start, image)
        x1, y1 = self.point(end, image)
        try:
            self.driver.execute_script("mobile: dragFromToWithVelocity", {
                "fromX": x0, "fromY": y0, "toX": x1, "toY": y1,
                "pressDuration": 0.05, "holdDuration": 0.05, "velocity": 900,
            })
        except Exception as exc:
            raise RaidScanIOSError(f"{self.label}: could not scroll the page") from exc
        time.sleep(settle)

    # --- the game -----------------------------------------------------------

    def ensure_foreground(self, settle: float = 2.0) -> None:
        """Make sure the game is the app in front, without starting or stopping it.

        A suspended or backgrounded game is resumed: that is the session it was
        already in.  A game that is *not running* is refused outright, because
        activating an app iOS has stopped is how it gets launched, and this
        worker never launches the game.

        Nothing in this class calls `terminate_app` or `mobile: terminateApp`.
        The Android worker tore down a live Party Play session that way and it
        had to be repaired by hand; `shouldTerminateApp: False` keeps a session
        ending from doing the same.
        """
        try:
            state = self.driver.execute_script(
                "mobile: queryAppState", {"bundleId": self.bundle_id}
            )
        except Exception as exc:
            raise RaidScanIOSError(
                f"{self.label}: could not ask iOS what the phone is showing ({exc})"
            ) from exc
        if state == IOS_APP_NOT_RUNNING:
            raise RaidScanIOSError(
                f"{self.label}: Pokémon GO is not running. This worker never "
                "launches or force-quits the game; open it on the handset and "
                "re-run."
            )
        if state != IOS_APP_FRONTMOST:
            self.driver.execute_script(
                "mobile: activateApp", {"bundleId": self.bundle_id}
            )
            time.sleep(settle)

    def close(self) -> None:
        """End the WebDriverAgent session and leave the game running."""
        try:
            self.driver.quit()
        except Exception:
            pass


# --- the scan --------------------------------------------------------------


class IOSStorageScanner:
    """Drive one iPhone's storage screen and collect per-species records.

    The per-species sequence is the Android worker's, unchanged: get to the
    storage grid, tap the search field, clear it, type the term, read the
    filtered grid, tap the cell holding the best CP, read that individual's
    detail page, record candy, XL candy and Max Moves.
    """

    def __init__(self, phone: IOSPhone, *, verbose: bool = True):
        self.phone = phone
        self.verbose = verbose
        self._no_field_control = False

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[{self.phone.label}] {message}", flush=True)

    def leave_detail_page(
        self, image: Image.Image, boxes: Sequence[vision_ocr.TextBox]
    ) -> bool:
        """Leave a detail page this scan opened, or say which screen it cannot.

        The page's own close disc is tried first: it is found on the
        screenshot just read, so it costs nothing, where each accessibility
        query costs a WebDriverAgent round trip and the game publishes none of
        these controls.  A labelled control is the fallback, then a close
        label read off the screen.  A screen offering none of the three is
        reported rather than swiped at -- an edge swipe is iOS's BACK
        equivalent and as dangerous as Android's on a screen it has not
        identified.
        """
        point = close_button_point(image) if classify(boxes) == "detail" else None
        if point is None:
            control = self.phone.find_control(CLOSE_CONTROL_LABELS)
            if control is not None:
                self.log("leaving the detail page")
                self.phone.tap_control(control, settle=1.6)
                return True
            point = detail_close_point(boxes, image)
        if point is None:
            raise RaidScanIOSError(
                f"{self.phone.label}: on a Pokémon detail page with no close "
                "control this worker can see. Dismiss it on the handset and "
                "re-run; nothing is tapped to guess at it."
            )
        self.phone.tap(point, image, settle=1.6)
        return True

    def ensure_grid(self, attempts: int = 18) -> None:
        """Return to the storage grid, or fail without leaving the game.

        The Android worker can retreat with BACK because a detail page is a
        screen it has just confirmed.  The equivalent here is the close control
        on that page, found on it.  Everything else is either an authorised
        dialog, a visible way out, or a stop -- there is no key press in this
        method that is not answered by something on the screen.
        """
        menu_pressed = False
        unsettled = 0
        for _ in range(attempts):
            image, boxes = self.phone.read()
            # Checked before anything else: the adventure sync card counts
            # candy, so it reads as a detail page, and treating it as one would
            # send a close tap straight at a reward card.
            allowed = authorised_dialog_tap(boxes, image)
            if allowed is not None:
                target, why = allowed
                self.log(f"clearing an authorised dialog: {why}")
                self.phone.tap(target, image, settle=2.4)
                continue
            blocked = blocking_screen(boxes)
            if blocked is not None:
                raise RaidScanIOSError(f"{self.phone.label}: {blocked}")
            if raid_inventory.is_mega_level_page(boxes):
                point = close_button_point(image)
                if point is None:
                    raise RaidScanIOSError(
                        f"{self.phone.label}: on a Mega Level page with no close "
                        "button this worker can see; nothing on it is tapped"
                    )
                self.log("closing a Mega Level page")
                self.phone.tap(point, image, settle=1.6)
                continue
            if storage_grid_visible(boxes, image):
                return
            if classify(boxes) == "detail":
                self.leave_detail_page(image, boxes)
                continue
            if looks_like_loading(boxes):
                unsettled += 1
                if unsettled > 8:
                    raise RaidScanIOSError(
                        f"{self.phone.label}: the game is still on its loading "
                        "screen; check the handset and re-run"
                    )
                time.sleep(8.0)
                continue
            target = navigation_tap(boxes, image)
            if target is not None:
                self.log("dismissing an unexpected screen")
                self.phone.tap(target, image, settle=1.8)
                continue
            if unsettled < 3:
                # Waking the phone throws up coach tips and progress cards that
                # clear themselves. Tapping through one of those is how a scan
                # ends up somewhere unrelated, so the first few unrecognised
                # reads only wait.
                unsettled += 1
                time.sleep(3.0)
                continue
            unsettled = 0
            if not menu_pressed:
                menu_pressed = True
                self.open_main_menu()
                continue
            raise RaidScanIOSError(
                f"{self.phone.label}: could not reach the Pokémon storage grid; "
                "the phone is on a screen this workflow will not tap through"
            )
        raise RaidScanIOSError(
            f"{self.phone.label}: could not reach the Pokémon storage grid"
        )

    def open_main_menu(self) -> None:
        """Open the map's main menu, from a control found on the map.

        Android taps a fixed ball here.  There is nothing to fall back on for an
        iPhone and no coordinate this workflow is willing to invent, so this is
        the real control or the run stops: if the map's menu button is not
        exposed to XCUITest, the operator opens storage by hand and the scan
        continues from there.
        """
        control = self.phone.find_control(MENU_CONTROL_LABELS)
        if control is None:
            raise RaidScanIOSError(
                f"{self.phone.label}: on the map, with no storage screen and no "
                "menu control XCUITest can see. Open the Pokémon storage screen "
                "on the handset and re-run; this worker will not tap a guessed "
                "coordinate to get there."
            )
        self.log("opening the map menu to reach storage")
        self.phone.tap_control(control, settle=2.2)

    def search(self, term: str):
        """Filter storage to `term`, and prove the filter took.

        The keyboard is left up: the parenthesised result counter is the only
        reliable "a filter is active" signal and it stays visible.  A counter
        is not enough on its own -- a search that did not take leaves the
        previous species' results, counter and all -- so the field's own text
        has to show the term too.
        """
        image, boxes = self.phone.read()
        # The game draws its field itself; once it has been seen not to
        # publish one, asking again on every search only costs round trips.
        field = None
        if not self._no_field_control:
            field = self.phone.find_control(SEARCH_FIELD_LABELS)
            self._no_field_control = field is None
        target: tuple[float, float] | None = None
        for attempt in range(3):
            if field is not None:
                self.phone.tap_control(field, settle=1.2)
            else:
                target = search_field_point(boxes, image)
                if target is None:
                    self.log(f"cannot locate the search field for '{term}'")
                    return None, [], image, boxes
                # A miss goes low on the SE; nudge down on the retries.
                self.phone.tap((target[0], min(target[1] + 0.012 * attempt, 0.30)),
                               image, settle=1.2)
            self.phone.clear_field(field)
            self.phone.type_text(term, field)
            image, boxes = self.phone.read()
            count = parse_result_count(boxes, image.height)
            if count is not None and raid_inventory.field_shows(image, boxes, term):
                return count, parse_grid_entries(boxes), image, boxes
            self.log(f"filter '{term}' did not take; retrying")
            if not storage_grid_visible(boxes, image):
                self.ensure_grid()
                image, boxes = self.phone.read()
        return None, parse_grid_entries(boxes), image, boxes

    def regrid(self):
        """Re-read the filtered grid after leaving a detail page."""
        image, boxes = self.phone.read()
        if not storage_grid_visible(boxes, image):
            self.ensure_grid()
            image, boxes = self.phone.read()
        return (parse_result_count(boxes, image.height), parse_grid_entries(boxes),
                image, boxes)

    def open_cell(
        self, cp: int, image: Image.Image, entries: Sequence[GridEntry]
    ) -> bool:
        """Tap the grid cell showing `cp`, at the point its CP was read."""
        point = grid_cell_point(cp, image, entries)
        if point is None:
            return False
        self.phone.tap(point, image, settle=1.9)
        return True

    def read_detail(self) -> list[tuple[Image.Image, list[vision_ocr.TextBox]]]:
        """The detail page, then the same page scrolled to its moves."""
        image, boxes = self.phone.read()
        for _ in range(2):
            promo = _button_label(boxes) if classify(boxes) == "detail" else None
            if promo is None:
                break
            self.log(f"clearing a card over the detail page ({promo.text.strip()})")
            self.phone.tap((promo.cx / image.width, promo.cy / image.height), image, settle=2.0)
            image, boxes = self.phone.read()
        if classify(boxes) != "detail":
            raise RaidScanIOSError(f"{self.phone.label}: the detail page did not open")
        anchor, image, boxes = raid_inventory.settled_anchor(self.phone.read, image, boxes)
        screens = [(image, boxes)]
        if anchor is not None:
            self.phone.drag(anchor, raid_inventory.scroll_target(anchor), image)
            screens.append(self.phone.read())
        return screens

    def leave_detail(self) -> None:
        image, boxes = self.phone.read()
        if raid_inventory.is_mega_level_page(boxes):
            # `ensure_grid` closes it with its own disc and goes on from there.
            self.ensure_grid()
            return
        self.leave_detail_page(image, boxes)


def resolve_device(device: str | pokemon_fleet.DeviceSpec) -> pokemon_fleet.DeviceSpec:
    """A registered iPhone by fleet name, or the spec itself."""
    if isinstance(device, pokemon_fleet.DeviceSpec):
        return device
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    spec = fleet.devices.get(device)
    if spec is None:
        raise RaidScanIOSError(f"{device} is not in the fleet configuration")
    if spec.platform != "ios":
        raise RaidScanIOSError(f"{device} is a {spec.platform} device")
    return spec


def scan_device(
    device: str | pokemon_fleet.DeviceSpec,
    terms: Sequence[tuple[str, str, bool]],
    *,
    label: str = "",
    checkpoint: Path | None = None,
    resume: bool = False,
) -> list:
    """Scan every `(term, display, shadow_eligible)` on one iPhone."""
    spec = resolve_device(device)
    phone = IOSPhone.connect(spec, label=label)
    try:
        return raid_inventory.scan_account(
            IOSStorageScanner(phone), terms, label=label or spec.name,
            checkpoint=checkpoint, resume=resume,
        )
    finally:
        phone.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan an iPhone's Pokémon GO storage for raid attackers."
    )
    parser.add_argument("--device", required=True, help="fleet device name")
    parser.add_argument("--label", default="", help="name for progress output")
    parser.add_argument("--terms", type=Path, required=True,
                        help="JSON list of [search_term, display, shadow_eligible]")
    parser.add_argument("--out", type=Path, help="checkpoint and result file")
    parser.add_argument("--limit", type=int, default=0, help="only scan the first N terms")
    parser.add_argument("--resume", action="store_true",
                        help="keep species already complete in --out and scan the rest")
    args = parser.parse_args(argv)

    terms = [(str(t[0]), str(t[1]), bool(t[2]) if len(t) > 2 else True)
             for t in json.loads(args.terms.read_text())]
    if args.limit:
        terms = terms[: args.limit]
    scan_device(args.device, terms, label=args.label, checkpoint=args.out, resume=args.resume)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
