"""Which way the friends list is sorted, read off the sort button's badge.

Shared by gift_android and gift_ios.
"""

from __future__ import annotations


# The sort button's small badge shows the list's direction: down puts the
# friends who can receive a gift on top.  The game remembers the direction now,
# so tapping CAN RECEIVE A GIFT twice just flipped it back and the run opened
# only friends who could not receive one (sample-receiver, 5 Nov: "cannot receive" on
# five friends in a row with 627 friends and 32 gifts).  Measured on sample-receiver
# (1316 wide): badge 0.07w right of SORT_BTN, arrow inside 0.017w of it,
# arrow pixels #b1ffb0.
SORT_ARROW_SEARCH_X = (0.04, 0.13)   # right of SORT_BTN, as a share of width
SORT_ARROW_SEARCH_Y = 0.035          # above and below SORT_BTN
SORT_ARROW_HALF_WIDTH = 0.014        # the arrow either side of its shaft
SORT_ARROW_MIN_PIXELS = 60           # at 1316 wide; scaled by area


def _arrow_pixel(red: int, green: int, blue: int) -> bool:
    return green >= 235 and red >= 160 and 160 <= blue <= 200


def sort_arrow(frame: tuple[int, int, int, bytes], sort_point: list[int]) -> str | None:
    """'down' (giftable friends on top), 'up', or None when not readable.

    The badge sits 0.070w right of SORT_BTN on sample-receiver and 0.079w on the SE
    (5 Nov), so it is found, not assumed: the column with the most arrow
    pixels is the shaft, and the head is the wide part either side of it.
    The button's own ring is the same green but lies outside that band.
    """
    width, height, offset, data = frame
    x0 = max(0, int(sort_point[0] + SORT_ARROW_SEARCH_X[0] * width))
    x1 = min(width, int(sort_point[0] + SORT_ARROW_SEARCH_X[1] * width))
    y0 = max(0, int(sort_point[1] - SORT_ARROW_SEARCH_Y * width))
    y1 = min(height, int(sort_point[1] + SORT_ARROW_SEARCH_Y * width))
    points = []
    for y in range(y0, y1):
        row = offset + y * width * 4
        for x in range(x0, x1):
            index = row + x * 4
            if _arrow_pixel(data[index], data[index + 1], data[index + 2]):
                points.append((x, y))
    if not points:
        return None
    columns: dict[int, int] = {}
    for x, _ in points:
        columns[x] = columns.get(x, 0) + 1
    shaft = max(columns, key=lambda x: (columns[x], -x))
    half = max(3, int(SORT_ARROW_HALF_WIDTH * width))
    # The arrow's height is the shaft's own; the ring can curve into the band
    # above it (sample-receiver), so pixels beyond the shaft's ends are not the arrow.
    # The badge's ring crosses the shaft's column too: the arrow is the
    # longest unbroken run down that column.
    shaft_ys = sorted({y for x, y in points if x == shaft})
    top = bottom = run_top = shaft_ys[0]
    for previous, y in zip(shaft_ys, shaft_ys[1:]):
        if y - previous > 1:
            run_top = y
        if y - run_top > bottom - top:
            top, bottom = run_top, y
    arrow = [(x, y) for x, y in points if abs(x - shaft) <= half and top <= y <= bottom]
    if len(arrow) < SORT_ARROW_MIN_PIXELS * (width / 1316) ** 2:
        return None
    mid_y = (top + bottom) / 2
    head = [y for x, y in arrow if abs(x - shaft) > half * 0.3]
    upper = sum(1 for y in head if y < mid_y)
    lower = len(head) - upper
    if max(upper, lower) < 2 * max(1, min(upper, lower)):
        return None
    return 'down' if lower > upper else 'up'


def sort_arrow_image(image, sort_point: list[int]) -> str | None:
    """sort_arrow on a PIL image; sort_point in the image's own pixels."""
    rgba = image.convert("RGBA")
    return sort_arrow((rgba.width, rgba.height, 0, rgba.tobytes()), sort_point)
