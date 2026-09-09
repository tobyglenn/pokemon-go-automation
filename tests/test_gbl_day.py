from __future__ import annotations

from tests import support as _test_support

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
import unittest.mock

from sources import gbl_day


class ParsePlayedTests(unittest.TestCase):
    def test_finished_line_gives_the_count_per_label(self) -> None:
        output = "\n".join(
            [
                "[android-two]   Battle 3/17 started, attacking",
                "[android-two] Battle 3/17 over (WIN)",
                "[android-two] Finished: 3 battle(s)",
                "[ios-two] Finished: 5 battle(s)",
            ]
        )
        self.assertEqual(
            gbl_day.parse_played(output), {"android-two": 3, "ios-two": 5}
        )

    def test_interrupted_battles_are_not_credited_to_the_day(self) -> None:
        """A battle written off mid-fight may never have been counted in-game.

        The android-two finished a leg reporting 25 battles, four of them written
        off, and the supervisor called the day spent; the phone was left on the
        league screen with battles still owed to it.
        """
        output = "[android-two] Finished: 25 battle(s) (4 of them interrupted)"
        self.assertEqual(gbl_day.parse_played(output), {"android-two": 21})

    def test_a_leg_of_nothing_but_interruptions_counts_as_no_progress(self) -> None:
        output = "[android-two] Finished: 2 battle(s) (2 of them interrupted)"
        self.assertEqual(gbl_day.parse_played(output), {"android-two": 0})

    def test_running_battle_counters_are_not_mistaken_for_results(self) -> None:
        """`Battle 3/17` is an attempt, not a completion; only Finished counts."""
        output = "[android-two]   Battle 3/17 started, attacking"
        self.assertEqual(gbl_day.parse_played(output), {})

    def test_a_leg_that_says_nothing_played_nothing(self) -> None:
        self.assertEqual(gbl_day.parse_played(""), {})


class LegCommandTests(unittest.TestCase):
    def test_leg_runs_the_public_command_for_one_phone(self) -> None:
        command = gbl_day.leg_command("android-two", 17, None)
        self.assertEqual(
            command[-6:],
            [str(gbl_day.PROJECT_ROOT / "gbl.py"), "--local", "--devices", "android-two", "--count", "17"],
        )

    def test_trace_directory_is_forwarded(self) -> None:
        command = gbl_day.leg_command("android-two", 5, Path("/tmp/t"))
        self.assertEqual(command[-2:], ["--trace", "/tmp/t"])


class DayLoopTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _runner(counts: list[int], asked: list[int] | None = None):
        """A leg runner that plays a scripted number of battles each time."""
        remaining = list(counts)

        async def run(device: str, count: int) -> gbl_day.LegResult:
            if asked is not None:
                asked.append(count)
            played = remaining.pop(0) if remaining else 0
            return gbl_day.LegResult(played={device: played}, last_line="done")

        return run

    async def test_a_short_leg_is_followed_by_another_one(self) -> None:
        """The live failure: 7 of 25 played, then the loop gave up on its own."""
        asked: list[int] = []
        progress = gbl_day.DeviceProgress(name="android-two", allotment=25)
        await gbl_day.play_device_day(progress, self._runner([7, 18], asked))
        self.assertEqual(asked, [25, 18])
        self.assertEqual(progress.played, 25)
        self.assertEqual(progress.legs, 2)
        self.assertEqual(progress.stopped, "allotment played")

    async def test_the_phone_is_left_alone_after_two_empty_legs(self) -> None:
        progress = gbl_day.DeviceProgress(name="android-two", allotment=25)
        await gbl_day.play_device_day(progress, self._runner([4, 0, 0, 21]))
        self.assertEqual(progress.played, 4)
        self.assertEqual(progress.legs, 3)
        self.assertIn("played nothing", progress.stopped)

    async def test_one_good_leg_clears_an_earlier_stall(self) -> None:
        progress = gbl_day.DeviceProgress(name="android-two", allotment=10)
        await gbl_day.play_device_day(progress, self._runner([5, 0, 5]))
        self.assertEqual(progress.played, 10)
        self.assertEqual(progress.stalls, 0)

    async def test_a_count_reported_under_a_serial_still_counts(self) -> None:
        """The bare runner labels lines with the serial, not the fleet name."""

        async def run(device: str, count: int) -> gbl_day.LegResult:
            return gbl_day.LegResult(played={"test_device_8": 25})

        progress = gbl_day.DeviceProgress(name="android-two", allotment=25)
        await gbl_day.play_device_day(progress, run)
        self.assertEqual(progress.played, 25)

    async def test_a_failed_leg_does_not_end_the_day(self) -> None:
        results = [
            gbl_day.LegResult(played={"android-two": 5}, status=1),
            gbl_day.LegResult(played={"android-two": 5}, status=0),
        ]

        async def run(device: str, count: int) -> gbl_day.LegResult:
            return results.pop(0)

        progress = gbl_day.DeviceProgress(name="android-two", allotment=10)
        await gbl_day.play_device_day(progress, run)
        self.assertEqual(progress.played, 10)

    async def test_both_phones_play_their_own_allotment_at_once(self) -> None:
        started: list[str] = []

        def make(name: str):
            async def run(device: str, count: int) -> gbl_day.LegResult:
                started.append(device)
                await asyncio.sleep(0)
                return gbl_day.LegResult(played={device: count})

            return run

        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            devices = await gbl_day.run_day(
                ["android-two", "ios-two"],
                allotment=25,
                status_path=status,
                runner=make,
                keep_mac_awake=False,
                still_connected=lambda name: True,
            )
            written = json.loads(status.read_text())

        self.assertEqual(sorted(started), ["android-two", "ios-two"])
        self.assertTrue(all(device.done for device in devices))
        self.assertEqual(written["devices"]["android-two"]["played"], 25)
        self.assertEqual(written["devices"]["ios-two"]["remaining"], 0)

    async def test_naming_no_phone_is_an_error(self) -> None:
        with self.assertRaises(gbl_day.GBLDayError):
            await gbl_day.run_day([])


if __name__ == "__main__":
    unittest.main()


class LegWatchdogTests(unittest.TestCase):
    """A leg that never returns was invisible until someone went looking.

    Live, a GBL day on the worker host ran for 44 hours and played nothing: the stall
    counter is only read when a leg *finishes*, and this leg never did.  The
    watchdog judges a leg while it is still running instead.
    """

    def test_a_leg_still_printing_and_still_battling_is_left_alone(self) -> None:
        self.assertEqual(
            gbl_day.watchdog_verdict(now=1000.0, last_output=995.0, last_progress=900.0),
            "",
        )

    def test_a_silent_leg_is_abandoned(self) -> None:
        verdict = gbl_day.watchdog_verdict(
            now=1000.0, last_output=600.0, last_progress=990.0
        )
        self.assertIn("nothing printed", verdict)

    def test_a_chatty_leg_that_starts_no_battles_is_abandoned(self) -> None:
        """The 44-hour shape: output kept coming, battles never did."""
        verdict = gbl_day.watchdog_verdict(
            now=10_000.0, last_output=9_999.0, last_progress=1_000.0
        )
        self.assertIn("no battle started", verdict)

    def test_startup_is_given_its_full_progress_budget(self) -> None:
        """WebDriverAgent can take minutes to build before the first battle."""
        self.assertEqual(
            gbl_day.watchdog_verdict(now=600.0, last_output=599.0, last_progress=0.0),
            "",
        )

    def test_the_wait_never_overshoots_the_nearer_deadline(self) -> None:
        wait = gbl_day.next_watchdog_wait(
            now=1000.0, last_output=900.0, last_progress=500.0
        )
        self.assertAlmostEqual(wait, gbl_day.LEG_SILENCE_SECONDS - 100.0)

    def test_the_wait_stays_positive_past_a_deadline(self) -> None:
        wait = gbl_day.next_watchdog_wait(
            now=10_000.0, last_output=0.0, last_progress=0.0
        )
        self.assertGreater(wait, 0.0)

    def test_a_battle_line_counts_as_progress(self) -> None:
        self.assertTrue(gbl_day.PROGRESS_LINE.search("[tall_device]   Battle 3/17 started"))
        self.assertTrue(gbl_day.PROGRESS_LINE.search("[tall_device] Finished: 3 battle(s)"))
        self.assertFalse(gbl_day.PROGRESS_LINE.search("[tall_device] Waiting for opponent"))


class AbandonedLegChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_chain_stops_after_repeated_watchdog_kills(self) -> None:
        """Abandoning legs forever is the 44-hour bug with extra steps."""

        async def run(device: str, count: int) -> gbl_day.LegResult:
            return gbl_day.LegResult(
                played={device: 1}, abandoned="nothing printed for 5 minute(s)"
            )

        progress = gbl_day.DeviceProgress(name="android-one", allotment=25)
        await gbl_day.play_device_day(progress, run)
        self.assertEqual(progress.legs, gbl_day.ABANDON_LIMIT)
        self.assertIn("watchdog", progress.stopped)

    async def test_a_healthy_leg_after_an_abandoned_one_keeps_the_day_going(
        self,
    ) -> None:
        results = [
            gbl_day.LegResult(played={"android-one": 2}, abandoned="silent"),
            gbl_day.LegResult(played={"android-one": 23}),
        ]

        async def run(device: str, count: int) -> gbl_day.LegResult:
            return results.pop(0)

        progress = gbl_day.DeviceProgress(name="android-one", allotment=25)
        await gbl_day.play_device_day(progress, run)
        self.assertEqual(progress.played, 25)
        self.assertEqual(progress.stopped, "allotment played")


class DisconnectedPhoneTests(unittest.IsolatedAsyncioTestCase):
    """Both Androids left the USB bus mid-run and the day kept tapping at air."""

    async def test_a_phone_off_the_bus_ends_its_own_chain(self) -> None:
        started: list[str] = []

        async def run(device: str, count: int) -> gbl_day.LegResult:
            started.append(device)
            return gbl_day.LegResult(played={device: 5})

        progress = gbl_day.DeviceProgress(name="tall_device", allotment=25)
        await gbl_day.play_device_day(progress, run, still_connected=lambda name: False)
        self.assertEqual(started, [])
        self.assertEqual(progress.stopped, "phone disconnected")

    async def test_a_phone_that_drops_partway_stops_after_the_leg_it_finished(
        self,
    ) -> None:
        plugged = [True, False]

        async def run(device: str, count: int) -> gbl_day.LegResult:
            return gbl_day.LegResult(played={device: 5})

        progress = gbl_day.DeviceProgress(name="tall_device", allotment=25)
        await gbl_day.play_device_day(
            progress, run, still_connected=lambda name: plugged.pop(0)
        )
        self.assertEqual(progress.played, 5)
        self.assertEqual(progress.legs, 1)
        self.assertEqual(progress.stopped, "phone disconnected")

    async def test_the_default_asks_the_hardware_nothing(self) -> None:
        """Callers that pass no checker -- every existing test and tool -- run on."""

        async def run(device: str, count: int) -> gbl_day.LegResult:
            return gbl_day.LegResult(played={device: 25})

        progress = gbl_day.DeviceProgress(name="tall_device", allotment=25)
        await gbl_day.play_device_day(progress, run)
        self.assertEqual(progress.played, 25)


class RunDayConnectionCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_day_asks_the_bus_before_starting_a_leg(self) -> None:
        """The default checker is the real one, so it has to be exercised."""
        asked: list[str] = []

        def make(name: str):
            async def run(device: str, count: int) -> gbl_day.LegResult:
                asked.append(device)
                return gbl_day.LegResult(played={device: 25})

            return run

        with unittest.mock.patch.object(
            gbl_day, "connected_device_names", return_value={"tall_device"}
        ):
            devices = await gbl_day.run_day(
                ["tall_device", "android-two"],
                allotment=25,
                runner=make,
                keep_mac_awake=False,
            )

        self.assertEqual(asked, ["tall_device"])
        stopped = {device.name: device.stopped for device in devices}
        self.assertEqual(stopped["android-two"], "phone disconnected")

    async def test_a_probe_that_fails_is_not_treated_as_a_disconnect(self) -> None:
        """A flaky `adb devices` must not end a working day."""

        def make(name: str):
            async def run(device: str, count: int) -> gbl_day.LegResult:
                return gbl_day.LegResult(played={device: 25})

            return run

        with unittest.mock.patch.object(
            gbl_day, "connected_device_names", side_effect=OSError("adb died")
        ):
            devices = await gbl_day.run_day(
                ["tall_device"], allotment=25, runner=make, keep_mac_awake=False
            )

        self.assertEqual(devices[0].played, 25)


class ConnectedNamesProbeTests(unittest.TestCase):
    """Same trap as the watchdog's: a bare `adb` is not on PATH in this tree."""

    def test_a_probe_that_cannot_run_raises_instead_of_reporting_none(self) -> None:
        from sources import pokemon_fleet

        with unittest.mock.patch.object(pokemon_fleet, "load_fleet", return_value=pokemon_fleet.FleetConfig(Path("test.yaml"), {})):
            with self.assertRaises(pokemon_fleet.ProbeError):
                gbl_day.connected_device_names("definitely-not-adb")

    def test_run_day_treats_a_raised_probe_as_still_connected(self) -> None:
        """Left unguarded this would end every Android chain before leg one."""
        from sources import pokemon_fleet

        def make(name: str):
            async def run(device: str, count: int) -> gbl_day.LegResult:
                return gbl_day.LegResult(played={device: 25})

            return run

        with unittest.mock.patch.object(
            gbl_day,
            "connected_device_names",
            side_effect=pokemon_fleet.ProbeError("adb is not on PATH"),
        ):
            devices = asyncio.run(
                gbl_day.run_day(
                    ["tall_device"], allotment=25, runner=make, keep_mac_awake=False
                )
            )
        self.assertEqual(devices[0].played, 25)
