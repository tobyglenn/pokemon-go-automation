import os
import unittest
from unittest import mock

from sources import android_memory

MEMINFO = "MemTotal:        3816236 kB\nMemFree:           85244 kB\nMemAvailable:     870352 kB\n"


class AndroidMemoryTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(android_memory.FREED_ENV, None)
        os.environ.pop(android_memory.ENABLE_ENV, None)

    def tearDown(self):
        self.env.stop()

    def test_parses_mem_available(self):
        self.assertEqual(android_memory.parse_available_kb(MEMINFO), 870352)
        self.assertIsNone(android_memory.parse_available_kb("nothing"))

    def test_commands_never_stop_the_game_or_phone_apps(self):
        line = android_memory.free_commands()
        for package in ("pokemongo", "messaging", "dialer", "inputmethod"):
            self.assertNotIn(package, line)
        self.assertTrue(line.endswith("am kill-all"))

    def test_frees_each_serial_once_and_children_inherit(self):
        os.environ[android_memory.ENABLE_ENV] = "1"
        calls = []
        with mock.patch.object(android_memory, "_shell", side_effect=lambda s, c: calls.append((s, c)) or MEMINFO):
            android_memory.free_memory("SER1", "android-one")
            android_memory.free_memory("SER1", "android-one")
            android_memory.free_memory("SER2", "android-two")
        self.assertEqual(sum(1 for _s, c in calls if "force-stop" in c), 2)
        self.assertEqual(os.environ[android_memory.FREED_ENV], "SER1,SER2")

    def test_adb_failure_does_not_raise(self):
        os.environ[android_memory.ENABLE_ENV] = "1"
        with mock.patch.object(android_memory, "_shell", side_effect=OSError("no adb")):
            android_memory.free_memory("SER3", "android-three")
        self.assertNotIn(android_memory.FREED_ENV, os.environ)

    def test_does_not_stop_background_apps_without_opt_in(self):
        with mock.patch.object(android_memory, "_shell") as shell:
            android_memory.free_memory("SER3", "android-three")
        shell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
