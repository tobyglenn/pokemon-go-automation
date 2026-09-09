from __future__ import annotations

from tests import support as _test_support

import asyncio
import signal
import unittest
from unittest import mock

from sources import pokemon_fleet


class FakeProcess:
    def __init__(self, exit_on_interrupt: bool = True) -> None:
        self.exit_on_interrupt = exit_on_interrupt
        self.returncode = None
        self.signals = []
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.done = asyncio.Event()

    async def wait(self) -> int:
        self.wait_calls += 1
        await self.done.wait()
        return self.returncode

    def send_signal(self, value: int) -> None:
        self.signals.append(value)
        if self.exit_on_interrupt:
            self.returncode = 0
            self.done.set()

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -signal.SIGTERM
        self.done.set()

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -signal.SIGKILL
        self.done.set()


class RunChildCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_sends_sigint_and_waits_for_child_cleanup(self) -> None:
        process = FakeProcess()
        create_process = mock.AsyncMock(return_value=process)

        with mock.patch.object(
            pokemon_fleet.asyncio,
            "create_subprocess_exec",
            new=create_process,
        ):
            task = asyncio.create_task(
                pokemon_fleet.run_child("ios-one", ["python", "berry_ios.py"])
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(process.signals, [signal.SIGINT])
        self.assertGreaterEqual(process.wait_calls, 2)
        self.assertEqual(process.terminate_calls, 0)
        self.assertEqual(process.kill_calls, 0)

    async def test_cancel_terminates_child_if_sigint_cleanup_times_out(self) -> None:
        process = FakeProcess(exit_on_interrupt=False)
        create_process = mock.AsyncMock(return_value=process)

        with mock.patch.object(
            pokemon_fleet.asyncio,
            "create_subprocess_exec",
            new=create_process,
        ), mock.patch.object(
            pokemon_fleet,
            "CHILD_INTERRUPT_TIMEOUT_SECONDS",
            0.001,
        ):
            task = asyncio.create_task(
                pokemon_fleet.run_child("ios-one", ["python", "berry_ios.py"])
            )
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(process.signals, [signal.SIGINT])
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)


if __name__ == "__main__":
    unittest.main()
