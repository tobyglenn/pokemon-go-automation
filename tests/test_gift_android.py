from __future__ import annotations

from tests import support as _test_support

from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image, ImageDraw

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
        # The walk reads the framebuffer itself and names it, so that the screen
        # it gives up on can be saved. One stand-in frame per scripted screen.
        frames = [None if screen is None else ("frame", screen) for screen in screens]
        with (
            mock.patch.object(
                gift, "screencap_raw", new=mock.AsyncMock(side_effect=frames)
            ),
            mock.patch.object(
                gift, "name_screen", side_effect=lambda frame, _: frame[1]
            ),
            mock.patch.object(gift, "save_frame", return_value=None) as saved,
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
            ok = await gift.walk_home(device)
            self.saved = saved
            return ok, taps

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

    async def test_a_panel_that_will_not_change_tabs_gets_the_x(self) -> None:
        # A friend profile headed in a pale buddy colour is named the panel, and
        # the FRIENDS tab tap lands on nothing there. That stopped an
        # android-three leg after nine cycles; the X closes it either way.
        device = self.device()
        ok, taps = await self.walk(
            device, [gift.FRIENDS_PANEL, gift.FRIENDS_PANEL, gift.FRIENDS_LIST]
        )
        self.assertTrue(ok)
        self.assertEqual(
            taps, [device.config["FRIENDS_TAB_BTN"], device.config["CLOSE_BTN"]]
        )

    async def test_a_walk_that_gives_up_saves_the_screen(self) -> None:
        # Without the picture, "out of ways off" names a screen nobody can check
        # afterwards — and being named wrong is what ends the walk.
        device = self.device()
        with mock.patch.object(
            gift, "escape_battle_screen", new=mock.AsyncMock(return_value=False)
        ):
            ok, _ = await self.walk(device, [gift.FRIEND_SCREEN] * 8)
        self.assertFalse(ok)
        self.assertEqual(self.saved.call_args[0][1], ("frame", gift.FRIEND_SCREEN))

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


# --- The friends list, in the colours both phones draw it in ---
# Measured on the android-one (1316x2560) and the android-three (1224x2992)
# on 2026-09-14: the rule between rows, the halo just outside an avatar, the
# orange a pinned remote trade's countdown is printed in, and the grey every
# other row prints its date in.
ROW_RULE_GREY = (228, 228, 228)
AVATAR_HALO = (213, 250, 252)
TRADE_ORANGE = (245, 140, 50)
DATE_GREY = (150, 150, 150)
AVATAR_SKIN = (240, 220, 200)
# The android-one's own geometry: the header ends at 727 and rows are 419
# tall, which is why its config carries NEXT_FRIEND_BTN [584, 937].
LIST_TOP = 727
ROW_PITCH = 419


def friends_list_frame(rows, width=1316, height=2560):
    """A friends list frame. Each row is (halo, waiting_on_a_trade)."""
    image = Image.new('RGB', (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for index, (halo, trade) in enumerate(rows):
        top = LIST_TOP + index * ROW_PITCH
        for rule in (top, top + ROW_PITCH):
            draw.line([(int(width * 0.06), rule), (int(width * 0.72), rule)],
                      fill=ROW_RULE_GREY, width=3)
        centre = (int(width * 0.15), top + ROW_PITCH // 2)
        radius = int(width * 0.062)
        if halo:
            glow = int(radius * 1.35)
            draw.ellipse([centre[0] - glow, centre[1] - glow,
                          centre[0] + glow, centre[1] + glow], fill=AVATAR_HALO)
        draw.ellipse([centre[0] - radius, centre[1] - radius,
                      centre[0] + radius, centre[1] + radius], fill=AVATAR_SKIN)
        # The countdown, or the date every other row shows in its place.
        draw.rectangle([int(width * 0.78), top + int(ROW_PITCH * 0.10),
                        int(width * 0.90), top + int(ROW_PITCH * 0.20)],
                       fill=TRADE_ORANGE if trade else DATE_GREY)
    return width, height, 0, image.convert('RGBA').tobytes()


class FriendRowTests(unittest.TestCase):
    """Reading the list before NEXT_FRIEND_BTN is tapped."""

    config = {'NEXT_FRIEND_BTN': [584, 937]}

    def test_rows_are_found_between_the_rules(self) -> None:
        rows = gift.friend_rows(friends_list_frame([(True, False)] * 4))
        self.assertEqual(len(rows), 4)
        first, second = rows[0], rows[1]
        self.assertAlmostEqual(sum(first) / 2, 937, delta=8)
        self.assertAlmostEqual(second[0] - first[0], ROW_PITCH, delta=4)

    def test_the_halo_is_read_off_a_row(self) -> None:
        frame = friends_list_frame([(True, False), (False, False)])
        rows = gift.friend_rows(frame)
        self.assertTrue(gift.row_is_highlighted(frame, rows[0]))
        self.assertFalse(gift.row_is_highlighted(frame, rows[1]))

    def test_a_countdown_is_a_trade_in_flight(self) -> None:
        frame = friends_list_frame([(False, True), (False, False)])
        rows = gift.friend_rows(frame)
        self.assertTrue(gift.row_waits_on_a_trade(frame, rows[0]))
        self.assertFalse(gift.row_waits_on_a_trade(frame, rows[1]))

    def test_a_plain_top_row_is_opened_and_its_gift_taken(self) -> None:
        choice = gift.choose_friend_row(
            friends_list_frame([(False, False)] * 3), self.config
        )
        self.assertTrue(choice.open_allowed)
        self.assertEqual(choice.point[0], 584)
        self.assertAlmostEqual(choice.point[1], 937, delta=8)

    def test_a_highlighted_friend_keeps_their_gift(self) -> None:
        choice = gift.choose_friend_row(
            friends_list_frame([(True, False)] * 3), self.config
        )
        self.assertFalse(choice.open_allowed)
        self.assertAlmostEqual(choice.point[1], 937, delta=8)

    def test_a_row_waiting_on_a_trade_is_stepped_over(self) -> None:
        """The android-one's pinned Lucky Remote Trade: no gift, cannot receive one,
        and it sat above the sort for every cycle of the run."""
        choice = gift.choose_friend_row(
            friends_list_frame([(False, True), (False, False), (True, False)]),
            self.config,
        )
        self.assertAlmostEqual(choice.point[1], 937 + ROW_PITCH, delta=8)
        self.assertTrue(choice.open_allowed)
        self.assertTrue(any('remote trade' in note for note in choice.notes))

    def test_the_row_below_a_trade_is_read_for_itself(self) -> None:
        choice = gift.choose_friend_row(
            friends_list_frame([(False, True), (True, False)]), self.config
        )
        self.assertAlmostEqual(choice.point[1], 937 + ROW_PITCH, delta=8)
        self.assertFalse(choice.open_allowed)

    def test_a_list_that_does_not_line_up_falls_back_to_the_config(self) -> None:
        # A part-drawn or part-scrolled list. Tapping a row worked out from
        # rules that do not match the configured point is worse than the blind
        # tap this replaced, so it does the blind tap.
        choice = gift.choose_friend_row(
            friends_list_frame([(True, False)] * 3), {'NEXT_FRIEND_BTN': [584, 2200]}
        )
        self.assertEqual(choice.point, [584, 2200])
        self.assertTrue(choice.open_allowed)

    def test_no_rules_at_all_falls_back_to_the_config(self) -> None:
        choice = gift.choose_friend_row(solid_frame((255, 255, 255)), self.config)
        self.assertEqual(choice.point, [584, 937])
        self.assertTrue(choice.open_allowed)


class SelectiveOpeningTests(unittest.IsolatedAsyncioTestCase):
    """Send to everyone who can receive; open only the rows without a halo."""

    def device(self):
        # A point of its own per step, so the taps can be read back by name.
        return SimpleNamespace(
            label='test-phone',
            config={
                step.name: [16, 16 + 8 * n]
                for n, step in enumerate(gift.GIFT_STEPS)
            },
        )

    async def cycle(self, *, open_allowed: bool):
        device = self.device()
        taps: list[str] = []
        steps = {tuple(v): k for k, v in device.config.items()}
        with (
            mock.patch.object(gift, 'GUARD', False),
            mock.patch.object(gift, 'SEND_POLL_GAP', 0),
            # OPEN lit, and the friend can receive a gift.
            mock.patch.object(
                gift, 'colored_action_available', new=mock.AsyncMock(return_value=True)
            ),
            mock.patch.object(
                gift, 'screencap_raw', new=mock.AsyncMock(return_value=FRAME)
            ),
            mock.patch.object(gift, 'frame_point_saturation', return_value=99),
            mock.patch.object(gift, 'open_through_bag_full_notice',
                              new=mock.AsyncMock(return_value=False)),
            mock.patch.object(
                gift, 'tap',
                new=mock.AsyncMock(side_effect=lambda _, p: taps.append(p)),
            ),
            mock.patch.object(gift, 'wait', new=mock.AsyncMock()),
        ):
            result = await gift.gift_sequence(
                device, all_mode=True, open_allowed=open_allowed
            )
        return result, [steps[tuple(point)] for point in taps]

    async def test_a_plain_friend_has_their_gift_opened(self) -> None:
        result, taps = await self.cycle(open_allowed=True)
        self.assertTrue(result.opened)
        self.assertTrue(result.sent)
        self.assertEqual(taps, [step.name for step in gift.GIFT_STEPS])

    async def test_a_highlighted_friend_is_sent_to_but_not_opened(self) -> None:
        result, taps = await self.cycle(open_allowed=False)
        self.assertFalse(result.opened)
        self.assertTrue(result.sent)
        self.assertNotIn('OPEN_BTN', taps)
        self.assertIn('SEND_BTN', taps)

    async def test_the_unopened_gift_is_closed_off_the_profile(self) -> None:
        # The postcard covers SEND GIFT, so skipping the open has to close it
        # before the send half of the cycle taps the profile underneath.
        _, taps = await self.cycle(open_allowed=False)
        self.assertEqual(taps.index('CLOSE_BTN'), 0)
        self.assertEqual(taps.count('CLOSE_BTN'), 2)
        self.assertLess(taps.index('CLOSE_BTN'), taps.index('SEND_GIFT_BTN'))

    async def test_the_row_read_at_the_end_reaches_the_next_cycle(self) -> None:
        device = self.device()
        with (
            mock.patch.object(gift, 'GUARD', True),
            mock.patch.object(gift, 'SEND_POLL_GAP', 0),
            mock.patch.object(
                gift, 'ensure_screen', new=mock.AsyncMock(return_value=None)
            ),
            mock.patch.object(
                gift, 'open_next_friend', new=mock.AsyncMock(return_value=False)
            ) as opened_next,
            mock.patch.object(
                gift, 'colored_action_available', new=mock.AsyncMock(return_value=True)
            ),
            mock.patch.object(
                gift, 'screencap_raw', new=mock.AsyncMock(return_value=FRAME)
            ),
            mock.patch.object(gift, 'frame_point_saturation', return_value=99),
            mock.patch.object(gift, 'open_through_bag_full_notice',
                              new=mock.AsyncMock(return_value=False)),
            mock.patch.object(gift, 'tap', new=mock.AsyncMock()),
            mock.patch.object(gift, 'wait', new=mock.AsyncMock()),
        ):
            result = await gift.gift_sequence(device, all_mode=True)
        opened_next.assert_awaited_once()
        self.assertFalse(result.open_next)

    async def test_gift_process_one_carries_it_between_cycles(self) -> None:
        device = self.device()
        cycles = [
            gift.GiftCycleResult(opened=True, sent=True, outgoing_available=True,
                                 open_next=False),
            gift.GiftCycleResult(opened=False, sent=True, outgoing_available=True,
                                 open_next=True),
        ]
        sequence = mock.AsyncMock(side_effect=cycles)
        with (
            mock.patch.object(gift, 'gift_sequence', new=sequence),
            mock.patch.object(gift, 'pointer', new=mock.AsyncMock()),
        ):
            await gift.gift_process_one(device, 2, all_mode=True)
        # The first cycle opens whatever the phone was left on; the second is
        # told what the first read off the list.
        self.assertTrue(sequence.await_args_list[0].kwargs['open_allowed'])
        self.assertFalse(sequence.await_args_list[1].kwargs['open_allowed'])


class NextFriendTapTests(unittest.IsolatedAsyncioTestCase):
    def device(self):
        return SimpleNamespace(
            label='test-phone', config={'NEXT_FRIEND_BTN': [584, 937]}
        )

    async def test_an_unreadable_screen_taps_the_configured_point(self) -> None:
        device = self.device()
        taps: list[list[int]] = []
        with (
            mock.patch.object(gift, 'GUARD', True),
            mock.patch.object(
                gift, 'screencap_raw', new=mock.AsyncMock(return_value=None)
            ),
            mock.patch.object(
                gift, 'tap',
                new=mock.AsyncMock(side_effect=lambda _, p: taps.append(p)),
            ),
        ):
            self.assertTrue(await gift.open_next_friend(device))
        self.assertEqual(taps, [[584, 937]])

    async def test_the_guard_switched_off_reads_nothing(self) -> None:
        device = self.device()
        with (
            mock.patch.object(gift, 'GUARD', False),
            mock.patch.object(gift, 'screencap_raw', new=mock.AsyncMock()) as read,
            mock.patch.object(gift, 'tap', new=mock.AsyncMock()),
        ):
            self.assertTrue(await gift.open_next_friend(device))
        read.assert_not_awaited()


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


class AdbBinaryTests(unittest.IsolatedAsyncioTestCase):
    """Taps go through the adb server, screen reads through the adb binary."""

    async def test_setup_refuses_to_run_without_the_adb_binary(self) -> None:
        # Otherwise the run is silently blind: every guard stands down, SEND
        # reads as unlit, and three tapped cycles are reported as an empty bag.
        with mock.patch.object(gift.shutil, "which", return_value=None):
            with self.assertRaises(gift.AutoGifterError) as raised:
                await gift.setup()
        self.assertIn("tap blind", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
