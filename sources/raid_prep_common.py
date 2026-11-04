"""Results and screen reads shared by the Android and iOS raid-prep workers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from sources import vision_ocr


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


def preview_cp(boxes: Sequence[vision_ocr.TextBox], fallback: int | None) -> int | None:
    """The CP the power-up dialog previews ("CP 547 → 610"), else `fallback`."""
    for b in boxes:
        m = re.search(r"→\s*(\d+)", b.text)
        if m:
            fallback = int(m.group(1))
    return fallback


def dialog_needs_xl(boxes: Sequence[vision_ocr.TextBox], top: float, bottom: float) -> bool:
    """True when the power-up cost rows between `top` and `bottom` ask for Candy XL."""
    return any(
        re.search(r"\bXL\b", b.text) or "CANDY XL" in b.text.upper()
        for b in boxes
        if top <= b.y <= bottom
    )


def overshot(
    boxes: Sequence[vision_ocr.TextBox],
    current_cp: int | None,
    target_cp: int | None,
    xl_band: tuple[float, float],
) -> bool:
    """The power-up preview needs Candy XL, or has passed the target CP."""
    return dialog_needs_xl(boxes, *xl_band) or (
        target_cp is not None and current_cp is not None and current_cp > target_cp
    )


def powered_up(initial_cp: int | None, final_cp: int | None) -> bool:
    """A power-up only counts once the detail page shows a CP above the start."""
    return final_cp is not None and (initial_cp is None or final_cp > initial_cp)
