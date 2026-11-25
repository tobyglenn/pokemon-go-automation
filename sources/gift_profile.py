"""Find the SEND GIFT button on a friend's profile.

Shared by gift_android and gift_ios.

The profile layout moved in a game update (6 Nov): REMOTE TRADE joined the
action row with a "Pokemon for remote trade" strip under it, and the row now
sits at a height that depends on the friend. Measured on sample-receiver (1316x2560) the
gift icon was 220px above its configured SEND_GIFT_BTN, on sample-android-a (720x1600)
90px above, so the fixed point read grey on every friend and the run reported
"Friend cannot receive a gift" on all of them while nothing was sent.

The lit icon is the only magenta on the profile (its ribbon), and a friend who
cannot receive shows it greyed out, so finding the ribbon answers both
questions: can this friend receive, and where to tap.
"""

from __future__ import annotations

# Where the icon can be, as shares of the picture. The button is the leftmost
# of two to four in the action row; trainer and buddy art end above 0.5h.
SEARCH_X = (0.02, 0.38)
SEARCH_Y = (0.48, 0.96)
# Ribbon pixels at full resolution, as a share of the picture's area: sample-receiver
# showed ~3600 (0.0011), sample-android-a ~1800 (0.0016).
MIN_SHARE = 0.0003
STEP = 2  # every other pixel each way is plenty for an icon this size
WINDOW = 0.07  # the icon's height, as a share of the picture's height


def _ribbon(red: int, green: int, blue: int) -> bool:
    # The profile's red side border is red with little blue; the ribbon is
    # magenta: red and blue both high, green low.
    return red >= 170 and blue >= 140 and green <= 90


def find_send_gift(frame: tuple[int, int, int, bytes]) -> tuple[int, int] | None:
    """Centre of the lit gift icon in frame pixels, or None when there is none.

    `frame` is (width, height, byte offset, RGBA bytes).
    """
    width, height, offset, data = frame
    x0, x1 = int(SEARCH_X[0] * width), int(SEARCH_X[1] * width)
    y0, y1 = int(SEARCH_Y[0] * height), int(SEARCH_Y[1] * height)
    points = []
    for y in range(y0, y1, STEP):
        row = offset + y * width * 4
        for x in range(x0, x1, STEP):
            index = row + x * 4
            if _ribbon(data[index], data[index + 1], data[index + 2]):
                points.append((x, y))
    if len(points) * STEP * STEP < MIN_SHARE * width * height:
        return None
    # The densest band one icon tall, so a stray pink pixel elsewhere in the
    # search box does not drag the tap point off the button.
    span = int(WINDOW * height)
    ys = sorted(y for _, y in points)
    best_start, best_count, start = ys[0], 0, 0
    for end, y in enumerate(ys):
        while y - ys[start] > span:
            start += 1
        if end - start + 1 > best_count:
            best_count, best_start = end - start + 1, ys[start]
    if best_count * STEP * STEP < MIN_SHARE * width * height:
        return None
    chosen = [(x, y) for x, y in points if best_start <= y <= best_start + span]
    return (
        sum(x for x, _ in chosen) // len(chosen),
        sum(y for _, y in chosen) // len(chosen),
    )


def find_send_gift_image(image) -> tuple[int, int] | None:
    """find_send_gift on a PIL image; the point is in the image's own pixels."""
    rgba = image.convert("RGBA")
    return find_send_gift((rgba.width, rgba.height, 0, rgba.tobytes()))
