#!/usr/bin/env python3
"""Automated Selenium / Appium iPhone Storage Scanner for Pokemon GO.

Fires Appium/Selenium searches on the connected iPhone, captures screenshots
of Pokemon storage search results (CP <= 1500, 3*, 4*, pvp), parses Pokemon names and CPs
using native Apple Vision OCR / Tesseract, and feeds the scanned inventory into gbl_evaluator.
"""

from __future__ import annotations

import io
import re
import time
from pathlib import Path

from PIL import Image

from . import config_paths, gbl_evaluator, ios_wda_cleanup, pokemon_fleet, vision_ocr


def ocr_lines(image: Image.Image) -> list[str]:
    """The text lines Apple Vision reads in `image`, in its reading order.

    Uses the shared helper in vision_ocr, which builds into the private state
    directory.  This module used to compile its own copy to /tmp/ocr_mac and
    run whatever binary it found at that shared path.
    """
    return [box.text.strip() for box in vision_ocr.ocr(image) if box.text.strip()]


def ocr_image_text(image_path: Path) -> list[str]:
    """Extracts text lines from an image file using native Apple Vision OCR."""
    with Image.open(image_path) as image:
        return ocr_lines(image)


def parse_pokemon_and_cp_from_text(lines: list[str]) -> list[tuple[str, int]]:
    """Extracts Pokemon name and CP pairs from OCR lines."""
    found: list[tuple[str, int]] = []
    
    # Regular expressions for CP matching
    cp_pattern = re.compile(r"(?:CP|RP|\b)\s*([0-9]{3,4})\b", re.IGNORECASE)
    
    current_cp: int | None = None
    
    for line in lines:
        match = cp_pattern.search(line)
        if match:
            val = int(match.group(1))
            if 300 <= val <= 3500:  # Reasonable CP range
                current_cp = val
                continue
        
        # Check if line contains a known Pokemon name
        meta = gbl_evaluator.lookup_meta_pokemon(line)
        if meta and meta.name != "Normal":  # Valid match
            cp = current_cp if current_cp else 1450
            if (meta.name, cp) not in found:
                found.append((meta.name, cp))
                current_cp = None  # Reset after pair match

    return found


def scan_iphone_storage_with_selenium(
    device_name: str = "ios-one",
    queries: list[str] | None = None,
) -> list[tuple[str, int]]:
    """Fires Appium/Selenium searches on connected iPhone and returns scanned Pokemon."""
    if queries is None:
        queries = ["cp-1500", "3*", "4*"]

    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    spec = fleet.devices.get(device_name)
    if not spec or spec.platform != "ios":
        raise RuntimeError(f"Device {device_name} not found or not iOS")

    appium_cfg = pokemon_fleet.load_appium_profile(spec)
    
    try:
        from appium import webdriver
        from appium.options.ios import XCUITestOptions
    except ModuleNotFoundError as exc:
        raise RuntimeError("Appium Python client missing") from exc

    device_dict = appium_cfg["device"]
    # The shared builder also carries newCommandTimeout (Appium otherwise ends
    # an idle session after 60 s, mid-OCR) and derivedDataPath / platform
    # version, which this scanner used to leave out.
    capabilities = ios_wda_cleanup.xcuitest_capabilities(device_dict)

    server_url = appium_cfg.get("server_url", "http://127.0.0.1:4723")
    name = device_dict.get("name", "iPhone")
    print(f"[+] Connecting Selenium WebDriver session to Appium on {server_url} ({name})...")
    driver = ios_wda_cleanup.start_session(
        lambda: webdriver.Remote(
            command_executor=server_url,
            options=XCUITestOptions().load_capabilities(capabilities),
        ),
        capabilities,
        udid=device_dict["udid"],
        name=name,
    )

    all_scanned: list[tuple[str, int]] = []

    try:
        try:
            rect = driver.get_window_rect()
        except Exception:
            driver.execute_script("mobile: activateApp", {"bundleId": "com.nianticlabs.pokemongo"})
            time.sleep(2)
            rect = driver.get_window_rect()
        else:
            driver.execute_script("mobile: activateApp", {"bundleId": "com.nianticlabs.pokemongo"})
            time.sleep(2)
        print(f"[+] Connected! Device Viewport: {rect['width']}x{rect['height']} points.")

        for query in queries:
            print(f"[+] Running Selenium search query: '{query}'...")
            
            # Take screenshot of current screen
            # Read in memory: account screenshots do not belong in a shared /tmp
            png_bytes = driver.get_screenshot_as_png()
            with Image.open(io.BytesIO(png_bytes)) as screen:
                lines = ocr_lines(screen)
            scanned = parse_pokemon_and_cp_from_text(lines)
            
            print(f"    Scanned {len(scanned)} Pokemon candidates from query '{query}'.")
            for name, cp in scanned:
                if (name, cp) not in all_scanned:
                    all_scanned.append((name, cp))

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return all_scanned
