#!/usr/bin/env python3
"""Apple Vision OCR with bounding boxes, for Android and iOS screenshots alike.

`sources/inventory_scanner_ios.py` already proved Vision reads Pokémon GO
screenshots, but its helper printed bare text lines with no coordinates, which is
not enough for this tree's problem: the storage detail page shows a regular
candy count and an XL candy count side by side, and telling them apart needs to
know *where* each number sat on the screen.

So this module compiles a Vision helper that returns, per recognised line, the
text and its bounding box in pixel coordinates with the origin at the top left.
Callers then anchor on a label ("... CANDY XL") and read the number sitting
directly above it, which is how the game draws those rows.

The helper is compiled once into the private state directory.  Nothing is
written into the checkout: the Swift source and the built binary are both
runtime artifacts, not source.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image

from . import config_paths

_SWIFT_SOURCE = r"""
import Foundation
import Vision
import AppKit

struct Box: Codable {
    let text: String
    let x: Double
    let y: Double
    let w: Double
    let h: Double
}

guard CommandLine.arguments.count > 2 else { exit(1) }
let path = CommandLine.arguments[1]
guard let pixelWidth = Double(CommandLine.arguments[2]),
      let pixelHeight = Double(CommandLine.arguments[3]) else { exit(1) }

guard let image = NSImage(contentsOfFile: path),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { exit(1) }

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
let request = VNRecognizeTextRequest()
request.recognitionLevel = VNRequestTextRecognitionLevel.accurate
// Game text is a fixed, stylised font: language correction only ever "fixes"
// names into something else (Zamazenta, Pheromosa) without helping digits.
request.usesLanguageCorrection = false
request.recognitionLanguages = ["en-US"]

try? handler.perform([request])

guard let results = request.results as? [VNRecognizedTextObservation] else {
    print("[]"); exit(0)
}
var out: [Box] = []
for obs in results {
    guard let candidate = obs.topCandidates(1).first else { continue }
    let rect = obs.boundingBox
    // Swift 6 exposes the CGRect edges as optionals; fall back to 0 rather than
    // dropping a line whose edges somehow failed to resolve.
    let x0: Double = rect.minX ?? 0
    let y0: Double = rect.minY ?? 0
    let x1: Double = rect.maxX ?? 0
    let y1: Double = rect.maxY ?? 0
    // Vision's origin is bottom-left; flip to top-left pixels so callers can
    // reason in the same space as the screenshot they took.
    out.append(Box(
        text: candidate.string,
        x: x0 * pixelWidth,
        y: (1.0 - y1) * pixelHeight,
        w: (x1 - x0) * pixelWidth,
        h: (y1 - y0) * pixelHeight
    ))
}
let data = (try? JSONEncoder().encode(out)) ?? Data("[]".utf8)
print(String(data: data, encoding: .utf8) ?? "[]")
"""


class VisionOcrError(RuntimeError):
    """The Vision helper could not be built or did not answer."""


@dataclass(frozen=True)
class TextBox:
    """One recognised line, in pixels, origin at the top left of the image."""

    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def contains_horizontal(self, other: "TextBox", slack: float = 0.0) -> bool:
        """True when `other` sits inside this box's horizontal span."""
        return (
            other.cx >= self.x - slack
            and other.cx <= self.x + self.w + slack
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TextBox({self.text!r}, {self.x:.0f},{self.y:.0f},{self.w:.0f}x{self.h:.0f})"


def helper_path() -> Path:
    """Location of the compiled helper, inside the private state directory."""
    return config_paths.state_dir() / "vision_ocr"


def ensure_helper(force: bool = False) -> Path:
    """Compile the Swift helper once and return its path.

    Compilation is keyed on the source text so an edit rebuilds automatically
    and an unchanged source costs nothing after the first run.
    """
    target = helper_path()
    stamp = target.with_suffix(".stamp")
    # Not hash(): that is salted per process, so the stamp would never match
    # and every scan would pay a rebuild.
    digest = hashlib.sha256(_SWIFT_SOURCE.encode()).hexdigest()
    if not force and target.is_file() and stamp.is_file():
        try:
            if stamp.read_text().strip() == digest:
                return target
        except OSError:
            pass

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Built inside the destination directory: /tmp is usually a different
    # filesystem, and os.replace cannot cross one.
    with tempfile.TemporaryDirectory(dir=str(target.parent)) as workspace:
        source = Path(workspace) / "vision_ocr.swift"
        source.write_text(_SWIFT_SOURCE)
        built = Path(workspace) / "vision_ocr"
        result = subprocess.run(
            ["swiftc", "-O", str(source), "-o", str(built)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode or not built.is_file():
            raise VisionOcrError(
                "swiftc could not build the Vision OCR helper: "
                + (result.stderr.strip() or "unknown error")
            )
        try:
            os.replace(built, target)
            target.chmod(0o700)
        except OSError:
            # A stale binary held by a concurrent scan is fine; the helper is
            # read-only and identical.
            if not target.is_file():
                raise
    try:
        stamp.write_text(digest)
        stamp.chmod(0o600)
    except OSError:
        pass
    return target


def ocr(image: Image.Image, *, timeout: float = 60.0) -> list[TextBox]:
    """Recognise every line in `image` and return boxes in its pixel space."""
    helper = ensure_helper()
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    with tempfile.TemporaryDirectory() as workspace:
        shot = Path(workspace) / "frame.png"
        rgb.save(shot)
        result = subprocess.run(
            [str(helper), str(shot), str(rgb.width), str(rgb.height)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    if result.returncode:
        raise VisionOcrError(
            f"Vision OCR helper failed: {result.stderr.strip() or result.returncode}"
        )
    payload = result.stdout.strip() or "[]"
    try:
        rows = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise VisionOcrError(f"Vision OCR helper returned unreadable JSON: {payload[:200]}") from exc
    return [
        TextBox(text=str(row["text"]), x=float(row["x"]), y=float(row["y"]),
                w=float(row["w"]), h=float(row["h"]))
        for row in rows
    ]


def crop(image: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    """Crop to `(left, top, right, bottom)` in pixels, clamped to the image."""
    left, top, right, bottom = box
    width, height = image.size
    return image.crop(
        (
            max(0, int(left)),
            max(0, int(top)),
            min(width, int(right)),
            min(height, int(bottom)),
        )
    )


def number_in(text: str) -> int | None:
    """First integer in `text`, or None. Tolerates the spaces Vision inserts
    in wide game type ("1 6,334,721") and stray dots."""
    cleaned = text.replace(",", "").replace(" ", "").replace(".", "")
    digits = ""
    for char in cleaned:
        if char.isdigit():
            digits += char
        elif digits:
            break
    return int(digits) if digits else None


def find_number_above(
    boxes: Sequence[TextBox],
    label: TextBox,
    *,
    max_gap: float | None = None,
) -> int | None:
    """The number drawn directly above a stat label.

    The storage detail page stacks value over label, so "ZACIAN CANDY XL 72" is
    two boxes: `72` above `ZACIAN CANDY XL`.  Anchoring on the label and reading
    upward is what keeps the regular candy count from being confused with the XL
    one; both rows are adjacent and a scan of the whole image cannot tell them
    apart by value alone.
    """
    gap = max_gap if max_gap is not None else max(label.h * 3.0, 24.0)
    best: TextBox | None = None
    for box in boxes:
        if box is label or number_in(box.text) is None:
            continue
        if not label.contains_horizontal(box, slack=label.w * 0.5):
            continue
        distance = label.y - box.bottom
        if distance < -label.h or distance > gap:
            continue
        if best is None or box.bottom > best.bottom:
            best = box
    return number_in(best.text) if best else None


def find_number_right_of(
    boxes: Sequence[TextBox],
    label: TextBox,
    *,
    max_gap: float | None = None,
) -> int | None:
    """The number drawn to the right of an inline label, on the same line.

    The detail page's header is `CP 5613`: the caption sits left of the value.
    """
    gap = max_gap if max_gap is not None else max(label.w, 24.0)
    best: TextBox | None = None
    for box in boxes:
        if box is label or number_in(box.text) is None:
            continue
        if abs(box.cy - label.cy) > label.h:
            continue
        distance = box.x - (label.x + label.w)
        if distance < -label.w or distance > gap:
            continue
        if best is None or box.x < best.x:
            best = box
    return number_in(best.text) if best else None


def find_label(
    boxes: Sequence[TextBox],
    pattern: str,
    *,
    exact: bool = False,
) -> TextBox | None:
    """First box matching `pattern`, lowercased."""
    needle = pattern.lower()
    for box in boxes:
        text = box.text.lower()
        if (text == needle) if exact else (needle in text):
            return box
    return None


def find_labels(boxes: Sequence[TextBox], pattern: str) -> list[TextBox]:
    """Every box matching `pattern`, lowercased, in reading order."""
    needle = pattern.lower()
    matches = [box for box in boxes if needle in box.text.lower()]
    matches.sort(key=lambda box: (box.y, box.x))
    return matches


def join_digits(boxes: Iterable[TextBox]) -> int | None:
    """Sum-independent helper: concatenate digits found left to right."""
    digits = "".join(str(number_in(box.text) or "") for box in boxes)
    return int(digits) if digits else None