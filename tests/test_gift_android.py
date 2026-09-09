from __future__ import annotations

from tests import support as _test_support

from types import SimpleNamespace
import unittest
from unittest import mock

from sources import gift_android as gift


FRAME = (32, 32, 0, bytes(32 * 32 * 4))


def solid_frame(rgb: tuple[int, int, int], width: int = 32, height: int = 32):
    pixel = bytes((*rgb, 255))
    return width, height, 0, pixel * width * height


class ActionAvailabilityTests(unittest.TestCase):
    def test_grey_button_is_unavailable(self) -> None:
        self.assertEqual(
            gift.frame_point_saturation(solid_frame((130, 130, 130)), [16, 16]),
            0,
        )

    def test_colored_button_is_available(self) -> None:
        spread = gift.frame_point_saturation(
            solid_frame((30, 190, 150)), [16, 16]
        )
        self.assertGreaterEqual(spread, gift.ACTION_MIN_SATURATION)


class SendGiftAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_either_profile_layout_sample(self) -> None:
        device = SimpleNamespace(config={
            'SEND_GIFT_BTN': [10, 10],
            'SEND_GIFT_2_BUTTON_SAMPLE': [20, 20],
            'SEND_GIFT_3_BUTTON_SAMPLE': [30, 30],
        })
        with mock.patch.object(
            gift,
            'colored_action_available',
            new=mock.AsyncMock(side_effect=[False, True]),
        ) as available:
            self.assertTrue(await gift.send_gift_action_available(device))
        self.assertEqual(available.await_count, 2)

    async def test_rejects_grey_samples_in_both_layouts(self) -> None:
        device = SimpleNamespace(config={
            'SEND_GIFT_BTN': [10, 10],
            'SEND_GIFT_2_BUTTON_SAMPLE': [20, 20],
            'SEND_GIFT_3_BUTTON_SAMPLE': [30, 30],
        })
        with mock.patch.object(
            gift,
            'colored_action_available',
            new=mock.AsyncMock(side_effect=[False, False]),
        ):
            self.assertFalse(await gift.send_gift_action_available(device))


class FriendsListGuardTests(unittest.TestCase):
    def test_white_header_is_friends_panel(self) -> None:
        self.assertTrue(gift.is_friends_panel(*solid_frame((255, 255, 255))))

    def test_pale_blue_profile_is_not_friends_panel(self) -> None:
        self.assertFalse(gift.is_friends_panel(*solid_frame((205, 240, 250))))


class ScreenNamingTests(unittest.TestCase):
    """The sort control is what tells the friends list from the panel's other tabs."""

    config = {"SORT_BTN": [16, 16]}

    def test_white_header_with_the_sort_control_is_the_friends_list(self) -> None:
        frame = solid_frame((255, 255, 255))
        with mock.patch.object(gift, "sort_control_showing", return_value=True):
            self.assertEqual(gift.name_screen(frame, self.config), gift.FRIENDS_LIST)

    def test_white_header_without_it_is_another_tab(self) -> None:
        # The SOCIAL tab: the same white ME/FRIENDS/SOCIAL header as the list,
        # and no sort control, which is what it used to pass the guard on.
        frame = solid_frame((255, 255, 255))
        with mock.patch.object(gift, "sort_control_showing", return_value=False):
            self.assertEqual(gift.name_screen(frame, self.config), gift.FRIENDS_PANEL)

    def test_sort_control_without_the_header_is_the_open_menu(self) -> None:
        frame = solid_frame((60, 140, 120))
        with mock.patch.object(gift, "sort_control_showing", return_value=True):
            self.assertEqual(gift.name_screen(frame, self.config), gift.SORT_MENU)

    def test_anything_else_is_a_friend_screen(self) -> None:
        frame = solid_frame((205, 240, 250))
        with mock.patch.object(gift, "sort_control_showing", return_value=False):
            self.assertEqual(gift.name_screen(frame, self.config), gift.FRIEND_SCREEN)


class WalkHomeTests(unittest.IsolatedAsyncioTestCase):
    """Every screen is named again after every step, which is the whole fix."""

    def device(self, keys=("CLOSE_BTN", "AVATAR_BTN", "FRIENDS_TAB_BTN",
                           "CAN_RECEIVE_GIFT_BTN", "GBL_MENU_BTN")):
        return SimpleNamespace(
            label="test-phone", config={key: [16, 16] for key in keys}
        )

    async def walk(self, device, screens):
        """Runs the walk over a scripted sequence of screens, returning the taps."""
        taps: list[list[int]] = []
        with (
            mock.patch.object(
                gift, "current_screen", new=mock.AsyncMock(side_effect=screens)
            ),
            mock.patch.object(
                gift, "tap", new=mock.AsyncMock(side_effect=lambda _, p: taps.append(p))
            ),
            mock.patch.object(
                gift,
                "press_back",
                new=mock.AsyncMock(side_effect=lambda _: taps.append(gift.BACK_KEY)),
            ),
            mock.patch.object(gift, "asyncio", wraps=gift.asyncio) as loop,
        ):
            loop.sleep = mock.AsyncMock()
            return await gift.walk_home(device), taps

    async def test_already_home_taps_nothing(self) -> None:
        ok, taps = await self.walk(self.device(), [gift.FRIENDS_LIST])
        self.assertTrue(ok)
        self.assertEqual(taps, [])

    async def test_friend_stack_is_closed_one_screen_at_a_time(self) -> None:
        # The gift sits over the profile, and both close with the same X. The
        # version this replaced sent three of these blind; the third would have
        # landed on the map and opened the main menu.
        ok, taps = await self.walk(
            self.device(),
            [gift.FRIEND_SCREEN, gift.FRIEND_SCREEN, gift.FRIENDS_LIST],
        )
        self.assertTrue(ok)
        self.assertEqual(len(taps), 2)

    async def test_the_map_asks_for_the_friends_tab_as_well(self) -> None:
        # The avatar reopens the panel on the tab it was last left on, so
        # without the second tap the ME tab comes back and the walk loops.
        device = self.device()
        ok, taps = await self.walk(device, [gift.MAP, gift.FRIENDS_LIST])
        self.assertTrue(ok)
        self.assertEqual(
            taps, [device.config["AVATAR_BTN"], device.config["FRIENDS_TAB_BTN"]]
        )

    async def test_a_sort_menu_that_stays_up_gets_the_back_key(self) -> None:
        # Choosing a sort row does not always close the menu. A tall_device leg spent
        # that one step, found the menu still up and refused; back dismisses it.
        device = self.device()
        ok, taps = await self.walk(
            device, [gift.SORT_MENU, gift.SORT_MENU, gift.FRIENDS_LIST]
        )
        self.assertTrue(ok)
        self.assertEqual(taps, [device.config["CAN_RECEIVE_GIFT_BTN"], gift.BACK_KEY])

    async def test_a_step_with_no_keys_falls_through_to_the_next_one(self) -> None:
        # A phone mapped without a sort row still gets the back key: only
        # running out of steps altogether is "no way off this screen".
        device = self.device(keys=("CLOSE_BTN", "AVATAR_BTN", "FRIENDS_TAB_BTN"))
        ok, taps = await self.walk(device, [gift.SORT_MENU, gift.FRIENDS_LIST])
        self.assertTrue(ok)
        self.assertEqual(taps, [gift.BACK_KEY])

    async def test_a_screen_that_never_changes_gives_up(self) -> None:
        # A battle screen: named a friend's screen, and nothing on it answers
        # CLOSE_BTN. It must stop rather than tap on forever.
        device = self.device()
        with mock.patch.object(
            gift, "escape_battle_screen", new=mock.AsyncMock(return_value=False)
        ):
            ok, taps = await self.walk(device, [gift.FRIEND_SCREEN] * 8)
        self.assertFalse(ok)
        self.assertEqual(len(taps), len(gift.STEP_HOME[gift.FRIEND_SCREEN]))

    async def test_missing_keys_do_not_stop_the_rest_of_a_step(self) -> None:
        # A phone mapped without FRIENDS_TAB_BTN still gets the avatar tap.
        device = self.device(keys=("CLOSE_BTN", "AVATAR_BTN"))
        ok, taps = await self.walk(device, [gift.MAP, gift.FRIENDS_LIST])
        self.assertTrue(ok)
        self.assertEqual(taps, [device.config["AVATAR_BTN"]])

    async def test_an_unreadable_screen_stands_the_guard_down(self) -> None:
        ok, taps = await self.walk(self.device(), [None])
        self.assertTrue(ok)
        self.assertEqual(taps, [])


class UntilIdleTests(unittest.IsolatedAsyncioTestCase):
    def device(self):
        return SimpleNamespace(
            label="test-phone",
            config={step.name: [16, 16] for step in gift.GIFT_STEPS},
        )

    def probes(self, *answers: bool | None) -> mock.AsyncMock:
        return mock.AsyncMock(side_effect=list(answers))

    async def test_a_cycle_reports_a_send_button_that_never_lights(self) -> None:
        """OPEN grey, the friend can receive, and SEND never lights: the cycle
        says so, and gift_process_one decides what it means."""
        device = self.device()
        with (
            mock.patch.object(gift, "GUARD", False),
            mock.patch.object(gift, "SEND_POLL_GAP", 0),
            mock.patch.object(
                gift, "colored_action_available", new=self.probes(False, True)
            ),
            mock.patch.object(
                gift, "screencap_raw", new=mock.AsyncMock(return_value=FRAME)
            ),
            mock.patch.object(gift, "frame_point_saturation", return_value=0),
            mock.patch.object(gift, "save_frame") as saved,
            mock.patch.object(gift, "tap", new=mock.AsyncMock()),
            mock.patch.object(gift, "wait", new=mock.AsyncMock()),
        ):
            result = await gift.gift_sequence(device, all_mode=True)

        self.assertFalse(result.opened)
        self.assertFalse(result.sent)
        self.assertFalse(result.outgoing_available)
        # The screen it called empty is kept: an unlit SEND is what ends a run,
        # and it does not say which screen was up.
        self.assertEqual(saved.call_count, 1)

    async def test_a_send_button_that_lights_late_is_still_used(self) -> None:
        """The sheet SEND lives on is the last thing in the cycle to paint, and
        on the slowest phone it has been read before it arrived."""
        device = self.device()
        with (
            mock.patch.object(gift, "GUARD", False),
            mock.patch.object(gift, "SEND_POLL_GAP", 0),
            mock.patch.object(
                gift, "colored_action_available", new=self.probes(False, True)
            ),
            # The middle read comes back unreadable, which is no answer at all.
            mock.patch.object(
                gift,
                "screencap_raw",
                new=mock.AsyncMock(side_effect=[FRAME, None, FRAME]),
            ),
            mock.patch.object(
                gift, "frame_point_saturation", side_effect=[0, 60]
            ),
            mock.patch.object(gift, "save_frame") as saved,
            mock.patch.object(gift, "tap", new=mock.AsyncMock()),
            mock.patch.object(gift, "wait", new=mock.AsyncMock()),
        ):
            result = await gift.gift_sequence(device, all_mode=True)

        self.assertTrue(result.sent)
        self.assertTrue(result.outgoing_available)
        saved.assert_not_called()

    async def test_one_unlit_cycle_does_not_stop_the_phone(self) -> None:
        """What stopped a android-one with gifts still in the bag: the picker had not
        opened, so SEND was read on the friend screen underneath."""
        device = self.device()
        unlit = gift.GiftCycleResult(opened=False, sent=False, outgoing_available=False)
        sent = gift.GiftCycleResult(opened=False, sent=True, outgoing_available=True)
        sequence = mock.AsyncMock(side_effect=[unlit, sent, unlit, sent])
        with (
            mock.patch.object(gift, "gift_sequence", new=sequence),
            mock.patch.object(gift, "pointer", new=mock.AsyncMock()),
        ):
            completed = await gift.gift_process_one(device, 4, all_mode=True)

        self.assertEqual(completed, 4)

    async def test_the_phone_stops_once_whole_cycles_agree(self) -> None:
        device = self.device()
        unlit = gift.GiftCycleResult(opened=False, sent=False, outgoing_available=False)
        sequence = mock.AsyncMock(return_value=unlit)
        with (
            mock.patch.object(gift, "gift_sequence", new=sequence),
            mock.patch.object(gift, "pointer", new=mock.AsyncMock()),
        ):
            completed = await gift.gift_process_one(device, 100, all_mode=True)

        self.assertEqual(completed, gift.EMPTY_BAG_CYCLES)

    async def test_a_send_resets_the_tally(self) -> None:
        """Consecutive, because a friend the picker would not open for is not
        evidence about the bag — only cycles in a row are."""
        device = self.device()
        unlit = gift.GiftCycleResult(opened=False, sent=False, outgoing_available=False)
        sent = gift.GiftCycleResult(opened=False, sent=True, outgoing_available=True)
        pattern = [unlit, unlit, sent] + [unlit] * gift.EMPTY_BAG_CYCLES
        sequence = mock.AsyncMock(side_effect=pattern)
        with (
            mock.patch.object(gift, "gift_sequence", new=sequence),
            mock.patch.object(gift, "pointer", new=mock.AsyncMock()),
        ):
            completed = await gift.gift_process_one(device, 100, all_mode=True)

        self.assertEqual(completed, len(pattern))


class BagFullNoticeTests(unittest.IsolatedAsyncioTestCase):
    """OPEN_BTN raises this instead of opening the gift once the bag is full,
    and it is what a "the gift bag must be empty" stop was really looking at."""

    def device(self):
        return SimpleNamespace(label="test-phone", config={})

    async def run_notice(self, *, metrics, text, pill=(100, 200)):
        taps: list[list[int]] = []
        with (
            mock.patch.object(
                gift, "screencap_raw", new=mock.AsyncMock(return_value=FRAME)
            ),
            mock.patch.object(gift, "frame_image", return_value=object()),
            mock.patch("sources.trade_ios_android.screen_metrics", return_value=metrics),
            mock.patch("sources.trade_ios_android.find_dialog_button", return_value=pill),
            mock.patch("sources.gbl_vision.recognize", return_value=[]),
            mock.patch("sources.gbl_vision.lines", return_value=[text]),
            mock.patch.object(
                gift, "tap", new=mock.AsyncMock(side_effect=lambda _, p: taps.append(p))
            ),
            mock.patch.object(gift, "wait", new=mock.AsyncMock()),
        ):
            pressed = await gift.open_through_bag_full_notice(self.device())
        return pressed, taps

    # Measured on the android-one, so these are the real numbers off the phone.
    NOTICE = {"card_band": 0.747, "card_edges": 0.295}
    PROFILE = {"card_band": 0.822, "card_edges": 0.961}
    PICKER = {"card_band": 0.211, "card_edges": 0.014}

    async def test_the_notice_is_opened_through(self) -> None:
        pressed, taps = await self.run_notice(
            metrics=self.NOTICE, text="Your Item Bag is full!"
        )
        self.assertTrue(pressed)
        self.assertEqual(taps, [[100, 200]])

    async def test_a_friend_profile_is_left_alone(self) -> None:
        # Its white panel is as broad as the notice's card; the edges are what
        # say it reaches the sides of the screen rather than floating over it.
        pressed, taps = await self.run_notice(
            metrics=self.PROFILE, text="Your Item Bag is full!"
        )
        self.assertFalse(pressed)
        self.assertEqual(taps, [])

    async def test_the_gift_picker_is_left_alone(self) -> None:
        pressed, taps = await self.run_notice(
            metrics=self.PICKER, text="Which Gift do you want to send?"
        )
        self.assertFalse(pressed)
        self.assertEqual(taps, [])

    async def test_a_card_saying_something_else_is_left_alone(self) -> None:
        pressed, taps = await self.run_notice(
            metrics=self.NOTICE, text="Do you want to exit Pokemon GO?"
        )
        self.assertFalse(pressed)
        self.assertEqual(taps, [])

    async def test_a_notice_with_no_pill_is_left_alone(self) -> None:
        pressed, taps = await self.run_notice(
            metrics=self.NOTICE, text="Your Item Bag is full!", pill=None
        )
        self.assertFalse(pressed)
        self.assertEqual(taps, [])


if __name__ == "__main__":
    unittest.main()
