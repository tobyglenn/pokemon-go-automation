from __future__ import annotations

from tests import support as _test_support

import argparse
import asyncio
from contextlib import ExitStack
from pathlib import Path
from typing import Sequence
import unittest
from unittest.mock import AsyncMock, patch

from sources import pokemon_fleet


def spec(name: str) -> pokemon_fleet.DeviceSpec:
    # The fleet's iPhones are all named "iphone-...", and which platform a
    # phone is decides how it gets out of a trade, so the fake has to answer
    # this the same way the real spec does.
    platform = "ios" if name.startswith(("iphone", "ios-")) else "android"
    return pokemon_fleet.DeviceSpec(
        name=name,
        platform=platform,
        enabled=True,
        config={"platform": platform, "serial": name, "operations": {"trade": {}}},
        base_dir=Path("/tmp"),
    )


class FakeScreen:
    """What a FakeController hands out instead of a screenshot: it knows what
    it is showing, so the patched vision below can read it back."""

    def __init__(self, controller: "FakeController"):
        self.controller = controller

    @property
    def screen(self) -> str:
        return self.controller.screens[0]

    @property
    def dialog_button(self) -> tuple[int, int] | None:
        """Where the patched find_dialog_button reads a pill off this screen."""
        return self.controller.dialog_button

    @property
    def dialog_label(self) -> tuple[int, int] | None:
        """Where the patched find_dialog_label_button reads CANCEL off it."""
        return self.controller.dialog_label


class FakeController:
    """A phone that walks a scripted list of screens as it is stepped back.

    Each answered tap moves it one screen along the list; the last screen is
    where it stays, so a one-item list is a phone that never budges.
    """

    DEFAULT_COORDINATES = {"X_BTN": [1, 2], "TRADE_CANCEL_YES_BTN": [3, 4]}

    def __init__(
        self,
        name: str,
        screens: Sequence[str] = ("friend",),
        coordinates: dict[str, list[int]] | None = None,
        dialog_button: tuple[int, int] | None = None,
        dialog_label: tuple[int, int] | None = (357, 920),
        in_front: bool | None = True,
    ):
        self.spec = spec(name)
        self.screens = list(screens)
        self.coordinates = (
            self.DEFAULT_COORDINATES if coordinates is None else coordinates
        )
        # Where this phone's dialogs draw their pill, or None for a dialog whose
        # button the vision cannot find.
        self.dialog_button = dialog_button
        # Where CANCEL sits under the pill; None for a prompt whose label
        # vision cannot find.
        self.dialog_label = dialog_label
        # What the phone answers when asked whether Pokémon GO is in front.
        self.in_front = in_front
        self.taps: list[str] = []

    def _advance(self) -> None:
        if len(self.screens) > 1:
            self.screens.pop(0)

    def point(self, name: str) -> list[int] | None:
        return self.coordinates.get(name)

    async def screenshot(self) -> FakeScreen:
        return FakeScreen(self)

    def image_point(self, image: object, name: str) -> tuple[int, int]:
        return (1, 2)

    def point_from_image(
        self, image: object, pixel: tuple[int, int]
    ) -> tuple[int, int]:
        return pixel

    async def tap_step(self, name: str) -> None:
        self.taps.append(name)
        self._advance()

    async def tap_point(self, point: Sequence[int]) -> None:
        named = next(
            (name for name, value in self.coordinates.items() if list(point) == value),
            str(list(point)),
        )
        self.taps.append(named)
        self._advance()

    async def game_in_front(self) -> bool | None:
        return self.in_front

    async def back(self) -> None:
        self.taps.append("BACK")
        self._advance()


async def no_sleep(_: float) -> None:
    await asyncio.sleep(0)


def fake_vision() -> ExitStack:
    """Read the fake phones' scripted screens wherever the real vision runs."""
    stack = ExitStack()
    stack.enter_context(
        patch(
            "sources.trade_ios_android.screen_metrics",
            side_effect=lambda image, point: {"screen": image.screen},
        )
    )
    stack.enter_context(
        patch(
            "sources.trade_ios_android.state_matches",
            side_effect=lambda expected, metrics: metrics["screen"] == expected,
        )
    )
    stack.enter_context(
        patch(
            "sources.trade_ios_android.describe_state",
            side_effect=lambda image, action_point: (
                "dialog" if image.screen == "exit_prompt" else image.screen,
                {"screen": image.screen},
            ),
        )
    )
    stack.enter_context(
        patch(
            "sources.trade_ios_android.find_dialog_button",
            side_effect=lambda image: image.dialog_button,
        )
    )
    # The real one reads the dialog's words; here a phone showing "expired" is
    # on the notice and every other dialog is one that asks a question.
    stack.enter_context(
        patch(
            "sources.trade_ios_android.is_trade_expired_dialog",
            side_effect=lambda image: image.screen == "expired",
        )
    )
    stack.enter_context(
        patch(
            "sources.trade_ios_android.is_trade_limit_dialog",
            side_effect=lambda image: image.screen == "capped",
        )
    )
    # A notice the game raised by itself, on top of the screen the step wanted:
    # the "notice" screen is that one, and no other screen is.
    stack.enter_context(
        patch(
            "sources.trade_ios_android.incidental_notice",
            side_effect=lambda image: (
                "New Mega Level available!" if image.screen == "notice" else None
            ),
        )
    )
    # Same for the one dialog that must never be answered with its pill.
    stack.enter_context(
        patch(
            "sources.trade_ios_android.is_exit_game_dialog",
            side_effect=lambda image: image.screen == "exit_prompt",
        )
    )
    stack.enter_context(
        patch(
            "sources.trade_ios_android.find_dialog_label_button",
            side_effect=lambda image: image.dialog_label,
        )
    )
    return stack


class RecoverToFriendTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_stranded_phone_is_stepped_back(self) -> None:
        """The tall_device's picker: X_BTN drops it into a lobby, BACK leaves that."""
        home = FakeController("tall_device")
        picker = FakeController("android-one", ["selection", "lobby", "friend"])

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0):
            recovered = await pokemon_fleet.recover_to_friend([home, picker], no_sleep)

        self.assertTrue(recovered)
        self.assertEqual(picker.taps, ["X_BTN", "BACK"])
        # The phone that was already home is never touched.
        self.assertEqual(home.taps, [])

    async def test_a_cancel_dialog_is_answered(self) -> None:
        """BACK inside an open trade raises this; nothing else clears it."""
        stuck = FakeController("tall_device", ["dialog", "friend"])

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0):
            recovered = await pokemon_fleet.recover_to_friend([stuck], no_sleep)

        self.assertTrue(recovered)
        self.assertEqual(stuck.taps, ["TRADE_CANCEL_YES_BTN"])

    async def test_a_dialog_s_button_is_read_out_of_the_picture(self) -> None:
        """A run meets several dialogs and their buttons sit at different
        heights, so where the pill is beats where one mapped coordinate says."""
        stuck = FakeController("tall_device", ["dialog", "friend"], dialog_button=(7, 8))

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0):
            recovered = await pokemon_fleet.recover_to_friend([stuck], no_sleep)

        self.assertTrue(recovered)
        self.assertEqual(stuck.taps, ["[7, 8]"])

    async def test_an_unrecognised_screen_is_left_alone(self) -> None:
        """The whole point of naming screens: a blind BACK here is what turned
        a stranded phone into one stuck on a dialog nothing answered."""
        lost = FakeController("tall_device", ["unknown"])

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0), patch.object(
            pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")
        ):
            recovered = await pokemon_fleet.recover_to_friend([lost], no_sleep)

        self.assertFalse(recovered)
        self.assertEqual(lost.taps, [])

    async def test_a_dialog_with_no_pill_and_no_mapped_yes_is_left_alone(self) -> None:
        """Neither route to a button: pressing anything here would be a guess."""
        unmapped = FakeController("tall_device", ["dialog"], coordinates={"X_BTN": [1, 2]})

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0), patch.object(
            pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")
        ):
            recovered = await pokemon_fleet.recover_to_friend([unmapped], no_sleep)

        self.assertFalse(recovered)
        self.assertEqual(unmapped.taps, [])

    async def test_a_phone_that_never_comes_home_is_reported(self) -> None:
        """A screen still on show is a screen the button did not leave, so the
        second visit tries the other way out rather than the same button four
        times. The android-two spent a whole recovery flipping picker -> detail
        screen -> picker doing exactly that."""
        stuck = FakeController("tall_device", ["selection"])

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0), patch.object(
            pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")
        ):
            recovered = await pokemon_fleet.recover_to_friend([stuck], no_sleep)

        self.assertFalse(recovered)
        rounds = pokemon_fleet.FRIEND_RECOVERY_ROUNDS
        self.assertEqual(stuck.taps, ["X_BTN"] + ["BACK"] * (rounds - 1))

    async def test_an_ios_phone_falls_back_to_the_corner_door(self) -> None:
        """iOS has no BACK key; the door is its only other way out."""
        stuck = FakeController(
            "ios-two",
            ["selection"],
            {"X_BTN": [1, 2], "TRADE_EXIT_BTN": [41, 57]},
        )

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0), patch.object(
            pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")
        ):
            recovered = await pokemon_fleet.recover_to_friend([stuck], no_sleep)

        self.assertFalse(recovered)
        rounds = pokemon_fleet.FRIEND_RECOVERY_ROUNDS
        self.assertEqual(stuck.taps, ["X_BTN"] + ["TRADE_EXIT_BTN"] * (rounds - 1))


class MapTests(unittest.IsolatedAsyncioTestCase):
    """A phone on the map has left the trade, and there is nothing on that
    screen recovery may press: BACK is the exit prompt, and the way back to a
    friend goes through the trainer profile, which a trade run has no
    coordinates for."""

    async def test_nothing_is_pressed_on_the_map(self) -> None:
        stuck = FakeController("android-two", ["map"])

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0), patch.object(
            pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")
        ):
            recovered = await pokemon_fleet.recover_to_friend([stuck], no_sleep)

        self.assertFalse(recovered)
        self.assertEqual(stuck.taps, [])

    async def test_a_phone_with_the_game_shut_is_named_apart(self) -> None:
        """The launcher shows a pokéball too — the game's own icon — so the
        picture alone cannot tell a wandering phone from a closed game."""
        closed = FakeController("android-two", ["map"], in_front=False)

        with fake_vision():
            state, pressed = await pokemon_fleet.step_towards_friend(closed, FakeScreen(closed))

        self.assertEqual(state, "off_game")
        self.assertIsNone(pressed)
        self.assertEqual(closed.taps, [])

    async def test_an_iphone_on_the_map_is_still_the_map(self) -> None:
        """iOS cannot be asked which app is in front, and a guess of "closed"
        would be the wrong one to make."""
        stuck = FakeController("ios-two", ["map"], in_front=None)

        with fake_vision():
            state, pressed = await pokemon_fleet.step_towards_friend(stuck, FakeScreen(stuck))

        self.assertEqual(state, "map")
        self.assertIsNone(pressed)


class ExitPromptTests(unittest.IsolatedAsyncioTestCase):
    """"Do you want to exit Pokémon GO?" — OK is the topmost pill on it, and
    recovery answers every other dialog by pressing the topmost pill. A android-two
    reached this screen with one recovery round left to spend."""

    async def test_the_exit_prompt_is_answered_with_cancel(self) -> None:
        stuck = FakeController("android-two", ["exit_prompt", "friend"], dialog_button=(367, 798))

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0):
            recovered = await pokemon_fleet.recover_to_friend([stuck], no_sleep)

        self.assertTrue(recovered)
        self.assertEqual(stuck.taps, ["[357, 920]"])

    async def test_a_prompt_with_no_readable_cancel_is_left_alone(self) -> None:
        """Standing in front of the prompt costs a run; pressing OK costs the
        game itself, on a phone nobody is watching."""
        stuck = FakeController(
            "android-two", ["exit_prompt"], dialog_button=(367, 798), dialog_label=None
        )

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0), patch.object(
            pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")
        ):
            recovered = await pokemon_fleet.recover_to_friend([stuck], no_sleep)

        self.assertFalse(recovered)
        self.assertEqual(stuck.taps, [])


class EmptyHandedTradeTests(unittest.IsolatedAsyncioTestCase):
    """The screen a phone sits on once the other trainer has offered and it has
    not. Nothing named it, so recovery left it alone for every round it had and
    the phone was still standing in a trade when the run ended."""

    async def test_android_steps_out_with_back(self) -> None:
        stuck = FakeController("android-two", ["empty_lobby", "dialog", "friend"])

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0):
            recovered = await pokemon_fleet.recover_to_friend([stuck], no_sleep)

        self.assertTrue(recovered)
        self.assertEqual(stuck.taps, ["BACK", "TRADE_CANCEL_YES_BTN"])

    async def test_ios_uses_the_door_in_the_corner(self) -> None:
        stuck = FakeController(
            "ios-two",
            ["empty_lobby", "friend"],
            {"X_BTN": [1, 2], "TRADE_EXIT_BTN": [41, 57]},
        )
        stuck.spec = pokemon_fleet.DeviceSpec(
            name="ios-two",
            platform="ios",
            enabled=True,
            config={"platform": "ios"},
            base_dir=Path("/tmp"),
        )

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0):
            recovered = await pokemon_fleet.recover_to_friend([stuck], no_sleep)

        self.assertTrue(recovered)
        self.assertEqual(stuck.taps, ["TRADE_EXIT_BTN"])

    async def test_an_iphone_with_no_door_mapped_is_left_alone(self) -> None:
        """Rather than pressing the disc in the middle, which opens the picker
        and comes straight back here."""
        stuck = FakeController("ios-two", ["empty_lobby"], {"X_BTN": [1, 2]})
        stuck.spec = pokemon_fleet.DeviceSpec(
            name="ios-two",
            platform="ios",
            enabled=True,
            config={"platform": "ios"},
            base_dir=Path("/tmp"),
        )

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            recovered = await pokemon_fleet.recover_to_friend([stuck], no_sleep)

        self.assertFalse(recovered)
        self.assertEqual(stuck.taps, [])


class RecoveryDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_screens_a_failed_recovery_gave_up_on_are_kept(self) -> None:
        """Recovery that runs out of rounds used to say only that the phones
        did not come home, and the screens were gone by the time anyone looked."""
        home = FakeController("android-two")
        stuck = FakeController("ios-two", ["unknown"])

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0),
            patch.object(
                pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")
            ) as saved,
        ):
            recovered = await pokemon_fleet.recover_to_friend([home, stuck], no_sleep)

        self.assertFalse(recovered)
        saved.assert_called_once()
        # Only the phone that never made it: the one that is home is not part
        # of what needs explaining. Every round of it, though — one last frame
        # cannot say whether a phone sat still or went round in a circle, and
        # that was exactly the question the android-two's ping-pong raised.
        controllers, images, step = saved.call_args.args
        self.assertEqual({c.spec.name for c in controllers}, {"ios-two"})
        rounds = pokemon_fleet.FRIEND_RECOVERY_ROUNDS + 1
        self.assertEqual(len(images), rounds)
        self.assertEqual(step, "RECOVERY")
        self.assertEqual(
            saved.call_args.kwargs["names"],
            [f"ios-two-round{index}" for index in range(rounds)],
        )


class GuardRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_friend_guard_steps_back_instead_of_raising(self) -> None:
        """The live stop: both phones left on the Pokémon picker by a dead run."""
        controllers = [
            FakeController("tall_device", ["selection", "friend"]),
            FakeController("android-one", ["selection", "friend"]),
        ]

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0):
            await pokemon_fleet.guard_trade_state(controllers, "friend", "TRADE_BTN", no_sleep)

        self.assertEqual([c.taps for c in controllers], [["X_BTN"], ["X_BTN"]])

    async def test_a_deep_strand_is_not_cut_off_by_the_guard_s_looks(self) -> None:
        """Four screens from home, one more than the guard has plain looks."""
        deep = FakeController("tall_device", ["next", "selection", "lobby", "dialog", "friend"])

        with fake_vision(), patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0):
            await pokemon_fleet.guard_trade_state([deep], "friend", "TRADE_BTN", no_sleep)

        # The detail screen leaves on BACK, not X_BTN: there is no X on it, and
        # the mapped one lands on the menu disc underneath.
        self.assertEqual(deep.taps, ["X_BTN", "X_BTN", "BACK", "TRADE_CANCEL_YES_BTN"])

    async def test_mid_sequence_states_are_never_stepped_back(self) -> None:
        """Only the friend screen is recoverable; mid-sequence is left alone."""
        controllers = [FakeController("tall_device", ["selection"]), FakeController("android-one", ["selection"])]

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.FleetError):
                await pokemon_fleet.guard_trade_state(controllers, "lobby", "CONFIRM_BTN", no_sleep)

        self.assertEqual([c.taps for c in controllers], [[], []])

    async def test_the_stop_names_the_screen_each_phone_is_on(self) -> None:
        """What an operator reads. The metrics that used to be dumped here went
        to metrics.txt beside the screenshots."""
        controllers = [FakeController("tall_device", ["dialog"]), FakeController("android-one", ["selection"])]

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "FRIEND_RECOVERY_DELAY", 0),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.FleetError) as raised:
                await pokemon_fleet.guard_trade_state(controllers, "lobby", "CONFIRM_BTN", no_sleep)

        message = str(raised.exception)
        self.assertIn("tall_device is on a dialog", message)
        self.assertIn("android-one is on the Pokémon picker", message)
        self.assertNotIn("action_green", message)


PICKER_COORDINATES = {
    "X_BTN": [1, 2],
    "TRADE_CANCEL_YES_BTN": [3, 4],
    "FIRST_PKMN_BTN": [5, 6],
}


class PickerReselectTests(unittest.IsolatedAsyncioTestCase):
    """The live stop on the worker host: search_pale passes the selection guard on a
    picker whose list has not rendered, so FIRST_PKMN_BTN hits nothing and that
    phone is still on the picker when its partner is already on a detail
    screen."""

    async def test_only_the_phone_left_on_the_picker_selects_again(self) -> None:
        moved_on = FakeController("android-two", ["next"], PICKER_COORDINATES)
        stalled = FakeController(
            "ios-two", ["selection", "next"], PICKER_COORDINATES
        )

        with fake_vision():
            await pokemon_fleet.guard_trade_state(
                [moved_on, stalled], "next", "NEXT_BTN", no_sleep
            )

        self.assertEqual(stalled.taps, ["FIRST_PKMN_BTN"])
        # The phone that did advance must not be tapped again: a second
        # FIRST_PKMN_BTN there would put the two sides out of step.
        self.assertEqual(moved_on.taps, [])

    async def test_reselecting_is_bounded_and_then_stops_the_run(self) -> None:
        """A picker that will not open anything is refusing, not lagging: the
        live case was one Deino the trade was not allowed to take. Retrying the
        cycle walks both phones into another trade for the game to cancel, so
        the stop has to be one the retry loop passes on."""
        stuck = FakeController("ios-two", ["selection"], PICKER_COORDINATES)
        waits: list[float] = []

        async def record(seconds: float) -> None:
            waits.append(seconds)

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.TradeStopped) as raised:
                await pokemon_fleet.guard_trade_state(
                    [stuck], "next", "NEXT_BTN", record
                )

        self.assertEqual(
            stuck.taps, ["FIRST_PKMN_BTN"] * pokemon_fleet.PICKER_RESELECT_ROUNDS
        )
        message = str(raised.exception)
        self.assertIn("picker on ios-two would not open anything", message)
        # And it says so straight away rather than sitting out the rest of the
        # guard's allowance in front of a screen that is not going to change.
        self.assertEqual(len(waits), pokemon_fleet.PICKER_RESELECT_ROUNDS)

    async def test_a_phone_on_another_screen_is_not_reselected(self) -> None:
        """Only the picker answers to FIRST_PKMN_BTN. Tapping it on a dialog
        would be the blind guess the named screens exist to avoid."""
        elsewhere = FakeController("ios-two", ["dialog"], PICKER_COORDINATES)

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.FleetError):
                await pokemon_fleet.guard_trade_state(
                    [elsewhere], "next", "NEXT_BTN", no_sleep
                )

        self.assertEqual(elsewhere.taps, [])


class DailyTradingLimitTests(unittest.IsolatedAsyncioTestCase):
    """"Daily trading limit reached. Come back tomorrow to trade more." — the
    day is over, and no retry, recovery or search change reaches past that.
    Unnamed it cost three retries and four recoveries, reported as a screen
    mismatch."""

    async def test_the_notice_is_pressed_and_the_run_stopped(self) -> None:
        capped = FakeController("android-two", ["capped"], dialog_button=(7, 8))

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.TradeStopped) as raised:
                await pokemon_fleet.guard_trade_state(
                    [capped], "selection", "FIRST_PKMN_BTN", no_sleep
                )

        self.assertEqual(capped.taps, ["[7, 8]"])
        message = str(raised.exception)
        self.assertIn("daily trading limit", message)
        self.assertIn("tomorrow", message)

    async def test_it_is_not_retried_as_a_screen_mismatch(self) -> None:
        """A stop, not a failure: the pair is walked home and left there."""
        capped = FakeController("android-two", ["capped"], dialog_button=(7, 8))

        self.assertTrue(issubclass(pokemon_fleet.TradeStopped, pokemon_fleet.FleetError))
        with (
            fake_vision(),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.TradeStopped):
                await pokemon_fleet.guard_trade_state(
                    [capped], "selection", "FIRST_PKMN_BTN", no_sleep
                )


class TradeExpiredTests(unittest.IsolatedAsyncioTestCase):
    """Pokémon GO cancels a trade it has been kept waiting on. The notice it
    leaves is not a screen the sequence can go on from, so the cycle is given
    up — but the notice is cleared first, or the retry starts against it."""

    async def test_the_notice_is_pressed_and_the_cycle_given_up(self) -> None:
        expired = FakeController("android-two", ["expired"], dialog_button=(7, 8))

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.FleetError) as raised:
                await pokemon_fleet.guard_trade_state(
                    [expired], "next", "NEXT_BTN", no_sleep
                )

        self.assertEqual(expired.taps, ["[7, 8]"])
        message = str(raised.exception)
        self.assertIn("The trade expired before NEXT_BTN", message)
        self.assertIn("android-two", message)

    async def test_the_guard_does_not_sit_out_its_allowance_first(self) -> None:
        """The point of naming it: waiting in front of this screen achieves
        nothing, so it is answered on the first look rather than the last."""
        expired = FakeController("android-two", ["expired"], dialog_button=(7, 8))
        waits: list[float] = []

        async def record(seconds: float) -> None:
            waits.append(seconds)

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.FleetError):
                await pokemon_fleet.guard_trade_state(
                    [expired], "next", "NEXT_BTN", record
                )

        self.assertEqual(waits, [])

    async def test_the_phone_that_is_where_it_should_be_is_left_alone(self) -> None:
        ready = FakeController("ios-two", ["next"], dialog_button=(7, 8))
        expired = FakeController("android-two", ["expired"], dialog_button=(7, 8))

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.FleetError):
                await pokemon_fleet.guard_trade_state(
                    [ready, expired], "next", "NEXT_BTN", no_sleep
                )

        self.assertEqual(ready.taps, [])
        self.assertEqual(expired.taps, ["[7, 8]"])

    async def test_a_dialog_that_asks_a_question_is_never_pressed_here(self) -> None:
        """"Are you sure you want to trade this Pokémon?" wears the same card
        and the same green pill. Answering that one puts a trade through."""
        asking = FakeController("android-two", ["dialog"], dialog_button=(7, 8))

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.FleetError):
                await pokemon_fleet.guard_trade_state(
                    [asking], "lobby", "CONFIRM_BTN", no_sleep
                )

        self.assertEqual(asking.taps, [])

    async def test_a_notice_with_no_pill_falls_back_to_the_mapped_button(self) -> None:
        expired = FakeController("android-two", ["expired"])

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.FleetError):
                await pokemon_fleet.guard_trade_state(
                    [expired], "next", "NEXT_BTN", no_sleep
                )

        self.assertEqual(expired.taps, ["TRADE_CANCEL_YES_BTN"])


class IncidentalNoticeTests(unittest.IsolatedAsyncioTestCase):
    """"New Mega Level available!" landing on the post-trade card.

    The game raises it by itself when a traded Pokémon's mega level moves, and
    it stopped two runs at X_BTN — 2026-09-02 and 2026-09-08, both the android-one —
    because the screen the step wanted was underneath it the whole time.
    """

    async def test_the_notice_is_pressed_away_and_the_step_carries_on(self) -> None:
        covered = FakeController("android-one", ["notice", "post_trade"], dialog_button=(7, 8))
        ready = FakeController("tall_device", ["post_trade"])

        with fake_vision():
            await pokemon_fleet.guard_trade_state(
                [covered, ready], "post_trade", "X_BTN", no_sleep
            )

        # Pressed once, and the guard returned rather than raising: the trade
        # goes on from the card that was under it.
        self.assertEqual(covered.taps, ["[7, 8]"])
        self.assertEqual(ready.taps, [])

    async def test_a_notice_that_will_not_clear_is_not_pressed_forever(self) -> None:
        stuck = FakeController("android-one", ["notice"], dialog_button=(7, 8))

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.FleetError):
                await pokemon_fleet.guard_trade_state(
                    [stuck], "post_trade", "X_BTN", no_sleep
                )

        self.assertEqual(
            stuck.taps, ["[7, 8]"] * pokemon_fleet.NOTICE_DISMISS_ROUNDS
        )

    async def test_a_notice_with_no_pill_is_left_alone(self) -> None:
        """Nothing mapped is pressed in its place: every coordinate this run
        holds is a trade button, and the trade underneath is finished."""
        covered = FakeController("android-one", ["notice"])

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.FleetError):
                await pokemon_fleet.guard_trade_state(
                    [covered], "post_trade", "X_BTN", no_sleep
                )

        self.assertEqual(covered.taps, [])

    async def test_a_dialog_that_asks_a_question_is_not_a_notice(self) -> None:
        """Only the words say which is which, and the ones that ask about the
        trade are answered by the branches that know what that trade was."""
        asking = FakeController("android-two", ["dialog"], dialog_button=(7, 8))

        with (
            fake_vision(),
            patch.object(pokemon_fleet, "save_trade_diagnostics", return_value=Path("/tmp/x")),
        ):
            with self.assertRaises(pokemon_fleet.FleetError):
                await pokemon_fleet.guard_trade_state(
                    [asking], "post_trade", "X_BTN", no_sleep
                )

        self.assertEqual(asking.taps, [])


class CycleRetryTests(unittest.IsolatedAsyncioTestCase):
    def args(self, count: int) -> argparse.Namespace:
        return argparse.Namespace(
            count=count, delay_modifier=0, dry_run=False, start_step=None
        )

    async def test_a_failed_trade_is_retried_rather_than_ending_the_run(self) -> None:
        attempts: list[int] = []

        async def sequence(*args: object, **kwargs: object) -> None:
            attempts.append(1)
            if len(attempts) == 2:
                raise pokemon_fleet.FleetError("Screen mismatch before X_BTN")

        controller = FakeController("tall_device")
        controller.pointer = AsyncMock()
        controller.quit = AsyncMock()

        with (
            patch.object(pokemon_fleet, "connect_trade_controller", AsyncMock(return_value=controller)),
            patch.object(pokemon_fleet, "execute_trade_sequence", side_effect=sequence),
            patch.object(pokemon_fleet, "recover_to_friend", AsyncMock(return_value=True)),
            patch("sources.trade_ios_android.active_gift_runner_pids", return_value=[]),
        ):
            await pokemon_fleet.run_trade([spec("tall_device")], self.args(3))

        # Three trades asked for, one of them failed once: four cycles, and the
        # failed trade still counts as owed rather than spent.
        self.assertEqual(len(attempts), 4)

    async def test_the_run_gives_up_after_consecutive_failures(self) -> None:
        async def always_fails(*args: object, **kwargs: object) -> None:
            raise pokemon_fleet.FleetError("Screen mismatch before TRADE_BTN")

        controller = FakeController("tall_device")
        controller.pointer = AsyncMock()
        controller.quit = AsyncMock()

        with (
            patch.object(pokemon_fleet, "connect_trade_controller", AsyncMock(return_value=controller)),
            patch.object(pokemon_fleet, "execute_trade_sequence", side_effect=always_fails),
            patch.object(pokemon_fleet, "recover_to_friend", AsyncMock(return_value=True)),
            patch("sources.trade_ios_android.active_gift_runner_pids", return_value=[]),
        ):
            with self.assertRaises(pokemon_fleet.FleetError):
                await pokemon_fleet.run_trade([spec("tall_device")], self.args(100))

    async def test_a_stop_is_not_retried(self) -> None:
        """The picker refusing to open anything answers the same way every
        cycle, and each retry walks both phones into a trade the game then
        cancels — which is where the "Trade expired." notices came from."""
        attempts: list[int] = []

        async def stops(*args: object, **kwargs: object) -> None:
            attempts.append(1)
            raise pokemon_fleet.TradeStopped("The Pokémon picker would not open anything")

        controller = FakeController("tall_device")
        controller.pointer = AsyncMock()
        controller.quit = AsyncMock()
        recover = AsyncMock(return_value=True)

        with (
            patch.object(pokemon_fleet, "connect_trade_controller", AsyncMock(return_value=controller)),
            patch.object(pokemon_fleet, "execute_trade_sequence", side_effect=stops),
            patch.object(pokemon_fleet, "recover_to_friend", recover),
            patch("sources.trade_ios_android.active_gift_runner_pids", return_value=[]),
        ):
            with self.assertRaises(pokemon_fleet.TradeStopped):
                await pokemon_fleet.run_trade([spec("tall_device")], self.args(100))

        self.assertEqual(len(attempts), 1)
        # Walked home all the same: two phones left standing in a trade are
        # what the game cancels, and the notice is what the next run opens on.
        recover.assert_awaited_once()

    async def test_a_failure_that_cannot_be_recovered_still_stops_the_run(self) -> None:
        async def always_fails(*args: object, **kwargs: object) -> None:
            raise pokemon_fleet.FleetError("Screen mismatch before TRADE_BTN")

        controller = FakeController("tall_device")
        controller.pointer = AsyncMock()
        controller.quit = AsyncMock()

        with (
            patch.object(pokemon_fleet, "connect_trade_controller", AsyncMock(return_value=controller)),
            patch.object(pokemon_fleet, "execute_trade_sequence", side_effect=always_fails),
            patch.object(pokemon_fleet, "recover_to_friend", AsyncMock(return_value=False)),
            patch("sources.trade_ios_android.active_gift_runner_pids", return_value=[]),
        ):
            with self.assertRaises(pokemon_fleet.FleetError):
                await pokemon_fleet.run_trade([spec("tall_device")], self.args(100))
