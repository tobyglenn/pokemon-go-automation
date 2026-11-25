from __future__ import annotations

from tests import support as _test_support

import unittest
from unittest import mock

from sources import ios_wda_cleanup


def status(testmanagerd: int = 65535) -> dict:
    """What /status returns -- the same shape whether or not WDA can still tap.

    The default is the SE's real reply while it was mid-run and tapping fine:
    testmanagerdVersion 65535 on a live, attached WDA. Nothing in this body
    says whether the session can drive the phone.
    """
    return {
        "build": {"version": "16.1.6"},
        "os": {"name": "iOS", "version": "26.6", "testmanagerdVersion": testmanagerd},
        "message": "WebDriverAgent is ready to accept commands",
        "state": "success",
        "ready": True,
    }


class HealthTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ios_wda_cleanup, "stop_device_wda_processes", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_health_follows_the_host_runner_not_the_status_body(self) -> None:
        # Identical /status in both -- including the 65535 that used to be read
        # as proof of death. Only the host xcodebuild tells them apart.
        for alive, expected in ((True, "live"), (False, "detached")):
            with self.subTest(host_runner_alive=alive):
                with (
                    mock.patch.object(
                        ios_wda_cleanup, "read_wda_status", return_value=status()),
                    mock.patch.object(
                        ios_wda_cleanup, "host_runner_alive", return_value=alive),
                    mock.patch.object(
                        ios_wda_cleanup, "wda_screenshot_authorized", return_value=True),
                ):
                    self.assertEqual(
                        ios_wda_cleanup.wda_health("PHONE-A", 8100), expected)

    def test_nothing_listening_is_absent(self) -> None:
        with mock.patch.object(
            ios_wda_cleanup, "read_wda_status_payload", return_value=None), mock.patch.object(
            ios_wda_cleanup, "read_wda_status", return_value=None):
            self.assertEqual(ios_wda_cleanup.wda_health("PHONE-A", 8100), "absent")

    def test_unauthorized_session_is_detached_even_when_host_runner_alive(self) -> None:
        payload = {"value": status(), "sessionId": "SESSION-123"}
        with (
            mock.patch.object(ios_wda_cleanup, "read_wda_status_payload", return_value=payload),
            mock.patch.object(ios_wda_cleanup, "host_runner_alive", return_value=True),
            mock.patch.object(ios_wda_cleanup, "wda_session_alive", return_value=False),
        ):
            self.assertEqual(ios_wda_cleanup.wda_health("PHONE-A", 8100), "detached")

    def test_authorized_session_is_live(self) -> None:
        payload = {"value": status(), "sessionId": "SESSION-123"}
        with (
            mock.patch.object(ios_wda_cleanup, "read_wda_status_payload", return_value=payload),
            mock.patch.object(ios_wda_cleanup, "host_runner_alive", return_value=True),
            mock.patch.object(ios_wda_cleanup, "wda_session_alive", return_value=True),
        ):
            self.assertEqual(ios_wda_cleanup.wda_health("PHONE-A", 8100), "live")

    def test_unauthorized_screenshot_is_detached_without_session(self) -> None:
        payload = {"value": status()}
        with (
            mock.patch.object(ios_wda_cleanup, "read_wda_status_payload", return_value=payload),
            mock.patch.object(ios_wda_cleanup, "host_runner_alive", return_value=True),
            mock.patch.object(ios_wda_cleanup, "wda_screenshot_authorized", return_value=False),
        ):
            self.assertEqual(ios_wda_cleanup.wda_health("PHONE-A", 8100), "detached")

    def test_authorized_screenshot_is_live_without_session(self) -> None:
        payload = {"value": status()}
        with (
            mock.patch.object(ios_wda_cleanup, "read_wda_status_payload", return_value=payload),
            mock.patch.object(ios_wda_cleanup, "host_runner_alive", return_value=True),
            mock.patch.object(ios_wda_cleanup, "wda_screenshot_authorized", return_value=True),
        ):
            self.assertEqual(ios_wda_cleanup.wda_health("PHONE-A", 8100), "live")

    def test_only_a_detached_runner_is_stopped(self) -> None:
        for health, expected in (("detached", True), ("live", False), ("absent", False)):
            with self.subTest(health=health):
                with (
                    mock.patch.object(
                        ios_wda_cleanup, "wda_health", return_value=health),
                    mock.patch.object(
                        ios_wda_cleanup, "stop_wda_runner") as stop,
                ):
                    cleared = ios_wda_cleanup.clear_detached_wda("PHONE-A", 8100)
                self.assertEqual(cleared, expected)
                # A cold start must not be mistaken for a corpse, and a warm
                # healthy WDA is kept on purpose -- killing it would cost every
                # run the rebuild it was saved from.
                self.assertEqual(stop.called, expected)

    def test_unreadable_status_is_absent_not_detached(self) -> None:
        # A truncated or non-JSON body is not evidence that WDA is broken, and
        # acting on it would kill a healthy runner on a slow reply.
        with mock.patch.object(
            ios_wda_cleanup.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertIsNone(ios_wda_cleanup.read_wda_status(8100))
            self.assertEqual(ios_wda_cleanup.wda_health("PHONE-A", 8100), "absent")


class ProcessSelectionTests(unittest.TestCase):
    def test_host_selection_is_scoped_to_udid_and_wda(self) -> None:
        processes = """
101 /usr/bin/xcodebuild -project /tmp/WebDriverAgent.xcodeproj -destination id=PHONE-A
102 /usr/bin/xcodebuild -project /tmp/WebDriverAgent.xcodeproj -destination id=PHONE-B
103 /usr/bin/xcodebuild -project /tmp/Other.xcodeproj -destination id=PHONE-A
104 python gift_ios.py PHONE-A
"""
        self.assertEqual(
            ios_wda_cleanup.find_host_wda_pids(processes, "PHONE-A"),
            [101],
        )

    def test_device_selection_finds_wda_runner(self) -> None:
        device_processes = """
12692   /usr/sbin/BTLEServer
12719   /System/Library/PrivateFrameworks/Pasteboard.framework/Support/pasted
12939   /private/var/containers/Bundle/Application/6207A5FD-2A3F-43CB-8075-96BC09B6FFAB/WebDriverAgentRunner-Runner.app/WebDriverAgentRunner-Runner
12940   /usr/libexec/mobileactivationd
"""
        self.assertEqual(
            ios_wda_cleanup.find_device_wda_pids(device_processes),
            [12939],
        )

    def test_stop_device_wda_processes_terminates_found_pids(self) -> None:
        devicectl_output = "12939   /path/to/WebDriverAgentRunner-Runner\n"
        with mock.patch.object(ios_wda_cleanup, "run_output") as run_mock:
            run_mock.side_effect = [devicectl_output, ""]
            terminated = ios_wda_cleanup.stop_device_wda_processes("PHONE-A")
            self.assertEqual(terminated, [12939])
            self.assertEqual(run_mock.call_count, 2)
            self.assertIn("terminate", run_mock.call_args_list[1][0][0])
            self.assertIn("12939", run_mock.call_args_list[1][0][0])

    def test_clear_detached_wda_cleans_orphaned_device_runner_when_absent(self) -> None:
        with (
            mock.patch.object(ios_wda_cleanup, "wda_health", return_value="absent"),
            mock.patch.object(
                ios_wda_cleanup, "host_runner_alive", return_value=False),
            mock.patch.object(
                ios_wda_cleanup, "stop_device_wda_processes", return_value=[12939]
            ) as stop_device,
        ):
            cleared = ios_wda_cleanup.clear_detached_wda("PHONE-A", 8101)
            self.assertTrue(cleared)
            stop_device.assert_called_once_with("PHONE-A")

    def test_a_starting_runner_is_not_mistaken_for_an_orphan(self) -> None:
        # The port is silent because WDA has not finished booting, not because
        # it is dead. Terminating the device runner now kills the launch.
        with (
            mock.patch.object(ios_wda_cleanup, "wda_health", return_value="absent"),
            mock.patch.object(
                ios_wda_cleanup, "host_runner_alive", return_value=True),
            mock.patch.object(
                ios_wda_cleanup, "stop_device_wda_processes") as stop_device,
        ):
            self.assertFalse(ios_wda_cleanup.clear_detached_wda("PHONE-A", 8101))
            stop_device.assert_not_called()


class BiometricRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        # Real profiles on the test Mac must not decide these cases.
        patcher = mock.patch.object(ios_wda_cleanup, "wda_profile_valid_for", return_value=None)
        self.profile_valid = patcher.start()
        self.addCleanup(patcher.stop)

    def test_code_65_with_only_expired_profiles_stops_at_once(self) -> None:
        self.profile_valid.return_value = False

        def fake_connect():
            raise RuntimeError("xcodebuild failed with code 65")

        with (
            mock.patch.object(ios_wda_cleanup, "display_backlight", return_value=True),
            mock.patch.object(ios_wda_cleanup, "device_requires_unlock", return_value=False),
            mock.patch.object(ios_wda_cleanup, "stop_wda_runner"),
            mock.patch.object(ios_wda_cleanup.time, "sleep") as sleep_mock,
        ):
            with self.assertRaises(RuntimeError) as raised:
                ios_wda_cleanup.connect_with_retry(fake_connect, udid="PHONE-A", name="SE")
        self.assertIn("provisioning profile", str(raised.exception))
        sleep_mock.assert_not_called()

    def test_identifies_biometric_and_lock_errors(self) -> None:
        self.assertTrue(
            ios_wda_cleanup.is_wda_biometric_or_unlock_error(
                Exception("Timed out while enabling automation mode.")
            )
        )
        self.assertTrue(
            ios_wda_cleanup.is_wda_biometric_or_unlock_error(
                Exception("Original error: xcodebuild failed with code 65.")
            )
        )
        self.assertTrue(
            ios_wda_cleanup.is_wda_biometric_or_unlock_error(
                Exception("device was not, or could not be, unlocked")
            )
        )
        self.assertFalse(
            ios_wda_cleanup.is_wda_biometric_or_unlock_error(
                Exception("some unexpected network timeout")
            )
        )

    def test_retry_on_screen_off_until_woken(self) -> None:
        calls = []
        def fake_connect():
            calls.append("connect")
            return "driver"

        with (
            mock.patch.object(ios_wda_cleanup, "display_backlight", side_effect=[False, True]),
            mock.patch.object(ios_wda_cleanup, "device_requires_unlock", return_value=False),
            mock.patch.object(ios_wda_cleanup.time, "sleep") as sleep_mock,
        ):
            res = ios_wda_cleanup.connect_with_retry(
                fake_connect, udid="PHONE-A", name="SE", interval=45.0
            )
            self.assertEqual(res, "driver")
            self.assertEqual(calls, ["connect"])
            sleep_mock.assert_called_once_with(45.0)

    def test_retry_on_wda_biometric_failure_until_success(self) -> None:
        attempts = 0
        def fake_connect():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("xcodebuild failed with code 65")
            return "driver"

        with (
            mock.patch.object(ios_wda_cleanup, "display_backlight", return_value=True),
            mock.patch.object(ios_wda_cleanup, "device_requires_unlock", return_value=False),
            mock.patch.object(ios_wda_cleanup, "stop_wda_runner") as stop_mock,
            mock.patch.object(ios_wda_cleanup.time, "sleep") as sleep_mock,
        ):
            res = ios_wda_cleanup.connect_with_retry(
                fake_connect, udid="PHONE-A", name="SE", interval=45.0
            )
            self.assertEqual(res, "driver")
            self.assertEqual(attempts, 2)
            stop_mock.assert_called_once_with("PHONE-A")
            sleep_mock.assert_called_once_with(45.0)

    def test_gives_up_with_the_original_error_after_the_wait_limit(self) -> None:
        error = RuntimeError("xcodebuild failed with code 65")

        def fake_connect():
            raise error

        with (
            mock.patch.object(ios_wda_cleanup, "display_backlight", return_value=True),
            mock.patch.object(ios_wda_cleanup, "device_requires_unlock", return_value=False),
            mock.patch.object(ios_wda_cleanup, "stop_wda_runner"),
            mock.patch.object(ios_wda_cleanup.time, "sleep") as sleep_mock,
            mock.patch.object(
                ios_wda_cleanup.time, "monotonic", side_effect=[0.0, 0.0, 50.0, 100.0]
            ),
        ):
            with self.assertRaises(RuntimeError) as raised:
                ios_wda_cleanup.connect_with_retry(
                    fake_connect, udid="PHONE-A", name="SE", interval=45.0, wait_limit=120.0
                )
        self.assertIs(raised.exception, error)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_stale_runner_restarts_at_once_then_waits(self) -> None:
        stale = RuntimeError(
            'Message: The previously found element "Application '
            "'com.nianticlabs.pokemongo'\" is not present in the current view "
            "anymore. Not authorized for performing UI testing actions."
        )
        attempts = []

        def fake_connect():
            attempts.append(1)
            if len(attempts) <= 2:
                raise stale
            return "driver"

        with (
            mock.patch.object(ios_wda_cleanup, "display_backlight", return_value=True),
            mock.patch.object(ios_wda_cleanup, "device_requires_unlock", return_value=False),
            mock.patch.object(ios_wda_cleanup, "stop_wda_runner") as stop_mock,
            mock.patch.object(ios_wda_cleanup.time, "sleep") as sleep_mock,
        ):
            res = ios_wda_cleanup.connect_with_retry(
                fake_connect, udid="PHONE-A", name="SE", interval=45.0
            )
        self.assertEqual(res, "driver")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(stop_mock.call_count, 2)
        # Only the second stale failure waits on the owner.
        sleep_mock.assert_called_once_with(45.0)

    def test_unrelated_error_is_raised_immediately(self) -> None:
        def fake_connect():
            raise ValueError("totally unrelated error")

        with (
            mock.patch.object(ios_wda_cleanup, "display_backlight", return_value=True),
            mock.patch.object(ios_wda_cleanup, "device_requires_unlock", return_value=False),
            mock.patch.object(ios_wda_cleanup.time, "sleep") as sleep_mock,
        ):
            with self.assertRaisesRegex(ValueError, "totally unrelated error"):
                ios_wda_cleanup.connect_with_retry(
                    fake_connect, udid="PHONE-A", name="SE", interval=45.0
                )
            sleep_mock.assert_not_called()

if __name__ == "__main__":
    unittest.main()


class ProfileValidityTests(unittest.TestCase):
    import datetime as _dt
    NOW = _dt.datetime(2026, 11, 12, 13, 0, tzinfo=_dt.timezone.utc)

    def profile(self, days: int, devices=("PHONE-A",), app="TEAM.com.example.WebDriverAgentRunner.xctrunner"):
        import datetime as dt
        return {
            "Entitlements": {"application-identifier": app},
            "ExpirationDate": (self.NOW + dt.timedelta(days=days)).replace(tzinfo=None),
            "ProvisionedDevices": list(devices),
        }

    def test_unexpired_profile_listing_the_phone_is_valid(self) -> None:
        self.assertTrue(ios_wda_cleanup.wda_profile_valid_for(
            "PHONE-A", [self.profile(-1), self.profile(6)], now=self.NOW))

    def test_expired_or_other_phone_only_is_invalid(self) -> None:
        self.assertFalse(ios_wda_cleanup.wda_profile_valid_for(
            "PHONE-A", [self.profile(-1), self.profile(6, devices=("PHONE-B",))], now=self.NOW))

    def test_no_wda_profile_at_all_is_unknown(self) -> None:
        self.assertIsNone(ios_wda_cleanup.wda_profile_valid_for(
            "PHONE-A", [self.profile(6, app="TEAM.com.example.game")], now=self.NOW))
