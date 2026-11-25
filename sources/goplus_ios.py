"""Reconnect Go Plus on iOS using the game's map icon."""

import asyncio
import time

from PIL import Image
from selenium.common.exceptions import NoAlertPresentException

from .goplus_android import ACCESSORY_KEY, CONNECTION_SETTLE, POLL_GAP, GoPlusError
from .goplus_android import accessory_point, find_accessory_disc, is_go_plus_name


def connected_icon(image: Image.Image, point: list[int] | None = None) -> bool:
    """An active accessory icon has a saturated green center."""
    point = point or find_accessory_disc(image)
    if point is None:
        return False
    x, y = point
    radius = max(1, round(image.width * 0.012))
    dot = image.convert("RGB").crop((x - radius, y - radius, x + radius + 1, y + radius + 1))
    green = sum(g >= 170 and g - r >= 90 and g - b >= 70 for r, g, b in dot.getdata())
    return green >= dot.width * dot.height * 0.4


def accept_pairing_alert(driver) -> bool:
    try:
        alert = driver.switch_to.alert
        text = alert.text
    except NoAlertPresentException:
        return False
    if "bluetooth pairing request" not in text.casefold() or not is_go_plus_name(text):
        return False
    driver.execute_script("mobile: alert", {"action": "accept", "buttonLabel": "Pair"})
    return True


async def reconnect(phone, *, timeout: float) -> str:
    """Confirm the game's connected indicator on successive screenshots."""
    target = phone.target
    driver = target.device.driver
    accepted = await asyncio.to_thread(accept_pairing_alert, driver)
    if not accepted:
        image = await phone.go_home()
        point = accessory_point(image, target.mapped(ACCESSORY_KEY, image))
        if point is None:
            raise GoPlusError("Go Plus button is missing from the map")
        if connected_icon(image, point):
            return "already connected (green Go Plus icon)"
        await phone.tap(point, image)
        phone.say(f"pressed the Go Plus button at {point}")

    deadline = time.monotonic() + timeout
    connected_since = None
    while time.monotonic() < deadline:
        image = await phone.look()
        now = time.monotonic()
        if connected_icon(image):
            if connected_since is None:
                connected_since = now
            elif now - connected_since >= CONNECTION_SETTLE:
                return "connected (green Go Plus icon)"
        else:
            connected_since = None
            if await asyncio.to_thread(accept_pairing_alert, driver):
                phone.say("accepted the iOS Go Plus pairing alert")
        await asyncio.sleep(POLL_GAP)
    raise GoPlusError("the Go Plus icon did not turn green")
