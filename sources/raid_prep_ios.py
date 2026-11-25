#!/usr/bin/env python3
"""iOS worker for supervised raid preparation: evolve, power-up to L40, tag Mega.

Enforces strict safety boundaries:
1. Never uses Rare Candies.
2. Stops power-ups strictly before/at Level 40 (avoids Candy XL).
3. Holds legacy-move candidates at pre-evolution while powering to Level 40.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Sequence

from PIL import Image
from sources import raid_scan_ios, vision_ocr
from sources.raid_prep_common import (  # noqa: F401  re-exported
    EvolveResult,
    PowerUpResult,
    overshot,
    powered_up,
    preview_cp,
)

# Where the power-up dialog's cost rows sit, in screenshot pixels.
XL_BAND = (800, 1050)

logger = logging.getLogger("raid_prep_ios")


class IOSRaidPrepWorker:
    def __init__(self, phone: raid_scan_ios.IOSPhone) -> None:
        self.phone = phone
        self.scanner = raid_scan_ios.IOSStorageScanner(phone)

    def ensure_grid(self) -> None:
        self.scanner.ensure_grid()

    def search_and_open(self, term: str, expected_cp: int | None = None) -> bool:
        """Search storage and tap the candidate Pokémon cell."""
        self.ensure_grid()
        count, entries, img, boxes = self.scanner.search(term, exact=True)
        if not count or count < 1:
            logger.warning("No results for '%s'", term)
            return False

        # If expected_cp is provided, find the cell nearest to that CP
        target_pt = (0.25, 0.40)  # default first cell
        if expected_cp is not None:
            # The CP names the individual to spend on: never fall back to
            # whichever Pokémon happens to sit in the first cell.
            for b in boxes:
                m = re.search(r"CP\s*(\d+)", b.text)
                if m and int(m.group(1)) == expected_cp:
                    target_pt = (b.cx / img.width, (b.cy + 30) / img.height)
                    break
            else:
                logger.warning("No '%s' result at CP %s; touching nothing", term, expected_cp)
                return False

        self.phone.tap(target_pt, img, settle=1.5)
        # Verify detail page opened
        detail_img, detail_boxes = self.phone.read()
        has_cp = any(re.search(r"CP\s*\d+", b.text) for b in detail_boxes)
        has_hp = any(re.search(r"\d+\s*/\s*\d+\s*HP", b.text) for b in detail_boxes)
        return has_cp or has_hp

    def close_detail(self) -> None:
        """Close Pokémon detail sheet and return to grid."""
        img, boxes = self.phone.read()
        is_grid = any("POKÉMON" in b.text and b.y < 120 for b in boxes)
        if not is_grid:
            self.phone.tap((0.5, 0.93), img, settle=1.2)

    def read_current_cp(self, boxes: Sequence[vision_ocr.TextBox]) -> int | None:
        for b in boxes:
            m = re.search(r"CP\s*(\d+)", b.text)
            if m and b.y < 200:
                return int(m.group(1))
        return None

    def power_up_to_level_40(
        self,
        target_cp: int | None = None,
        max_taps: int = 60,
    ) -> PowerUpResult:
        """Open power up dialog, increment to target L40 CP, confirm, and verify."""
        img, boxes = self.phone.read()
        initial_cp = self.read_current_cp(boxes)

        # Look for POWER UP button on detail page
        pwr_box = next((b for b in boxes if "POWER UP" in b.text.upper()), None)
        if not pwr_box:
            # Scroll slightly down
            self.phone.drag((0.5, 0.80), (0.5, 0.60), img, settle=1.0)
            img, boxes = self.phone.read()
            pwr_box = next((b for b in boxes if "POWER UP" in b.text.upper()), None)
            if not pwr_box:
                return PowerUpResult(False, initial_cp, note="No POWER UP button found")

        # Tap POWER UP
        self.phone.tap((pwr_box.cx / img.width, pwr_box.cy / img.height), img, settle=1.5)
        d_img, d_boxes = self.phone.read()

        # Check if dialog is open
        dialog_title = next((b for b in d_boxes if "POWER UP:" in b.text.upper()), None)
        if not dialog_title:
            return PowerUpResult(False, initial_cp, note="Power up dialog did not open")

        plus_box = next((b for b in d_boxes if b.text.strip() == "+"), None)
        if not plus_box:
            # Already at max level or cannot power up
            self._cancel_dialog(d_img, d_boxes)
            return PowerUpResult(False, initial_cp, note="No + button (may be maxed)")

        plus_pt = (plus_box.cx / d_img.width, plus_box.cy / d_img.height)

        # Tap + button in increments while checking for target CP and Candy XL
        taps_done = 0
        current_target_cp = initial_cp
        stardust_cost = 0
        candy_cost = 0

        while taps_done < max_taps:
            # Batch tap 4 times for speed
            batch = min(4, max_taps - taps_done)
            for _ in range(batch):
                self.phone.tap(plus_pt, d_img, settle=0.35)
                taps_done += 1

            time.sleep(0.4)
            d_img, d_boxes = self.phone.read()
            # Read target CP: e.g. "CP 547 →610"
            current_target_cp = preview_cp(d_boxes, current_target_cp)

            # A batch can carry the preview past the target or into Candy XL
            # levels.  Step back one level at a time, re-reading, until it no
            # longer does; the whole batch is the most that can be undone.
            if overshot(d_boxes, current_target_cp, target_cp, XL_BAND):
                minus_pt = (1.0 - plus_pt[0], plus_pt[1])
                for _ in range(batch):
                    self.phone.tap(minus_pt, d_img, settle=0.4)
                    time.sleep(0.4)
                    d_img, d_boxes = self.phone.read()
                    current_target_cp = preview_cp(d_boxes, current_target_cp)
                    if not overshot(d_boxes, current_target_cp, target_cp, XL_BAND):
                        break
                else:
                    self._cancel_dialog(d_img, d_boxes)
                    return PowerUpResult(
                        False, initial_cp, note="Could not step back below Candy XL / target CP"
                    )
                logger.info("Stopped below Candy XL / target at CP %s", current_target_cp)
                break

            if target_cp is not None and current_target_cp and current_target_cp >= target_cp:
                logger.info("Reached target L40 CP %s", current_target_cp)
                break

        # Read costs
        numbers = []
        for b in d_boxes:
            if 850 <= b.y <= 1020:
                t = b.text.replace(",", "").strip()
                if t.isdigit():
                    numbers.append(int(t))
        if len(numbers) >= 2:
            stardust_cost = max(numbers)
            candy_cost = min(numbers)

        # Confirm power up
        confirm_btn = next(
            (b for b in d_boxes if b.text.strip() == "POWER UP" and b.y > 1050), None
        )
        if not confirm_btn:
            self._cancel_dialog(d_img, d_boxes)
            return PowerUpResult(False, initial_cp, note="No confirm button on dialog")

        logger.info(
            "Confirming Power Up to %s (Dust: %s, Candy: %s)",
            current_target_cp,
            stardust_cost,
            candy_cost,
        )
        self.phone.tap((confirm_btn.cx / d_img.width, confirm_btn.cy / d_img.height), d_img, settle=1.5)

        # Secondary confirmation popup: "YES"
        pop_img, pop_boxes = self.phone.read()
        yes_btn = next((b for b in pop_boxes if b.text.strip().upper() == "YES"), None)
        if yes_btn:
            self.phone.tap((yes_btn.cx / pop_img.width, yes_btn.cy / pop_img.height), pop_img, settle=3.0)
        else:
            time.sleep(2.0)

        # Re-read detail screen for final CP: success is what the page shows
        final_img, final_boxes = self.phone.read()
        final_cp = self.read_current_cp(final_boxes)

        return PowerUpResult(
            success=powered_up(initial_cp, final_cp),
            initial_cp=initial_cp,
            final_cp=final_cp,
            stardust_cost=stardust_cost,
            candy_cost=candy_cost,
        )

    def _cancel_dialog(self, img: Image.Image, boxes: Sequence[vision_ocr.TextBox]) -> None:
        cancel_box = next((b for b in boxes if "CANCEL" in b.text.upper()), None)
        if cancel_box:
            self.phone.tap((cancel_box.cx / img.width, cancel_box.cy / img.height), img, settle=1.0)
        else:
            self.phone.tap((0.5, 0.91), img, settle=1.0)

    def evolve(self) -> EvolveResult:
        """Trigger evolution, confirm, wait through animation, and verify."""
        img, boxes = self.phone.read()
        old_cp = self.read_current_cp(boxes)

        # Scroll down slightly to ensure EVOLVE is visible
        evolve_box = next((b for b in boxes if b.text.strip().upper() == "EVOLVE"), None)
        if not evolve_box:
            self.phone.drag((0.5, 0.85), (0.5, 0.38), img, settle=1.2)
            img, boxes = self.phone.read()
            evolve_box = next((b for b in boxes if b.text.strip().upper() == "EVOLVE"), None)
            if not evolve_box:
                return EvolveResult(False, old_cp=old_cp, note="EVOLVE button not visible")

        logger.info("Tapping EVOLVE button")
        self.phone.tap((evolve_box.cx / img.width, evolve_box.cy / img.height), img, settle=1.5)

        # Confirmation popup: "YES" / "NO"
        pop_img, pop_boxes = self.phone.read()
        yes_btn = next((b for b in pop_boxes if b.text.strip().upper() == "YES"), None)
        if not yes_btn:
            # Look for item evolution prompt or tooltip
            use_item_btn = next((b for b in pop_boxes if "USE" in b.text.upper() or "STONE" in b.text.upper()), None)
            if use_item_btn:
                self.phone.tap((use_item_btn.cx / pop_img.width, use_item_btn.cy / pop_img.height), pop_img, settle=1.5)
                pop_img, pop_boxes = self.phone.read()
                yes_btn = next((b for b in pop_boxes if b.text.strip().upper() == "YES"), None)

        if not yes_btn:
            # Cancel popup if open
            no_btn = next((b for b in pop_boxes if b.text.strip().upper() == "NO"), None)
            if no_btn:
                self.phone.tap((no_btn.cx / pop_img.width, no_btn.cy / pop_img.height), pop_img, settle=1.0)
            return EvolveResult(False, old_cp=old_cp, note="Could not find YES confirmation button")

        # Confirm evolution
        logger.info("Confirming evolution (tapping YES at %s, %s)", yes_btn.cx, yes_btn.cy)
        self.phone.tap((yes_btn.cx / pop_img.width, yes_btn.cy / pop_img.height), pop_img, settle=1.0)

        # Evolution animation takes ~25-28 seconds
        logger.info("Waiting 28s for evolution animation...")
        time.sleep(28.0)

        # Tap through the new-Pokémon and Pokédex screens only while no detail
        # page shows. A blind tap low on the detail page lands on POWER UP.
        detail_img, detail_boxes = self.phone.read()
        for _ in range(8):
            if any(re.search(r"\d+\s*/\s*\d+\s*HP", b.text, re.I) for b in detail_boxes):
                break
            self.phone.tap((0.5, 0.22), detail_img, settle=1.8)
            detail_img, detail_boxes = self.phone.read()
        new_cp = self.read_current_cp(detail_boxes)
        return EvolveResult(success=new_cp is not None, old_cp=old_cp, new_cp=new_cp,
                            note="" if new_cp is not None else "No detail page after evolving")

    def tag_pokemon(self, tag_name: str = "Mega") -> bool:
        """Apply a tag (e.g. Mega) via the detail hamburger menu."""
        img, boxes = self.phone.read()

        # Check if already tagged: tag pill visible in upper region
        if any(b.text.strip().lower() == tag_name.lower() and b.y < img.height * 0.45 for b in boxes):
            logger.info("Pokémon already has tag '%s'", tag_name)
            return True

        # Tap hamburger menu at bottom right
        self.phone.tap((0.88, 0.93), img, settle=1.5)
        menu_img, menu_boxes = self.phone.read()

        tag_box = next((b for b in menu_boxes if b.text.strip() == "TAG"), None)
        if not tag_box:
            self.phone.tap((0.5, 0.5), menu_img, settle=1.0)
            return False

        self.phone.tap((tag_box.cx / menu_img.width, tag_box.cy / menu_img.height), menu_img, settle=1.5)
        t_img, t_boxes = self.phone.read()

        target_tag_box = next((b for b in t_boxes if tag_name.lower() in b.text.lower()), None)
        if not target_tag_box:
            # Cancel tag dialog
            self.phone.tap((0.5, 0.93), t_img, settle=1.0)
            return False

        # Tap target tag
        self.phone.tap((target_tag_box.cx / t_img.width, target_tag_box.cy / t_img.height), t_img, settle=1.0)
        # Tap DONE
        d_img, d_boxes = self.phone.read()
        done_btn = next((b for b in d_boxes if b.text.strip().upper() == "DONE"), None)
        if done_btn:
            self.phone.tap((done_btn.cx / d_img.width, done_btn.cy / d_img.height), d_img, settle=1.2)
        else:
            self.phone.tap((0.5, 0.82), d_img, settle=1.2)
        return True

    def mega_evolve(self) -> bool:
        """Trigger Mega Evolution, confirm, wait through animation, and dismiss."""
        img, boxes = self.phone.read()
        # Scroll down to bring MEGA EVOLVE into view
        self.phone.drag((0.5, 0.85), (0.5, 0.50), img, settle=1.2)
        img, boxes = self.phone.read()

        mega_box = next(
            (b for b in boxes if "MEGA EVOLVE" in b.text.upper() or ("MEGA" in b.text.upper() and b.y > 350)),
            None,
        )
        if not mega_box:
            logger.warning("No MEGA EVOLVE button found on detail screen")
            return False

        self.phone.tap((mega_box.cx / img.width, mega_box.cy / img.height), img, settle=1.5)
        pop_img, pop_boxes = self.phone.read()
        yes_btn = next((b for b in pop_boxes if b.text.strip().upper() == "YES"), None)
        if not yes_btn:
            no_btn = next((b for b in pop_boxes if b.text.strip().upper() == "NO"), None)
            if no_btn:
                self.phone.tap((no_btn.cx / pop_img.width, no_btn.cy / pop_img.height), pop_img, settle=1.0)
            return False

        self.phone.tap((yes_btn.cx / pop_img.width, yes_btn.cy / pop_img.height), pop_img, settle=1.0)
        logger.info("Mega evolving... waiting 16s")
        time.sleep(16.0)
        post_img = self.phone.screenshot()
        self.phone.tap((0.5, 0.85), post_img, settle=2.0)
        return True
