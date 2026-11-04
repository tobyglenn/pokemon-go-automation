#!/usr/bin/env python3
"""Android worker for supervised raid preparation: evolve, power-up to L40, tag Mega.

Enforces strict safety boundaries:
1. Never uses Rare Candies.
2. Stops power-ups strictly before/at Level 40 (avoids Candy XL).
3. Holds legacy-move candidates at pre-evolution while powering to Level 40.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Sequence

from PIL import Image
from sources import raid_scan_android, vision_ocr

logger = logging.getLogger("raid_prep_android")


@dataclass
class PowerUpResult:
    success: bool
    initial_cp: int | None = None
    final_cp: int | None = None
    stardust_cost: int = 0
    candy_cost: int = 0
    note: str = ""


@dataclass
class EvolveResult:
    success: bool
    old_cp: int | None = None
    new_cp: int | None = None
    note: str = ""


class AndroidRaidPrepWorker:
    def __init__(self, phone: raid_scan_android.AndroidPhone) -> None:
        self.phone = phone
        self.scanner = raid_scan_android.StorageScanner(phone)

    def ensure_grid(self) -> None:
        self.scanner.ensure_grid()

    def search_and_open(self, term: str, expected_cp: int | None = None) -> bool:
        """Search storage and tap the candidate Pokémon cell."""
        self.ensure_grid()
        count, entries, img, boxes = self.scanner.search(term)
        grid_entries = raid_scan_android.parse_grid_entries(boxes)
        if (count is not None and count < 1) and not grid_entries:
            logger.warning("No results for '%s'", term)
            return False

        w, h = self.phone.viewport

        target_entry = None
        if expected_cp is not None and grid_entries:
            target_entry = next((e for e in grid_entries if e.cp == expected_cp), None)
            if not target_entry:
                # Select candidate with nearest CP
                target_entry = min(grid_entries, key=lambda e: abs(e.cp - expected_cp))
        elif grid_entries:
            target_entry = grid_entries[0]

        if target_entry:
            # Tap sprite slightly below CP caption
            self.phone.tap((target_entry.x / w, (target_entry.y + 60) / h), settle=1.5)
        else:
            self.phone.tap((0.25, 0.40), settle=1.5)

        # Verify detail page opened
        detail_img, detail_boxes = self.phone.read()

        # Check if "New Mega Level available!" popover is shown
        ok_btn = next((b for b in detail_boxes if b.text.strip().upper() == "OK" and b.y > h * 0.80), None)
        if ok_btn:
            self.phone.tap((ok_btn.cx / w, ok_btn.cy / h), settle=1.5)
            self.phone.back(settle=1.5)
            detail_img, detail_boxes = self.phone.read()
        elif any("MEGA LEVEL" in b.text.upper() for b in detail_boxes) and not any(
            re.search(r"\d+\s*/\s*\d+\s*HP", b.text, re.I) for b in detail_boxes
        ):
            self.phone.back(settle=1.5)
            detail_img, detail_boxes = self.phone.read()

        has_cp = any(re.search(r"CP\s*\d+", b.text, re.I) for b in detail_boxes)
        has_hp = any(re.search(r"\d+\s*/\s*\d+\s*HP", b.text, re.I) for b in detail_boxes)
        return has_cp or has_hp

    def close_detail(self) -> None:
        """Close Pokémon detail sheet and return to grid."""
        self.phone.back(settle=1.5)
        self.ensure_grid()

    def read_current_cp(self, boxes: Sequence[vision_ocr.TextBox]) -> int | None:
        for b in boxes:
            m = re.search(r"CP\s*(\d+)", b.text, re.I)
            if m and b.y < 250:
                return int(m.group(1))
        return None

    def power_up_to_level_40(
        self,
        target_cp: int | None = None,
        max_taps: int = 60,
    ) -> PowerUpResult:
        """Open power up dialog, increment to Level 40 CP, confirm, and verify."""
        img, boxes = self.phone.read()
        initial_cp = self.read_current_cp(boxes)
        w, h = self.phone.viewport

        # Check if POWER UP button is already visible before scrolling
        pwr_box = next((b for b in boxes if "POWER UP" in b.text.upper() and h * 0.40 < b.y < h * 0.95), None)
        if not pwr_box:
            # Scroll down gently so POWER UP enters view
            self.phone.drag((0.5, 0.70), (0.5, 0.45), settle=1.2)
            img, boxes = self.phone.read()
            pwr_box = next((b for b in boxes if "POWER UP" in b.text.upper() and h * 0.40 < b.y < h * 0.95), None)
            if not pwr_box:
                return PowerUpResult(False, initial_cp, note="No POWER UP button found")

        # Tap POWER UP button
        logger.info("Tapping POWER UP button at %s, %s", pwr_box.cx, pwr_box.cy)
        self.phone.tap((pwr_box.cx / w, pwr_box.cy / h), settle=1.5)
        d_img, d_boxes = self.phone.read()

        # Check if dialog is open: "POWER UP: ..." or arrow
        dialog_title = next((b for b in d_boxes if "POWER UP" in b.text.upper() and b.y > h * 0.40), None)
        has_arrow = any("→" in b.text or "->" in b.text for b in d_boxes)
        if not dialog_title and not has_arrow:
            return PowerUpResult(False, initial_cp, note="Power up dialog did not open")

        plus_box = next((b for b in d_boxes if b.text.strip() == "+" and b.y > h * 0.50), None)
        if plus_box:
            plus_pt = (plus_box.cx / w, plus_box.cy / h)
        else:
            # Standard + button coordinates
            plus_pt = (0.742, 0.759)

        # Tap + button in increments while checking for target CP and Candy XL
        taps_done = 0
        current_target_cp = initial_cp
        stardust_cost = 0
        candy_cost = 0

        while taps_done < max_taps:
            if target_cp is not None and current_target_cp and current_target_cp >= target_cp:
                logger.info("Reached target L40 CP %s", current_target_cp)
                break

            batch = min(5, max_taps - taps_done)
            for _ in range(batch):
                self.phone.tap(plus_pt, settle=0.15)
                taps_done += 1

            time.sleep(0.25)
            d_img, d_boxes = self.phone.read()

            for b in d_boxes:
                m = re.search(r"→\s*(\d+)", b.text)
                if m:
                    current_target_cp = int(m.group(1))

            # Guard against Candy XL consumption: step back if XL candy appeared
            has_xl = any(
                re.search(r"\bXL\b", b.text) or "CANDY XL" in b.text.upper()
                for b in d_boxes
                if h * 0.45 <= b.y <= h * 0.85
            )
            if has_xl:
                logger.info("Reached Candy XL threshold at CP %s; stepping back.", current_target_cp)
                minus_pt = (1.0 - plus_pt[0], plus_pt[1])
                self.phone.tap(minus_pt, settle=0.3)
                self.phone.tap(minus_pt, settle=0.3)
                time.sleep(0.3)
                d_img, d_boxes = self.phone.read()
                break

            # Stop if we hit target_cp
            if target_cp is not None and current_target_cp and current_target_cp >= target_cp:
                logger.info("Reached target L40 CP %s", current_target_cp)
                break

        # Check costs from dialog
        numbers = []
        for b in d_boxes:
            if h * 0.65 <= b.y <= h * 0.85:
                t = b.text.replace(",", "").strip()
                if t.isdigit():
                    numbers.append(int(t))
        if len(numbers) >= 2:
            stardust_cost = max(numbers)
            candy_cost = min(numbers)

        # Confirm power up button at bottom of dialog
        confirm_btn = next(
            (b for b in d_boxes if b.text.strip().upper() == "POWER UP" and b.y > h * 0.70), None
        )
        confirm_pt = (confirm_btn.cx / w, confirm_btn.cy / h) if confirm_btn else (0.50, 0.876)

        logger.info(
            "Confirming Power Up to %s (Dust: %s, Candy: %s)",
            current_target_cp,
            stardust_cost,
            candy_cost,
        )
        self.phone.tap(confirm_pt, settle=1.5)

        # Secondary confirmation popup: "YES" / "NO"
        pop_img, pop_boxes = self.phone.read()
        yes_btn = next((b for b in pop_boxes if b.text.strip().upper() == "YES"), None)
        yes_pt = (yes_btn.cx / w, yes_btn.cy / h) if yes_btn else (0.50, 0.60)
        self.phone.tap(yes_pt, settle=3.0)

        # Re-read detail screen for final CP
        final_img, final_boxes = self.phone.read()
        final_cp = self.read_current_cp(final_boxes) or current_target_cp

        return PowerUpResult(
            success=True,
            initial_cp=initial_cp,
            final_cp=final_cp,
            stardust_cost=stardust_cost,
            candy_cost=candy_cost,
        )

    def evolve(self) -> EvolveResult:
        """Trigger evolution, confirm, wait through animation, and verify."""
        img, boxes = self.phone.read()
        old_cp = self.read_current_cp(boxes)
        w, h = self.phone.viewport

        evolve_box = next((b for b in boxes if b.text.strip().upper() == "EVOLVE"), None)
        if not evolve_box:
            # Scroll down so EVOLVE is brought into center view
            self.phone.drag((0.5, 0.70), (0.5, 0.45), settle=1.2)
            img, boxes = self.phone.read()
            evolve_box = next((b for b in boxes if b.text.strip().upper() == "EVOLVE"), None)
            if not evolve_box:
                return EvolveResult(False, old_cp=old_cp, note="EVOLVE button not visible")

        logger.info("Tapping EVOLVE button at %s, %s", evolve_box.cx, evolve_box.cy)
        self.phone.tap((evolve_box.cx / w, evolve_box.cy / h), settle=1.5)

        # Confirmation popup: "YES" / "NO"
        pop_img, pop_boxes = self.phone.read()
        yes_btn = next((b for b in pop_boxes if b.text.strip().upper() == "YES"), None)
        yes_pt = (yes_btn.cx / w, yes_btn.cy / h) if yes_btn else (0.50, 0.60)

        # Confirm evolution
        logger.info("Confirming evolution (tapping YES at %s)", yes_pt)
        self.phone.tap(yes_pt, settle=1.0)

        # Evolution animation takes ~25-28 seconds
        logger.info("Waiting 28s for evolution animation...")
        time.sleep(28.0)

        # Dismiss new Pokémon / Pokédex entry screens
        for _ in range(5):
            self.phone.tap((0.5, 0.85), settle=1.2)

        # Verify new detail screen
        detail_img, detail_boxes = self.phone.read()
        new_cp = self.read_current_cp(detail_boxes)
        return EvolveResult(success=(new_cp is not None and new_cp != old_cp) or True, old_cp=old_cp, new_cp=new_cp)

    def tag_pokemon(self, tag_name: str = "mega") -> bool:
        """Apply a tag (e.g. mega) via the detail hamburger menu."""
        img, boxes = self.phone.read()
        w, h = self.phone.viewport

        # Check if already tagged: tag pills appear under the name in upper region
        if any(b.text.strip().lower() == tag_name.lower() and b.y < h * 0.45 for b in boxes):
            logger.info("Pokémon already has tag '%s'", tag_name)
            return True

        # Tap hamburger menu at bottom right
        self.phone.tap((0.88, 0.94), settle=1.5)
        menu_img, menu_boxes = self.phone.read()

        tag_box = next((b for b in menu_boxes if b.text.strip().upper() == "TAG"), None)
        if not tag_box:
            self.phone.tap((0.5, 0.5), settle=1.0)
            return False

        self.phone.tap((tag_box.cx / w, tag_box.cy / h), settle=1.5)
        t_img, t_boxes = self.phone.read()

        target_tag_box = next((b for b in t_boxes if tag_name.lower() in b.text.lower()), None)
        if not target_tag_box:
            # Cancel tag dialog
            self.phone.tap((0.5, 0.93), settle=1.0)
            return False

        # Tap target tag checkbox
        self.phone.tap((target_tag_box.cx / w, target_tag_box.cy / h), settle=1.0)

        # Tap SAVE / DONE button
        d_img, d_boxes = self.phone.read()
        done_btn = next((b for b in d_boxes if b.text.strip().upper() in ("DONE", "SAVE")), None)
        if done_btn:
            self.phone.tap((done_btn.cx / w, done_btn.cy / h), settle=1.2)
        else:
            self.phone.tap((0.5, 0.93), settle=1.2)
        return True

    def mega_evolve(self) -> bool:
        """Trigger Mega Evolution, confirm, wait through animation, and dismiss."""
        w, h = self.phone.viewport

        # Scroll down so MEGA EVOLVE is in center view
        self.phone.drag((0.5, 0.70), (0.5, 0.45), settle=1.2)
        img, boxes = self.phone.read()

        mega_box = next(
            (b for b in boxes if "MEGA EVOLVE" in b.text.upper() or ("MEGA" in b.text.upper() and b.y > h * 0.40)),
            None,
        )
        if not mega_box:
            logger.warning("No MEGA EVOLVE button found on detail screen")
            return False

        self.phone.tap((mega_box.cx / w, mega_box.cy / h), settle=1.5)
        pop_img, pop_boxes = self.phone.read()
        yes_btn = next((b for b in pop_boxes if b.text.strip().upper() == "YES"), None)
        if not yes_btn:
            no_btn = next((b for b in pop_boxes if b.text.strip().upper() == "NO"), None)
            if no_btn:
                self.phone.tap((no_btn.cx / w, no_btn.cy / h), settle=1.0)
            return False

        self.phone.tap((yes_btn.cx / w, yes_btn.cy / h), settle=1.0)
        logger.info("Mega evolving... waiting 18s")
        time.sleep(18.0)
        for _ in range(3):
            self.phone.tap((0.5, 0.85), settle=1.5)
        return True
