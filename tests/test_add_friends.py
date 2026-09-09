from __future__ import annotations

from tests import support as _test_support

import unittest
from unittest import mock

from contextlib import nullcontext
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace

from PIL import Image, ImageDraw

import add_friends


class CodeTests(unittest.TestCase):
    def test_default_is_one_hundred_codes_per_device(self) -> None:
        self.assertEqual(add_friends.DEFAULT_COUNT, 100)

    def test_extracts_and_deduplicates_public_codes(self) -> None:
        text = "Codes: 1234 5678 9012, 123456789012 and 0000-1111-2222"
        self.assertEqual(
            add_friends.extract_trainer_codes(text),
            ["123456789012", "000011112222"],
        )

    def test_rejects_non_twelve_digit_code(self) -> None:
        with self.assertRaises(add_friends.FriendAutomationError):
            add_friends.normalize_code("1234")

    def test_public_page_extraction_ignores_submission_placeholder(self) -> None:
        html = (
            '<input placeholder="1234 5678 9012">'
            '<button data-friend-code="1111 2222 3333">copy</button>'
        )

        self.assertEqual(add_friends._public_codes_from_page(html), ["111122223333"])

    def test_public_code_fetch_paginates_until_minimum(self) -> None:
        def response(*codes: str) -> mock.MagicMock:
            html = "".join(
                f'<button data-friend-code="{code}">copy</button>' for code in codes
            )
            result = mock.MagicMock()
            result.__enter__.return_value.read.return_value = html.encode()
            return result

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "codes.json"
            with mock.patch.object(
                add_friends,
                "urlopen",
                side_effect=[
                    response("1111 2222 3333", "4444 5555 6666"),
                    response("7777 8888 9999", "0000 1111 2222"),
                ],
            ) as opened:
                codes = add_friends.fetch_public_codes(
                    "https://pokemongofriendcodes.com/",
                    cache_path=cache,
                    minimum_count=3,
                )

        self.assertEqual(
            codes,
            ["111122223333", "444455556666", "777788889999", "000011112222"],
        )
        self.assertEqual(
            opened.call_args_list[1].args[0].full_url,
            "https://pokemongofriendcodes.com/?page=2",
        )

    def test_defaults_to_one_hundred_codes_from_requested_site(self) -> None:
        args = add_friends.parse_args([])

        self.assertEqual(args.count, 100)
        self.assertEqual(args.source_url, "https://pokemongofriendcodes.com/")


class ScreenDetectionTests(unittest.TestCase):
    def test_recognizes_send_cancel_confirmation_on_tall_android(self) -> None:
        image = Image.new("RGB", (1224, 2992), (45, 165, 145))
        draw = ImageDraw.Draw(image)
        draw.rectangle((35, 565, 1189, 1485), fill="white")
        draw.rounded_rectangle(
            (260, 850, 965, 990), radius=60, fill=(45, 210, 175)
        )
        draw.rectangle((510, 1100, 715, 1125), fill=(35, 190, 165))

        self.assertLess(add_friends.dialog_height_share(image), 0.40)
        self.assertIsNotNone(add_friends.find_confirmation_button(image))

    def test_finds_trainer_field_outline(self) -> None:
        image = Image.new("RGB", (750, 1334), (248, 255, 245))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (125, 376, 625, 459), radius=38, outline=(35, 209, 205), width=5
        )
        point = add_friends.find_trainer_field(image)
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point.x, 375, delta=5)
        self.assertAlmostEqual(point.y, 417, delta=5)

    def test_colored_map_bands_are_not_a_trainer_field(self) -> None:
        image = Image.new("RGB", (750, 1334), (95, 155, 178))
        draw = ImageDraw.Draw(image)
        draw.line((100, 300, 650, 300), fill=(35, 209, 205), width=5)
        draw.line((100, 380, 650, 380), fill=(35, 209, 205), width=5)
        self.assertIsNone(add_friends.find_trainer_field(image))

    def test_finds_filled_gradient_button_not_field_outline(self) -> None:
        image = Image.new("RGB", (750, 1334), (248, 255, 245))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (125, 376, 625, 459), radius=38, outline=(35, 209, 205), width=5
        )
        draw.rounded_rectangle(
            (192, 488, 558, 590), radius=48, fill=(48, 207, 178)
        )
        point = add_friends.find_gradient_button(image)
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point.x, 375, delta=5)
        self.assertAlmostEqual(point.y, 539, delta=5)

    def test_ignores_disabled_pale_gradient_button(self) -> None:
        image = Image.new("RGB", (750, 1334), (248, 255, 245))
        ImageDraw.Draw(image).rounded_rectangle(
            (192, 488, 558, 590), radius=48, fill=(208, 240, 219)
        )
        self.assertIsNone(add_friends.find_gradient_button(image))

    def test_finds_add_friend_icon_on_friends_list(self) -> None:
        image = Image.new("RGB", (750, 1334), "white")
        draw = ImageDraw.Draw(image)
        draw.ellipse((650, 120, 710, 200), outline=(245, 91, 49), width=9)
        draw.line((665, 155, 695, 155), fill=(245, 91, 49), width=8)
        draw.line((680, 140, 680, 170), fill=(245, 91, 49), width=8)
        point = add_friends.find_add_friend_button(image)
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point.x, 680, delta=8)
        self.assertAlmostEqual(point.y, 160, delta=10)

    def test_entry_preparation_opens_add_friend_from_friends_list(self) -> None:
        friends = Image.new("RGB", (750, 1334), "white")
        friends_draw = ImageDraw.Draw(friends)
        friends_draw.ellipse((650, 120, 710, 200), outline=(245, 91, 49), width=9)
        friends_draw.line((665, 155, 695, 155), fill=(245, 91, 49), width=8)
        friends_draw.line((680, 140, 680, 170), fill=(245, 91, 49), width=8)
        entry = Image.new("RGB", (750, 1334), (248, 255, 245))
        ImageDraw.Draw(entry).rounded_rectangle(
            (125, 376, 625, 459), radius=38, outline=(35, 209, 205), width=5
        )
        controller = mock.Mock()
        controller.label = "test-phone"
        controller.screenshot.side_effect = [friends, entry]

        with mock.patch.object(add_friends.time, "sleep"):
            image, field = add_friends.prepare_entry_screen(controller)

        self.assertIs(image, entry)
        self.assertAlmostEqual(field.x, 375, delta=5)
        controller.tap.assert_called_once()
        controller.swipe_up.assert_not_called()

    def test_entry_preparation_dismisses_tall_single_action_result(self) -> None:
        result = Image.new("RGB", (750, 1334), (45, 165, 145))
        result_draw = ImageDraw.Draw(result)
        result_draw.rectangle((40, 390, 710, 950), fill="white")
        result_draw.rounded_rectangle(
            (170, 760, 580, 870), radius=50, fill=(48, 207, 178)
        )
        entry = Image.new("RGB", (750, 1334), (248, 255, 245))
        ImageDraw.Draw(entry).rounded_rectangle(
            (125, 376, 625, 459), radius=38, outline=(35, 209, 205), width=5
        )
        controller = mock.Mock()
        controller.label = "test-phone"
        controller.screenshot.side_effect = [result, entry]

        with mock.patch.object(add_friends.time, "sleep"):
            image, field = add_friends.prepare_entry_screen(controller)

        self.assertIs(image, entry)
        self.assertAlmostEqual(field.x, 375, delta=5)
        controller.tap.assert_called_once()

    def test_entry_preparation_scrolls_field_out_from_under_close_button(self) -> None:
        # Pokémon GO pins the close "X" over the foot of the Add Friend page, so
        # a field resting there is untappable until the page is scrolled up.
        buried = Image.new("RGB", (750, 1334), (248, 255, 245))
        buried_draw = ImageDraw.Draw(buried)
        buried_draw.rounded_rectangle(
            (125, 1160, 625, 1243), radius=38, outline=(35, 209, 205), width=5
        )
        buried_draw.ellipse((330, 1135, 420, 1225), fill=(28, 133, 148))
        scrolled = Image.new("RGB", (750, 1334), (248, 255, 245))
        ImageDraw.Draw(scrolled).rounded_rectangle(
            (125, 376, 625, 459), radius=38, outline=(35, 209, 205), width=5
        )
        controller = mock.Mock()
        controller.label = "test-phone"
        controller.screenshot.side_effect = [buried, scrolled]

        with mock.patch.object(add_friends.time, "sleep"):
            image, field = add_friends.prepare_entry_screen(controller)

        self.assertIs(image, scrolled)
        controller.swipe_up.assert_called_once()
        self.assertTrue(add_friends.field_is_reachable(image, field))

    def test_entry_preparation_scrolls_field_up_from_below_the_fold(self) -> None:
        # On iPhone the field falls off the bottom of the freshly opened page,
        # leaving only its top border, so find_trainer_field sees nothing.
        buried = Image.new("RGB", (750, 1334), (248, 255, 245))
        ImageDraw.Draw(buried).line((155, 1291, 595, 1291), fill=(35, 209, 205), width=6)
        scrolled = Image.new("RGB", (750, 1334), (248, 255, 245))
        ImageDraw.Draw(scrolled).rounded_rectangle(
            (125, 376, 625, 459), radius=38, outline=(35, 209, 205), width=5
        )
        self.assertIsNone(add_friends.find_trainer_field(buried))
        self.assertTrue(add_friends.field_edge_below_fold(buried))

        controller = mock.Mock()
        controller.label = "test-phone"
        controller.screenshot.side_effect = [buried, scrolled]

        with mock.patch.object(add_friends.time, "sleep"):
            image, field = add_friends.prepare_entry_screen(controller)

        self.assertIs(image, scrolled)
        controller.swipe_up.assert_called_once()
        self.assertTrue(add_friends.field_is_reachable(image, field))

    def test_unrecognized_screen_is_not_scrolled(self) -> None:
        # Scrolling a screen we cannot identify would pan the overworld map.
        plain = Image.new("RGB", (750, 1334), (248, 255, 245))
        self.assertFalse(add_friends.field_edge_below_fold(plain))

    def test_field_under_close_button_is_reported_as_unreachable(self) -> None:
        image = Image.new("RGB", (750, 1334), (248, 255, 245))
        self.assertFalse(
            add_friends.field_is_reachable(image, add_friends.ImagePoint(375, 1201))
        )
        self.assertTrue(
            add_friends.field_is_reachable(image, add_friends.ImagePoint(375, 417))
        )

    def test_distinguishes_confirmation_and_result_dialog_sizes(self) -> None:
        confirmation = Image.new("RGB", (750, 1334), (45, 165, 145))
        result = confirmation.copy()
        ImageDraw.Draw(confirmation).rectangle((32, 257, 718, 1077), fill="white")
        ImageDraw.Draw(result).rectangle((40, 466, 709, 866), fill="white")
        self.assertGreaterEqual(add_friends.dialog_height_share(confirmation), 0.40)
        self.assertLess(add_friends.dialog_height_share(result), 0.40)

    def test_normal_add_friend_page_is_not_a_dialog(self) -> None:
        normal = Image.new("RGB", (750, 1334), (248, 255, 245))
        ImageDraw.Draw(normal).rectangle((0, 0, 749, 160), fill="white")
        self.assertEqual(add_friends.dialog_height_share(normal), 0.0)

    def test_two_action_dialog_is_not_dismissed_automatically(self) -> None:
        image = Image.new("RGB", (750, 1334), (45, 165, 145))
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 380, 710, 910), fill="white")
        draw.rounded_rectangle((170, 610, 580, 730), radius=55, fill=(45, 210, 175))
        draw.rectangle((300, 790, 450, 810), fill=(35, 190, 165))
        controller = mock.Mock()
        controller.label = "test-phone"

        with self.assertRaises(add_friends.FriendScreenError):
            add_friends._dismiss_small_dialog(controller, image)

        controller.tap.assert_not_called()

    def test_detects_invalid_trainer_code_toast(self) -> None:
        image = Image.new("RGB", (750, 1334), (248, 255, 245))
        ImageDraw.Draw(image).rounded_rectangle(
            (80, 630, 670, 700), radius=30, fill=(226, 118, 180)
        )
        self.assertTrue(add_friends.has_error_toast(image))

    def test_normal_add_friend_page_has_no_error_toast(self) -> None:
        image = Image.new("RGB", (750, 1334), (248, 255, 245))
        self.assertFalse(add_friends.has_error_toast(image))

    def test_detects_visible_ios_keyboard(self) -> None:
        image = Image.new("RGB", (750, 1334), "white")
        ImageDraw.Draw(image).rectangle((0, 900, 749, 980), fill=(30, 30, 30))
        self.assertTrue(add_friends.has_ios_keyboard(image))

    def test_plain_entry_screen_has_no_ios_keyboard(self) -> None:
        image = Image.new("RGB", (750, 1334), (248, 255, 245))
        self.assertFalse(add_friends.has_ios_keyboard(image))

    def test_medium_placeholder_digits_are_not_an_entered_code(self) -> None:
        image = Image.new("RGB", (750, 1334), (248, 255, 245))
        field = add_friends.ImagePoint(375, 402)
        ImageDraw.Draw(image).rectangle((300, 390, 450, 414), fill=(150, 170, 171))
        self.assertFalse(add_friends.trainer_field_has_code(image, field))


class IOSKeyboardTests(unittest.TestCase):
    def test_ios_uses_measured_keyboard_points_without_page_source(self) -> None:
        controller = object.__new__(add_friends.IOSFriendController)
        controller.label = "test-iphone"
        controller.viewport = {"width": 375, "height": 667}
        controller.driver = mock.Mock()
        controller._dismiss_known_system_alert = mock.Mock(return_value=False)
        image = Image.new("RGB", (750, 1334), "white")
        keyboard = image.copy()
        ImageDraw.Draw(keyboard).rectangle((0, 900, 749, 980), fill=(30, 30, 30))
        controller.screenshot = mock.Mock(return_value=keyboard)

        with mock.patch.object(add_friends.time, "sleep"):
            controller.focus_and_type(
                add_friends.ImagePoint(375, 400), image, "123456789012"
            )

        calls = controller.driver.execute_script.call_args_list
        self.assertEqual(
            calls, [mock.call("mobile: tap", {"x": 188, "y": 200})]
        )
        self.assertEqual(
            [
                add_friends.ImagePoint(
                    call.args[1]["actions"][0]["actions"][0]["x"],
                    call.args[1]["actions"][0]["actions"][0]["y"],
                )
                for call in controller.driver.execute.call_args_list
            ],
            [
                controller._keyboard_digit_points_from_viewport()[digit]
                for digit in "123456789012"
            ],
        )

    def test_ios_reopens_add_friend_instead_of_tapping_keyboard_clear(self) -> None:
        controller = object.__new__(add_friends.IOSFriendController)
        controller.label = "test-iphone"
        controller.viewport = {"width": 375, "height": 667}
        controller.driver = mock.Mock()
        controller._dismiss_known_system_alert = mock.Mock(return_value=False)
        image = Image.new("RGB", (750, 1334), (248, 255, 245))
        field = add_friends.ImagePoint(375, 400)
        ImageDraw.Draw(image).rectangle((300, 385, 450, 415), fill=(70, 100, 100))
        fresh = Image.new("RGB", (750, 1334), (248, 255, 245))
        keyboard = fresh.copy()
        ImageDraw.Draw(keyboard).rectangle((0, 900, 749, 980), fill=(30, 30, 30))
        controller.screenshot = mock.Mock(return_value=keyboard)

        with (
            mock.patch.object(add_friends.time, "sleep"),
            mock.patch.object(
                add_friends, "prepare_entry_screen", return_value=(fresh, field)
            ) as prepare,
        ):
            controller.focus_and_type(field, image, "123456789012")

        calls = controller.driver.execute_script.call_args_list
        self.assertEqual(calls[0], mock.call("mobile: tap", {"x": 188, "y": 336}))
        self.assertEqual(calls[1], mock.call("mobile: tap", {"x": 188, "y": 200}))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(controller.driver.execute.call_args_list), 12)
        prepare.assert_called_once_with(controller)

    def test_keyboard_row_includes_zero_at_measured_position(self) -> None:
        controller = object.__new__(add_friends.IOSFriendController)
        controller.viewport = {"width": 375, "height": 667}
        points = controller._keyboard_digit_points_from_viewport()

        self.assertEqual(points["1"], add_friends.ImagePoint(22, 480))
        self.assertEqual(points["0"], add_friends.ImagePoint(353, 480))
        self.assertEqual(
            controller._keyboard_delete_point_from_viewport(),
            add_friends.ImagePoint(353, 587),
        )

    def test_dismisses_only_known_paste_permission_alert(self) -> None:
        alert = mock.Mock()
        alert.text = '“Pokémon GO” would like to paste from “Mac”'
        driver = SimpleNamespace(switch_to=SimpleNamespace(alert=alert))

        with mock.patch.object(add_friends.time, "sleep"):
            dismissed = add_friends.IOSFriendController._dismiss_known_system_alert(
                driver, "test-iphone"
            )

        self.assertTrue(dismissed)
        alert.dismiss.assert_called_once_with()

    def test_dismisses_universal_clipboard_pasting_from_alert(self) -> None:
        alert = mock.Mock()
        alert.text = "Pasting from “controller-host”…"
        driver = SimpleNamespace(switch_to=SimpleNamespace(alert=alert))

        with mock.patch.object(add_friends.time, "sleep"):
            dismissed = add_friends.IOSFriendController._dismiss_known_system_alert(
                driver, "test-iphone"
            )

        self.assertTrue(dismissed)
        alert.dismiss.assert_called_once_with()

    def test_refuses_unknown_ios_system_alert(self) -> None:
        alert = mock.Mock()
        alert.text = "Allow location access?"
        driver = SimpleNamespace(switch_to=SimpleNamespace(alert=alert))

        with self.assertRaises(add_friends.FriendScreenError):
            add_friends.IOSFriendController._dismiss_known_system_alert(
                driver, "test-iphone"
            )

        alert.dismiss.assert_not_called()

    def test_cancel_input_does_not_send_a_synthetic_tap(self) -> None:
        controller = object.__new__(add_friends.IOSFriendController)
        controller.driver = mock.Mock()

        controller.cancel_input()

        controller.driver.execute_script.assert_not_called()

    def test_parses_coredevice_point_scale(self) -> None:
        output = "• bounds: (0.0, 0.0, 750.0, 1334.0)\n• pointScale: 2\n"
        self.assertEqual(
            add_friends.IOSFriendController._point_scale_from_output(output), 2.0
        )

    def test_rejects_coredevice_output_without_point_scale(self) -> None:
        with self.assertRaises(add_friends.FriendAutomationError):
            add_friends.IOSFriendController._point_scale_from_output("LCD primary")

    def test_extracts_digit_and_delete_centers_from_page_source(self) -> None:
        keys = "".join(
            f'<XCUIElementTypeKey type="XCUIElementTypeKey" name="{digit}" '
            f'x="{index * 10}" y="100" width="10" height="20" />'
            for index, digit in enumerate("1234567890")
        )
        source = (
            "<AppiumAUT>"
            + keys
            + '<XCUIElementTypeKey type="XCUIElementTypeKey" name="delete" '
            'x="300" y="200" width="40" height="40" />'
            + "</AppiumAUT>"
        )
        controller = object.__new__(add_friends.IOSFriendController)
        controller.label = "test-iphone"
        digits, delete = controller._keyboard_points_from_source(source)
        self.assertEqual(digits["1"], add_friends.ImagePoint(5, 110))
        self.assertEqual(digits["0"], add_friends.ImagePoint(95, 110))
        self.assertEqual(delete, add_friends.ImagePoint(320, 220))


class AndroidScreenshotTests(unittest.TestCase):
    def test_ignores_multidisplay_warning_before_png_signature(self) -> None:
        image = Image.new("RGB", (20, 30), "white")
        output = io.BytesIO()
        image.save(output, format="PNG")
        controller = object.__new__(add_friends.AndroidFriendController)
        controller.label = "test-android"
        controller._run = mock.Mock(
            return_value=mock.Mock(stdout=b"display warning\n" + output.getvalue())
        )
        self.assertEqual(controller.screenshot().size, (20, 30))

    def test_focuses_and_types_in_single_tap(self) -> None:
        controller = object.__new__(add_friends.AndroidFriendController)
        controller.label = "test-android"
        controller.tap = mock.Mock()
        controller._run = mock.Mock(return_value=mock.Mock(stdout=b""))
        controller.cancel_input = mock.Mock()
        image = Image.new("RGB", (20, 30), "white")

        with mock.patch.object(add_friends.time, "sleep"):
            controller.focus_and_type(add_friends.ImagePoint(10, 15), image, "123456789012")

        # Only the field is tapped. A second "neutral" tap to dismiss the
        # keyboard would land on SEND and submit the code early.
        self.assertEqual(controller.tap.call_count, 1)

    def test_hides_keyboard_with_back_instead_of_tapping_send(self) -> None:
        controller = object.__new__(add_friends.AndroidFriendController)
        controller.label = "test-android"
        controller.tap = mock.Mock()
        controller._run = mock.Mock(return_value=mock.Mock(stdout=b""))
        controller._keyboard_visible = mock.Mock(side_effect=[True, False])
        image = Image.new("RGB", (20, 30), "white")

        with mock.patch.object(add_friends.time, "sleep"):
            controller.focus_and_type(add_friends.ImagePoint(10, 15), image, "123456789012")
            controller.hide_keyboard_if_needed()

        self.assertEqual(controller.tap.call_count, 1)
        keyevents = [
            call.args for call in controller._run.call_args_list if "keyevent" in call.args
        ]
        self.assertTrue(any("KEYCODE_BACK" in args for args in keyevents))

    def test_does_not_press_back_when_keyboard_already_closed(self) -> None:
        controller = object.__new__(add_friends.AndroidFriendController)
        controller.label = "test-android"
        controller.tap = mock.Mock()
        controller._run = mock.Mock(return_value=mock.Mock(stdout=b""))
        controller._keyboard_visible = mock.Mock(return_value=False)
        image = Image.new("RGB", (20, 30), "white")

        with mock.patch.object(add_friends.time, "sleep"):
            controller.focus_and_type(add_friends.ImagePoint(10, 15), image, "123456789012")

        # A stray BACK on a closed keyboard exits the Add Friend page.
        self.assertFalse(
            any(
                "KEYCODE_BACK" in call.args
                for call in controller._run.call_args_list
            )
        )


class HistoryTests(unittest.TestCase):
    def test_concurrent_records_are_all_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = add_friends.History(Path(directory) / "history.json")
            threads = [
                threading.Thread(
                    target=history.record,
                    args=(f"device-{index % 2}", f"{index:012d}"),
                )
                for index in range(20)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(
                sum(len(records) for records in history.data["devices"].values()),
                20,
            )


class ConcurrentDeviceTests(unittest.TestCase):
    def test_main_starts_selected_devices_concurrently(self) -> None:
        specs = [
            SimpleNamespace(name="iphone", platform="ios"),
            SimpleNamespace(name="tall_device", platform="android"),
        ]
        barrier = threading.Barrier(2, timeout=2)

        def run_device(*_args, **_kwargs) -> int:
            barrier.wait()
            return 1

        with (
            mock.patch.object(add_friends.pokemon_fleet, "load_fleet"),
            mock.patch.object(
                add_friends.pokemon_fleet, "select_devices", return_value=specs
            ),
            mock.patch.object(
                add_friends.pokemon_fleet,
                "acquire_device_locks",
                return_value=nullcontext(),
            ),
            mock.patch.object(add_friends, "History"),
            mock.patch.object(add_friends, "run_device", side_effect=run_device),
        ):
            result = add_friends.main(
                ["--local", "--code", "123456789012", "--count", "1"]
            )

        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
