from tests import support as _test_support
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from PIL import Image
from sources import gbl_android as g, gbl_vision as v


def boxes(*texts):
    return [v.OCRBox(text, 0.99, 100, 400 + i * 100, 400, 40)
            for i, text in enumerate(texts)]


class SeasonInfoTests(unittest.IsolatedAsyncioTestCase):
    def article(self):
        return boxes('GO Battle League: A Future Season',
                     'Dates and times below are listed in local', 'time.',
                     'Great League: Mega Edition', 'Ultra League: Mega Edition',
                     'Master League: Mega Edition')

    def test_article_is_not_game_card(self):
        self.assertTrue(v.season_info_visible(self.article()))
        self.assertFalse(v.gbl_card_visible(iter(self.article())))

    def test_real_menus_are_not_articles(self):
        for text in [('Choose your league', 'Great League', 'Master League'),
                     ('GO Battle League', 'Basic Rewards', 'BATTLE'),
                     ('GO Battle League', 'End of season rewards', 'BATTLE'),
                     ('Choose reward tier', 'Great League', 'Premium Rewards')]:
            self.assertFalse(v.season_info_visible(boxes(*text)))

    def test_article_prose_can_wrap_across_ocr_boxes(self):
        self.assertTrue(v.season_info_visible(boxes(
            'Dates and times below are', 'listed in local time.')))

    async def test_article_outranks_league_geometry(self):
        with patch.object(g, 'read_screen_state', return_value=('league', [300, 800])), \
             patch.object(g, 'frame_image', return_value=Image.new('RGB', (720, 1600))), \
             patch.object(v, 'recognize', return_value=self.article()):
            result = await g.smart_screen_state(None, SimpleNamespace())
        self.assertEqual(result[:3], ('season_info', None, 'GBL season information'))

    async def test_recovery_uses_back_without_tapping_prose(self):
        device = SimpleNamespace(label='test-handset')
        with patch.object(g, 'send_input', new_callable=AsyncMock) as send, \
             patch.object(g, 'tap', new_callable=AsyncMock) as tap:
            await g.recover_to_gbl(device, None, self.article(), 1)
        send.assert_awaited_once_with(device, 'input keyevent KEYCODE_BACK')
        tap.assert_not_awaited()

    async def test_orange_link_above_rewards_is_not_tapped(self):
        menu = boxes('GO BATTLE LEAGUE', 'A Future Season', 'BASIC REWARDS')
        for tile, expected in [(None, ('unconfirmed', None)),
                               ([300, 800], ('orange', [300, 800])),
                               ([300, 450], ('unconfirmed', None))]:
            with patch.object(g, 'read_screen_state', return_value=('orange', [300, 500])), \
                 patch.object(g, 'frame_image', return_value=Image.new('RGB', (720, 1600))), \
                 patch.object(v, 'recognize', return_value=menu), \
                 patch.object(v, 'reward_point', return_value=None), \
                 patch.object(g, 'find_reward_tile', return_value=tile):
                result = await g.smart_screen_state(None, SimpleNamespace())
            self.assertEqual(result[:2], expected)
