"""The fleet lock, and what it says when it refuses.

`razr is already controlled by another fleet command` reads like a stale file
and never is: the lock is an flock, so it exists only while a live process
holds the fd.  On 2026-09-21 the holder was a `gbl.py --devices razr` leg that
had outlived its supervisor by 19 minutes, and the message gave no way to find
it.  The error now names the pid and its command.
"""

from __future__ import annotations

from tests import support as _test_support

import os
from pathlib import Path
import tempfile
import unittest
import unittest.mock

from sources import pokemon_fleet


def device(name: str = "razr", identifier: str = "ZY22LJS32X") -> pokemon_fleet.DeviceSpec:
    return pokemon_fleet.DeviceSpec(
        name=name,
        platform="android",
        enabled=True,
        config={"serial": identifier},
        base_dir=Path("/tmp"),
    )


class LockFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock_dir = Path(tempfile.mkdtemp())

    def test_the_holder_is_written_down_where_the_next_run_can_read_it(self) -> None:
        spec = device()
        with pokemon_fleet.acquire_device_locks([spec], self.lock_dir):
            written = (self.lock_dir / "ZY22LJS32X.lock").read_text(encoding="utf-8")
        self.assertIn(f"pid={os.getpid()}", written)
        self.assertIn("device=razr", written)

    def test_a_supervised_leg_records_the_supervisor_too(self) -> None:
        """So an orphan can be told from a leg whose supervisor is still there."""
        with unittest.mock.patch.dict(os.environ, {"POKEMON_SUPERVISOR_PID": "4242"}):
            with pokemon_fleet.acquire_device_locks([device()], self.lock_dir):
                written = (self.lock_dir / "ZY22LJS32X.lock").read_text(encoding="utf-8")
        self.assertIn("supervisor=4242", written)

    def test_a_hand_run_records_that_it_had_no_supervisor(self) -> None:
        environment = {k: v for k, v in os.environ.items() if k != "POKEMON_SUPERVISOR_PID"}
        with unittest.mock.patch.dict(os.environ, environment, clear=True):
            with pokemon_fleet.acquire_device_locks([device()], self.lock_dir):
                written = (self.lock_dir / "ZY22LJS32X.lock").read_text(encoding="utf-8")
        self.assertIn("supervisor=-", written)

    def test_the_lock_is_released_when_the_run_ends(self) -> None:
        spec = device()
        with pokemon_fleet.acquire_device_locks([spec], self.lock_dir):
            pass
        with pokemon_fleet.acquire_device_locks([spec], self.lock_dir):
            pass  # A leftover file is not a lock.


class RefusalTests(unittest.TestCase):
    """What the operator is told when the phone really is taken."""

    def setUp(self) -> None:
        self.lock_dir = Path(tempfile.mkdtemp())

    def test_a_held_phone_is_refused(self) -> None:
        spec = device()
        with pokemon_fleet.acquire_device_locks([spec], self.lock_dir):
            with self.assertRaises(pokemon_fleet.FleetError) as caught:
                with pokemon_fleet.acquire_device_locks([spec], self.lock_dir):
                    pass
        self.assertIn("razr is already controlled", str(caught.exception))

    def test_the_refusal_names_the_process_to_kill(self) -> None:
        """The whole point: the holder was findable all along, just unnamed."""
        spec = device()
        with pokemon_fleet.acquire_device_locks([spec], self.lock_dir):
            with self.assertRaises(pokemon_fleet.FleetError) as caught:
                with pokemon_fleet.acquire_device_locks([spec], self.lock_dir):
                    pass
        message = str(caught.exception)
        self.assertIn(f"pid {os.getpid()}", message)
        self.assertIn("kill -INT", message)

    def test_a_refusal_still_reads_when_the_holder_cannot_be_described(self) -> None:
        spec = device()
        with unittest.mock.patch.object(pokemon_fleet, "process_command", return_value=""):
            with pokemon_fleet.acquire_device_locks([spec], self.lock_dir):
                with self.assertRaises(pokemon_fleet.FleetError) as caught:
                    with pokemon_fleet.acquire_device_locks([spec], self.lock_dir):
                        pass
        self.assertIn(f"pid {os.getpid()}", str(caught.exception))

    def test_a_phone_taken_by_the_second_of_two_leaves_no_lock_behind(self) -> None:
        """A half-acquired run must not leave the first phone locked."""
        first, second = device("moto-g", "ZY22K9WZH9"), device()
        with pokemon_fleet.acquire_device_locks([second], self.lock_dir):
            with self.assertRaises(pokemon_fleet.FleetError):
                with pokemon_fleet.acquire_device_locks([first, second], self.lock_dir):
                    pass
        with pokemon_fleet.acquire_device_locks([first], self.lock_dir):
            pass


class HolderDescriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock_dir = Path(tempfile.mkdtemp())

    def test_a_file_with_no_pid_describes_nobody(self) -> None:
        path = self.lock_dir / "empty.lock"
        path.write_text("", encoding="utf-8")
        self.assertEqual(pokemon_fleet.lock_holder(path), "")

    def test_a_missing_file_describes_nobody(self) -> None:
        self.assertEqual(pokemon_fleet.lock_holder(self.lock_dir / "gone.lock"), "")

    def test_a_live_holder_is_described_by_its_command(self) -> None:
        path = self.lock_dir / "held.lock"
        path.write_text(f"pid={os.getpid()} device=razr\n", encoding="utf-8")
        described = pokemon_fleet.lock_holder(path)
        self.assertIn(f"pid {os.getpid()}", described)
        self.assertIn("python", described.lower())

    def test_a_pid_nobody_is_running_still_gets_named(self) -> None:
        """Not expected -- the flock refused us -- but it must not crash."""
        path = self.lock_dir / "gone.lock"
        path.write_text("pid=999999 device=razr\n", encoding="utf-8")
        self.assertEqual(pokemon_fleet.lock_holder(path), "pid 999999")


if __name__ == "__main__":
    unittest.main()
