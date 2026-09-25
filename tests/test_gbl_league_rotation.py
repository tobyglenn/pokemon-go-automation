"""A configured league that is not in today's rotation must not end the day.

On 2026-09-16 Pokemon GO offered no Master League of any edition, and every
phone in the fleet spent its whole run on the same circle: take the only card
on the list, read GREAT LEAGUE on the party screen, back out because the
config asks for Master, and stop -- `0/40 battle(s) over 2 leg(s)`.  The
preference is worth having on the days the league is there, and worth nothing
on the days it is not.
"""

from __future__ import annotations

from tests import support as _test_support

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from sources import gbl_android as gbl
from sources import gbl_ios, gbl_meta, gbl_strategy, gbl_vision

# The module rather than the names: importing its TestCases would run the
# whole iPhone suite again under this file.
from tests import test_gbl_screens as screens

as_frame, frame, team_member = (
    screens.as_frame, screens.frame, screens.team_member)


def profile(league: str = "Master League") -> gbl_strategy.StrategyProfile:
    return gbl_strategy.StrategyProfile(
        gbl_strategy.BattleTeam(
            (team_member("Noctowl"), team_member("Lanturn"), team_member("Whiscash"))
        ),
        gbl_strategy.StrategySettings(preferred_league=league),
    )


def boxes(*labels: str) -> list[gbl_vision.OCRBox]:
    return [
        gbl_vision.OCRBox(text, 1.0, 300, 200 + index * 40, 200, 30)
        for index, text in enumerate(labels)
    ]


PARTY = boxes("CHOOSE YOUR PARTY", "GREAT LEAGUE", "USE THIS PARTY")
CARDS = boxes("CHOOSE YOUR LEAGUE", "GREAT LEAGUE")


class AdoptLeagueTests(unittest.TestCase):
    """Adopting a league has to move the CP cap with the name."""

    def setUp(self) -> None:
        self.addCleanup(gbl_meta.set_active_cap, gbl_meta.ACTIVE_CAP)

    def test_the_name_and_the_cap_both_move(self) -> None:
        gbl_meta.set_active_cap(10000)
        adopted = gbl_strategy.adopt_league(profile(), "GREAT LEAGUE")
        self.assertEqual(adopted.settings.preferred_league, "GREAT LEAGUE")
        self.assertEqual(gbl_meta.ACTIVE_CAP, 1500)

    def test_nothing_else_about_the_profile_changes(self) -> None:
        original = profile()
        adopted = gbl_strategy.adopt_league(original, "ULTRA LEAGUE")
        self.assertIs(adopted.team, original.team)
        self.assertEqual(
            adopted.settings.switch_cooldown_seconds,
            original.settings.switch_cooldown_seconds,
        )
        self.assertEqual(original.settings.preferred_league, "Master League")


class AndroidLeagueRotationTests(unittest.IsolatedAsyncioTestCase):
    """The moto/razr/ph-1 half of the 16 Sep stall."""

    def setUp(self) -> None:
        self.addCleanup(gbl_meta.set_active_cap, gbl_meta.ACTIVE_CAP)

    async def run_reads(self, reads, stops=False):
        """Play against a scripted sequence of screen reads.

        The list is exhausted by design: the loop only stops on its own when a
        battle is played, so the last read raises and the caller inspects the
        log up to that point. A caller passing ``stops`` expects the leg to
        end on its own before the reads run out.
        """
        stop = RuntimeError("out of scripted reads")
        device = SimpleNamespace(
            config={"GBL_MOVE_BTN": [658, 1200]},
            label="moto-g",
            serial="ZY22K9WZH9",
            display_id=None,
        )
        messages: list[str] = []
        with (
            patch.object(gbl, "read_screen",
                         AsyncMock(return_value=as_frame(frame(720, 1600)))),
            patch.object(gbl, "smart_screen_state",
                         AsyncMock(side_effect=list(reads) + [stop])),
            patch.object(gbl, "read_screen_state", lambda _frame: ("unknown", None)),
            patch.object(gbl, "loading_screen", lambda _frame: False),
            patch.object(gbl, "prepare_best_party", AsyncMock(return_value=None)),
            patch.object(gbl, "fast_attack", AsyncMock()),
            patch.object(gbl, "tap", AsyncMock()),
            patch.object(gbl, "send_input", AsyncMock()) as self.send_input,
            patch.object(gbl, "wait", AsyncMock()),
            patch.object(gbl, "log",
                         lambda _device, message: messages.append(message)),
        ):
            if stops:
                await gbl.play_device(device, 1, profile())
            else:
                with self.assertRaises(RuntimeError):
                    await gbl.play_device(device, 1, profile())
        return messages

    @staticmethod
    def league_read(label="GREAT LEAGUE", y=600):
        """One league list, read twice: the settle check reads it again."""
        return [("league", [360, y], label, CARDS)] * 2

    @staticmethod
    def party_read(times=2):
        """A party screen. The first read of one is spent building the party."""
        return [("pill", [360, 1200], None, PARTY)] * times

    async def test_a_league_that_is_not_on_the_list_is_given_up_on(self) -> None:
        messages = await self.run_reads(
            self.league_read()
            + self.party_read()
            + self.league_read()
            + self.party_read()
        )
        self.assertTrue(
            any("is not on today's league list" in line for line in messages),
            messages,
        )
        # And having given up on it, the set it opens is played rather than
        # backed out of: no second mismatch after the fallback.
        after = messages.index(
            next(line for line in messages if "is not on today's" in line))
        self.assertFalse(
            any("backing out to the league list" in line
                for line in messages[after:]),
            messages[after:],
        )
        self.assertEqual(gbl_meta.ACTIVE_CAP, 1500)

    async def test_one_mismatched_read_is_not_enough(self) -> None:
        """A single chooser pass could be a card the list had not drawn yet."""
        messages = await self.run_reads(
            self.league_read() + self.party_read()
        )
        self.assertFalse(
            any("is not on today's league list" in line for line in messages),
            messages,
        )
        self.assertTrue(
            any("backing out to the league list" in line for line in messages),
            messages,
        )

    async def test_a_set_that_cannot_be_left_is_played_rather_than_refused(
        self,
    ) -> None:
        """The card says Master, the set says Great: the door is not working.

        This is the other way into the same stall -- the league list keeps
        naming the configured league, so the preference is never given up, and
        every exit lands back on a Great League party screen.
        """
        reads = []
        for _ in range(gbl.LEAGUE_EXIT_LIMIT + 2):
            reads += self.league_read(label="MASTER LEAGUE")
            reads += self.party_read()
        messages = await self.run_reads(reads)
        self.assertTrue(
            any("rather than none at all" in line for line in messages), messages)
        self.assertFalse(
            any("stopping rather than playing" in line for line in messages),
            messages,
        )

    async def test_a_stray_set_after_the_league_was_played_is_not_adopted(
        self,
    ) -> None:
        """The 23 Sep moto-g: four Master battles, then a Retro Cup party.

        The configured league had just been played, so it is on the list;
        the Retro Cup set was a stray tap, and a door that would not open it
        is no reason to play Retro Cup for the rest of the day.
        """
        master_party = boxes(
            "CHOOSE YOUR PARTY", "MASTER LEAGUE: MEGA EDITION", "USE THIS PARTY")
        retro_party = boxes("CHOOSE YOUR PARTY", "RETRO CUP", "USE THIS PARTY")
        reads = (
            self.league_read(label="Master League: Mega")
            + [("pill", [360, 1200], None, master_party)] * 2
            + [("pill", [360, 1200], None, retro_party)]
            * (2 * gbl.LEAGUE_EXIT_LIMIT_PLAYED)
        )
        messages = await self.run_reads(reads, stops=True)
        self.assertFalse(
            any("rather than none at all" in line for line in messages), messages)
        self.assertTrue(
            any("stopping rather than playing" in line for line in messages),
            messages,
        )
        # The door failed on the moto-g, so the back key takes turns with it.
        self.send_input.assert_any_await(
            unittest.mock.ANY, "input keyevent KEYCODE_BACK")


class IOSLeagueRotationTests(unittest.IsolatedAsyncioTestCase):
    """The same refusal, on the iPhone loop."""

    def setUp(self) -> None:
        self.addCleanup(gbl_meta.set_active_cap, gbl_meta.ACTIVE_CAP)

    async def run_reads(self, reads, scroll=None):
        stop = RuntimeError("out of scripted reads")
        scroll = scroll or AsyncMock()
        device = screens.IOSBattleStartingTests.device()
        image = Image.new("RGB", (750, 1334), (60, 90, 50))
        device.screenshot = AsyncMock(return_value=image)
        messages: list[str] = []
        with (
            patch.object(gbl_ios, "smart_screen_state",
                         AsyncMock(side_effect=list(reads) + [stop])),
            patch.object(gbl_ios, "prepare_best_party", AsyncMock(return_value=None)),
            patch.object(gbl_ios, "fast_attack", AsyncMock()),
            patch.object(gbl_ios, "wait", AsyncMock()),
            patch.object(gbl_ios.gbl_vision, "recognize", return_value=[]),
            patch.object(gbl_ios.gbl_home_recovery, "on_map", return_value=False),
            patch.object(gbl_ios.gbl_home_recovery, "main_menu_open",
                         return_value=False),
            patch.object(gbl_ios, "scroll_league_list", scroll),
            patch.object(gbl_ios, "log",
                         lambda _device, message: messages.append(message)),
        ):
            with self.assertRaises(RuntimeError):
                await gbl_ios.play_device(device, 1, profile())
        return messages

    async def test_a_set_that_cannot_be_left_is_played_rather_than_refused(
        self,
    ) -> None:
        reads = [("pill", [284, 1192], None, PARTY)] * (
            2 * gbl_ios.LEAGUE_EXIT_LIMIT + 4)
        messages = await self.run_reads(reads)
        self.assertTrue(
            any("rather than none at all" in line for line in messages), messages)
        self.assertFalse(
            any("stopping rather than playing" in line for line in messages),
            messages,
        )
        self.assertEqual(gbl_meta.ACTIVE_CAP, 1500)

    async def test_ios_league_that_is_not_on_the_list_is_given_up_on(self) -> None:
        # A list that never moves is at its top and then at its bottom.
        reads = (
            [("league_scroll", None, None, CARDS)] * (2 * gbl_ios.LEAGUE_END_READS + 1)
            + [("league", [408, 591], "GREAT LEAGUE", CARDS)]
            + [("pill", [360, 1200], None, PARTY)] * 2
        )
        messages = await self.run_reads(reads)
        self.assertTrue(
            any(
                "never appeared in the league list; falling back to the easiest available league"
                in line
                for line in messages
            ),
            messages,
        )
        self.assertEqual(gbl_meta.ACTIVE_CAP, 1500)

    async def test_ios_hunt_reaches_a_card_above_where_the_list_opened(
        self,
    ) -> None:
        """24 Sep: the SE's list opened part-way down.  Two scrolls down and
        two "back up" returned it to exactly where it began, the Master League:
        Mega card above that was never seen, and the fallback played Retro Cup.
        The hunt now climbs to the top first."""
        reads = [
            ("league_scroll", None, None, boxes("RETRO CUP", "LITTLE CUP")),
            ("league_scroll", None, None, boxes("ULTRA LEAGUE", "RETRO CUP")),
            ("league", [408, 591], "MASTER LEAGUE: MEGA",
             boxes("CHOOSE YOUR LEAGUE", "MASTER LEAGUE: MEGA")),
        ] + [("pill", [360, 1200], None, PARTY)] * 2
        scroll = AsyncMock()
        messages = await self.run_reads(reads, scroll)
        self.assertFalse(
            any("never appeared" in line for line in messages), messages)
        self.assertTrue(
            any("taking Master League" in line for line in messages), messages)
        self.assertEqual(
            [call.kwargs["back"] for call in scroll.await_args_list], [True, True])

    async def test_ios_hunt_turns_down_only_once_the_top_stops_moving(
        self,
    ) -> None:
        top = boxes("CHOOSE YOUR LEAGUE", "GREAT LEAGUE")
        reads = [("league_scroll", None, None, top)] * (gbl_ios.LEAGUE_END_READS + 1) + [
            ("league", [408, 591], "MASTER LEAGUE: MEGA",
             boxes("ULTRA LEAGUE", "MASTER LEAGUE: MEGA")),
        ] + [("pill", [360, 1200], None, PARTY)] * 2
        scroll = AsyncMock()
        messages = await self.run_reads(reads, scroll)
        self.assertFalse(
            any("never appeared" in line for line in messages), messages)
        self.assertEqual(
            [call.kwargs["back"] for call in scroll.await_args_list],
            [True] * gbl_ios.LEAGUE_END_READS + [False],
        )

class UpcomingPreviewTests(unittest.IsolatedAsyncioTestCase):
    """Next week's line-up is drawn like a card and cannot be tapped.

    Under the week's playable cards the chooser prints "SEE WHAT'S COMING NEXT
    ON <date>!" and then the next rotation, greyed out.  On 16 Sep the SE
    scrolled past a perfectly good Great League card to the Master League: Mega
    Edition preview below that line and tapped it every five seconds for the
    rest of the day.
    """

    DIVIDER = "SEE WHAT'S COMING NEXT ON SEPTEMBER 22!"

    @classmethod
    def chooser(cls, height: int) -> list[gbl_vision.OCRBox]:
        """A chooser with one live card, the divider, and one preview card."""
        def box(text: str, fraction: float) -> gbl_vision.OCRBox:
            return gbl_vision.OCRBox(text, 1.0, 100, int(height * fraction), 400, 30)

        return [
            box("CHOOSE YOUR LEAGUE", 0.32),
            box("Great League", 0.42),
            box(cls.DIVIDER, 0.60),
            box("Master League: Mega Edition", 0.70),
        ]

    def test_the_divider_is_found_by_its_own_words(self) -> None:
        boxes = self.chooser(1334)
        self.assertEqual(
            gbl_vision.upcoming_league_divider_y(boxes), int(1334 * 0.60))
        self.assertIsNone(
            gbl_vision.upcoming_league_divider_y(
                [b for b in boxes if b.text != self.DIVIDER]))

    async def ios_state(self, league: str):
        image = Image.new("RGB", (750, 1334), (60, 90, 50))
        with (
            patch.object(gbl_ios.gbl_vision, "recognize",
                         return_value=self.chooser(image.height)),
            # A flat test image reads as a battlefield, and a battlefield skips
            # OCR altogether.
            patch.object(gbl_ios.android_gbl, "read_screen_state",
                         lambda _frame: ("unknown", None)),
        ):
            return await gbl_ios.smart_screen_state(image, profile(league))

    async def test_ios_will_not_take_a_preview_of_the_wanted_league(self) -> None:
        state, point, _label, _boxes = await self.ios_state("Master League")
        self.assertEqual(state, "league_scroll")
        self.assertIsNone(point)

    async def test_ios_takes_the_live_card_when_asked_for_the_easiest(self) -> None:
        state, point, label, _boxes = await self.ios_state("auto")
        self.assertEqual(state, "league")
        self.assertEqual(label, "Great League")
        self.assertLess(point[1], int(1334 * 0.60))

    async def test_android_will_not_take_a_preview_of_the_wanted_league(self) -> None:
        image = Image.new("RGB", (720, 1600), (60, 90, 50))
        with (
            patch.object(gbl.gbl_vision, "recognize",
                         return_value=self.chooser(image.height)),
            patch.object(gbl, "frame_image", lambda _frame: image),
            patch.object(gbl, "read_screen_state", lambda _frame: ("league", [360, 1100])),
        ):
            state, point, label, _boxes = await gbl.smart_screen_state(
                as_frame(frame(720, 1600)), profile("Master League"))
        # The preview sits at 0.70 of the screen; nothing below the divider is
        # worth a tap, so the live card above it is the only answer -- the
        # preference is given up by the loop, once it reads what it opened.
        self.assertEqual(state, "league")
        self.assertEqual(label, "Great League")
        self.assertLess(point[1], int(1600 * 0.60))


if __name__ == "__main__":
    unittest.main()
