from __future__ import annotations

from tests import support as _test_support

import unittest
from unittest import mock

from sources import berry_android


def card(n: int) -> list[bool]:
    """A fingerprint distinct from every other n."""
    bits = [False] * 8
    bits[n] = True
    return bits


class TraversalTests(unittest.IsolatedAsyncioTestCase):
    """The gym walk must sweep back before it calls a gym finished.

    The forward carousel ring skips cards -- seen live on the android-one, where the
    forward walk found 5 defenders and the sweep back found the 6th -- so
    reaching MAX_DEFENDERS going forward is not evidence that every defender
    was reached. Ending the run there cancelled the sweep back and left
    defenders unfed with cheap berries still in the bag.
    """

    async def _walk(self, cards, feeds):
        device = mock.Mock(spec=berry_android.DeviceAsyncWrapper)
        device.serial = 'TESTSERIAL'
        device.label = 'test'
        turn = mock.AsyncMock(return_value=True)
        feed = mock.AsyncMock(side_effect=feeds)
        with (
            mock.patch.object(berry_android, 'pointer', new=mock.AsyncMock()),
            mock.patch.object(berry_android, 'wait', new=mock.AsyncMock()),
            mock.patch.object(
                berry_android, 'on_feed_screen', new=mock.AsyncMock(return_value=True)
            ),
            mock.patch.object(
                berry_android, 'card_fingerprint', new=mock.AsyncMock(side_effect=cards)
            ),
            mock.patch.object(
                berry_android, 'berry_offered', new=mock.AsyncMock(return_value=None)
            ),
            mock.patch.object(berry_android, 'feed_defender', new=feed),
            mock.patch.object(berry_android, 'turn_card', new=turn),
        ):
            processed = await berry_android.berry_process_one(device)
        return processed, turn, feed

    async def test_full_roster_going_forward_sweeps_back_before_stopping(self) -> None:
        # Six distinct cards forward, then a seventh only the sweep back reaches.
        cards = [card(n) for n in range(berry_android.MAX_DEFENDERS + 1)]
        cards += [cards[-1]] * berry_android.IDLE_TURNS
        feeds = [(1, None, 'razz')] * len(cards)

        processed, turn, feed = await self._walk(cards, feeds)

        self.assertEqual(processed, berry_android.MAX_DEFENDERS + 1)
        self.assertTrue(
            any(call.args[1] is True for call in turn.await_args_list),
            turn.await_args_list,
        )

    async def test_swept_back_full_roster_stops(self) -> None:
        # Once the gym has been swept both ways the roster cap does end the run,
        # so a gym that really is full does not walk forever.
        cards = [card(n) for n in range(berry_android.MAX_DEFENDERS + 1)]
        cards += [card(0)] * 20
        feeds = [(1, None, 'razz')] * len(cards)

        processed, turn, feed = await self._walk(cards, feeds)

        self.assertEqual(processed, berry_android.MAX_DEFENDERS + 1)
        self.assertLess(
            turn.await_count,
            berry_android.MAX_DEFENDERS * berry_android.MAX_TURNS_PER_DEFENDER * 2,
        )


class PickerBadgeTests(unittest.TestCase):
    """Reading how deep a stock is off the width of its "xN" pill."""

    WIDTH, HEIGHT = 1316, 2560
    SLOT = [219, 2125]
    PILL = (38, 99, 120)        # the teal sampled off a android-one picker

    def frame(self, pill_width: int, glyphs: bool = True) -> tuple:
        """A slot with a count pill of the given width drawn under it.

        The pill is drawn where the android-one puts it: hanging to the left of the
        slot centre, starting a little below it. With glyphs on, white bars run
        down the middle of it the way the digits do.
        """
        data = bytearray(b'\xd2\xd2\xd4\xff' * (self.WIDTH * self.HEIGHT))
        cx, cy = self.SLOT
        right = cx - 4
        top = cy + 16
        for y in range(top, top + 76):
            for x in range(right - pill_width + 1, right + 1):
                i = (y * self.WIDTH + x) * 4
                data[i:i + 3] = bytes(self.PILL)
        if glyphs:
            for x in range(right - pill_width + 12, right - 8, 24):
                for y in range(top + 24, top + 56):
                    for dx in range(8):
                        i = (y * self.WIDTH + x + dx) * 4
                        data[i:i + 3] = b'\xfe\xfe\xfe'
        return self.WIDTH, self.HEIGHT, 0, bytes(data)

    def test_double_digit_pill_widens_the_window(self) -> None:
        # 119 across is what x33 measured on the android-one.
        self.assertEqual(
            berry_android.picker_recheck_for(self.frame(119), self.SLOT),
            berry_android.PICKER_WIDE)

    def test_single_digit_pill_keeps_the_narrow_window(self) -> None:
        # A digit is worth about 32, so one digit is around 87 across -- and
        # reading that as deep is the mistake that spends golden razz.
        self.assertEqual(
            berry_android.picker_recheck_for(self.frame(87), self.SLOT),
            berry_android.PICKER_RECHECK)

    def test_digits_do_not_shorten_the_measurement(self) -> None:
        # The white digits cut the pill in two on every row they cross, so a
        # measurement taken through the middle of the text sees half a pill.
        # The rows above and below the text are what has to be found.
        self.assertEqual(
            berry_android.picker_badge_width(self.frame(119), self.SLOT),
            berry_android.picker_badge_width(self.frame(119, glyphs=False),
                                             self.SLOT))

    def test_no_badge_at_all_keeps_the_narrow_window(self) -> None:
        self.assertEqual(
            berry_android.picker_recheck_for(self.frame(0), self.SLOT),
            berry_android.PICKER_RECHECK)


if __name__ == '__main__':
    unittest.main()
