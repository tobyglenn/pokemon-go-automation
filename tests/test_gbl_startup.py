"""Exercise the iOS entry path, including the first real screen probe."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from sources import gbl_ios


class IOSStartupTests(unittest.IsolatedAsyncioTestCase):
    async def run_startup(self, strategy_key, dry_run):
        config = SimpleNamespace(ios_device={"udid": "example-device"}, battles=1)
        if strategy_key is not None:
            config.ios_device["strategy_key"] = strategy_key
        args = SimpleNamespace(check=False, dry_run=dry_run, battles=1)
        device = SimpleNamespace(
            screenshot=AsyncMock(return_value=Image.new("RGB", (100, 200))),
            quit=AsyncMock(),
        )
        profile = object()
        with (
            patch.object(gbl_ios.gbl_strategy, "load_strategy_profile", return_value=profile) as load,
            patch.object(gbl_ios.IOSGBLDevice, "connect", return_value=device),
            patch.object(gbl_ios, "probe", AsyncMock(return_value="pill")) as probe,
            patch.object(gbl_ios.android_gbl, "teal_result_ready", return_value=False),
            patch.object(gbl_ios, "play_device", AsyncMock(return_value=1)) as play,
            patch.object(gbl_ios.ios_wda_cleanup, "stop_wda_runner") as stop,
        ):
            self.assertEqual(await gbl_ios.async_main(args, config), 0)
        load.assert_called_once_with(device_name=strategy_key or "ios-one")
        self.assertIs(probe.await_args.args[2], profile)
        device.quit.assert_awaited_once()
        stop.assert_not_called()
        if dry_run:
            play.assert_not_awaited()
        else:
            play.assert_awaited_once_with(device, 1, profile)

    async def test_configured_profile_reaches_probe_and_battle(self):
        await self.run_startup("example-strategy", False)

    async def test_missing_profile_uses_fallback_in_dry_run(self):
        await self.run_startup(None, True)

    async def test_empty_profile_uses_fallback(self):
        await self.run_startup("", False)

    async def test_profile_failure_does_not_open_device_session(self):
        config = SimpleNamespace(ios_device={})
        with (
            patch.object(gbl_ios.gbl_strategy, "load_strategy_profile", side_effect=ValueError("bad profile")),
            patch.object(gbl_ios.IOSGBLDevice, "connect") as connect,
        ):
            with self.assertRaisesRegex(ValueError, "bad profile"):
                await gbl_ios.async_main(SimpleNamespace(), config)
        connect.assert_not_called()
