"""A backgrounded or crashed game is put back, not tapped through.

Seen on the moto-g, 22 Sep 2026: Google Messages took the screen mid-leg with
Pokemon GO still running behind it, and the leg printed nothing for an hour
because every recovery tap landed in the messaging app.
"""

from __future__ import annotations

from tests import support as _test_support

import unittest
from unittest import mock

from PIL import Image

from sources import gbl_home_recovery, gbl_vision


FOCUSED = ('  mCurrentFocus=Window{326ab68 u0 '
           'com.google.android.apps.messaging/'
           'com.google.android.apps.messaging.ui.ConversationListActivity}')
GAME_FOCUSED = ('  mCurrentFocus=Window{7d1f2c u0 com.nianticlabs.pokemongo/'
                'com.nianticlabs.pokemongo.UnityPlayerActivity}')


class ForegroundPackageTests(unittest.TestCase):
    def test_the_app_in_front_is_read_from_dumpsys(self) -> None:
        self.assertEqual(
            gbl_home_recovery.package_in_front(FOCUSED),
            'com.google.android.apps.messaging',
        )

    def test_the_game_in_front_is_recognised_as_itself(self) -> None:
        self.assertEqual(
            gbl_home_recovery.package_in_front(GAME_FOCUSED),
            gbl_home_recovery.GAME_PACKAGE,
        )

    def test_nothing_focused_names_nothing(self) -> None:
        """Between apps, and on the lock screen, dumpsys says null."""
        self.assertEqual(
            gbl_home_recovery.package_in_front('  mCurrentFocus=null'), '')

    def test_a_focused_window_with_no_package_names_nothing(self) -> None:
        """The notification shade is a window without an activity behind it."""
        self.assertEqual(
            gbl_home_recovery.package_in_front(
                '  mCurrentFocus=Window{a1b2 u0 NotificationShade}'), '')

    def test_an_unreadable_dump_names_nothing(self) -> None:
        self.assertEqual(gbl_home_recovery.package_in_front(''), '')

    def test_the_focus_line_is_found_among_the_rest_of_the_dump(self) -> None:
        dumped = '\n'.join([
            'WINDOW MANAGER WINDOWS (dumpsys window windows)',
            '  mCurrentFocus=Window{326ab68 u0 com.example.app/com.example.Main}',
            '  mFocusedApp=AppWindowToken{other}',
        ])
        self.assertEqual(gbl_home_recovery.package_in_front(dumped), 'com.example.app')


class AndroidRestoreTests(unittest.IsolatedAsyncioTestCase):
    def target(self, front: str, pid: str = '17996') -> gbl_home_recovery.AndroidTarget:
        phone = gbl_home_recovery.AndroidTarget('moto-g', 'SERIAL')
        self.sent: list[tuple[str, ...]] = []

        def fake_adb(*arguments, **_options):
            self.sent.append(arguments)
            if 'dumpsys' in arguments:
                # The launch is what changes the answer, as on the phone.
                launched = gbl_home_recovery.LAUNCH_INTENT in self.sent
                return mock.Mock(stdout=GAME_FOCUSED if launched else front)
            if 'pidof' in arguments:
                return mock.Mock(stdout=pid)
            return mock.Mock(stdout='')

        phone._adb = fake_adb
        return phone

    async def test_a_game_behind_another_app_is_brought_forward(self) -> None:
        phone = self.target(FOCUSED)
        self.assertTrue(await phone.restore())
        self.assertIn(gbl_home_recovery.LAUNCH_INTENT, self.sent)
        self.assertFalse(phone.restored_cold)

    async def test_a_dead_game_is_started_and_flagged_cold(self) -> None:
        """iOS leaves a dead game for a human; Android can just restart it."""
        phone = self.target(FOCUSED, pid='')
        self.assertTrue(await phone.restore())
        self.assertIn(gbl_home_recovery.LAUNCH_INTENT, self.sent)
        self.assertTrue(phone.restored_cold)

    async def test_a_game_already_in_front_is_left_alone(self) -> None:
        """An unrecognised screen belonging to the game is the walk's problem."""
        phone = self.target(GAME_FOCUSED)
        self.assertFalse(await phone.restore())
        self.assertNotIn(gbl_home_recovery.LAUNCH_INTENT, self.sent)

    async def test_the_launch_is_reported_even_when_it_does_not_come_forward(self) -> None:
        """Saying it acted stops the caller tapping the app it can still see."""
        phone = gbl_home_recovery.AndroidTarget('moto-g', 'SERIAL')
        phone._adb = lambda *arguments, **_options: mock.Mock(stdout=FOCUSED)
        with mock.patch.object(gbl_home_recovery, 'FOREGROUND_TIMEOUT', 0.0):
            self.assertTrue(await phone.restore())


class ColdStartPatienceTests(unittest.IsolatedAsyncioTestCase):
    """A cold start needs minutes; the 12s menu budget called it a failure."""

    async def test_a_restarted_game_is_given_the_long_budget(self) -> None:
        waited: list[float | None] = []

        class Phone(gbl_home_recovery.Target):
            label = 'moto-g'
            restored_cold = True

            async def screenshot(self):
                return mock.Mock(size=(1080, 2400))

            async def restore(self) -> bool:
                return True

        async def fake_settle(target, predicate, timeout=None):
            waited.append(timeout)
            return False, await target.screenshot()

        with (
            mock.patch.object(gbl_home_recovery, '_settle', fake_settle),
            mock.patch.object(gbl_home_recovery, '_save', lambda *a: None),
            mock.patch.object(gbl_home_recovery, 'gbl_card_confirmed', lambda _: False),
            mock.patch.object(gbl_home_recovery, 'on_map', lambda _: False),
            mock.patch.object(gbl_home_recovery, 'main_menu_open', lambda _: False),
        ):
            steps = await gbl_home_recovery.recover(Phone(), mock.Mock())

        self.assertEqual(waited, [gbl_home_recovery.COLD_START_SETTLE])
        self.assertEqual(steps[-1].name, 'foreground')
        self.assertFalse(steps[-1].ok)

    async def test_a_resumed_game_keeps_the_ordinary_budget(self) -> None:
        waited: list[float | None] = []

        class Phone(gbl_home_recovery.Target):
            label = 'moto-g'

            async def screenshot(self):
                return mock.Mock(size=(1080, 2400))

            async def restore(self) -> bool:
                return True

        async def fake_settle(target, predicate, timeout=None):
            waited.append(timeout)
            return True, await target.screenshot()

        with (
            mock.patch.object(gbl_home_recovery, '_settle', fake_settle),
            mock.patch.object(gbl_home_recovery, '_save', lambda *a: None),
            mock.patch.object(gbl_home_recovery, 'gbl_card_confirmed', lambda _: False),
            mock.patch.object(gbl_home_recovery, 'on_map', lambda _: False),
            mock.patch.object(gbl_home_recovery, 'main_menu_open', lambda _: False),
            mock.patch.object(gbl_home_recovery, 'find_pokeball', lambda _: None),
        ):
            await gbl_home_recovery.recover(Phone(), mock.Mock())

        self.assertEqual(waited, [None])


class SafetyNoticeTests(unittest.TestCase):
    """The card a cold-started game stops on, measured from the moto-g.

    Vision read the title and the body and nothing else: the button's letters
    are spaced too widely to come back as a word, so the pill has to be found
    by its own colour.
    """

    def boxes(self, *extra: gbl_vision.OCRBox) -> list[gbl_vision.OCRBox]:
        return [
            gbl_vision.OCRBox('Stay Aware of Your Surroundings', 1.0, 95, 855, 530, 35),
            gbl_vision.OCRBox('Do not enter dangerous areas while', 1.0, 108, 948, 504, 35),
            gbl_vision.OCRBox('playing Pokémon GO.', 1.0, 205, 996, 310, 35),
            *extra,
        ]

    def card(self) -> Image.Image:
        """A 720x1600 stand-in: white card, green-to-teal pill at y 1085-1185."""
        image = Image.new('RGB', (720, 1600), (44, 113, 115))
        for y in range(330, 1255):
            for x in range(36, 684):
                image.putpixel((x, y), (248, 250, 248))
        for y in range(1085, 1186):
            for x in range(160, 561):
                share = (x - 160) / 400
                image.putpixel(
                    (x, y),
                    (int(162 - 94 * share), int(218 - 11 * share), int(148 + 16 * share)),
                )
        return image

    def test_the_card_is_recognised_by_what_it_says(self) -> None:
        self.assertTrue(gbl_vision.safety_notice_visible(self.boxes()))

    def test_one_word_alone_is_not_enough_to_press_a_dialog(self) -> None:
        single = [gbl_vision.OCRBox('Stay Aware', 1.0, 95, 855, 530, 35)]
        self.assertFalse(gbl_vision.safety_notice_visible(single))

    def test_a_readable_ok_is_pressed_where_it_is_written(self) -> None:
        written = gbl_vision.OCRBox('OK', 1.0, 340, 1120, 40, 32)
        self.assertEqual(
            gbl_vision.safety_notice_point(self.boxes(written), self.card()),
            [360, 1136],
        )

    def test_the_spaced_out_spelling_is_the_same_button(self) -> None:
        written = gbl_vision.OCRBox('O K', 1.0, 340, 1120, 40, 32)
        self.assertEqual(
            gbl_vision.safety_notice_point(self.boxes(written), self.card())[1], 1136)

    def test_an_unreadable_button_is_found_by_its_pill(self) -> None:
        """The live case: no OK box at all, and the walk still has to press it."""
        self.assertEqual(
            gbl_vision.safety_notice_point(self.boxes(), self.card()), [360, 1135])

    def test_nothing_is_pressed_on_a_screen_that_is_not_the_warning(self) -> None:
        elsewhere = [
            gbl_vision.OCRBox('GO BATTLE LEAGUE', 1.0, 100, 400, 300, 40),
            gbl_vision.OCRBox('OK', 1.0, 340, 1120, 40, 32),
        ]
        self.assertIsNone(gbl_vision.safety_notice_point(elsewhere, self.card()))

    def test_a_card_with_no_pill_is_left_alone(self) -> None:
        """Better to report nothing than to tap the middle of a white card."""
        blank = Image.new('RGB', (720, 1600), (248, 250, 248))
        self.assertIsNone(gbl_vision.safety_notice_point(self.boxes(), blank))

    def test_without_a_picture_only_a_readable_button_can_be_pressed(self) -> None:
        self.assertIsNone(gbl_vision.safety_notice_point(self.boxes()))


class SafetyNoticeWalkTests(unittest.IsolatedAsyncioTestCase):
    """A walk that steps over the warning is walking over a wall."""

    def phone(self, screens: list) -> gbl_home_recovery.Target:
        pressed = self.pressed = []

        class Phone(gbl_home_recovery.Target):
            label = 'moto-g'

            async def screenshot(self):
                return screens[min(len(pressed), len(screens) - 1)]

            async def tap(self, point):
                pressed.append(point)

            async def restore(self) -> bool:
                return False

        return Phone()

    async def test_the_warning_is_pressed_before_the_walk_starts(self) -> None:
        notice = mock.Mock(size=(720, 1600))
        map_screen = mock.Mock(size=(720, 1600))
        phone = self.phone([notice, map_screen])

        def ok(image):
            return [360, 1136] if image is notice else None

        with (
            mock.patch.object(gbl_home_recovery, 'safety_notice_ok', ok),
            mock.patch.object(gbl_home_recovery, '_save', lambda *a: None),
            mock.patch.object(gbl_home_recovery, 'gbl_card_confirmed', lambda _: False),
            mock.patch.object(gbl_home_recovery, 'on_map', lambda shot: shot is map_screen),
            mock.patch.object(gbl_home_recovery, 'main_menu_open', lambda _: False),
            mock.patch.object(gbl_home_recovery, 'find_pokeball', lambda _: None),
        ):
            steps = await gbl_home_recovery.recover(phone, mock.Mock())

        self.assertEqual(self.pressed, [[360, 1136]])
        self.assertEqual(steps[0].name, 'safety notice')
        self.assertTrue(steps[0].ok)

    async def test_a_warning_that_will_not_go_stops_the_walk(self) -> None:
        """Every tap below it would land on the card, so there is no point."""
        phone = self.phone([mock.Mock(size=(720, 1600))])

        with (
            mock.patch.object(gbl_home_recovery, 'safety_notice_ok',
                              lambda _: [360, 1136]),
            mock.patch.object(gbl_home_recovery, 'SETTLE_TIMEOUT', 0.0),
            mock.patch.object(gbl_home_recovery, '_save', lambda *a: None),
            mock.patch.object(gbl_home_recovery, 'gbl_card_confirmed', lambda _: False),
            mock.patch.object(gbl_home_recovery, 'on_map', lambda _: False),
            mock.patch.object(gbl_home_recovery, 'main_menu_open', lambda _: False),
        ):
            steps = await gbl_home_recovery.recover(phone, mock.Mock())

        self.assertEqual(steps[-1].name, 'safety notice')
        self.assertFalse(steps[-1].ok)


class InLegForegroundTests(unittest.IsolatedAsyncioTestCase):
    """The leg itself checks, so a stall costs one read rather than 5 minutes."""

    def phone(self, dumped: str, pid: str = '17996'):
        device = mock.MagicMock()
        device.serial = 'SERIAL'

        async def shell(command: str) -> str:
            if command.startswith('dumpsys window'):
                return dumped
            if command.startswith('pidof'):
                return pid
            self.commands.append(command)
            return ''

        self.commands: list[str] = []
        device.shell = shell
        return device

    async def test_another_app_in_front_is_reported(self) -> None:
        from sources import gbl_android

        self.assertFalse(await gbl_android.game_in_front(self.phone(FOCUSED)))

    async def test_the_game_in_front_is_reported(self) -> None:
        from sources import gbl_android

        self.assertTrue(await gbl_android.game_in_front(self.phone(GAME_FOCUSED)))

    async def test_a_phone_that_cannot_answer_is_not_relaunched(self) -> None:
        """A probe that fails says so; it does not condemn a working game."""
        from sources import gbl_android

        device = mock.MagicMock()
        device.serial = 'SERIAL'
        device.shell = mock.AsyncMock(side_effect=RuntimeError('adb died'))
        self.assertTrue(await gbl_android.game_in_front(device))

    async def test_relaunch_sends_the_launcher_intent(self) -> None:
        from sources import gbl_android

        device = self.phone(FOCUSED)
        with mock.patch.object(gbl_android, 'wait', mock.AsyncMock()) as slept:
            await gbl_android.relaunch_game(device)

        self.assertEqual(
            self.commands,
            [' '.join(gbl_home_recovery.LAUNCH_INTENT[1:])],
        )
        slept.assert_awaited_once_with(gbl_android.MENU_SETTLE, use_modifier=False)

    async def test_a_dead_game_is_given_time_to_start(self) -> None:
        from sources import gbl_android

        device = self.phone(FOCUSED, pid='')
        with mock.patch.object(gbl_android, 'wait', mock.AsyncMock()) as slept:
            await gbl_android.relaunch_game(device)

        slept.assert_awaited_once_with(gbl_android.GAME_COLD_START, use_modifier=False)


if __name__ == '__main__':
    unittest.main()
