from __future__ import annotations

from tests import support as _test_support

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml
from PIL import Image, ImageDraw

from sources import gbl_android, gbl_day, gbl_home_recovery, gbl_ios, gbl_vision

CONFIG_DIR = Path(__file__).resolve().parent.parent / 'config'


def frame(width: int = 720, height: int = 1600, background=(60, 90, 50)):
    """A blank frame in the raw layout the runners read out of `screencap`."""
    data = bytes(bytearray(bytes(background) + b'\xff') * (width * height))
    return (width, height, 0, data)


def android_device(**config):
    return SimpleNamespace(config=dict(config))


def ios_device(width: int, height: int, **coordinates):
    """A real IOSGBLDevice with only the point maths wired up.

    The conversions under test -- logical_point and scale_point -- read nothing
    but `viewport`, so the genuine methods can be exercised without a phone.
    """
    device = gbl_ios.IOSGBLDevice.__new__(gbl_ios.IOSGBLDevice)
    device.viewport = {'width': width, 'height': height}
    device.config = SimpleNamespace(coordinates=dict(coordinates))
    return device


class MappedPointTests(unittest.TestCase):
    def test_a_calibrated_pair_is_returned(self) -> None:
        self.assertEqual(gbl_android.mapped_point([359, 1414]), [359, 1414])

    def test_the_caller_cannot_edit_the_config_through_it(self) -> None:
        value = [359, 1414]
        point = gbl_android.mapped_point(value)
        assert point is not None
        point[0] = 0
        self.assertEqual(value, [359, 1414])

    def test_zero_means_not_calibrated_on_this_handset(self) -> None:
        """[0, 0] is how the fleet spells "unmapped", not the top left corner."""
        self.assertIsNone(gbl_android.mapped_point([0, 0]))

    def test_a_half_mapped_pair_still_counts(self) -> None:
        self.assertEqual(gbl_android.mapped_point([0, 905]), [0, 905])

    def test_rubbish_is_refused(self) -> None:
        for value in (None, [], [1], [1, 2, 3], 'x', [1.5, 2.5], ['a', 'b'], {'x': 1}):
            with self.subTest(value=value):
                self.assertIsNone(gbl_android.mapped_point(value))


class AndroidMenuPointTests(unittest.TestCase):
    """The pokeball, in the order the runner is willing to trust."""

    def test_the_screenshot_beats_the_config(self) -> None:
        device = android_device(GBL_MENU_BTN=[359, 1414])
        with patch.object(gbl_home_recovery, 'find_pokeball', return_value=[10, 20]):
            self.assertEqual(gbl_android.recovery_menu_point(device, frame()), [10, 20])

    def test_the_config_answers_when_nothing_is_found(self) -> None:
        device = android_device(GBL_MENU_BTN=[359, 1414])
        with patch.object(gbl_home_recovery, 'find_pokeball', return_value=None):
            self.assertEqual(gbl_android.recovery_menu_point(device, frame()), [359, 1414])

    def test_an_unmapped_handset_falls_back_to_the_fraction(self) -> None:
        device = android_device(GBL_MENU_BTN=[0, 0])
        with patch.object(gbl_home_recovery, 'find_pokeball', return_value=None):
            point = gbl_android.recovery_menu_point(device, frame())
        self.assertEqual(point, [360, 1412])

    def test_a_phone_with_no_gbl_keys_at_all_still_gets_a_point(self) -> None:
        with patch.object(gbl_home_recovery, 'find_pokeball', return_value=None):
            point = gbl_android.recovery_menu_point(android_device(), frame())
        self.assertEqual(point, [360, 1412])


class AndroidMenuBattlePointTests(unittest.TestCase):
    """The main menu's BATTLE disc, in the same order."""

    def test_the_screenshot_beats_ocr_and_the_config(self) -> None:
        device = android_device(GBL_MENU_BATTLE_BTN=[559, 905])
        with patch.object(gbl_home_recovery, 'find_menu_battle', return_value=[11, 22]), \
                patch.object(gbl_vision, 'menu_battle_point', return_value=[33, 44]):
            point = gbl_android.recovery_menu_battle_point(device, frame(), [])
        self.assertEqual(point, [11, 22])

    def test_ocr_beats_the_config(self) -> None:
        device = android_device(GBL_MENU_BATTLE_BTN=[559, 905])
        with patch.object(gbl_home_recovery, 'find_menu_battle', return_value=None), \
                patch.object(gbl_vision, 'menu_battle_point', return_value=[33, 44]):
            point = gbl_android.recovery_menu_battle_point(device, frame(), [])
        self.assertEqual(point, [33, 44])

    def test_the_config_answers_when_neither_sees_it(self) -> None:
        device = android_device(GBL_MENU_BATTLE_BTN=[559, 905])
        with patch.object(gbl_home_recovery, 'find_menu_battle', return_value=None), \
                patch.object(gbl_vision, 'menu_battle_point', return_value=None):
            point = gbl_android.recovery_menu_battle_point(device, frame(), [])
        self.assertEqual(point, [559, 905])

    def test_the_other_battle_buttons_are_never_borrowed(self) -> None:
        """BATTLE_BTN is the friend screen's icon and GBL_BATTLE_BTN the GBL
        card's green pill.  Both name a BATTLE the main menu does not have, and
        pressing either one's coordinates from here taps whatever sits there."""
        device = android_device(BATTLE_BTN=[100, 200], GBL_BATTLE_BTN=[612, 2525])
        with patch.object(gbl_home_recovery, 'find_menu_battle', return_value=None), \
                patch.object(gbl_vision, 'menu_battle_point', return_value=None):
            point = gbl_android.recovery_menu_battle_point(device, frame(), [])
        self.assertNotIn(point, ([100, 200], [612, 2525]))
        # 0.565 of 1600 rounds down through the float: the blind fallback is
        # approximate by nature, which is the whole reason detection runs first.
        self.assertEqual(point, [360, 903])


class IOSMenuPointTests(unittest.TestCase):
    def test_a_found_pokeball_is_converted_to_logical_points(self) -> None:
        device = ios_device(440, 956)
        image = Image.new('RGB', (1320, 2868))
        with patch.object(gbl_home_recovery, 'find_pokeball', return_value=[660, 2682]):
            point = gbl_ios._menu_point(device, image, (1320, 2868))
        self.assertEqual(point, [220, 894])

    def test_a_configured_point_is_scaled_off_the_baseline(self) -> None:
        """iOS configs are calibrated on 375x667 and scaled per phone, so the
        Pro Max must not be handed the SE's numbers unconverted."""
        device = ios_device(440, 956, GBL_MENU_BTN=[187, 624])
        with patch.object(gbl_home_recovery, 'find_pokeball', return_value=None):
            point = gbl_ios._menu_point(device, Image.new('RGB', (10, 10)), (10, 10))
        self.assertEqual(point, [219, 894])

    def test_the_baseline_phone_is_left_alone(self) -> None:
        device = ios_device(375, 667, GBL_MENU_BTN=[187, 614])
        with patch.object(gbl_home_recovery, 'find_pokeball', return_value=None):
            point = gbl_ios._menu_point(device, Image.new('RGB', (10, 10)), (10, 10))
        self.assertEqual(point, [187, 614])

    def test_an_unmapped_iphone_falls_back_to_the_fraction(self) -> None:
        device = ios_device(375, 667)
        with patch.object(gbl_home_recovery, 'find_pokeball', return_value=None):
            point = gbl_ios._menu_point(device, Image.new('RGB', (10, 10)), (10, 10))
        self.assertEqual(point, [187, 588])


class IOSMenuBattlePointTests(unittest.TestCase):
    def test_the_disc_is_converted_to_logical_points(self) -> None:
        device = ios_device(440, 956)
        image = Image.new('RGB', (1320, 2868))
        with patch.object(gbl_home_recovery, 'find_menu_battle', return_value=[1045, 1736]):
            point = gbl_ios._menu_battle_point(device, image, (1320, 2868), [])
        self.assertEqual(point, [348, 579])

    def test_ocr_is_tried_before_the_config(self) -> None:
        device = ios_device(375, 667, GBL_MENU_BATTLE_BTN=[297, 345])
        image = Image.new('RGB', (750, 1334))
        with patch.object(gbl_home_recovery, 'find_menu_battle', return_value=None), \
                patch.object(gbl_vision, 'menu_battle_point', return_value=[594, 690]):
            point = gbl_ios._menu_battle_point(device, image, (750, 1334), [])
        self.assertEqual(point, [297, 345])

    def test_a_configured_disc_is_scaled_off_the_baseline(self) -> None:
        device = ios_device(440, 956, GBL_MENU_BATTLE_BTN=[297, 403])
        with patch.object(gbl_home_recovery, 'find_menu_battle', return_value=None), \
                patch.object(gbl_vision, 'menu_battle_point', return_value=None):
            point = gbl_ios._menu_battle_point(
                device, Image.new('RGB', (10, 10)), (10, 10), [])
        self.assertEqual(point, [348, 578])


class IOSConfigTests(unittest.TestCase):
    """The shipped configs, checked against what was measured on each phone."""

    def test_the_loader_is_allowed_to_keep_the_recovery_keys(self) -> None:
        """load_config drops every key it has not been told about, so a config
        entry nobody named here is read as though it were never written."""
        self.assertLessEqual(
            {gbl_home_recovery.BALL_KEY, gbl_home_recovery.BATTLE_KEY},
            gbl_ios.REQUIRED_COORDINATES | gbl_ios.OPTIONAL_COORDINATES,
        )

    def test_the_recovery_keys_are_not_demanded(self) -> None:
        """A phone nobody has measured still has to be able to start a set."""
        self.assertNotIn(gbl_home_recovery.BALL_KEY, gbl_ios.REQUIRED_COORDINATES)
        self.assertNotIn(gbl_home_recovery.BATTLE_KEY, gbl_ios.REQUIRED_COORDINATES)






class RecoverDeviceTests(unittest.TestCase):
    """The wrapper exists to let the phone go again -- see the session leak that
    left xcodebuild holding an iPhone through the next leg."""

    def setUp(self) -> None:
        self.closed = 0
        outer = self

        class FakeTarget:
            async def close(self) -> None:
                outer.closed += 1

        self.target = FakeTarget()

    def run_recover(self, steps=None, error=None):
        async def open_target(name):
            return self.target

        async def recover(target, shots, *, dry_run=False):
            if error is not None:
                raise error
            return steps or []

        with patch.object(gbl_home_recovery, 'open_target', open_target), \
                patch.object(gbl_home_recovery, 'recover', recover):
            return gbl_day.asyncio.run(
                gbl_home_recovery.recover_device('android-two', Path('/tmp/shots')))

    def test_a_finished_walk_reports_the_last_step(self) -> None:
        steps = [gbl_home_recovery.Step('menu', True, 'opened'),
                 gbl_home_recovery.Step('battle', True, 'on the GBL card')]
        reached, reported = self.run_recover(steps)
        self.assertTrue(reached)
        self.assertEqual(reported, steps)
        self.assertEqual(self.closed, 1)

    def test_a_failed_last_step_is_not_a_recovery(self) -> None:
        steps = [gbl_home_recovery.Step('menu', True, 'opened'),
                 gbl_home_recovery.Step('battle', False, 'no disc found')]
        reached, _ = self.run_recover(steps)
        self.assertFalse(reached)
        self.assertEqual(self.closed, 1)

    def test_a_walk_that_did_nothing_is_not_a_recovery(self) -> None:
        reached, reported = self.run_recover([])
        self.assertFalse(reached)
        self.assertEqual(reported, [])

    def test_the_phone_is_released_even_when_the_walk_throws(self) -> None:
        with self.assertRaises(RuntimeError):
            self.run_recover(error=RuntimeError('screencap wedged'))
        self.assertEqual(self.closed, 1)


class RecoverHomeScreenTests(unittest.TestCase):
    """gbl_day's own hook: worth another leg whatever happens here."""

    def test_it_passes_the_verdict_through(self) -> None:
        async def recover_device(name, shots, *, dry_run=False):
            return True, [gbl_home_recovery.Step('battle', True, 'on the GBL card')]

        with patch.object(gbl_home_recovery, 'recover_device', recover_device):
            self.assertTrue(gbl_day.asyncio.run(gbl_day.recover_home_screen('android-two')))

    def test_a_broken_recovery_does_not_end_the_day(self) -> None:
        async def recover_device(name, shots, *, dry_run=False):
            raise gbl_home_recovery.HomeRecoveryError('no such device')

        with patch.object(gbl_home_recovery, 'recover_device', recover_device):
            self.assertFalse(gbl_day.asyncio.run(gbl_day.recover_home_screen('tall_device')))

    def test_the_shots_go_where_the_day_is_logging(self) -> None:
        seen: list[Path] = []

        async def recover_device(name, shots, *, dry_run=False):
            seen.append(shots)
            return False, []

        with patch.object(gbl_home_recovery, 'recover_device', recover_device):
            gbl_day.asyncio.run(
                gbl_day.recover_home_screen('tall_device', Path('/tmp/day/home-recovery')))
        self.assertEqual(seen, [Path('/tmp/day/home-recovery')])


class DayRecoveryTests(unittest.TestCase):
    """An empty leg is the symptom the walk was written for: the runner is gone
    and the phone is standing on the map, so the next leg reads the map too."""

    def setUp(self) -> None:
        self.assertGreater(gbl_day.STALL_LIMIT, 1, 'a stalled leg must get a second try')

    @staticmethod
    def legs(*counts):
        remaining = list(counts)

        async def run(name, allowed):
            played = remaining.pop(0) if remaining else 0
            return gbl_day.LegResult(
                played={name: played} if played else {}, status=0, last_line='')

        return run

    def play(self, progress, run, recovered=True):
        calls: list[str] = []

        async def recover(name):
            calls.append(name)
            return recovered

        gbl_day.asyncio.run(gbl_day.play_device_day(progress, run, recover=recover))
        return calls

    def test_an_empty_leg_walks_the_phone_back(self) -> None:
        progress = gbl_day.DeviceProgress(name='tall_device', allotment=3)
        calls = self.play(progress, self.legs(0, 3))
        self.assertEqual(calls, ['tall_device'])
        self.assertEqual(progress.played, 3)
        self.assertEqual(progress.recoveries, 1)

    def test_a_leg_that_played_is_left_alone(self) -> None:
        progress = gbl_day.DeviceProgress(name='android-two', allotment=5)
        calls = self.play(progress, self.legs(5))
        self.assertEqual(calls, [])
        self.assertEqual(progress.recoveries, 0)

    def test_a_failed_walk_is_not_counted(self) -> None:
        progress = gbl_day.DeviceProgress(name='tall_device', allotment=3)
        calls = self.play(progress, self.legs(0, 3), recovered=False)
        self.assertEqual(calls, ['tall_device'])
        self.assertEqual(progress.recoveries, 0)

    def test_a_phone_that_is_finished_is_not_walked_anywhere(self) -> None:
        """The last leg of the day plays the allotment out and stops; there is
        no next leg to prepare a screen for."""
        progress = gbl_day.DeviceProgress(name='android-two', allotment=5)
        calls = self.play(progress, self.legs(5, 5))
        self.assertEqual(calls, [])

    def test_the_walk_is_dropped_once_the_phone_keeps_stalling(self) -> None:
        """STALL_LIMIT wins: two silent legs end the day even if the walk keeps
        claiming success, rather than looping on a phone nobody is driving."""
        progress = gbl_day.DeviceProgress(name='tall_device', allotment=3)
        calls = self.play(progress, self.legs(0, 0, 0, 0))
        self.assertEqual(len(calls), gbl_day.STALL_LIMIT - 1)
        self.assertEqual(progress.legs, gbl_day.STALL_LIMIT)
        self.assertIn('played nothing', progress.stopped)

    def test_the_count_reaches_the_status_file(self) -> None:
        progress = gbl_day.DeviceProgress(name='tall_device', allotment=3)
        self.play(progress, self.legs(0, 3))
        self.assertEqual(progress.as_dict()['recoveries'], 1)

    def test_the_supervisor_touches_no_phone_by_default(self) -> None:
        """play_device_day is called straight from the tests and from tooling;
        the default has to be a no-op, not a walk on a real handset."""
        progress = gbl_day.DeviceProgress(name='tall_device', allotment=3)
        gbl_day.asyncio.run(gbl_day.play_device_day(progress, self.legs(0, 3)))
        self.assertEqual(progress.played, 3)
        self.assertEqual(progress.recoveries, 0)


if __name__ == '__main__':
    unittest.main()


class FakeTarget(gbl_home_recovery.Target):
    """One phone, faked down to the four things the walk asks of it."""

    label = 'android-two'

    def __init__(self, image: Image.Image) -> None:
        self.image = image
        self.taps: list[list[int]] = []
        self.restores = 0

    async def screenshot(self) -> Image.Image:
        return self.image

    async def tap(self, point: list[int]) -> None:
        self.taps.append(point)

    def mapped(self, key: str, image: Image.Image) -> list[int] | None:
        # Both handsets now carry these, so the walk has to be safe with them.
        return {gbl_home_recovery.BALL_KEY: [359, 1414],
                gbl_home_recovery.BATTLE_KEY: [559, 905]}.get(key)

    async def restore(self) -> bool:
        self.restores += 1
        return False


class RecoverWalkTests(unittest.TestCase):
    """What the walk does with screens it was not looking for.

    The live failure this covers: a moto g that had stopped with the GBL card up
    was walked anyway, and the pokeball's mapped coordinate is the X that closes
    the card, so the rescue put the phone on the map.
    """

    def walk(self, target: FakeTarget, **seen):
        detectors = {'gbl_card_confirmed': False, 'find_pokeball': None,
                     'main_menu_open': False}
        detectors.update(seen)
        with tempfile.TemporaryDirectory() as shots, \
                patch.object(gbl_home_recovery, 'SETTLE_TIMEOUT', 0), \
                patch.multiple(gbl_home_recovery,
                               **{k: (lambda *_a, v=v: v) for k, v in detectors.items()}):
            return gbl_day.asyncio.run(
                gbl_home_recovery.recover(target, Path(shots)))

    def test_a_phone_already_on_the_card_is_left_alone(self) -> None:
        target = FakeTarget(Image.new('RGB', (720, 1600)))
        steps = self.walk(target, gbl_card_confirmed=True)
        self.assertEqual(target.taps, [])
        self.assertEqual([(s.name, s.ok) for s in steps], [('GO BATTLE LEAGUE', True)])

    def test_the_card_is_checked_before_the_game_is_prodded(self) -> None:
        """The card is not the map and not the menu, so without this check the
        walk asks for the game and then presses on regardless."""
        target = FakeTarget(Image.new('RGB', (720, 1600)))
        self.walk(target, gbl_card_confirmed=True)
        self.assertEqual(target.restores, 0)

    def test_an_unrecognised_screen_is_not_tapped(self) -> None:
        target = FakeTarget(Image.new('RGB', (720, 1600)))
        steps = self.walk(target)
        self.assertEqual(target.taps, [])
        self.assertFalse(steps[-1].ok)
        self.assertEqual(steps[-1].name, 'pokeball')
        self.assertIn('leaving this screen alone', steps[-1].detail)

    def test_a_pokeball_that_was_seen_is_pressed(self) -> None:
        target = FakeTarget(Image.new('RGB', (720, 1600)))
        steps = self.walk(target, find_pokeball=[359, 1414])
        self.assertEqual(target.taps, [[359, 1414]])
        self.assertTrue(steps[0].ok)
        self.assertIn('found in the picture', steps[0].detail)

    def test_a_menu_that_is_already_up_keeps_its_progress(self) -> None:
        """Pressing the pokeball from the menu would close it again."""
        target = FakeTarget(Image.new('RGB', (720, 1600)))
        steps = self.walk(target, main_menu_open=True, find_menu_battle=[559, 905])
        self.assertEqual(target.taps, [[559, 905]])
        self.assertIn('already up', steps[0].detail)

    def test_the_walk_saves_what_it_saw(self) -> None:
        """The shots are the only evidence left when a phone drops out hours
        into a day, so a refusal has to leave one behind too."""
        target = FakeTarget(Image.new('RGB', (720, 1600)))
        with tempfile.TemporaryDirectory() as shots, \
                patch.object(gbl_home_recovery, 'SETTLE_TIMEOUT', 0), \
                patch.multiple(gbl_home_recovery,
                               gbl_card_confirmed=lambda *_a: False,
                               find_pokeball=lambda *_a: None,
                               main_menu_open=lambda *_a: False):
            steps = gbl_day.asyncio.run(
                gbl_home_recovery.recover(target, Path(shots)))
            saved = sorted(p.name for p in Path(shots).glob('*.png'))
        self.assertTrue(saved, 'the walk left no screenshot behind')
        self.assertIsNotNone(steps[-1].shot)


class CardConfirmedTests(unittest.TestCase):
    """Deciding to do nothing needs better evidence than deciding to check."""

    def test_no_ocr_means_no_verdict(self) -> None:
        """gbl_card_reached guesses "card" for anything that is not the map or
        the menu.  Used to skip the walk, that guess reports every unknown
        screen -- an exit confirmation, the GBL welcome screen -- as arrived."""
        image = Image.new('RGB', (720, 1600))
        with patch.object(gbl_vision, 'recognize', side_effect=RuntimeError('no bridge')):
            self.assertFalse(gbl_home_recovery.gbl_card_confirmed(image))
            self.assertTrue(gbl_home_recovery.gbl_card_reached(image))

    def test_ocr_naming_the_card_is_a_verdict(self) -> None:
        image = Image.new('RGB', (720, 1600))
        with patch.object(gbl_vision, 'recognize', return_value=[]), \
                patch.object(gbl_vision, 'gbl_card_visible', return_value=True):
            self.assertTrue(gbl_home_recovery.gbl_card_confirmed(image))

    def test_an_unreadable_screen_is_not_skipped_by_the_walk(self) -> None:
        target = FakeTarget(Image.new('RGB', (720, 1600)))
        with tempfile.TemporaryDirectory() as shots, \
                patch.object(gbl_home_recovery, 'SETTLE_TIMEOUT', 0), \
                patch.object(gbl_vision, 'recognize', side_effect=RuntimeError('no bridge')), \
                patch.multiple(gbl_home_recovery,
                               find_pokeball=lambda *_a: None,
                               main_menu_open=lambda *_a: False):
            steps = gbl_day.asyncio.run(gbl_home_recovery.recover(target, Path(shots)))
        self.assertEqual([(s.name, s.ok) for s in steps], [('pokeball', False)])
        self.assertEqual(target.taps, [])


# The measurements these fixtures reproduce were taken off real frames on
# 2026-09-02: the menu's discs from a moto g and a tall_device that both walked back to
# the GBL card, and the battlefield from a android-one mid-battle that did not.
MENU_GREEN = (206, 232, 178)
DISC_TEAL = (40, 120, 140)
DISC_PALE = (238, 252, 234)


def menu_ring(image: Image.Image, centre: tuple[int, int], radius: int) -> None:
    """One action disc: a teal ring around a pale middle, as the menu draws it."""
    draw = ImageDraw.Draw(image)
    box = [centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius]
    draw.ellipse(box, fill=DISC_TEAL)
    inner = int(radius * 0.82)
    draw.ellipse([centre[0] - inner, centre[1] - inner,
                  centre[0] + inner, centre[1] + inner], fill=DISC_PALE)


def teal_pokemon(image: Image.Image, centre: tuple[int, int], radius: int) -> None:
    """A blue-green Pokemon: the same colour as a ring, filled in and far bigger."""
    ImageDraw.Draw(image).ellipse(
        [centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius],
        fill=DISC_TEAL)


class MenuDiscTests(unittest.TestCase):
    """The main menu, told apart from a GBL battlefield by shape alone."""

    def menu(self) -> Image.Image:
        """The moto g's menu: BATTLE over ITEMS, 101px across, x 559 and 558."""
        image = Image.new('RGB', (720, 1600), MENU_GREEN)
        menu_ring(image, (559, 905), 50)
        menu_ring(image, (558, 1256), 50)
        return image

    def test_the_menus_own_discs_are_still_found(self) -> None:
        image = self.menu()
        self.assertTrue(gbl_home_recovery.main_menu_open(image))
        self.assertEqual(len(gbl_home_recovery.menu_discs(image)), 2)

    def test_battle_is_the_upper_disc_of_the_column(self) -> None:
        battle = gbl_home_recovery.find_menu_battle(self.menu())
        self.assertIsNotNone(battle)
        self.assertAlmostEqual(battle[1], 905, delta=4)

    def test_a_teal_pokemon_is_too_big_to_be_a_disc(self) -> None:
        """The android-one's opponent measured 0.45 of the screen across.

        It passed the old aspect and minimum-size tests outright, and the walk
        that read it tapped 1019,900 fourteen times over.
        """
        image = Image.new('RGB', (1316, 2560), (90, 110, 90))
        teal_pokemon(image, (1019, 912), 290)
        self.assertEqual(gbl_home_recovery.menu_discs(image), [])
        self.assertFalse(gbl_home_recovery.main_menu_open(image))

    def test_a_filled_shape_the_size_of_a_disc_is_not_one(self) -> None:
        """Hollowness, not size, is what makes a disc a disc."""
        image = Image.new('RGB', (720, 1600), MENU_GREEN)
        teal_pokemon(image, (559, 905), 50)
        teal_pokemon(image, (558, 1256), 50)
        self.assertEqual(gbl_home_recovery.menu_discs(image), [])
        self.assertFalse(gbl_home_recovery.main_menu_open(image))

    def test_two_rings_out_of_column_are_not_the_menu(self) -> None:
        """A battlefield's teal patches have no reason to line up.

        The pair that stopped the android-one stood 170px apart on a 1316px screen,
        which is what this reproduces; the menu's own two are 1px apart.
        """
        image = Image.new('RGB', (1316, 2560), (90, 110, 90))
        menu_ring(image, (1019, 912), 92)
        menu_ring(image, (1189, 1506), 92)
        self.assertEqual(len(gbl_home_recovery.menu_discs(image)), 2)
        self.assertFalse(gbl_home_recovery.main_menu_open(image))
        self.assertIsNone(gbl_home_recovery.find_menu_battle(image))

    def test_a_lone_disc_is_not_a_menu(self) -> None:
        image = Image.new('RGB', (720, 1600), MENU_GREEN)
        menu_ring(image, (559, 905), 50)
        self.assertFalse(gbl_home_recovery.main_menu_open(image))
        self.assertIsNone(gbl_home_recovery.find_menu_battle(image))
