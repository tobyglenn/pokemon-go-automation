from __future__ import annotations
from tests import support as _test_support
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml
from sources import config_paths, fleet_entrypoint, pokemon_fleet
from pokemon_go_automation.cli import init_config

class PrivateConfigTests(unittest.TestCase):
    def test_config_never_falls_back_to_checkout_or_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir()
            other = root / "working"
            other.mkdir()
            (other / "profile.yaml").write_text("private: misplaced\n")
            with patch.dict(os.environ, {"POGO_CONFIG_DIR": str(private)}), patch.object(Path, "cwd", return_value=other):
                with self.assertRaises(config_paths.ConfigPathError):
                    config_paths.find_config("profile.yaml")
                ((private / "profile.yaml").resolve()).write_text("private: intended\n")
                self.assertEqual(config_paths.find_config("profile.yaml"), (private / "profile.yaml").resolve())

    def test_missing_absolute_path_does_not_fall_back_by_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "profile.yaml").write_text("device: {}\n")
            with patch.dict(os.environ, {"POGO_CONFIG_DIR": str(root)}):
                with self.assertRaises(config_paths.ConfigPathError):
                    config_paths.find_config(str(root / "missing" / "profile.yaml"))

    def test_init_preserves_existing_private_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            registry = target / "pokemon-fleet.yaml"
            original = "version: 1\ndevices: {}\n"
            registry.write_text(original)
            init_config(target)
            self.assertEqual(registry.read_text(), original)
            self.assertTrue((target / "ios-gifter.yaml").exists())
            self.assertFalse((target / "machines.example.yaml").exists())

class FriendFilePathTests(unittest.TestCase):
    def test_private_paths_rebase_to_each_workers_roots(self):
        import add_friends
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.dict(os.environ, {"POGO_CONFIG_DIR": str(root / "controller-config"), "POGO_STATE_DIR": str(root / "controller-state")}):
                args = add_friends._friend_file_arguments(["--codes-file", str(root / "controller-config" / "friends.txt"), "--history=" + str(root / "controller-state" / "history.json")], portable=True)
            with patch.dict(os.environ, {"POGO_CONFIG_DIR": str(root / "worker-config"), "POGO_STATE_DIR": str(root / "worker-state")}):
                resolved = add_friends._friend_file_arguments(args, portable=False)
            self.assertEqual(resolved, ["--codes-file", str(root / "worker-config" / "friends.txt"), "--history=" + str(root / "worker-state" / "history.json")])

    def test_relative_codes_are_loaded_from_private_configuration(self):
        import add_friends
        resolved = add_friends._friend_file_arguments(["--codes-file", "friends.txt"], portable=False)
        self.assertEqual(resolved[1], str(config_paths.private_config_dir() / "friends.txt"))

class MachineRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        registry = {"version": 1, "machines": {
            "controller": {"local": True},
            "worker": {"ssh_host": "worker.example", "project_dir": "~/a folder/pogo", "python": ".venv/bin/python", "config_dir": "~/.config/pogo"},
        }, "devices": {
            "android-one": {"platform": "android", "serial": "TEST_SERIAL_A", "machine": "controller", "operations": {"gifts": {}}},
            "android-two": {"platform": "android", "serial": "TEST_SERIAL_B", "machine": "worker", "operations": {"gifts": {}}},
        }}
        (root / "pokemon-fleet.yaml").write_text(yaml.safe_dump(registry))
        # These cover the configured-tag fallback on its own; bus discovery, which
        # overrides the tags when a phone has moved, is covered in
        # tests/test_device_owners.py with synthetic buses.
        env = patch.dict(os.environ, {"POGO_CONFIG_DIR": str(root), "POGO_MACHINE": "controller",
                                      "POGO_DEVICE_DISCOVERY": "off"})
        env.start(); self.addCleanup(env.stop)
        os.environ.pop("POGO_FLEET_CONFIG", None)

    def test_all_devices_are_discovered_on_every_host(self):
        routed = fleet_entrypoint.route_machine_arguments(["--devices", "all", "--plan"])
        self.assertEqual(set(routed), {"controller", "worker"})
        self.assertTrue(all("all" in argv and "--allow-empty" in argv for argv in routed.values()))

    def test_explicit_devices_go_only_to_their_configured_host(self):
        routed = fleet_entrypoint.route_machine_arguments(["--devices", "android-two", "--plan"])
        self.assertEqual(set(routed), {"worker"})

    def test_equals_form_selection_stays_on_its_owner(self):
        routed = fleet_entrypoint.route_machine_arguments(["--devices=android-two", "--plan"])
        self.assertEqual(set(routed), {"worker"})
        self.assertEqual(routed["worker"], ["--devices", "android-two", "--plan"])

    def test_pair_cannot_span_hosts(self):
        with self.assertRaisesRegex(pokemon_fleet.FleetError, "same host"):
            fleet_entrypoint.route_machine_arguments(["--pair", "android-one", "android-two", "--plan"])

    def test_remote_command_preserves_spaces_and_prevents_recursive_fanout(self):
        commands = fleet_entrypoint._commands_for_machines("gift.py", ["--devices", "all", "--plan"])
        self.assertIn("--local", commands["controller"])
        self.assertIn("worker.example", commands["worker"])
        shell = commands["worker"][-1]
        self.assertIn('"$HOME"/\'a folder/pogo\'', shell)
        self.assertIn("-m gift --local", shell)
        self.assertIn("POGO_CONFIG_DIR=~/.config/pogo", shell)

    def test_every_workers_line_says_which_computer_it_came_from(self):
        """The remote worker wakes, boots and probes while the local one is
        already reporting phones, so its lines land late among battle output."""
        self.assertEqual(
            fleet_entrypoint.label_line("remote", "[gbl-day] Target devices (2): moto-g\n"),
            "[remote] [gbl-day] Target devices (2): moto-g",
        )

    def test_a_line_that_already_names_its_machine_is_left_alone(self):
        """Each worker labels its own readout; two labels would read worse."""
        self.assertEqual(
            fleet_entrypoint.label_line("remote", "[remote] gbl on moto-g\n"),
            "[remote] gbl on moto-g",
        )

    def test_relaying_labels_a_workers_whole_output(self):
        import io

        class FakeProcess:
            stdout = io.StringIO("first\n[remote] second\n")

        printed = io.StringIO()
        fleet_entrypoint._relay("remote", FakeProcess(), printed)
        self.assertEqual(printed.getvalue(), "[remote] first\n[remote] second\n")

    def test_a_worker_with_nothing_to_say_relays_nothing(self):
        class Silent:
            stdout = None

        fleet_entrypoint._relay("remote", Silent())  # must not raise

    def test_the_local_worker_is_told_which_computer_it_is(self):
        """Both halves of the fan-out print; only one knew its own name."""
        started = {}

        import io

        class FakeProcess:
            def __init__(self, machine):
                self.machine = machine
                self.stdout = io.StringIO("")

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        def fake_popen(command, cwd=None, env=None, **_spawn):
            machine = "controller" if "--local" in command else "worker"
            started[machine] = env
            return FakeProcess(machine)

        with patch.object(fleet_entrypoint.subprocess, "Popen", fake_popen):
            fleet_entrypoint.run_all_machines("gift.py", ["--devices", "all", "--plan"])

        self.assertEqual(started["controller"]["POGO_MACHINE"], "controller")
        # The remote worker gets its name inside the ssh command instead, and
        # the ssh client itself has no business carrying one.
        self.assertIsNone(started["worker"])

    def test_coordinator_registry_override_is_not_forwarded_as_remote_path(self):
        config = config_paths.default_config("pokemon-fleet.yaml")
        commands = fleet_entrypoint._commands_for_machines("gift.py", ["--config", str(config), "--devices", "all", "--plan"])
        self.assertNotIn(str(config), commands["worker"][-1])

if __name__ == "__main__":
    unittest.main()
