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


if __name__ == "__main__":
    unittest.main()
