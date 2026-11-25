from __future__ import annotations
import unittest

from PIL import Image, ImageDraw

from sources import goplus_android


def draw_disc(image: Image.Image, centre: tuple[int, int], radius: int = 26) -> None:
    """The accessory disc as the game draws it: rose over navy, on a sky."""
    draw = ImageDraw.Draw(image)
    x, y = centre
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(148, 97, 122))
    draw.ellipse((x - radius, y, x + radius, y + radius), fill=(47, 82, 118))


class BluetoothStateTests(unittest.TestCase):
    def test_the_bonded_section_is_read_with_its_name(self) -> None:
        dump = (
            "  Bonded devices:\n"
            "    98:B6:E9:16:AA:BB [  LE  ][ 0x000000 ] Pokemon GO Plus\n"
            "\n"
            "  Scan Mode Changes:\n"
        )
        state = goplus_android.parse_bluetooth(dump)
        self.assertIn("98:B6:E9:16:AA:BB", state.bonded)
        self.assertEqual(state.bonded["98:B6:E9:16:AA:BB"], "Pokemon GO Plus")

    def test_the_section_ends_at_the_first_line_that_is_not_a_device(self) -> None:
        dump = (
            "  Bonded devices:\n"
            "mSnoopLogSettingAtEnable = empty\n"
            "  AdapterState:\n"
            "    98:B6:E9:16:AA:CC connected\n"
        )
        state = goplus_android.parse_bluetooth(dump)
        self.assertEqual(state.bonded, {})

    def test_old_connection_events_and_a_saved_bond_are_not_live(self) -> None:
        state = goplus_android.parse_bluetooth(
            "Bonded devices:\n"
            "    98:B6:E9:16:AA:BB [  LE  ] Pokemon GO Plus\n"
            "\nConnection Events:\n"
            "  11-25 09:26:31.216 DISCONNECTED 98:b6:e9:16:aa:bb reason=22\n"
            "  11-25 08:26:32.460 CONNECTED    98:b6:e9:16:aa:bb\n"
        )
        self.assertEqual(state.connected(), set())

    def test_only_the_games_current_client_connections_count(self) -> None:
        state = goplus_android.parse_bluetooth(
            "Bonded devices:\n"
            "    AA:BB:CC:DD:EE:01 [  LE  ] Pokémon GO Plus +\n"
            "\nGATT Client Map\n"
            "  com.nianticlabs.pokemongo (Registered)\n"
            "  Connections: 1\n"
            "    11-25 11:45:08 - 65323ms : AA:BB:CC:DD:EE:01 (6)\n"
            "\n  android.uid.bluetooth:1002 (Registered)\n"
            "  Connections: 1\n"
            "    11-25 11:45:25 - 46205ms : 98:B6:E9:16:CC:DD (8)\n"
        )
        self.assertEqual(state.connected(), {"AA:BB:CC:DD:EE:01"})


class AccessoryDiscTests(unittest.TestCase):
    def test_the_disc_is_found_in_the_picture(self) -> None:
        image = Image.new("RGB", (720, 1600), (85, 164, 224))
        draw_disc(image, (657, 192))
        found = goplus_android.find_accessory_disc(image)
        self.assertIsNotNone(found)
        # The point has to be on the disc, which is the only thing there.
        self.assertAlmostEqual(found[0], 657, delta=6)
        self.assertAlmostEqual(found[1], 192, delta=12)

    def test_a_map_without_the_disc_finds_nothing(self) -> None:
        image = Image.new("RGB", (720, 1600), (85, 164, 224))
        self.assertIsNone(goplus_android.find_accessory_disc(image))

    def test_the_pink_badge_further_down_the_column_is_not_the_disc(self) -> None:
        image = Image.new("RGB", (720, 1600), (85, 164, 224))
        draw = ImageDraw.Draw(image)
        draw.ellipse((628, 385, 688, 445), fill=(148, 97, 122))
        self.assertIsNone(goplus_android.find_accessory_disc(image))

    def test_the_picture_is_preferred_over_the_calibrated_point(self) -> None:
        image = Image.new("RGB", (720, 1600), (85, 164, 224))
        draw_disc(image, (657, 192))
        point = goplus_android.accessory_point(image, [10, 10])
        self.assertIsNotNone(point)
        self.assertGreater(point[0], 600)

    def test_the_calibrated_point_is_used_when_the_picture_has_nothing(self) -> None:
        image = Image.new("RGB", (720, 1600), (85, 164, 224))
        self.assertEqual(goplus_android.accessory_point(image, [11, 22]), [11, 22])

    def test_unmapped_screen_without_a_disc_is_left_alone(self) -> None:
        image = Image.new("RGB", (720, 1600), (85, 164, 224))
        point = goplus_android.accessory_point(image, None)
        self.assertIsNone(point)


DIALOG = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<hierarchy rotation="0">'
    '<node package="com.oplus.wirelesssettings" text="Pair with Pokemon GO Plus?" resource-id="android:id/alertTitle" bounds="[40,300][680,420]" />'
    '<node package="com.oplus.wirelesssettings" text="CANCEL" resource-id="android:id/button1" bounds="[420,560][640,680]" />'
    '<node package="com.oplus.wirelesssettings" text="PAIR" resource-id="android:id/button2" bounds="[120,560][340,680]" />'
    '</hierarchy>'
)


class PairingDialogTests(unittest.TestCase):
    def test_the_pair_button_is_found(self) -> None:
        self.assertEqual(goplus_android.pairing_dialog_button(DIALOG), [230, 620])

    def test_a_pair_button_that_names_no_wearable_is_left_alone(self) -> None:
        other = DIALOG.replace("Pair with Pokemon GO Plus?", "Pair a device")
        self.assertIsNone(goplus_android.pairing_dialog_button(other))

    def test_a_non_bluetooth_package_cannot_supply_the_pair_button(self) -> None:
        xml = DIALOG.replace('com.oplus.wirelesssettings', 'com.example.app')
        self.assertIsNone(goplus_android.pairing_dialog_button(xml))

    def test_a_game_screen_is_not_a_dialog(self) -> None:
        self.assertIsNone(goplus_android.pairing_dialog_button('<hierarchy><node text="Game view" /></hierarchy>'))

    def test_broken_xml_is_not_a_dialog(self) -> None:
        self.assertIsNone(goplus_android.pairing_dialog_button('<hierarchy>'))


class SelectionTests(unittest.TestCase):
    class Spec:
        def __init__(self, name: str) -> None:
            self.name = name

    def test_all_selects_every_android_phone(self) -> None:
        specs = [self.Spec('sample-android-b'), self.Spec('sample-android-a')]
        chosen = goplus_android._wanted(
            type('Args', (), {'devices': ['all']})(), specs)
        self.assertEqual([spec.name for spec in chosen], ['sample-android-b', 'sample-android-a'])

    def test_named_devices_are_selected(self) -> None:
        specs = [self.Spec('sample-android-b'), self.Spec('sample-android-a')]
        chosen = goplus_android._wanted(
            type('Args', (), {'devices': ['sample-android-a']})(), specs)
        self.assertEqual([spec.name for spec in chosen], ['sample-android-a'])

    def test_no_devices_selects_every_android_phone(self) -> None:
        specs = [self.Spec('sample-android-b'), self.Spec('sample-android-a')]
        chosen = goplus_android._wanted(type('Args', (), {'devices': []})(), specs)
        self.assertEqual([spec.name for spec in chosen], ['sample-android-b', 'sample-android-a'])


if __name__ == '__main__':
    unittest.main()
