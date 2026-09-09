from __future__ import annotations

from tests import support as _test_support

import json
from pathlib import Path
import unittest
from unittest import mock

from sources import pokemon_fleet


def device(name: str, platform: str, identifier: str) -> pokemon_fleet.DeviceSpec:
    config = {
        "platform": platform,
        "operations": {"berries": {}},
    }
    if platform == "android":
        config["serial"] = identifier
    else:
        config["udid"] = identifier
    return pokemon_fleet.DeviceSpec(
        name=name,
        platform=platform,
        enabled=True,
        config=config,
        base_dir=Path("/tmp"),
    )


class ConnectedDeviceSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.iphone = device("ios-one", "ios", "iphone-udid")
        self.tall_device = device("tall_device", "android", "tall_device-serial")
        self.fleet = pokemon_fleet.FleetConfig(
            path=Path("/tmp/pokemon-fleet.yaml"),
            devices={"ios-one": self.iphone, "tall_device": self.tall_device},
        )

    def test_all_selects_only_connected_configured_devices(self) -> None:
        with (
            mock.patch.object(
                pokemon_fleet,
                "load_appium_profile",
                return_value={"device": {"udid": "iphone-udid"}},
            ),
            mock.patch.object(
                pokemon_fleet, "ios_connected_udids", return_value={"iphone-udid"}
            ),
            mock.patch.object(pokemon_fleet, "adb_states", return_value={}),
            mock.patch.object(
                pokemon_fleet, "operation_readiness", return_value="ready"
            ),
        ):
            selected = pokemon_fleet.select_devices(
                self.fleet, ["all"], "berries"
            )

        self.assertEqual([spec.name for spec in selected], ["ios-one"])

    def test_explicit_disconnected_device_still_errors(self) -> None:
        with mock.patch.object(
            pokemon_fleet,
            "operation_readiness",
            return_value="unavailable: disconnected",
        ):
            with self.assertRaisesRegex(
                pokemon_fleet.FleetError,
                "tall_device berries: unavailable: disconnected",
            ):
                pokemon_fleet.select_devices(self.fleet, ["tall_device"], "berries")

    def test_all_plan_can_select_without_connection_checks(self) -> None:
        selected = pokemon_fleet.select_devices(
            self.fleet, ["all"], "berries", allow_unready=True
        )
        self.assertEqual(
            [spec.name for spec in selected], ["ios-one", "tall_device"]
        )

    def test_all_skips_a_connected_but_uncalibrated_device(self) -> None:
        def readiness(spec: pokemon_fleet.DeviceSpec, _operation: str) -> str:
            return "ready" if spec.name == "ios-one" else "needs calibration"

        with (
            mock.patch.object(
                pokemon_fleet,
                "load_appium_profile",
                return_value={"device": {"udid": "iphone-udid"}},
            ),
            mock.patch.object(
                pokemon_fleet, "ios_connected_udids", return_value={"iphone-udid"}
            ),
            mock.patch.object(
                pokemon_fleet, "adb_states", return_value={"tall_device-serial": "device"}
            ),
            mock.patch.object(
                pokemon_fleet, "operation_readiness", side_effect=readiness
            ),
        ):
            selected = pokemon_fleet.select_devices(
                self.fleet, ["all"], "berries"
            )

        self.assertEqual([spec.name for spec in selected], ["ios-one"])

    def test_all_adds_an_unregistered_connected_android(self) -> None:
        with (
            mock.patch.object(
                pokemon_fleet, "ios_connected_udids", return_value=set()
            ),
            mock.patch.object(
                pokemon_fleet,
                "adb_states",
                return_value={"new-android": "device"},
            ),
            mock.patch.object(
                pokemon_fleet, "operation_readiness", return_value="ready"
            ),
        ):
            selected = pokemon_fleet.select_devices(
                self.fleet, ["all"], "berries"
            )

        self.assertEqual([spec.identifier for spec in selected], ["new-android"])

    def test_all_can_be_empty_for_cross_machine_gbl_host(self) -> None:
        with (
            mock.patch.object(
                pokemon_fleet,
                "load_appium_profile",
                return_value={"device": {"udid": "iphone-udid"}},
            ),
            mock.patch.object(
                pokemon_fleet, "ios_connected_udids", return_value=set()
            ),
            mock.patch.object(pokemon_fleet, "adb_states", return_value={}),
        ):
            selected = pokemon_fleet.select_devices(
                self.fleet, ["all"], "gbl", allow_empty=True
            )

        self.assertEqual(selected, [])


class IOSConnectionDetectionTests(unittest.TestCase):
    def test_wired_phone_counts_while_coredevice_tunnel_is_restarting(self) -> None:
        self.assertTrue(
            pokemon_fleet.ios_device_is_connected(
                {
                    "connectionProperties": {
                        "transportType": "wired",
                        "tunnelState": "disconnected",
                    }
                }
            )
        )

    def test_offline_network_pairing_does_not_count(self) -> None:
        self.assertFalse(
            pokemon_fleet.ios_device_is_connected(
                {
                    "connectionProperties": {
                        "transportType": "localNetwork",
                        "tunnelState": "disconnected",
                    }
                }
            )
        )

    def test_connected_udids_returns_wired_phone_from_devicectl(self) -> None:
        payload = {
            "result": {
                "devices": [
                    {
                        "hardwareProperties": {"udid": "wired-iphone"},
                        "connectionProperties": {
                            "transportType": "wired",
                            "tunnelState": "disconnected",
                        },
                    },
                    {
                        "hardwareProperties": {"udid": "offline-iphone"},
                        "connectionProperties": {
                            "transportType": "localNetwork",
                            "tunnelState": "disconnected",
                        },
                    },
                ]
            }
        }
        with (
            mock.patch.object(
                pokemon_fleet.subprocess,
                "run",
                return_value=mock.Mock(returncode=0),
            ),
            mock.patch.object(Path, "exists", return_value=True),
            mock.patch.object(Path, "read_text", return_value=json.dumps(payload)),
        ):
            self.assertEqual(
                pokemon_fleet.ios_connected_udids(), {"wired-iphone"}
            )




class AndroidGBLDevicePreparationTests(unittest.IsolatedAsyncioTestCase):
    """The fleet entrypoint prepares phones itself instead of calling setup().

    A phone that reaches a battle without the timing measurement raises on its
    first fast attack and takes every Android phone on the host down with it,
    which no screen test can see.
    """

    def setUp(self) -> None:
        self.tall_device = device("tall_device", "android", "tall_device-serial")

    async def test_fleet_path_measures_input_cost_before_battling(self) -> None:
        from sources import gbl_android

        phone = mock.MagicMock()
        phone.serial = "tall_device-serial"
        phone.config = {"GBL_MOVE_BTN": [465, 1765]}

        client = mock.MagicMock()
        client.devices = mock.AsyncMock(return_value=[phone])

        with (
            mock.patch.object(gbl_android, "ClientAsync", return_value=client),
            mock.patch.object(gbl_android, "get_config", mock.AsyncMock()),
            mock.patch.object(
                gbl_android, "find_display_id", mock.AsyncMock(return_value=None)
            ),
            mock.patch.object(
                gbl_android, "measure_input_cost", mock.AsyncMock(return_value=0.68)
            ),
            # A phone this slow is offered a monkey channel; whether it gets one
            # is the transport's business, not this path's.
            mock.patch.object(
                gbl_android, "open_monkey", mock.AsyncMock(return_value=None)
            ),
        ):
            ready = await pokemon_fleet.connected_android_gbl_devices([self.tall_device])

        self.assertEqual(len(ready), 1)
        self.assertAlmostEqual(ready[0].input_cost, 0.68)

    def test_batches_shrink_on_a_slow_phone_and_never_reach_zero(self) -> None:
        from sources import gbl_android

        fast = gbl_android.batch_size(
            0.037, gbl_android.MOVE_BATCH_BUDGET,
            gbl_android.MOVE_TAPS_PER_BATCH, gbl_android.MOVE_TAPS_MINIMUM)
        slow = gbl_android.batch_size(
            0.68, gbl_android.MOVE_BATCH_BUDGET,
            gbl_android.MOVE_TAPS_PER_BATCH, gbl_android.MOVE_TAPS_MINIMUM)
        self.assertEqual(fast, gbl_android.MOVE_TAPS_PER_BATCH)
        self.assertEqual(slow, gbl_android.MOVE_TAPS_MINIMUM)
        self.assertGreaterEqual(slow, 1)

    def test_unprepared_device_falls_back_instead_of_raising(self) -> None:
        from sources import gbl_android

        bare = mock.MagicMock(spec=["serial"])
        self.assertEqual(
            gbl_android.input_cost(bare), gbl_android.INPUT_COST_DEFAULT
        )


if __name__ == "__main__":
    unittest.main()
