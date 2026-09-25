from __future__ import annotations

from tests import support as _test_support

import unittest
from unittest.mock import AsyncMock, patch

from sources import gbl_android as gbl
from tests.test_gbl_league_rotation import CARDS, AndroidLeagueRotationTests


class StuckScreenRestartTests(unittest.IsolatedAsyncioTestCase):
    """24 Sep: ph-1 and the moto-g each sat on a screen no recovery tap moved.

    Each leg quit on it after 25 reads, the next leg found the same screen, and
    gbl_day gave up on both phones.  The first time, the game is restarted.
    """

    run_reads = AndroidLeagueRotationTests.run_reads

    async def stuck(self, reads):
        restart = AsyncMock()
        with (
            patch.object(gbl, "restart_game", restart),
            patch.object(gbl, "recover_to_gbl", AsyncMock()),
            patch.object(gbl, "save_unknown_frame", lambda *_args: None),
        ):
            messages = await self.run_reads(reads, stops=True)
        return restart, messages

    async def test_the_first_dead_end_restarts_the_game(self) -> None:
        blocked = [("blocked", None, None, CARDS)] * gbl.STRAY_LIMIT
        restart, messages = await self.stuck(blocked * 2)
        restart.assert_awaited_once()
        self.assertTrue(any("restarting Pokemon GO" in m for m in messages), messages)
        self.assertIn("  Off the battle flow for 25 reads; stopping", messages)


if __name__ == "__main__":
    unittest.main()
