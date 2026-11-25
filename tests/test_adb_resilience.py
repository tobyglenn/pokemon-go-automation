from __future__ import annotations

from tests import support as _test_support  # noqa: F401

import asyncio
import unittest
from unittest import mock

from ppadb.connection_async import ConnectionAsync

from sources import adb_resilience, android_common, pokemon_fleet


REFUSED = RuntimeError("ERROR: connecting to 127.0.0.1:5037 [Errno 61] Connect call failed.")


class SettledTests(unittest.TestCase):
    def test_phones_back_online(self) -> None:
        self.assertTrue(adb_resilience.phones_settled(
            "List of devices attached\nAAA\tdevice\nBBB\tdevice\n"))

    def test_a_phone_still_reconnecting(self) -> None:
        self.assertFalse(adb_resilience.phones_settled(
            "List of devices attached\nAAA\tdevice\nBBB\toffline\n"))

    def test_no_phone_yet(self) -> None:
        self.assertFalse(adb_resilience.phones_settled("List of devices attached\n\n"))


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    """A server that something else killed is started again and the call retried."""

    async def test_installed_on_import(self) -> None:
        self.assertTrue(adb_resilience._installed)

    async def test_adb_timeout_reaps_the_client(self) -> None:
        proc = mock.Mock(communicate=mock.AsyncMock(side_effect=asyncio.TimeoutError))
        with (
            mock.patch.object(pokemon_fleet, "default_adb_binary", return_value="adb"),
            mock.patch.object(asyncio, "create_subprocess_exec", new=mock.AsyncMock(return_value=proc)),
            mock.patch.object(android_common, "kill_and_reap", new=mock.AsyncMock()) as reap,
        ):
            self.assertEqual(await adb_resilience._adb("devices"), (1, ""))
        reap.assert_awaited_once_with(proc)

    async def test_refused_connection_restarts_the_server_and_retries(self) -> None:
        calls = []

        async def flaky(self):
            calls.append(1)
            if len(calls) == 1:
                raise REFUSED
            return self

        with (
            mock.patch.object(adb_resilience, "_installed", False),
            mock.patch.object(ConnectionAsync, "connect", flaky),
            mock.patch.object(adb_resilience, "restore_server",
                              new=mock.AsyncMock(return_value=True)) as restored,
        ):
            adb_resilience.install()
            connection = ConnectionAsync("127.0.0.1", 5037)
            self.assertIs(await ConnectionAsync.connect(connection), connection)
        self.assertEqual(len(calls), 2)
        restored.assert_awaited()

    async def test_a_server_that_never_returns_is_still_an_error(self) -> None:
        async def refused(self):
            raise REFUSED

        with (
            mock.patch.object(adb_resilience, "_installed", False),
            mock.patch.object(adb_resilience, "RECOVERY_SECONDS", 0.05),
            mock.patch.object(adb_resilience, "POLL_SECONDS", 0.01),
            mock.patch.object(ConnectionAsync, "connect", refused),
            mock.patch.object(adb_resilience, "restore_server",
                              new=mock.AsyncMock(return_value=False)),
        ):
            adb_resilience.install()
            with self.assertRaises(RuntimeError):
                await ConnectionAsync.connect(ConnectionAsync("127.0.0.1", 5037))

    async def test_other_errors_pass_straight_through(self) -> None:
        async def failing(self):
            raise RuntimeError("ERROR: 'FAIL' device offline")

        with (
            mock.patch.object(adb_resilience, "_installed", False),
            mock.patch.object(ConnectionAsync, "connect", failing),
            mock.patch.object(adb_resilience, "restore_server", new=mock.AsyncMock()) as restored,
        ):
            adb_resilience.install()
            with self.assertRaises(RuntimeError):
                await ConnectionAsync.connect(ConnectionAsync("127.0.0.1", 5037))
        restored.assert_not_awaited()


class PhoneReattachTests(unittest.IsolatedAsyncioTestCase):
    """The server is back but the phone is not yet: wait for it, then retry."""

    def test_phone_ready_reads_its_own_line(self) -> None:
        listing = "List of devices attached\nAAA\toffline\nBBB\tdevice\n"
        self.assertFalse(adb_resilience.phone_ready(listing, "AAA"))
        self.assertTrue(adb_resilience.phone_ready(listing, "BBB"))

    async def test_missing_phone_is_waited_for_and_retried(self) -> None:
        from ppadb.device_async import DeviceAsync

        calls = []

        async def flaky(self, set_transport=True, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("ERROR: 'FAIL' 0023device 'AAA' not found")
            return "conn"

        with (
            mock.patch.object(adb_resilience, "_installed", False),
            mock.patch.object(DeviceAsync, "create_connection", flaky),
            mock.patch.object(adb_resilience, "wait_for_phone",
                              new=mock.AsyncMock(return_value=True)) as waited,
        ):
            adb_resilience.install()
            device = DeviceAsync(client=None, serial="AAA")
            self.assertEqual(await DeviceAsync.create_connection(device), "conn")
        self.assertEqual(len(calls), 2)
        waited.assert_awaited()


if __name__ == "__main__":
    unittest.main()
