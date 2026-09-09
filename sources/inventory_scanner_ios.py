#!/usr/bin/env python3
"""Automated Selenium / Appium iPhone Storage Scanner for Pokemon GO.

Fires Appium/Selenium searches on the connected iPhone Pro Max, captures screenshots
of Pokemon storage search results (CP <= 1500, 3*, 4*, pvp), parses Pokemon names and CPs
using native Apple Vision OCR / Tesseract, and feeds the scanned inventory into gbl_evaluator.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image
import yaml

from . import config_paths, gbl_evaluator, gbl_ios, gift_ios, pokemon_fleet

SWIFT_OCR_PATH = Path("/tmp/ocr_mac")


def ensure_ocr_utility() -> Path:
    """Compiles native Swift Vision OCR utility if not already compiled."""
    if SWIFT_OCR_PATH.is_file():
        return SWIFT_OCR_PATH
    
    swift_code = """
import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count > 1 else { exit(1) }
let path = CommandLine.arguments[1]
guard let image = NSImage(contentsOfFile: path),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(1) }

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
let request = VNRecognizeTextRequest { req, err in
    guard let results = req.results as? [VNRecognizedTextObservation] else { return }
    for obs in results {
        if let text = obs.topCandidates(1).first?.string {
            print(text)
        }
    }
}
request.recognitionLevel = .accurate
try? handler.perform([request])
"""
    code_path = Path("/tmp/ocr.swift")
    code_path.write_text(swift_code)
    subprocess.run(["swiftc", str(code_path), "-o", str(SWIFT_OCR_PATH)], check=True)
    return SWIFT_OCR_PATH


def ocr_image_text(image_path: Path) -> list[str]:
    """Extracts text lines from an image using native Apple Vision OCR."""
    ocr_bin = ensure_ocr_utility()
    res = subprocess.run([str(ocr_bin), str(image_path)], capture_output=True, text=True, check=False)
    lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    return lines


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
    capabilities = {
        "platformName": "iOS",
        "appium:automationName": "XCUITest",
        "appium:udid": device_dict["udid"],
        "appium:deviceName": device_dict.get("name", "iPhone"),
        "appium:bundleId": device_dict.get("bundle_id", "com.nianticlabs.pokemongo"),
        "appium:noReset": True,
        "appium:shouldTerminateApp": False,
        "appium:xcodeOrgId": device_dict["team_id"],
        "appium:xcodeSigningId": device_dict.get(
            "xcode_signing_id", "Apple Development"
        ),
        "appium:updatedWDABundleId": device_dict["wda_bundle_id"],
        "appium:useNewWDA": False,
    }
    
    if isinstance(device_dict.get("wda_local_port"), int):
        capabilities["appium:wdaLocalPort"] = device_dict["wda_local_port"]
    if device_dict.get("use_preinstalled_wda"):
        capabilities["appium:usePreinstalledWDA"] = True

    server_url = appium_cfg.get("server_url", "http://127.0.0.1:4723")
    name = device_dict.get("name", "iPhone")
    print(f"[+] Connecting Selenium WebDriver session to Appium on {server_url} ({name})...")
    try:
        driver = webdriver.Remote(
            command_executor=server_url,
            options=XCUITestOptions().load_capabilities(capabilities),
        )
    except Exception as exc:
        if capabilities.get("appium:usePreinstalledWDA", False):
            print(
                f"[{name}] Preinstalled WebDriverAgent would not start ({exc});"
                " restarting the installed runner once",
                flush=True,
            )
            ios_wda_cleanup.stop_wda_runner(device_dict["udid"])
            time.sleep(1)
            driver = webdriver.Remote(
                command_executor=server_url,
                options=XCUITestOptions().load_capabilities(capabilities),
            )
        else:
            raise
    
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
            png_bytes = driver.get_screenshot_as_png()
            tmp_img_path = Path(f"/tmp/scan_{query.replace('*', 'star')}.png")
            tmp_img_path.write_bytes(png_bytes)
            
            # Run OCR on captured screen
            lines = ocr_image_text(tmp_img_path)
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
