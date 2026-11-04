"""Synthetic tests for the helpers the platform workers now share."""

import tests.support as _test_support  # noqa: F401

import asyncio
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sources import android_common, fleet_entrypoint, ios_wda_cleanup


class ConfigError(Exception):
    pass


class FakeDevice:
    def __init__(self, serial: str, content: str | None) -> None:
        self.serial = serial
        self.content = content
        self.pulled_to: Path | None = None
        self.shell_calls: list[str] = []

    async def pull(self, remote: str, local: Path) -> None:
        self.pulled_to = Path(local)
        if self.content is not None:
            Path(local).write_text(self.content)

    async def shell(self, command: str) -> str:
        self.shell_calls.append(command)
        return ""


class PullConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"POGO_STATE_DIR": self.state.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.state.cleanup)

    def pull(self, device: FakeDevice, **kwargs):
        return asyncio.run(android_common.pull_config(
            device, "/sdcard/x.yaml", "tmp_test_{}.yaml", {"A_BTN"}, ConfigError, **kwargs
        ))

    def test_loads_into_the_state_dir_and_cleans_up(self) -> None:
        device = FakeDevice("emulator-5554", "A_BTN: [1, 2]\nB_BTN: [3, 4]\n")
        config = self.pull(device)
        self.assertEqual(config, {"A_BTN": [1, 2], "B_BTN": [3, 4]})
        self.assertIs(device.config, config)
        self.assertTrue(str(device.pulled_to).startswith(self.state.name))
        self.assertIn("emulator_5554", device.pulled_to.name)
        self.assertFalse(device.pulled_to.exists())

    def test_missing_file_is_the_workers_error(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Found no config file"):
            self.pull(FakeDevice("serial", None))

    def test_bad_shapes_raise_rather_than_assert(self) -> None:
        for content, message in (
            ("- 1\n- 2\n", "Incorrect config file format"),
            ("B_BTN: [1, 2]\n", "Missing config key"),
            ("A_BTN: [1, 2, 3]\n", "Invalid coords format for A_BTN"),
            ("A_BTN: [1, 'x']\n", "Invalid coords format for A_BTN"),
        ):
            with self.subTest(content=content), self.assertRaisesRegex(ConfigError, message):
                self.pull(FakeDevice("serial", content))

    def test_coordinate_keys_limit_the_point_check(self) -> None:
        config = self.pull(
            FakeDevice("serial", "A_BTN: [1, 2]\nNOTE: text\n"), coordinate_keys={"A_BTN"}
        )
        self.assertEqual(config["NOTE"], "text")


class FramebufferTests(unittest.TestCase):
    def frame(self, width: int, height: int, header: int, fmt: int = 1, extra: bytes = b"") -> bytes:
        head = struct.pack("<III", width, height, fmt) + (b"\0" * (header - 12))
        return head + bytes(width * height * 4) + extra

    def test_lenient_accepts_both_headers(self) -> None:
        for header in (12, 16):
            parsed = android_common.parse_framebuffer(self.frame(2, 3, header))
            self.assertEqual(parsed[:3], (2, 3, header))

    def test_exact_rejects_trailing_bytes_and_other_formats(self) -> None:
        self.assertIsNotNone(android_common.parse_framebuffer(self.frame(2, 2, 16), exact=True))
        self.assertIsNone(android_common.parse_framebuffer(self.frame(2, 2, 16, extra=b"x"), exact=True))
        self.assertIsNone(android_common.parse_framebuffer(self.frame(2, 2, 16, fmt=4), exact=True))

    def test_short_data_is_none(self) -> None:
        self.assertIsNone(android_common.parse_framebuffer(b"tiny"))
        self.assertIsNone(android_common.parse_framebuffer(self.frame(4, 4, 12)[:-1]))


class AndroidInputTests(unittest.TestCase):
    def test_swipe_tap_is_a_tiny_swipe(self) -> None:
        device = FakeDevice("serial", None)
        asyncio.run(android_common.swipe_tap(device, [10, 20]))
        self.assertEqual(device.shell_calls, ["input swipe 10 20 11 21 100"])

    def test_workers_use_the_fleet_adb(self) -> None:
        from sources import berry_android, gift_android, luckytrash_android, pokemon_fleet, trade_android

        expected = pokemon_fleet.default_adb_binary()
        for module in (berry_android, gift_android, luckytrash_android, trade_android):
            self.assertEqual(module.ADB_BINARY, expected, module.__name__)


class IOSSessionTests(unittest.TestCase):
    DEVICE = {"udid": "UDID", "team_id": "TEAM", "wda_bundle_id": "wda.bundle"}

    def test_capabilities_carry_optional_settings(self) -> None:
        caps = ios_wda_cleanup.xcuitest_capabilities(
            {**self.DEVICE, "wda_local_port": 8200, "use_preinstalled_wda": True,
             "platform_version": "18.0", "derived_data_path": "/tmp/dd"},
            use_new_wda=True, show_xcode_log=True,
        )
        self.assertEqual(caps["appium:wdaLocalPort"], 8200)
        self.assertEqual(caps["appium:wdaRemotePort"], ios_wda_cleanup.WDA_DEFAULT_PORT)
        self.assertEqual(caps["appium:newCommandTimeout"], 3600)
        self.assertTrue(caps["appium:usePreinstalledWDA"])
        self.assertTrue(caps["appium:useNewWDA"])
        self.assertTrue(caps["appium:showXcodeLog"])
        self.assertEqual(caps["platformVersion"], "18.0")
        self.assertEqual(caps["appium:derivedDataPath"], "/tmp/dd")

    def test_minimal_device_omits_optional_keys(self) -> None:
        caps = ios_wda_cleanup.xcuitest_capabilities(self.DEVICE)
        for key in ("appium:wdaLocalPort", "appium:usePreinstalledWDA", "appium:showXcodeLog"):
            self.assertNotIn(key, caps)

    def test_start_session_retries_a_preinstalled_runner_once(self) -> None:
        attempts = iter([RuntimeError("runner busy"), "driver"])

        def create():
            value = next(attempts)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch.object(ios_wda_cleanup, "stop_wda_runner") as stop, \
                mock.patch.object(ios_wda_cleanup.time, "sleep"):
            driver = ios_wda_cleanup.start_session(
                create, {"appium:usePreinstalledWDA": True}, udid="UDID"
            )
        self.assertEqual(driver, "driver")
        stop.assert_called_once_with("UDID")

    def test_start_session_does_not_retry_without_a_preinstalled_runner(self) -> None:
        create = mock.Mock(side_effect=RuntimeError("no"))
        with mock.patch.object(ios_wda_cleanup, "stop_wda_runner") as stop:
            with self.assertRaises(RuntimeError):
                ios_wda_cleanup.start_session(create, {}, udid="UDID")
        stop.assert_not_called()
        self.assertEqual(create.call_count, 1)


class WorkerMarkerTests(unittest.TestCase):
    def test_either_marker_identifies_a_worker(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POKEMON_FLEET_CHILD", None)
            self.assertFalse(fleet_entrypoint.is_worker_invocation(["x.py"]))
            self.assertTrue(fleet_entrypoint.is_worker_invocation(["x.py", "--fleet-child"]))
        with mock.patch.dict(os.environ, {"POKEMON_FLEET_CHILD": "1"}):
            self.assertTrue(fleet_entrypoint.is_worker_invocation(["x.py"]))

    def test_worker_runs_main_and_never_fans_out(self) -> None:
        main = mock.Mock(return_value=3)
        with mock.patch.dict(os.environ, {"POKEMON_FLEET_CHILD": "1"}), \
                mock.patch.object(fleet_entrypoint, "run_operation") as route:
            self.assertEqual(fleet_entrypoint.run_worker_module(main, "gifts", "gift.py"), 3)
        route.assert_not_called()

    def test_direct_invocation_routes_through_the_fleet(self) -> None:
        main = mock.Mock()
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(fleet_entrypoint.sys, "argv", ["gift_ios.py"]), \
                mock.patch.object(fleet_entrypoint, "run_operation", return_value=0) as route:
            os.environ.pop("POKEMON_FLEET_CHILD", None)
            self.assertEqual(fleet_entrypoint.run_worker_module(main, "gifts", "gift.py"), 0)
        route.assert_called_once_with("gifts", "gift.py")
        main.assert_not_called()

    def test_worker_errors_become_exit_codes(self) -> None:
        with mock.patch.dict(os.environ, {"POKEMON_FLEET_CHILD": "1"}), \
                mock.patch("sys.stderr"):
            self.assertEqual(
                fleet_entrypoint.run_worker_module(mock.Mock(side_effect=RuntimeError("x")), "g", "g.py"), 1
            )
            self.assertEqual(
                fleet_entrypoint.run_worker_module(mock.Mock(side_effect=KeyboardInterrupt), "g", "g.py"), 130
            )


if __name__ == "__main__":
    unittest.main()
