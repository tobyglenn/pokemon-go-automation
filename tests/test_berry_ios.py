from __future__ import annotations

from tests import support as _test_support

import argparse
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PIL import Image
import yaml

from sources import berry_android as android_berry
from sources import berry_ios


class FakeDriver:
    def get_window_rect(self):
        return {"width": 375, "height": 667}


class BlockingDevice:
    def __init__(self) -> None:
        self.screenshot_started = asyncio.Event()
        self.quit_called = False

    async def screenshot(self):
        self.screenshot_started.set()
        await asyncio.Event().wait()

    async def quit(self) -> None:
        self.quit_called = True


def runtime() -> berry_ios.RuntimeConfig:
    return berry_ios.RuntimeConfig(
        appium_server_url="http://127.0.0.1:4723",
        ios_device={"udid": "UDID", "team_id": "TEAM", "wda_bundle_id": "WDA"},
        coordinates={
            "BERRY_BTN": [187, 500],
            "NEXT_MON_FROM": [320, 310],
            "NEXT_MON_TO": [55, 310],
        },
        detection={"CARD_BAND_Y": [58, 86], "BERRY_DISC_Y": [840, 930]},
        delay_modifier=0,
        spend_cap=None,
    )


class ImageAnalysisTests(unittest.TestCase):
    def test_logical_and_screenshot_coordinates_round_trip(self) -> None:
        device = berry_ios.IOSBerryDevice(FakeDriver(), runtime())
        image = Image.new("RGB", (750, 1334))
        point = device.image_point([187, 500], image)
        self.assertEqual(point, [374, 1000])
        self.assertEqual(device.logical_point(point, image), [187, 500])

    def test_synthetic_feed_screen_and_berry_are_detected(self) -> None:
        device = berry_ios.IOSBerryDevice(FakeDriver(), runtime())
        image = Image.new("RGB", (750, 1334), (40, 60, 80))
        pixels = image.load()

        # Grey item-disc band with a mean inside Android's guarded range.
        for y in range(int(1334 * 0.840), int(1334 * 0.930) + 1):
            for x in range(int(750 * 0.063), int(750 * 0.185) + 1):
                pixels[x, y] = (150, 150, 150)

        # Exactly 80 bright samples in the defender-name fingerprint band.
        x0, x1 = android_berry.CARD_BAND_X
        y0, y1 = 0.058, 0.086
        for yi in range(android_berry.CARD_NY):
            y = int(1334 * (y0 + (y1 - y0) * yi / (android_berry.CARD_NY - 1)))
            for xi in range(10):
                x = int(750 * (x0 + (x1 - x0) * xi / (android_berry.CARD_NX - 1)))
                pixels[x, y] = (255, 255, 255)

        # Warm, vivid floor berry inside the configured anchor search box.
        for y in range(970, 1031):
            for x in range(345, 406):
                pixels[x, y] = (230, 30, 60)

        disc, ink, matched = berry_ios.frame_metrics(device, image)
        frame = berry_ios.raw_frame(image)
        anchor = device.image_point(device.config.coordinates["BERRY_BTN"], image)
        found = android_berry.find_berry(*frame, anchor)
        self.assertTrue(matched, (disc, ink))
        self.assertIsNotNone(found)


class ConfigTests(unittest.TestCase):
    def test_relative_appium_config_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ios.yaml").write_text(
                yaml.safe_dump(
                    {
                        "server_url": "http://127.0.0.1:4723",
                        "device": {
                            "udid": "UDID",
                            "team_id": "TEAM",
                            "wda_bundle_id": "WDA",
                        },
                    }
                )
            )
            (root / "berry.yaml").write_text(
                yaml.safe_dump(
                    {
                        "appium_config": "ios.yaml",
                        "coordinates": runtime().coordinates,
                        "detection": runtime().detection,
                    }
                )
            )
            loaded = berry_ios.load_runtime_config(root / "berry.yaml")
            self.assertEqual(loaded.ios_device["udid"], "UDID")

    def test_placeholder_coordinate_is_rejected(self) -> None:
        with self.assertRaisesRegex(berry_ios.IOSBerryError, "placeholder"):
            berry_ios.validate_point("BERRY_BTN", [0, 0])


class ConnectionRecoveryTests(unittest.TestCase):
    def test_stale_wda_error_detection(self) -> None:
        exc1 = Exception("The previously found element Application 'com.nianticlabs.pokemongo' is not present in the current view anymore. Original error: Not authorized for performing UI testing actions.")
        exc2 = Exception("random socket error")
        self.assertTrue(berry_ios.stale_wda_error(exc1))
        self.assertFalse(berry_ios.stale_wda_error(exc2))

    def test_stale_wda_triggers_activate_app(self) -> None:
        driver = mock.Mock()
        stale_exc = Exception("Not authorized for performing UI testing actions")
        driver.get_window_rect.side_effect = [stale_exc, {"width": 375, "height": 667}]

        dev = berry_ios.IOSBerryDevice(driver, runtime())
        self.assertEqual(dev.viewport, {"width": 375, "height": 667})
        driver.execute_script.assert_called_once_with(
            "mobile: activateApp", {"bundleId": "com.nianticlabs.pokemongo"}
        )


class FeedDefenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_picker_selects_cheap_berry_and_feeds_until_refused(self) -> None:
        device = mock.Mock(label="iPhone")
        image = Image.new("RGB", (20, 20))
        offer = ([10, 10], image, 1.0)

        with (
            mock.patch.object(
                berry_ios,
                "berry_offered",
                new=mock.AsyncMock(side_effect=[offer, offer, None]),
            ),
            mock.patch.object(
                berry_ios,
                "select_cheap_berry",
                new=mock.AsyncMock(return_value=("razz", android_berry.PICKER_RECHECK)),
            ) as select,
            mock.patch.object(berry_ios, "feed_berry", new=mock.AsyncMock()) as feed,
            mock.patch.object(berry_ios, "wait", new=mock.AsyncMock()),
        ):
            fed, stop, chosen = await berry_ios.feed_defender(device, None, None)

        self.assertEqual((fed, stop, chosen), (1, None, "razz"))
        select.assert_awaited_once()
        feed.assert_awaited_once()

    async def test_premium_only_stops_without_tapping(self) -> None:
        device = mock.Mock(label="iPhone")
        image = Image.new("RGB", (20, 20))
        offer = ([10, 10], image, 0.0)

        with (
            mock.patch.object(
                berry_ios, "berry_offered", new=mock.AsyncMock(return_value=offer)
            ),
            mock.patch.object(
                berry_ios,
                "select_cheap_berry",
                new=mock.AsyncMock(
                    return_value=(None, android_berry.PICKER_RECHECK)
                ),
            ),
            mock.patch.object(berry_ios, "feed_berry", new=mock.AsyncMock()) as feed,
            mock.patch.object(berry_ios, "wait", new=mock.AsyncMock()),
        ):
            fed, stop, chosen = await berry_ios.feed_defender(device, "razz", None)

        self.assertEqual((fed, stop, chosen), (0, "premium", None))
        feed.assert_not_awaited()

    async def test_disabled_picker_means_defender_refused_without_tapping(self) -> None:
        device = mock.Mock(label="iPhone")
        image = Image.new("RGB", (20, 20))
        offer = ([10, 10], image, 0.0)

        with (
            mock.patch.object(
                berry_ios, "berry_offered", new=mock.AsyncMock(return_value=offer)
            ),
            mock.patch.object(
                berry_ios,
                "select_cheap_berry",
                new=mock.AsyncMock(
                    return_value=(
                        android_berry.PICKER_UNAVAILABLE,
                        android_berry.PICKER_RECHECK,
                    )
                ),
            ),
            mock.patch.object(berry_ios, "feed_berry", new=mock.AsyncMock()) as feed,
            mock.patch.object(berry_ios, "wait", new=mock.AsyncMock()),
        ):
            fed, stop, chosen = await berry_ios.feed_defender(device, "razz", None)

        self.assertEqual((fed, stop, chosen), (0, None, "razz"))
        feed.assert_not_awaited()

    async def test_briefly_disabled_picker_keeps_feeding(self) -> None:
        """A greyed disc for one look is the feed animation, not a full defender.

        The disc greys out for a moment after every berry, so a defender that
        was still being offered one used to stop at exactly PICKER_RECHECK fed
        and be picked up again only by a swipe away and back.
        """
        device = mock.Mock(label="iPhone")
        image = Image.new("RGB", (20, 20))
        offer = ([10, 10], image, 0.0)

        with (
            mock.patch.object(
                berry_ios,
                "berry_offered",
                new=mock.AsyncMock(side_effect=[offer, offer, offer, None]),
            ),
            mock.patch.object(
                berry_ios,
                "select_cheap_berry",
                new=mock.AsyncMock(
                    side_effect=[
                        (android_berry.PICKER_UNAVAILABLE, android_berry.PICKER_RECHECK),
                        ("razz", android_berry.PICKER_RECHECK),
                    ]
                ),
            ),
            mock.patch.object(berry_ios, "feed_berry", new=mock.AsyncMock()) as feed,
            mock.patch.object(berry_ios, "wait", new=mock.AsyncMock()),
        ):
            fed, stop, chosen = await berry_ios.feed_defender(device, "razz", None)

        self.assertEqual((fed, stop, chosen), (1, None, "razz"))
        feed.assert_awaited_once()


class SwipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_swipe_uses_carousel_direction(self) -> None:
        driver = mock.Mock()
        driver.get_window_rect.return_value = {"width": 375, "height": 667}
        device = berry_ios.IOSBerryDevice(driver, runtime())

        await device.swipe([320, 310], [55, 310], native=True)

        driver.execute_script.assert_called_once_with(
            "mobile: swipe", {"direction": "left"}
        )


class TraversalTests(unittest.IsolatedAsyncioTestCase):
    async def test_rechecked_card_does_not_inflate_unique_roster(self) -> None:
        image = Image.new("RGB", (20, 20))
        device = mock.Mock(label="iPhone")
        device.screenshot = mock.AsyncMock(return_value=image)
        card_a = [True, False, False]
        card_b = [False, True, False]
        offer = ([10, 10], image, 0.0)

        with (
            mock.patch.object(berry_ios, "frame_metrics", return_value=(150, 80, True)),
            mock.patch.object(berry_ios, "berry_gold_share", new=mock.AsyncMock(return_value=None)),
            mock.patch.object(
                berry_ios,
                "card_fingerprint",
                new=mock.AsyncMock(side_effect=[card_a, card_a, card_b]),
            ),
            mock.patch.object(
                berry_ios, "berry_offered", new=mock.AsyncMock(return_value=offer)
            ),
            mock.patch.object(
                berry_ios,
                "feed_defender",
                new=mock.AsyncMock(
                    side_effect=[(0, None, "razz"), (0, None, "razz"), (1, None, "razz")]
                ),
            ) as feed,
            mock.patch.object(berry_ios, "turn_card", new=mock.AsyncMock(return_value=True)),
        ):
            processed = await berry_ios.run_berries(device, spend_cap=1)

        self.assertEqual(processed, 2)
        self.assertEqual(feed.await_count, 3)

    async def test_zero_attempt_rechecks_trigger_reverse_sweep(self) -> None:
        image = Image.new("RGB", (20, 20))
        device = mock.Mock(label="iPhone")
        device.screenshot = mock.AsyncMock(return_value=image)
        card_a = [True, False, False]
        card_b = [False, True, False]
        offer = ([10, 10], image, 0.0)
        turn = mock.AsyncMock(return_value=True)

        with (
            mock.patch.object(berry_ios, "frame_metrics", return_value=(150, 80, True)),
            mock.patch.object(berry_ios, "berry_gold_share", new=mock.AsyncMock(return_value=None)),
            mock.patch.object(
                berry_ios,
                "card_fingerprint",
                new=mock.AsyncMock(
                    side_effect=[card_a, card_a, card_a, card_a, card_a, card_b]
                ),
            ),
            mock.patch.object(
                berry_ios, "berry_offered", new=mock.AsyncMock(return_value=offer)
            ),
            mock.patch.object(
                berry_ios,
                "feed_defender",
                new=mock.AsyncMock(
                    side_effect=[
                        (0, None, "razz"),
                        (0, None, "razz"),
                        (0, None, "razz"),
                        (0, None, "razz"),
                        (0, None, "razz"),
                        (1, None, "razz"),
                    ]
                ),
            ),
            mock.patch.object(berry_ios, "turn_card", new=turn),
        ):
            processed = await berry_ios.run_berries(device, spend_cap=1)

        self.assertEqual(processed, 2)
        self.assertTrue(
            any(call.args == (device, True) for call in turn.await_args_list),
            turn.await_args_list,
        )


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(berry_ios.ios_wda_cleanup, "stop_wda_runner", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_cancel_closes_appium_session(self) -> None:
        device = BlockingDevice()
        args = argparse.Namespace(
            check=False,
            carousel_check=False,
            picker_check=False,
            dry_run=False,
            spend=None,
            screenshot=Path("unused.png"),
            fleet_child=False,
        )

        with mock.patch.object(
            berry_ios, "gift_runner_pids", return_value=[]
        ), mock.patch.object(
            berry_ios.IOSBerryDevice, "connect", return_value=device
        ):
            task = asyncio.create_task(berry_ios.async_main(args, runtime()))
            await device.screenshot_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(device.quit_called)


if __name__ == "__main__":
    unittest.main()
