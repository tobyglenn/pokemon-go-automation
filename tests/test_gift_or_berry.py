from __future__ import annotations

from tests import support as _test_support

import argparse
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sources import berry_android, berry_ios, excellent_throw_android, pokemon_fleet


def spec(name: str, platform: str) -> pokemon_fleet.DeviceSpec:
    return pokemon_fleet.DeviceSpec(
        name=name,
        platform=platform,
        enabled=True,
        config={"platform": platform, "serial": f"{name}-serial", "operations": {"gifts": {}, "berries": {}}},
        base_dir=Path("/tmp"),
    )


def gift_args(**overrides) -> argparse.Namespace:
    values = dict(
        all=True, count=None, max_cycles=100, android_count=None, no_guard=False,
        gifts_only=False, spend=None, spend_overrides={},
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class AndroidRoutingTests(unittest.IsolatedAsyncioTestCase):
    """The screen each phone is parked on picks gifts or berries."""

    async def route(self, feeding: set[str], **overrides):
        phones = [spec("moto-g", "android"), spec("razr", "android")]
        gift_process = AsyncMock()
        berry_process = AsyncMock()
        gifters = AsyncMock(side_effect=lambda specs: [s.name for s in specs])
        feeders = AsyncMock(side_effect=lambda specs: [s.name for s in specs])
        looked = AsyncMock(return_value={f"{name}-serial" for name in feeding})
        with patch.object(pokemon_fleet, "android_feed_screens", looked), \
                patch.object(pokemon_fleet, "connected_android_gifters", gifters), \
                patch.object(pokemon_fleet, "connected_android_berry_devices", feeders), \
                patch("sources.gift_android.gift_process", gift_process), \
                patch("sources.berry_android.berry_process", berry_process):
            await pokemon_fleet.run_gifts(phones, gift_args(**overrides))
        return looked, gift_process, berry_process

    async def test_a_phone_on_a_gym_feeds_while_the_rest_gift(self) -> None:
        _looked, gift_process, berry_process = await self.route({"razr"})
        self.assertEqual(gift_process.await_args.args[0], ["moto-g"])
        self.assertEqual(berry_process.await_args.args[0], ["razr"])

    async def test_no_gym_screen_means_every_phone_gifts(self) -> None:
        _looked, gift_process, berry_process = await self.route(set())
        self.assertEqual(gift_process.await_args.args[0], ["moto-g", "razr"])
        berry_process.assert_not_awaited()

    async def test_every_phone_on_a_gym_sends_no_gifts(self) -> None:
        _looked, gift_process, berry_process = await self.route({"moto-g", "razr"})
        gift_process.assert_not_awaited()
        self.assertEqual(berry_process.await_args.args[0], ["moto-g", "razr"])

    async def test_gifts_only_does_not_look(self) -> None:
        looked, gift_process, berry_process = await self.route({"razr"}, gifts_only=True)
        looked.assert_not_awaited()
        self.assertEqual(gift_process.await_args.args[0], ["moto-g", "razr"])
        berry_process.assert_not_awaited()


class EncounterTests(unittest.TestCase):
    """An encounter passes the feeding-screen guard; its ball in hand is the tell."""

    def frame(self):
        return (750, 1334, 0, bytes(750 * 1334 * 4))

    def test_a_ball_in_hand_is_an_encounter(self) -> None:
        with patch.object(excellent_throw_android.excellent_throw_ios, "locate_throw_ball", lambda *_a: object()):
            self.assertTrue(berry_android.encounter_showing(*self.frame()))

    def test_no_ball_is_not_an_encounter(self) -> None:
        with patch.object(excellent_throw_android.excellent_throw_ios, "locate_throw_ball", lambda *_a: None):
            self.assertFalse(berry_android.encounter_showing(*self.frame()))


class IPhoneRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def route(self, berry_code: int) -> list[list[str]]:
        launched: list[list[str]] = []

        async def child(_label, arguments, *, accepted=(0,)):
            launched.append(list(arguments))
            return berry_code if "sources.berry_ios" in arguments else 0

        with patch.object(pokemon_fleet, "run_child", child), \
                patch.object(pokemon_fleet, "operation_config_path", lambda s, op: Path(f"/tmp/{op}.yaml")), \
                patch.object(pokemon_fleet, "operation_readiness", lambda s, op: "ready"):
            await pokemon_fleet.run_gifts([spec("iphone-second", "ios")], gift_args())
        return launched

    async def test_a_gym_screen_feeds_and_sends_no_gifts(self) -> None:
        launched = await self.route(0)
        self.assertEqual(len(launched), 1)
        self.assertIn("sources.berry_ios", launched[0])
        self.assertIn("--if-feed-screen", launched[0])

    async def test_any_other_screen_hands_the_phone_to_the_gifts(self) -> None:
        launched = await self.route(pokemon_fleet.IOS_NOT_A_FEED_SCREEN)
        self.assertEqual([command[2] for command in launched], ["sources.berry_ios", "sources.gift_ios"])

    def test_the_exit_code_is_the_berry_workers_own(self) -> None:
        self.assertEqual(pokemon_fleet.IOS_NOT_A_FEED_SCREEN, berry_ios.NOT_A_FEED_SCREEN)


if __name__ == "__main__":
    unittest.main()
