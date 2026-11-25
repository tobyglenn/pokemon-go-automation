"""Exercise starter's worker handoff using the Appium client against a local test server."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import support as _support
from sources import gbl_ios, gift_ios, pokemon_fleet, starter_ios_session as shared


@unittest.skipUnless(importlib.util.find_spec("appium"), "iOS client is optional")
class StarterSessionTests(unittest.TestCase):
    def setUp(self):
        from appium import webdriver
        from appium.options.ios import XCUITestOptions

        self.spec = SimpleNamespace(identifier=f"test-starter-{os.getpid()}", name="test", platform="ios")
        self.posts = []
        self.deletes = []
        self.active = set()
        test = self

        class Appium(BaseHTTPRequestHandler):
            def reply(self, value, status=200):
                body = json.dumps({"value": value}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path != "/session":
                    return self.reply({}, 404)
                sid = str(len(test.posts) + 1)
                test.posts.append(sid)
                test.active.add(sid)
                self.reply({"sessionId": sid, "capabilities": {"platformName": "iOS"}})

            def do_GET(self):
                sid = self.path.split("/")[2]
                if sid in test.active and self.path.endswith("/window/rect"):
                    self.reply({"x": 0, "y": 0, "width": 375, "height": 667})
                else:
                    self.reply({"error": "invalid session id", "message": "session expired"}, 404)

            def do_DELETE(self):
                sid = self.path.split("/")[2]
                test.deletes.append(sid)
                test.active.discard(sid)
                self.reply(None)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Appium)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}"
        self.config = SimpleNamespace(appium_server_url=self.url, ios_device={"udid": self.spec.identifier})
        self.create = lambda: webdriver.Remote(command_executor=self.url, options=XCUITestOptions())

    def test_gbl_gift_subprocess_and_final_connection_share_one_session(self):
        with shared.reserve(self.spec):
            with pokemon_fleet.acquire_device_locks([self.spec]), patch.object(
                gbl_ios.IOSGBLDevice, "_connect_new", side_effect=lambda cfg: SimpleNamespace(driver=self.create())
            ):
                device = gbl_ios.IOSGBLDevice.connect(self.config)
                asyncio.run(device.quit())
            # A separately launched gift client goes through its real connect/quit path.
            result = subprocess.run([sys.executable, "-c", """
import sys
from sources import gift_ios, pokemon_fleet
from types import SimpleNamespace
spec = SimpleNamespace(identifier=sys.argv[1], name='test', platform='ios')
with pokemon_fleet.acquire_device_locks([spec]):
    driver = gift_ios.connect({'device': {'udid': spec.identifier, 'team_id': 'test',
                              'wda_bundle_id': 'test'}, 'server_url': sys.argv[2]}, retry=False)
    assert driver.get_window_rect()['height'] == 667
    gift_ios.close_driver(driver)
""", self.spec.identifier, self.url], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            with pokemon_fleet.acquire_device_locks([self.spec]):
                with self.assertRaises(pokemon_fleet.FleetError):
                    with pokemon_fleet.acquire_device_locks([self.spec]):
                        self.fail("two steps acquired the same phone")
            # A process outside the run cannot take the handset between steps either.
            with patch.dict(os.environ, clear=True), self.assertRaises(pokemon_fleet.FleetError):
                with pokemon_fleet.acquire_device_locks([self.spec]):
                    self.fail("an unrelated command acquired the reserved phone")
            with pokemon_fleet.acquire_device_locks([self.spec]):
                client = shared.connect(self.spec.identifier, self.url, self.create)
                client.quit()
            self.assertEqual((self.posts, self.deletes), (["1"], []))
        self.assertEqual(self.deletes, ["1"])
        with pokemon_fleet.acquire_device_locks([self.spec]):
            self.assertIsNone(shared.directory(self.spec.identifier))

    def test_worker_failures_leave_the_reserved_runner_available(self):
        device = SimpleNamespace(screenshot=AsyncMock(return_value=None), quit=AsyncMock())
        config = {"device": {"udid": self.spec.identifier, "team_id": "test", "wda_bundle_id": "test"}}
        with (
            shared.reserve(self.spec),
            patch.object(gbl_ios.ios_wda_cleanup, "stop_wda_runner") as stop,
            patch.object(gbl_ios.gbl_strategy, "load_strategy_profile"),
            patch.object(gbl_ios.IOSGBLDevice, "connect", return_value=device),
            patch.object(sys, "argv", ["gift.py", "--count", "1"]),
            patch.object(gift_ios, "load_config", return_value=config),
            patch.dict(os.environ, {"POKEMON_FLEET_LOCK_HELD": "1"}),
            patch.object(gift_ios, "connect_ready", side_effect=RuntimeError("gift unavailable")),
        ):
            with self.assertRaisesRegex(gbl_ios.IOSGBLError, "Could not read"):
                asyncio.run(gbl_ios.async_main(SimpleNamespace(), self.config))
            with self.assertRaisesRegex(RuntimeError, "gift unavailable"):
                gift_ios.main()
            device.quit.assert_awaited_once()
            stop.assert_not_called()

    def test_expired_session_is_replaced_and_exception_cleans_up_replacement(self):
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with shared.reserve(self.spec), pokemon_fleet.acquire_device_locks([self.spec]):
                shared.connect(self.spec.identifier, self.url, self.create).quit()
                self.active.clear()
                shared.connect(self.spec.identifier, self.url, self.create).quit()
                self.assertEqual(self.posts, ["1", "2"])
                raise RuntimeError("interrupted")
        self.assertEqual(self.deletes, ["1", "2"])
        self.assertFalse(self.active)
