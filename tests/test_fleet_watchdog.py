from __future__ import annotations

from tests import support as _test_support

import io
import os
import sys
import threading
import unittest

from pathlib import Path
import unittest.mock

from sources import fleet_watchdog, pokemon_fleet


class IdleVerdictTests(unittest.TestCase):
    def test_a_run_that_printed_recently_is_left_alone(self) -> None:
        self.assertEqual(
            fleet_watchdog.idle_verdict(1000.0, 999.0, 0, idle_seconds=1800.0), ""
        )

    def test_a_run_that_went_quiet_is_stopped(self) -> None:
        """The 44-hour shape, seen from inside the run instead of outside it."""
        verdict = fleet_watchdog.idle_verdict(200_000.0, 1_000.0, 0, idle_seconds=1800.0)
        self.assertIn("nothing printed", verdict)

    def test_a_run_with_no_phones_left_is_stopped_even_while_chatty(self) -> None:
        verdict = fleet_watchdog.idle_verdict(1000.0, 999.0, 3, idle_seconds=1800.0)
        self.assertIn("no phones", verdict)

    def test_a_couple_of_empty_readings_are_not_a_disconnect(self) -> None:
        """`adb devices` reports nothing while its own daemon restarts."""
        for readings in (1, 2):
            self.assertEqual(
                fleet_watchdog.idle_verdict(1000.0, 999.0, readings, idle_seconds=1800.0),
                "",
            )

    def test_the_idle_half_can_be_switched_off(self) -> None:
        self.assertEqual(
            fleet_watchdog.idle_verdict(200_000.0, 0.0, 0, idle_seconds=0.0), ""
        )

    def test_switching_the_idle_half_off_keeps_the_disconnect_half(self) -> None:
        verdict = fleet_watchdog.idle_verdict(1000.0, 999.0, 3, idle_seconds=0.0)
        self.assertIn("no phones", verdict)


class ActivityTests(unittest.TestCase):
    def test_printing_counts_as_activity(self) -> None:
        activity = fleet_watchdog.Activity(at=0.0)
        buffer = io.StringIO()
        clock = iter([50.0])
        stream = fleet_watchdog.WatchedStream(buffer, activity, lambda: next(clock))
        stream.write("[tall_device] Battle 3/17 started\n")
        self.assertEqual(activity.last(), 50.0)
        self.assertEqual(buffer.getvalue(), "[tall_device] Battle 3/17 started\n")

    def test_the_wrapped_stream_still_behaves_like_a_stream(self) -> None:
        """Scripts call flush() constantly; the wrapper must pass it through."""
        buffer = io.StringIO()
        stream = fleet_watchdog.WatchedStream(
            buffer, fleet_watchdog.Activity(at=0.0), lambda: 0.0
        )
        stream.write("x")
        stream.flush()
        self.assertFalse(stream.closed)


class WatchLoopTests(unittest.TestCase):
    def _run_loop(
        self,
        attached,
        clock_values,
        *,
        idle_seconds=1800.0,
        rounds=3,
        orphaned=lambda: False,
    ):
        stopped: list[str] = []
        clock = iter(clock_values)
        activity = fleet_watchdog.Activity(at=0.0)
        fleet_watchdog._watch(
            activity,
            clock=lambda: next(clock),
            sleep=lambda seconds: None,
            attached=attached,
            stop=stopped.append,
            poll_seconds=0.0,
            idle_seconds=idle_seconds,
            rounds=rounds,
            orphaned=orphaned,
        )
        return stopped

    def test_a_healthy_run_is_never_stopped(self) -> None:
        stopped = self._run_loop(lambda: 2, [10.0, 20.0, 30.0])
        self.assertEqual(stopped, [])

    def test_a_run_stops_once_the_phones_are_all_gone(self) -> None:
        stopped = self._run_loop(lambda: 0, [10.0, 20.0, 30.0])
        self.assertEqual(len(stopped), 1)
        self.assertIn("no phones", stopped[0])

    def test_a_failed_probe_does_not_end_the_run(self) -> None:
        """`adb` falling over is not evidence that the phones went away."""

        def attached() -> int:
            raise OSError("adb: device offline")

        self.assertEqual(self._run_loop(attached, [10.0, 20.0, 30.0]), [])

    def test_a_probe_that_recovers_resets_the_count(self) -> None:
        readings = iter([0, 2, 0])
        stopped = self._run_loop(lambda: next(readings), [10.0, 20.0, 30.0])
        self.assertEqual(stopped, [])

    def test_the_run_stops_after_long_silence(self) -> None:
        stopped = self._run_loop(lambda: 2, [10.0, 5_000.0, 6_000.0])
        self.assertEqual(len(stopped), 1)
        self.assertIn("nothing printed", stopped[0])

    def test_a_leg_whose_supervisor_died_stops_itself(self) -> None:
        """The 2026-09-21 orphan: busy, phones attached, nobody reading it."""
        stopped = self._run_loop(lambda: 2, [10.0, 20.0, 30.0], orphaned=lambda: True)
        self.assertEqual(len(stopped), 1)
        self.assertIn("supervisor", stopped[0])

    def test_an_orphan_stops_even_while_its_phone_is_attached_and_chatty(self) -> None:
        """Neither other half would ever fire on a leg that is working fine."""
        gone = iter([False, True])
        stopped = self._run_loop(
            lambda: 2, [10.0, 20.0, 30.0], orphaned=lambda: next(gone)
        )
        self.assertEqual(len(stopped), 1)


class SupervisorTests(unittest.TestCase):
    """Only a leg that was handed a supervisor answers to one."""

    def test_a_live_supervisor_leaves_the_leg_alone(self) -> None:
        self.assertFalse(
            fleet_watchdog.supervisor_gone("4242", alive=lambda pid: pid == 4242)
        )

    def test_a_dead_supervisor_orphans_the_leg(self) -> None:
        self.assertTrue(
            fleet_watchdog.supervisor_gone("4242", alive=lambda pid: False)
        )

    def test_a_run_started_by_hand_has_no_supervisor_to_lose(self) -> None:
        """`nohup gbl.py &` is reparented to launchd and is meant to keep going."""
        for text in ("", "   ", "none", "1"):
            with self.subTest(text=text):
                self.assertFalse(
                    fleet_watchdog.supervisor_gone(text, alive=lambda pid: False)
                )

    def test_the_real_check_reads_this_process_as_alive(self) -> None:
        self.assertFalse(fleet_watchdog.supervisor_gone(str(os.getpid())))


class InstallTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch.object(fleet_watchdog, "ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_install_watches_stdout_and_returns_a_daemon_thread(self) -> None:
        original = sys.stdout
        try:
            thread = fleet_watchdog.install(
                attached=lambda: 2,
                sleep=lambda seconds: None,
                stop=lambda reason: None,
                rounds=1,
            )
            self.assertIsInstance(thread, threading.Thread)
            self.assertTrue(thread.daemon)
            thread.join(timeout=5)
            self.assertIsInstance(sys.stdout, fleet_watchdog.WatchedStream)
        finally:
            sys.stdout = original


class ProbeHonestyTests(unittest.TestCase):
    """The 2026-08-27 false positive: a trade killed with both phones plugged in.

    `adb` is not on PATH in this tree -- the automation drives `./adb`. The
    watchdog asked the lenient probe with a bare `adb`, the probe turned the
    resulting OSError into `{}`, and an empty dict is indistinguishable from an
    empty USB bus. So the count has to come from a probe that raises.
    """

    def test_the_count_refuses_to_guess_when_adb_cannot_be_run(self) -> None:
        with self.assertRaises(pokemon_fleet.ProbeError):
            fleet_watchdog.count_attached("definitely-not-adb")

    def test_a_missing_adb_never_reads_as_a_disconnect(self) -> None:
        """End to end: the exact call that killed the trade must now be inert."""
        stopped: list[str] = []
        clock = iter([10.0, 20.0, 30.0, 40.0])
        fleet_watchdog._watch(
            fleet_watchdog.Activity(at=0.0),
            clock=lambda: next(clock),
            sleep=lambda seconds: None,
            attached=lambda: fleet_watchdog.count_attached("definitely-not-adb"),
            stop=stopped.append,
            poll_seconds=0.0,
            idle_seconds=1800.0,
            rounds=4,
            orphaned=lambda: False,
        )
        self.assertEqual(stopped, [])

    def test_adb_fallback_uses_path_without_a_bundled_binary(self) -> None:
        with unittest.mock.patch.object(pokemon_fleet.Path, "exists", return_value=False):
            self.assertEqual(pokemon_fleet.default_adb_binary(), "adb")

    def test_the_lenient_probe_still_shrugs_for_read_only_callers(self) -> None:
        """`status` prints an empty table rather than blowing up."""
        self.assertEqual(pokemon_fleet.adb_states("definitely-not-adb"), {})

    def test_a_totally_failed_iphone_probe_raises_rather_than_reporting_none(
        self,
    ) -> None:
        with unittest.mock.patch.object(
            pokemon_fleet.subprocess, "run", side_effect=OSError("xcrun missing")
        ), unittest.mock.patch.object(
            pokemon_fleet.ios_attached_devices,
            "usb_udids",
            side_effect=pokemon_fleet.ios_attached_devices.AttachedDeviceError("no usbmux"),
        ):
            with self.assertRaises(pokemon_fleet.ProbeError):
                pokemon_fleet.ios_connected_udids_or_raise()
            self.assertEqual(pokemon_fleet.ios_connected_udids(), set())


class KillSwitchTests(unittest.TestCase):
    def test_the_whole_guard_can_be_switched_off(self) -> None:
        """There must always be a way to say "leave my run alone"."""
        original_enabled, original_stdout = fleet_watchdog.ENABLED, sys.stdout
        try:
            fleet_watchdog.ENABLED = False
            self.assertIsNone(fleet_watchdog.install(attached=lambda: 0, rounds=1))
            self.assertIs(sys.stdout, original_stdout)
        finally:
            fleet_watchdog.ENABLED = original_enabled
            sys.stdout = original_stdout
