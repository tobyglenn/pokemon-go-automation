"""Locate nearby Meltan on Pokemon GO Android overworld screenshots.

Measurements come from 1316x2560 frames. Detection runs at at most 329 pixels
wide; spatial thresholds are fractions of image width, not fixed pixels.

Meltan's mustard nut is darker and smaller than Pikachu's yellow body. A
nearby grey body provides positive evidence; a large red Santa hat provides
negative evidence. White feet-ring pixels deliberately do not count as body.

The map gate checks the small menu ball's red/white signature and separately
vetoes the wider signature of an encounter ball. It is a visual heuristic,
not a general scene classifier.
"""

from __future__ import annotations

import argparse
import colorsys
import math

from PIL import Image


# Quarter-width analysis preserves roughly 4-8 pixels across a measured nut.
ANALYSIS_WIDTH = 329
HSV_BYTE_MAX = 255.0
HUE_DEGREES = 360.0

# The map menu button is about 0.11w across, centred near (0.5w, 0.93h).
# Sampling inside its edge avoids outlines, highlights, and antialiasing.
MENU_X_FRACTION = 0.50
MENU_Y_FRACTION = 0.93
MENU_SAMPLE_RADIUS_FRACTION = 0.04
BALL_SAMPLE_STEPS = 8
BALL_SEAM_EXCLUSION = 0.15
MENU_RED_MIN_FRACTION = 0.65
MENU_WHITE_MIN_FRACTION = 0.65

# Map-button red is saturated; its lower half is nearly neutral and bright.
RED_HUE_LOW_MAX = 15.0
RED_HUE_HIGH_MIN = 340.0
MENU_RED_SAT_MIN = 0.50
MENU_RED_VALUE_MIN = 0.35
WHITE_SAT_MAX = 0.15
WHITE_VALUE_MIN = 0.85

# Encounter balls are roughly 0.25w across. Probe an annulus outside the
# small menu button, at several plausible lower-screen ball positions.
ENCOUNTER_Y_FRACTIONS = (0.84, 0.89, 0.93)
ENCOUNTER_SAMPLE_RADIUS_FRACTION = 0.10
ENCOUNTER_INNER_RADIUS_RATIO = 0.60
ENCOUNTER_COLOUR_MIN_FRACTION = 0.50
ENCOUNTER_WHITE_MIN_FRACTION = 0.55
ENCOUNTER_BLUE_HUE_MIN = 190.0
ENCOUNTER_BLUE_HUE_MAX = 255.0
ENCOUNTER_BLUE_SAT_MIN = 0.45
ENCOUNTER_BLUE_VALUE_MIN = 0.35

# Player position and HUD exclusions measured in the reference portrait.
PLAYER_X_FRACTION = 0.50
PLAYER_Y_FRACTION = 0.621
SEARCH_RADIUS_FRACTION = 0.35
HUD_RIGHT_FRACTION = 0.85
HUD_TOP_FRACTION = 0.15
HUD_BOTTOM_FRACTION = 0.88

# Nut colours exclude peach road edges and most bright lemon-yellow pixels.
GOLD_HUE_MIN = 36.0
GOLD_HUE_MAX = 48.0
GOLD_SAT_MIN = 0.45
GOLD_SAT_MAX = 0.80
GOLD_VALUE_MIN = 0.50
GOLD_VALUE_MAX = 0.95

# Expected gold area is about 1040 pixels at reference width. Broad bounds
# allow occlusion and resampling. Narrow nuts can lose an edge at quarter size.
NUT_EXPECTED_AREA_FRACTION = 0.00060
NUT_AREA_MIN_MULTIPLIER = 0.25
NUT_AREA_MAX_MULTIPLIER = 3.0
NUT_WIDTH_MIN_FRACTION = 0.009
NUT_WIDTH_MAX_FRACTION = 0.050
NUT_HEIGHT_MIN_FRACTION = 0.012
NUT_HEIGHT_MAX_FRACTION = 0.055
NUT_ASPECT_MIN = 0.30
NUT_ASPECT_MAX = 2.50
NUT_FILL_MIN = 0.35

# A Santa hat is comparable in area to the yellow body. Meltan's small red
# streak is only a few percent of its nut area and remains below this limit.
HAT_SIDE_MARGIN_FRACTION = 0.015
HAT_ABOVE_MARGIN_FRACTION = 0.040
HAT_BELOW_MARGIN_FRACTION = 0.006
HAT_RED_SAT_MIN = 0.55
HAT_RED_VALUE_MIN = 0.35
HAT_RED_AREA_RATIO_MAX = 0.45

# The exposed body is only 3-4 analysis rows deep, approximately the nut's
# width. Sample exactly left..right, with no side margin, for ceil(0.012w)
# rows (4 at width 329). The old wider/deeper box diluted m8/m9's support.
BODY_DEPTH_FRACTION = 0.012
# Measured body pixels have S < 0.18 and V roughly 0.45-0.82. These bounds
# allow modest resampling variation while excluding road S=0.25-0.34 and
# the m2 avatar's white clothing V>0.9, rather than accepting its hidden nut.
BODY_SAT_MAX = 0.18
BODY_VALUE_MIN = 0.40
BODY_VALUE_MAX = 0.85
# Tight-box grey fraction measured on the sample-receiver frames (9 Nov 2026):
# Meltans 0.83 (m7), 0.75 (now), 0.58 (m8), 0.33 (m9 -- its body peeks out
# only two rows deep at the nut's lower left).  Negatives: Pikachu bodies
# 0.00-0.23 (the 0.23 also carries a Santa hat the red check vetoes), tail
# fragments <= 0.06, and m2's nut half hidden behind the avatar 0.11.  That
# nut has to fail: its body is behind the trainer, and a tap there lands on
# him.  0.27 sits between the best negative and the weakest Meltan.
BODY_GREY_MIN_FRACTION = 0.27

# Prefer nearby candidates, with modest body support and a brightness penalty.
NUT_BRIGHT_VALUE = 0.88
DISTANCE_SCORE_WEIGHT = 1.0
BODY_SCORE_WEIGHT = 0.25
BRIGHTNESS_SCORE_WEIGHT = 2.0
# Tap the lower half of the nut, not the body: the nut is the biggest solid
# target Meltan offers, and the body is only ~15px deep at 1316 wide.  At
# 0.6 the tap fell on the body's lower edge (m9: 30px under the nut).
TAP_BELOW_CENTROID_HEIGHT_FRACTION = 0.30

NEIGHBOURS = (
    (-1, -1), (0, -1), (1, -1),
    (-1, 0),             (1, 0),
    (-1, 1),  (0, 1),   (1, 1),
)


def _red(hue: float, saturation: float, value: float,
         saturation_min: float, value_min: float) -> bool:
    return (
        (hue < RED_HUE_LOW_MAX or hue > RED_HUE_HIGH_MIN)
        and saturation > saturation_min
        and value > value_min
    )


def _normalised_hsv(pixel: tuple[int, int, int]) -> tuple[float, float, float]:
    """Translate PIL's byte-valued HSV into degrees and unit intervals."""
    hue, saturation, value = pixel
    return (
        hue * HUE_DEGREES / HSV_BYTE_MAX,
        saturation / HSV_BYTE_MAX,
        value / HSV_BYTE_MAX,
    )


def _ball_fractions(
    rgb: Image.Image,
    centre_y_fraction: float,
    radius_fraction: float,
    inner_radius_ratio: float = 0.0,
) -> tuple[float, float, float] | None:
    """Return upper-red, upper-blue and lower-white sampled fractions."""
    width, height = rgb.size
    centre_x = MENU_X_FRACTION * width
    centre_y = centre_y_fraction * height
    radius = radius_fraction * width
    pixels = rgb.load()
    top_count = bottom_count = red_count = blue_count = white_count = 0

    for iy in range(-BALL_SAMPLE_STEPS, BALL_SAMPLE_STEPS + 1):
        dy = iy / BALL_SAMPLE_STEPS
        if abs(dy) < BALL_SEAM_EXCLUSION:
            continue
        for ix in range(-BALL_SAMPLE_STEPS, BALL_SAMPLE_STEPS + 1):
            dx = ix / BALL_SAMPLE_STEPS
            distance_squared = dx * dx + dy * dy
            if not inner_radius_ratio**2 <= distance_squared <= 1.0:
                continue

            x = round(centre_x + dx * radius)
            y = round(centre_y + dy * radius)
            if not (0 <= x < width and 0 <= y < height):
                return None

            red, green, blue = pixels[x, y]
            hue, saturation, value = colorsys.rgb_to_hsv(
                red / HSV_BYTE_MAX,
                green / HSV_BYTE_MAX,
                blue / HSV_BYTE_MAX,
            )
            hue *= HUE_DEGREES
            if dy < 0:
                top_count += 1
                red_count += _red(
                    hue, saturation, value,
                    MENU_RED_SAT_MIN, MENU_RED_VALUE_MIN,
                )
                blue_count += (
                    ENCOUNTER_BLUE_HUE_MIN <= hue <= ENCOUNTER_BLUE_HUE_MAX
                    and saturation > ENCOUNTER_BLUE_SAT_MIN
                    and value > ENCOUNTER_BLUE_VALUE_MIN
                )
            else:
                bottom_count += 1
                white_count += (
                    saturation < WHITE_SAT_MAX and value > WHITE_VALUE_MIN
                )

    if not top_count or not bottom_count:
        return None
    return (
        red_count / top_count,
        blue_count / top_count,
        white_count / bottom_count,
    )


def on_map(image: Image.Image) -> bool:
    """Cheaply identify the small map-menu ball, rejecting encounter balls."""
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    signature = _ball_fractions(
        rgb, MENU_Y_FRACTION, MENU_SAMPLE_RADIUS_FRACTION,
    )
    if signature is None:
        return False
    red, _, white = signature
    if red < MENU_RED_MIN_FRACTION or white < MENU_WHITE_MIN_FRACTION:
        return False

    for centre_y in ENCOUNTER_Y_FRACTIONS:
        signature = _ball_fractions(
            rgb,
            centre_y,
            ENCOUNTER_SAMPLE_RADIUS_FRACTION,
            ENCOUNTER_INNER_RADIUS_RATIO,
        )
        if signature is None:
            continue
        red, blue, white = signature
        if (
            max(red, blue) >= ENCOUNTER_COLOUR_MIN_FRACTION
            and white >= ENCOUNTER_WHITE_MIN_FRACTION
        ):
            return False
    return True


def _gold_mask(hsv: Image.Image) -> set[tuple[int, int]]:
    width, height = hsv.size
    player_x = PLAYER_X_FRACTION * width
    player_y = PLAYER_Y_FRACTION * height
    radius = SEARCH_RADIUS_FRACTION * width
    radius_squared = radius * radius
    pixels = hsv.load()
    mask: set[tuple[int, int]] = set()

    left = max(0, math.ceil(player_x - radius))
    right = min(
        width - 1,
        math.floor(player_x + radius),
        math.floor(HUD_RIGHT_FRACTION * width),
    )
    top = max(
        0, math.ceil(player_y - radius), math.ceil(HUD_TOP_FRACTION * height),
    )
    bottom = min(
        height - 1,
        math.floor(player_y + radius),
        math.floor(HUD_BOTTOM_FRACTION * height),
    )

    for y in range(top, bottom + 1):
        for x in range(left, right + 1):
            if (x - player_x)**2 + (y - player_y)**2 > radius_squared:
                continue
            hue, saturation, value = _normalised_hsv(pixels[x, y])
            if (
                GOLD_HUE_MIN <= hue <= GOLD_HUE_MAX
                and GOLD_SAT_MIN <= saturation <= GOLD_SAT_MAX
                and GOLD_VALUE_MIN <= value <= GOLD_VALUE_MAX
            ):
                mask.add((x, y))
    return mask


def _components(mask: set[tuple[int, int]]):
    """Yield eight-connected components, consuming the mask without recursion."""
    while mask:
        start = min(mask, key=lambda point: (point[1], point[0]))
        mask.remove(start)
        stack = [start]
        component = []
        while stack:
            x, y = stack.pop()
            component.append((x, y))
            for dx, dy in NEIGHBOURS:
                neighbour = (x + dx, y + dy)
                if neighbour in mask:
                    mask.remove(neighbour)
                    stack.append(neighbour)
        yield component


def _score_component(
    hsv: Image.Image,
    component: list[tuple[int, int]],
) -> tuple[float, tuple[float, float]] | None:
    width, height = hsv.size
    area = len(component)
    expected_area = NUT_EXPECTED_AREA_FRACTION * width * width
    if not (
        expected_area * NUT_AREA_MIN_MULTIPLIER
        <= area
        <= expected_area * NUT_AREA_MAX_MULTIPLIER
    ):
        return None

    left = min(x for x, _ in component)
    right = max(x for x, _ in component)
    top = min(y for _, y in component)
    bottom = max(y for _, y in component)
    box_width = right - left + 1
    box_height = bottom - top + 1
    if not (
        NUT_WIDTH_MIN_FRACTION * width
        <= box_width <= NUT_WIDTH_MAX_FRACTION * width
        and NUT_HEIGHT_MIN_FRACTION * width
        <= box_height <= NUT_HEIGHT_MAX_FRACTION * width
        and NUT_ASPECT_MIN <= box_width / box_height <= NUT_ASPECT_MAX
        and area / (box_width * box_height) >= NUT_FILL_MIN
    ):
        return None

    pixels = hsv.load()
    hat_left = max(0, math.floor(left - HAT_SIDE_MARGIN_FRACTION * width))
    hat_right = min(
        width - 1, math.ceil(right + HAT_SIDE_MARGIN_FRACTION * width),
    )
    hat_top = max(0, math.floor(top - HAT_ABOVE_MARGIN_FRACTION * width))
    hat_bottom = min(
        height - 1, math.ceil(bottom + HAT_BELOW_MARGIN_FRACTION * width),
    )
    red_count = 0
    for y in range(hat_top, hat_bottom + 1):
        for x in range(hat_left, hat_right + 1):
            hue, saturation, value = _normalised_hsv(pixels[x, y])
            red_count += _red(
                hue, saturation, value, HAT_RED_SAT_MIN, HAT_RED_VALUE_MIN,
            )
    if red_count / area > HAT_RED_AREA_RATIO_MAX:
        return None

    # Inclusive rows bottom+1 .. bottom+ceil(0.012w), exactly nut width.
    body_left = left
    body_right = right
    body_top = bottom + 1
    body_bottom = min(
        height - 1, bottom + math.ceil(BODY_DEPTH_FRACTION * width),
    )
    body_area = (body_right - body_left + 1) * (body_bottom - body_top + 1)
    if body_area <= 0:
        return None

    grey_count = 0
    for y in range(body_top, body_bottom + 1):
        for x in range(body_left, body_right + 1):
            _, saturation, value = _normalised_hsv(pixels[x, y])
            grey_count += (
                saturation < BODY_SAT_MAX
                and BODY_VALUE_MIN < value < BODY_VALUE_MAX
            )
    body_fraction = grey_count / body_area
    if body_fraction < BODY_GREY_MIN_FRACTION:
        return None

    centre_x = sum(x for x, _ in component) / area
    centre_y = sum(y for _, y in component) / area
    mean_value = sum(pixels[x, y][2] for x, y in component) / (
        area * HSV_BYTE_MAX
    )
    distance = math.hypot(
        centre_x - PLAYER_X_FRACTION * width,
        centre_y - PLAYER_Y_FRACTION * height,
    ) / (SEARCH_RADIUS_FRACTION * width)
    brightness_penalty = max(
        0.0, (mean_value - NUT_BRIGHT_VALUE) / (1.0 - NUT_BRIGHT_VALUE),
    )
    score = (
        DISTANCE_SCORE_WEIGHT * distance
        + BRIGHTNESS_SCORE_WEIGHT * brightness_penalty
        - BODY_SCORE_WEIGHT * body_fraction
    )
    tap = (
        centre_x,
        centre_y + TAP_BELOW_CENTROID_HEIGHT_FRACTION * box_height,
    )
    return score, tap


def find_meltan(image: Image.Image) -> tuple[int, int] | None:
    """Return a full-resolution Meltan tap point, or None on failure/non-map."""
    if not on_map(image):
        return None

    analysis_width = min(ANALYSIS_WIDTH, image.width)
    analysis_height = max(
        1, round(image.height * analysis_width / image.width),
    )
    rgb = image.convert("RGB")
    if rgb.size != (analysis_width, analysis_height):
        rgb = rgb.resize(
            (analysis_width, analysis_height), Image.Resampling.BOX,
        )
    hsv = rgb.convert("HSV")

    best: tuple[float, tuple[float, float]] | None = None
    for component in _components(_gold_mask(hsv)):
        candidate = _score_component(hsv, component)
        if candidate is not None and (best is None or candidate[0] < best[0]):
            best = candidate

    if best is None:
        return None

    x, y = best[1]
    # Map pixel centres back, including independently rounded analysis height.
    full_x = round((x + 0.5) * image.width / analysis_width - 0.5)
    full_y = round((y + 0.5) * image.height / analysis_height - 0.5)
    return (
        min(image.width - 1, max(0, full_x)),
        min(image.height - 1, max(0, full_y)),
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Android screenshot paths")
    args = parser.parse_args()
    for path in args.paths:
        with Image.open(path) as image:
            print(
                f"{path}: on_map={on_map(image)} "
                f"meltan={find_meltan(image)}"
            )


if __name__ == "__main__":
    _main()
