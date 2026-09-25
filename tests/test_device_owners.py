"""Routing follows the cable: synthetic buses only, no phone is ever touched."""

from __future__ import annotations

from tests import support as _test_support

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from sources import device_owners, fleet_entrypoint, pokemon_fleet

SERIAL_HERE = "TEST_SERIAL_HERE"
SERIAL_THERE = "TEST_SERIAL_THERE"
SERIAL_NOWHERE = "TEST_SERIAL_NOWHERE"
UDID_THERE = "TEST_UDID_THERE"


def snapshot(machine: str, serials: dict[str, str] | None = None, udids: set[str] | None = None,
             probed: tuple[str, ...] = device_owners.PLATFORMS) -> device_owners.Snapshot:
    return device_owners.Snapshot(
        machine=machine,
        android=serials or {},
        ios=frozenset(udids or set()),
        probed=frozenset(probed),
        attempted=frozenset(device_owners.PLATFORMS),
    )


class SnapshotTests(unittest.TestCase):
    def test_a_failed_probe_is_recorded_as_asked_but_undecided(self):
        with patch.object(pokemon_fleet, "adb_states_or_raise", side_effect=pokemon_fleet.ProbeError("no adb")), \
             patch.object(pokemon_fleet, "ios_connected_udids_or_raise", return_value={UDID_THERE}):
            result = device_owners.local_snapshot("here")
        self.assertFalse(result.looked_at("android"))
        self.assertIn("android", result.attempted)
        self.assertTrue(result.looked_at("ios"))
        self.assertFalse(result.holds("android", SERIAL_HERE))
        self.assertTrue(result.holds("ios", UDID_THERE))
        self.assertTrue(any("no adb" in error for error in result.errors))

    def test_an_unauthorized_phone_does_not_count_as_attached(self):
        result = snapshot("here", {SERIAL_HERE: "unauthorized"})
        self.assertFalse(result.holds("android", SERIAL_HERE))

    def test_a_narrower_probe_folds_into_the_earlier_one(self):
        first = device_owners.Snapshot(machine="here", android={SERIAL_HERE: "device"},
                                       probed=frozenset({"android"}), attempted=frozenset({"android"}))
        second = device_owners.Snapshot(machine="here", ios=frozenset({UDID_THERE}),
                                        probed=frozenset({"ios"}), attempted=frozenset({"ios"}))
        merged = first.merged(second)
        self.assertTrue(merged.holds("android", SERIAL_HERE))
        self.assertTrue(merged.holds("ios", UDID_THERE))

    def test_a_remote_probe_survives_a_host_without_the_module(self):
        failure = subprocess.CompletedProcess([], 1, "", "/x/python: No module named sources.device_owners\n")
        with patch.object(subprocess, "run", return_value=failure):
            result = device_owners.remote_snapshot("worker", {"ssh_host": "worker.example", "project_dir": "~/pogo"})
        self.assertFalse(result.looked_at("android"))
        self.assertIn("No module named", result.errors[0])

    def test_a_remote_probe_reads_the_hosts_json(self):
        payload = json.dumps(snapshot("worker", {SERIAL_THERE: "device"}).as_dict())
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, payload, "")):
            result = device_owners.remote_snapshot("worker", {"ssh_host": "worker.example", "project_dir": "~/pogo"})
        self.assertTrue(result.holds("android", SERIAL_THERE))


class RoutingTests(unittest.TestCase):
    """The fixture is deliberately mis-tagged: every phone's `machine` is wrong."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        registry = {
            "version": 1,
            "machines": {
                "controller": {"local": True},
                "worker": {"ssh_host": "worker.example", "project_dir": "~/pogo"},
            },
            "devices": {
                "phone-here": {"platform": "android", "serial": SERIAL_HERE, "machine": "worker",
                               "operations": {"gifts": {}, "trade": {}}},
                "phone-there": {"platform": "android", "serial": SERIAL_THERE, "machine": "controller",
                                "operations": {"gifts": {}, "trade": {}}},
                "phone-nowhere": {"platform": "android", "serial": SERIAL_NOWHERE, "machine": "worker",
                                  "operations": {"gifts": {}}},
                "phone-untagged": {"platform": "android", "serial": SERIAL_THERE, "operations": {"gifts": {}}},
            },
        }
        (root / "pokemon-fleet.yaml").write_text(yaml.safe_dump(registry))
        env = patch.dict(os.environ, {"POGO_CONFIG_DIR": str(root), "POGO_MACHINE": "controller"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("POGO_FLEET_CONFIG", None)
        os.environ.pop("POGO_DEVICE_DISCOVERY", None)
        fleet_entrypoint._DISCOVERY = None
        fleet_entrypoint._ANNOUNCED.clear()
        self.addCleanup(setattr, fleet_entrypoint, "_DISCOVERY", None)
        self.remote = patch.object(
            device_owners, "remote_snapshot",
            side_effect=lambda machine, config, platforms=device_owners.PLATFORMS: snapshot(
                machine, {SERIAL_THERE: "device"}),
        )
        self.remote_probe = self.remote.start()
        self.addCleanup(self.remote.stop)

    def local(self, **kwargs):
        return patch.object(device_owners, "local_snapshot",
                            return_value=snapshot("controller", {SERIAL_HERE: "device"}, **kwargs))

    def test_a_phone_on_this_computer_stays_here_whatever_the_tag_says(self):
        with self.local():
            routed = fleet_entrypoint.route_machine_arguments(["--devices", "phone-here", "--plan"])
        self.assertEqual(set(routed), {"controller"})
        self.remote_probe.assert_not_called()

    def test_a_phone_the_local_bus_cannot_see_goes_to_the_only_other_computer(self):
        with self.local():
            routed = fleet_entrypoint.route_machine_arguments(["--devices", "phone-there", "--plan"])
        self.assertEqual(set(routed), {"worker"})
        # Two computers leave one candidate, so no Mac is woken to confirm it.
        self.remote_probe.assert_not_called()

    def test_a_dead_local_probe_leaves_the_configured_tag_in_charge(self):
        broken = device_owners.Snapshot(machine="controller", attempted=frozenset(device_owners.PLATFORMS),
                                        errors=("android: no adb",))
        with patch.object(device_owners, "local_snapshot", return_value=broken):
            routed = fleet_entrypoint.route_machine_arguments(["--devices", "phone-here", "--plan"])
        self.assertEqual(set(routed), {"worker"})

    def test_an_untagged_phone_with_no_bus_answer_is_a_clear_error(self):
        broken = device_owners.Snapshot(machine="controller", attempted=frozenset(device_owners.PLATFORMS),
                                        errors=("android: no adb",))
        with patch.object(device_owners, "local_snapshot", return_value=broken):
            with self.assertRaisesRegex(pokemon_fleet.FleetError, "not attached to any configured computer"):
                fleet_entrypoint.route_machine_arguments(["--devices", "phone-untagged", "--plan"])

    def test_an_unplugged_phone_keeps_its_tag_so_that_host_reports_it(self):
        with self.local():
            routed = fleet_entrypoint.route_machine_arguments(["--devices", "phone-nowhere", "--plan"])
        self.assertEqual(set(routed), {"worker"})

    def test_a_pair_is_refused_by_where_the_phones_are_not_by_their_tags(self):
        with self.local():
            with self.assertRaisesRegex(pokemon_fleet.FleetError, "same host"):
                fleet_entrypoint.route_machine_arguments(["--pair", "phone-here", "phone-there", "--plan"])

    def test_a_pair_on_one_computer_is_allowed_though_both_tags_disagree(self):
        both = snapshot("controller", {SERIAL_HERE: "device", SERIAL_THERE: "device"})
        with patch.object(device_owners, "local_snapshot", return_value=both):
            routed = fleet_entrypoint.route_machine_arguments(["--pair", "phone-here", "phone-there", "--plan"])
        self.assertEqual(set(routed), {"controller"})

    def test_an_unregistered_serial_is_routed_to_the_computer_holding_it(self):
        with self.local():
            routed = fleet_entrypoint.route_machine_arguments(["--devices", f"android:{SERIAL_HERE}", "--plan"])
        self.assertEqual(set(routed), {"controller"})

    def test_discovery_can_be_turned_off_to_route_on_tags_alone(self):
        with patch.dict(os.environ, {"POGO_DEVICE_DISCOVERY": "off"}), self.local() as probe:
            routed = fleet_entrypoint.route_machine_arguments(["--devices", "phone-here", "--plan"])
        self.assertEqual(set(routed), {"worker"})
        probe.assert_not_called()

    def test_every_device_in_one_command_shares_a_single_bus_survey(self):
        with self.local() as probe:
            routed = fleet_entrypoint.route_machine_arguments(
                ["--devices", "phone-here", "phone-there", "phone-nowhere", "--plan"])
        self.assertEqual(set(routed), {"controller", "worker"})
        self.assertEqual(probe.call_count, 1)

    def test_a_third_computer_is_probed_to_break_a_real_ambiguity(self):
        registry_path = Path(self.temp.name) / "pokemon-fleet.yaml"
        registry = yaml.safe_load(registry_path.read_text())
        registry["machines"]["spare"] = {"ssh_host": "spare.example", "project_dir": "~/pogo"}
        registry["devices"]["phone-there"].pop("machine")
        registry_path.write_text(yaml.safe_dump(registry))
        holders = {"worker": {SERIAL_THERE: "device"}}
        with patch.object(device_owners, "remote_snapshot",
                          side_effect=lambda machine, config, platforms=device_owners.PLATFORMS: snapshot(
                              machine, holders.get(machine, {}))) as probe, self.local():
            routed = fleet_entrypoint.route_machine_arguments(["--devices", "phone-there", "--plan"])
        self.assertEqual(set(routed), {"worker"})
        self.assertEqual({call.args[0] for call in probe.call_args_list}, {"spare", "worker"})


class SurveyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        registry = {
            "version": 1,
            "machines": {"controller": {"local": True},
                         "worker": {"ssh_host": "worker.example", "project_dir": "~/pogo"}},
            "devices": {
                "phone-here": {"platform": "android", "serial": SERIAL_HERE, "machine": "worker",
                               "operations": {"gifts": {}}},
                "phone-nowhere": {"platform": "android", "serial": SERIAL_NOWHERE, "machine": "worker",
                                  "operations": {"gifts": {}}},
            },
        }
        (self.root / "pokemon-fleet.yaml").write_text(yaml.safe_dump(registry))
        env = patch.dict(os.environ, {"POGO_CONFIG_DIR": str(self.root), "POGO_MACHINE": "controller"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("POGO_FLEET_CONFIG", None)

    def test_the_survey_names_the_holder_the_mismatch_and_the_stranger(self):
        fleet = pokemon_fleet.load_fleet(self.root / "pokemon-fleet.yaml")
        with patch.object(device_owners, "local_snapshot",
                          return_value=snapshot("controller", {SERIAL_HERE: "device", "TEST_SERIAL_GUEST": "device"})), \
             patch.object(device_owners, "remote_snapshot", return_value=snapshot("worker")):
            rows, snapshots = device_owners.survey(fleet)
            strangers = device_owners.unregistered_serials(fleet, snapshots)
        rows_by_name = {row.name: row for row in rows}
        self.assertEqual(rows_by_name["phone-here"].attached_to, "controller")
        self.assertTrue(rows_by_name["phone-here"].mismatched)
        self.assertIsNone(rows_by_name["phone-nowhere"].attached_to)
        self.assertFalse(rows_by_name["phone-nowhere"].mismatched)
        self.assertEqual(strangers, {"controller": ["TEST_SERIAL_GUEST"]})

    def test_the_survey_stays_on_the_coordinator(self):
        self.assertTrue(fleet_entrypoint.coordinator_only(["owners", "--json"]))
        self.assertFalse(fleet_entrypoint.coordinator_only(["status"]))
        self.assertFalse(fleet_entrypoint.coordinator_only(["gifts", "--devices", "all"]))


if __name__ == "__main__":
    unittest.main()
