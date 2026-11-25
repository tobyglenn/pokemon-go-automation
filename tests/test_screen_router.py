from __future__ import annotations

from tests import support as _test_support

import argparse
import asyncio
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from sources import catch_rewards, claim_research_android, gbl_vision, pokemon_fleet, screen_router


def box(text: str) -> gbl_vision.OCRBox:
    return gbl_vision.OCRBox(text, 0.9, 10, 10, 100, 20)


def spec(name: str, platform: str, identifier: str) -> pokemon_fleet.DeviceSpec:
    return SimpleNamespace(name=name, platform=platform, identifier=identifier, enabled=True)


class ClassifyTests(unittest.TestCase):
    """Each screen names one job; the ones that look alike are told apart."""

    def read(
        self,
        words: tuple[str, ...] = (),
        *,
        card: bool = False,
        gopass: object | None = None,
        claims: tuple[str, ...] = (),
        ball: bool = False,
        plate: bool = False,
        feeding: bool = False,
        gbl_card: bool = False,
        friends_header: bool = False,
        on_map: bool = False,
    ) -> str | None:
        claim_boxes = [claim_research_android.ClaimButton(label, [1, 1], 0, 2) for label in claims]
        with ExitStack() as stack:
            for target, name, value in (
                (screen_router.catch_awarded_android, "card_from_frame", object() if card else None),
                (screen_router.claim_gopass, "read_pass_screen", gopass),
                (screen_router.claim_research_android, "claim_buttons", claim_boxes),
                (screen_router.claim_research_android, "plate_visible", plate),
                (screen_router.berry_android, "encounter_showing", ball),
                (screen_router.gbl_vision, "recognize", [box(word) for word in words]),
                (screen_router.gbl_vision, "gbl_card_visible", gbl_card),
                (screen_router.gift_android, "is_friends_panel", friends_header),
                (screen_router.gbl_home_recovery, "on_map", on_map),
            ):
                stack.enter_context(patch.object(target, name, lambda *_a, _v=value, **_k: _v))
            return screen_router.classify(Image.new("RGB", (20, 40)), lambda _frame: feeding)

    def test_an_award_card_is_a_catch(self) -> None:
        self.assertEqual(self.read(card=True), screen_router.CATCH)

    def test_a_go_pass_with_claimable_tiles_is_a_catch(self) -> None:
        from sources import claim_gopass

        active = claim_gopass.PassScreen([], 2, [50, 800], "all", [400, 200], False)
        self.assertEqual(self.read(gopass=active), screen_router.CATCH)

    def test_an_empty_go_pass_is_left_alone(self) -> None:
        from sources import claim_gopass

        empty = claim_gopass.PassScreen([], 0, None, "claimable", [400, 200], False)
        self.assertIsNone(self.read(gopass=empty))

    def test_a_research_claim_is_a_catch(self) -> None:
        self.assertEqual(self.read(claims=("CLAIM REWARD",)), screen_router.CATCH)

    def test_the_rank_reward_claim_belongs_to_gbl(self) -> None:
        self.assertEqual(
            self.read(("GO BATTLE LEAGUE", "CLAIM RANK REWARDS!"), claims=("CLAIM RANK REWARDS!",)),
            screen_router.GBL,
        )

    def test_an_open_encounter_is_a_catch(self) -> None:
        self.assertEqual(self.read(("Eevee",), ball=True, plate=True, feeding=True), screen_router.CATCH)

    def test_a_feeding_screen_without_an_encounter_is_berries(self) -> None:
        self.assertEqual(self.read(feeding=True), screen_router.BERRIES)

    def test_the_party_picker_is_gbl_though_it_shows_a_ball(self) -> None:
        self.assertEqual(self.read(("CHOOSE YOUR PARTY",), ball=True, plate=True), screen_router.GBL)

    def test_the_map_is_gbl_even_when_the_feed_check_passes(self) -> None:
        self.assertEqual(self.read(on_map=True, feeding=True), screen_router.GBL)

    def test_an_encounter_is_not_mistaken_for_the_map(self) -> None:
        self.assertEqual(
            self.read(ball=True, plate=True, on_map=True), screen_router.CATCH
        )

    def test_the_battle_card_is_gbl(self) -> None:
        self.assertEqual(self.read(gbl_card=True), screen_router.GBL)

    def test_a_trade_picker_is_left_alone(self) -> None:
        words = ("SPECIAL TRADES REMAINING: 1", "Bulbasaur", "CP 693")
        self.assertIsNone(self.read(words, ball=True, plate=True, friends_header=True))

    def test_the_friends_list_is_gifts(self) -> None:
        self.assertEqual(self.read(("FRIENDS", "ADD FRIEND", "SEARCH")), screen_router.GIFTS)

    def test_a_friend_profile_is_gifts(self) -> None:
        self.assertEqual(self.read(("SEND GIFT", "TRADE", "BATTLE")), screen_router.GIFTS)

    def test_a_bright_sky_alone_is_not_the_friends_list(self) -> None:
        self.assertIsNone(self.read(("Weather",), friends_header=True))


class CommandTests(unittest.TestCase):
    def args(self, spend: int | None = None) -> argparse.Namespace:
        return argparse.Namespace(spend=spend)

    def test_each_job_runs_locally_on_its_phones(self) -> None:
        phones = [spec("android-one", "android", "SERIAL1"), spec("ios-one", "ios", "UDID")]
        self.assertEqual(
            screen_router.command(screen_router.GBL, phones, self.args()),
            ["gbl_day.py", "--local", "--devices", "android-one", "ios-one"],
        )
        self.assertEqual(
            screen_router.command(screen_router.BERRIES, phones[:1], self.args(3)),
            ["scripts/berry.py", "--local", "--devices", "android-one", "--spend", "3"],
        )
        self.assertEqual(
            screen_router.command(screen_router.GIFTS, phones[1:], self.args()),
            ["gift.py", "--local", "--all", "--gifts-only", "--devices", "ios-one"],
        )

    def test_catch_names_androids_by_serial(self) -> None:
        phones = [spec("android-one", "android", "SERIAL1"), spec("ios-one", "ios", "UDID")]
        self.assertEqual(
            screen_router.command(screen_router.CATCH, phones, self.args()),
            ["catch.py", "--devices", "SERIAL1", "--devices", "ios-one"],
        )


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def run_router(self, arguments: list[str], readings: dict, launch=None):
        phones = {
            "android-one": spec("android-one", "android", "SERIAL1"),
            "ios-one": spec("ios-one", "ios", "UDID"),
            "android-two": spec("android-two", "android", "ELSEWHERE"),
        }
        launched = AsyncMock(return_value=0, side_effect=launch)

        async def read(phone, _androids):
            reading = readings[phone.name]
            if len(reading) == 3:
                # A third field is how long that phone's look takes.
                await asyncio.sleep(reading[2])
                return reading[:2]
            return reading

        with patch.object(screen_router.pokemon_fleet, "load_fleet", lambda _p: SimpleNamespace(devices=phones)), \
                patch.object(screen_router, "attached_androids", AsyncMock(return_value={"SERIAL1": object()})), \
                patch.object(screen_router.pokemon_fleet, "ios_connected_udids", lambda: {"UDID"}), \
                patch.object(screen_router, "read_screen", read), \
                patch.object(screen_router, "launch", launched):
            status = await screen_router.run(screen_router.parse_args(arguments))
        return status, launched

    async def test_a_plan_reads_but_starts_nothing(self) -> None:
        status, launched = await self.run_router(
            ["--plan"], {"android-one": ("gbl", "GBL"), "ios-one": ("catch", "catching")}
        )
        self.assertEqual(status, 0)
        launched.assert_not_awaited()

    async def test_phones_on_one_screen_share_one_command(self) -> None:
        _status, launched = await self.run_router(
            [], {"android-one": ("gbl", "GBL"), "ios-one": ("gbl", "GBL")}
        )
        launched.assert_awaited_once_with(["gbl_day.py", "--local", "--devices", "android-one", "ios-one"])

    async def test_a_slow_iphone_does_not_hold_back_the_other_phones(self) -> None:
        # The iPhone's look is still retrying behind a Face ID prompt when the
        # Android has been read; the Android's day must not wait for it.
        started: list[tuple[float, list[str]]] = []
        loop = asyncio.get_running_loop()
        begin = loop.time()

        async def record(arguments):
            started.append((loop.time() - begin, arguments))
            return 0

        with patch.object(screen_router, "GROUP_WINDOW_SECONDS", 0.05):
            _status, launched = await self.run_router(
                [], {"android-one": ("gbl", "GBL"), "ios-one": ("gbl", "GBL", 0.3)},
                launch=record,
            )
        self.assertEqual(
            [arguments for _at, arguments in started],
            [
                ["gbl_day.py", "--local", "--devices", "android-one"],
                ["gbl_day.py", "--local", "--devices", "ios-one"],
            ],
        )
        self.assertLess(started[0][0], 0.2)
        self.assertGreaterEqual(started[1][0], 0.3)

    async def test_an_unrecognised_screen_is_left_alone(self) -> None:
        _status, launched = await self.run_router(
            [], {"android-one": (None, "left alone"), "ios-one": ("gifts", "gifts")}
        )
        launched.assert_awaited_once_with(
            ["gift.py", "--local", "--all", "--gifts-only", "--devices", "ios-one"]
        )

    async def test_a_held_phone_is_reported_not_read(self) -> None:
        phone = spec("android-one", "android", "SERIAL1")

        @contextmanager
        def held(_specs):
            raise pokemon_fleet.FleetError("android-one is already controlled by pid 1")
            yield

        with patch.object(screen_router.pokemon_fleet, "acquire_device_locks", held):
            job, note = await screen_router.read_screen(phone, {})
        self.assertIsNone(job)
        self.assertIn("already controlled", note)


class EncounterModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_encounter_mode_waits_through_empty_looks_and_counts_catches(self) -> None:
        device = type("Phone", (), {"label": "fake-g", "viewport": (720, 1600)})()
        args = catch_rewards.parse_args(
            ["--artifacts", tempfile.mkdtemp(), "--mode", "encounter", "--count", "2"]
        )
        catch_one = AsyncMock(side_effect=[False, True, True])
        with patch.object(catch_rewards.catch_awarded_android, "catch_one", catch_one), \
                patch.object(catch_rewards.catch_awarded_android, "clear_catch_screens", AsyncMock()), \
                patch.object(catch_rewards, "detect_mode", AsyncMock()) as detected:
            self.assertEqual(await catch_rewards.run_phone(device, args), 0)
        self.assertEqual(catch_one.await_count, 3)
        detected.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
