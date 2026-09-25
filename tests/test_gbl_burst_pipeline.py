"""A normal combat burst must not stall for a duplicate screen transfer."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sources import gbl_android as gbl


class BurstPromptTests(unittest.IsolatedAsyncioTestCase):
    async def check_prompt(self, label):
        device = SimpleNamespace(label="example-phone")
        old_frame, observed = object(), object()
        with (
            patch.object(gbl, "charged_move_point", return_value=[100, 200]),
            patch.object(gbl, "tap", AsyncMock()) as tap,
            patch.object(gbl, "read_charged_prompt", AsyncMock()) as read,
            patch.object(gbl, "charged_prompt_from_frame", AsyncMock(return_value=label)) as detect,
            patch.object(gbl, "wait", AsyncMock()) as wait,
            patch.object(gbl, "charged_minigame", AsyncMock()) as minigame,
            patch.object(gbl, "capture_charged_rating", AsyncMock()),
        ):
            result = await gbl.probe_charged_after_fast_attacks(
                device, old_frame, observed_frame=observed
            )
        tap.assert_not_awaited()
        read.assert_not_awaited()
        detect.assert_awaited_once_with(observed)
        if label is None:
            self.assertFalse(result)
            wait.assert_not_awaited()
            minigame.assert_not_awaited()
        else:
            self.assertTrue(result)
            minigame.assert_awaited_once_with(device, observed)

    async def test_ordinary_combat_has_no_extra_tap_read_or_settle(self):
        await self.check_prompt(None)

    async def test_get_ready_still_runs_charged_minigame(self):
        await self.check_prompt("get ready")

    async def test_swipe_still_runs_charged_minigame(self):
        await self.check_prompt("swipe")
