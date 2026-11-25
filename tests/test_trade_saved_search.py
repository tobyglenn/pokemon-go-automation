import asyncio
import json
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image, ImageDraw

from tests import support as _test_support  # noqa: F401
from sources import collect_trades as collect, trade_saved_search as saved


def box(text, x, y):
    return collect.OCRBox(text, 1, x, y, 50, 24)


class SavedSearchTests(unittest.TestCase):
    def test_donor_replaces_hundo_review_with_lucky_offer_and_excludes_perfect_ivs(self):
        from sources import storage_favorites as favorites
        policy = SimpleNamespace(receiver="primary", favorites=lambda _: {
            "SafeTrash": "!favorite&10", "HundoReview": "10,12", "ReceiverPriority": "!traded&12"})
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder)/"favorites.json"
            config.write_text(json.dumps({"common":{"98%":"4attack","Far Not 98":"distance100-"},
                                          "donor":{"Traded Lucky":"lucky&traded"}}))
            with mock.patch.object(favorites.config_paths,"default_config",return_value=config), \
                 mock.patch.object(favorites,"power_term",return_value="#ToPower,94&#ToPower,cp3000"), \
                 mock.patch.object(favorites.trading_accounts,"account_for",return_value="donor"):
                wanted = favorites.wanted("donor",policy)
        self.assertNotIn("HundoReview",wanted)
        # ToPower lists the candy targets; every other non-appraisal list hides them.
        self.assertEqual(wanted["ToPower"],"#ToPower,94&#ToPower,cp3000")
        for name,expr in wanted.items():
            if name not in {"98%","ToPower"}:
                self.assertIn("!#ToPower",expr.split("&"))
        self.assertIn("2*,#ToTrade",wanted["LuckyOffer"].split("&"))
        self.assertEqual(wanted["98%"],"4attack")
        for name,expr in wanted.items():
            if name not in {"98%","ToPower"}:
                self.assertIn("!4*",expr.split("&"))
            self.assertNotIn("4star",expr.casefold())

    def test_compact_full_android_drawer_skips_toggles_only_when_ime_is_hidden(self):
        image = Image.new("RGB", (720, 1600))
        boxes = [box("Favorite", 20, 300), box("See More", 500, 300)]
        for discs, state, toggles in [(12, "mInputShown=false", 0), (12, "mInputShown=true", 2),
                                      (12, "", 2), (11, "mInputShown=false", 2)]:
            with self.subTest(discs=discs, state=state):
                adb = mock.Mock(return_value=SimpleNamespace(stdout=state))
                phone = SimpleNamespace(ios=False, target=SimpleNamespace(_adb=adb),
                                        read=mock.AsyncMock(return_value=(image, boxes)))
                toggle = mock.AsyncMock(return_value=(image, boxes))
                with mock.patch.object(saved, "icons", return_value=[(10, 10)] * discs), \
                     mock.patch.object(saved, "_toggle", toggle):
                    self.assertEqual(asyncio.run(saved.compact_drawer(phone, image, boxes)), (image, boxes))
                self.assertEqual(toggle.await_count, toggles)

    def test_android_compact_drawer_hides_confirmed_ime_before_reusing_the_frame(self):
        first = Image.new("RGB", (720, 1600))
        after = first.copy()
        boxes = [box("See More", 500, 300), box("Recommended", 150, 1400)]
        adb = mock.Mock(side_effect=[SimpleNamespace(stdout="mInputShown=true"), None,
                                    SimpleNamespace(stdout="mInputShown=false")])
        phone = SimpleNamespace(ios=False, target=SimpleNamespace(_adb=adb),
                                read=mock.AsyncMock(return_value=(after, boxes)))
        with mock.patch.object(saved.asyncio, "sleep", mock.AsyncMock()), \
             mock.patch.object(saved, "_toggle", mock.AsyncMock(side_effect=AssertionError)):
            image, _ = asyncio.run(saved.compact_drawer(phone, first, boxes))
        self.assertIs(image, after)
        self.assertIn(mock.call("shell", "input keyevent KEYCODE_BACK"), adb.call_args_list)

    def test_named_picker_relocates_the_tile_after_confirmed_android_keyboard_dismissal(self):
        from unittest.mock import Mock

        image = Image.new("RGB", (720, 1600))
        drawer = [box("Favorite",20,300),box("See More",500,300)]
        grid = [box("POKÉMON",300,50),box("(32)",300,90)]
        first, second = box("ReceiverLucky",400,900), box("ReceiverLucky",400,700)
        adb = Mock(side_effect=[SimpleNamespace(stdout="mInputShown=true"), None,
                                SimpleNamespace(stdout="mInputShown=false")])
        phone = SimpleNamespace(name="donor", ios=False, target=SimpleNamespace(_adb=adb),
                                read=mock.AsyncMock(return_value=(image,drawer)), tap_box=mock.AsyncMock())
        same=mock.AsyncMock(return_value=(image,drawer))
        with mock.patch.object(saved.asyncio,"sleep",mock.AsyncMock()), \
             mock.patch.object(saved,"_top_of_favorites",same), mock.patch.object(saved,"compact_drawer",same), \
             mock.patch.object(saved,"find_in_favorites",mock.AsyncMock(side_effect=[
                 (image,drawer,first),(image,drawer,second)])), \
             mock.patch.object(saved.trade_local,"settle",mock.AsyncMock(side_effect=[
                 (False,image,drawer),(True,image,grid)])):
            self.assertEqual(asyncio.run(saved._open_named_tile(phone,"ReceiverLucky","!traded",image,drawer)),
                             (image,grid))
        self.assertEqual(phone.tap_box.await_args_list,[mock.call(first,image),mock.call(second,image)])
        self.assertEqual(sum("input keyevent KEYCODE_BACK" in call.args for call in adb.call_args_list),1)

    def test_clipped_favorite_requires_native_name_before_reuse(self):
        image = object()
        point = (12, 17)
        async def pages(_phone):
            yield 0, (image, [("TradedLuck У", point)])
        phone = SimpleNamespace(name="donor")
        for native, expected in [("TradedLucky", (image,point)), ("TradedLuck", None)]:
            with self.subTest(native=native), mock.patch.object(saved, "_numbered_pages", pages), \
                 mock.patch.object(saved, "_preview_at", mock.AsyncMock(return_value=(image,point))), \
                 mock.patch.object(saved, "_read_name_at", mock.AsyncMock(return_value=native)):
                self.assertEqual(asyncio.run(saved._find_label(phone,"TradedLucky")), expected)

    def test_named_storage_rejects_changed_query_without_opening_candidates(self):
        phone = SimpleNamespace(name="primary")
        with mock.patch.object(saved, "read_favorite", mock.AsyncMock(return_value="!shiny")), \
             mock.patch.object(saved, "_find_label", mock.AsyncMock()) as locate:
            with self.assertRaises(saved.starter.StepFailed):
                asyncio.run(saved.open_named_storage(phone, "TradeReturns", "!shiny&!#KeepOne"))
            locate.assert_not_awaited()

    def test_named_picker_does_not_type_or_use_unverified_matches(self):
        image = Image.new("RGB", (720,1600))
        phone = SimpleNamespace(name="donor", tap_box=mock.AsyncMock())
        with mock.patch.object(saved.trade_local, "picker", return_value=True), \
             mock.patch.object(saved, "_open_named_tile", mock.AsyncMock(return_value=(image, []))), \
             mock.patch.object(collect, "_picker_search_field", return_value=box("Search", 10, 190)), \
             mock.patch.object(saved, "editor_text", mock.AsyncMock(return_value="!traded&147")), \
             mock.patch.object(saved.asyncio, "sleep", mock.AsyncMock()), \
             mock.patch.object(saved.trade_local, "submit", mock.AsyncMock()) as submit:
            with self.assertRaises(saved.starter.StepFailed):
                asyncio.run(saved.open_named_picker(phone, "ReceiverLucky", "!traded&148", image, []))
            submit.assert_not_awaited()

    def test_visible_ios_return_dismisses_the_plain_drawer_keyboard(self):
        button = mock.Mock()
        driver = mock.Mock()
        driver.find_elements.side_effect = [[], [button]]
        phone = SimpleNamespace(target=SimpleNamespace(device=SimpleNamespace(driver=driver)),
                                ios_keyboard=mock.AsyncMock(side_effect=[True, True, False]))
        with mock.patch.object(saved.asyncio, "sleep", mock.AsyncMock()):
            self.assertTrue(asyncio.run(saved.dismiss_ios_return(phone)))
        button.click.assert_called_once_with()
        self.assertIn("name == 'Return' AND visible == 1", driver.find_elements.call_args.args[1])

    def test_hidden_ios_editor_rejects_old_or_replaced_clipboard(self):
        driver = mock.Mock(capabilities={"udid": "SE"})
        driver.find_elements.side_effect = lambda kind, value: [mock.Mock(rect=None)] if value == "Clear text" else []
        def info(revision):
            return ('{"result":{"changeCount":%d}}' % revision).encode()
        for responses, expected in [
            ([info(3), info(3), info(3)], None),
            ([info(3), info(4), b"98plus", info(5)], None),
            ([info(3), info(4), b"98plus", info(4)], "98plus"),
        ]:
            with self.subTest(expected=expected, reads=len(responses)), \
                    mock.patch.object(saved, "_copy_ios_selection", return_value=False), mock.patch.object(
                    saved.subprocess, "check_output", side_effect=responses):
                self.assertEqual(saved.read_ios_editor(driver), expected)

    def test_ios_replaces_handoff_clipboard_before_requiring_a_new_local_revision(self):
        driver = mock.Mock(capabilities={"udid": "SE"})
        driver.find_elements.side_effect = lambda kind, value: [mock.Mock(rect=None)] if value == "Clear text" else []
        one = b'{"result":{"changeCount":11}}'
        two = b'{"result":{"changeCount":12}}'
        with mock.patch.object(saved.subprocess, "check_output",
                               side_effect=[saved.subprocess.CalledProcessError(1, "info"), one, two, b"4attack", two]), \
                mock.patch.object(saved, "_copy_ios_selection", return_value=True) as native_copy:
            self.assertEqual(saved.read_ios_editor(driver), "4attack")
        native_copy.assert_called_once()
        self.assertEqual(driver.execute_script.call_count, 2)

    def test_wrapped_staging_suffix_stays_distinct_from_original_and_its_count(self):
        image = Image.new("RGB", (355, 771))
        def line(name, y):
            return SimpleNamespace(text=name, center_y=y, x=280)
        lines = [line("Non-Raid", 550), line("new", 568), line("125", 587)]
        with mock.patch.object(saved, "icons", return_value=[(298, 505)]), \
             mock.patch.object(saved.gbl_vision, "recognize", return_value=lines):
            self.assertEqual(saved.favorite_tiles(image, []), [("Non-Raid new", (298, 505))])
            lines[:] = [line("Non-Raid", 550), line("18", 572)]
            self.assertEqual(saved.favorite_tiles(image, []), [("Non-Raid", (298, 505))])

    def test_staged_names_fit_game_limit_and_distinguish_similar_favorites(self):
        labels = ["98%", "Far Trade", "Far Not 98", "PH1 Lucky", "PH1 Hundo", "PH1 Priority"]
        stages = [saved._staged_name(label) for label in labels]
        self.assertEqual(len(set(stages)), len(labels))
        self.assertTrue(all(len(stage) <= 12 and " " not in stage for stage in stages))

    def test_duplicate_preview_renames_only_the_exact_tile(self):
        plane = object()
        async def pages(_phone):
            yield 0, (plane, [("!98plus&!d...", (1, 1)), ("!98plus&!d...", (2, 1))])
        phone = SimpleNamespace(name="phone")
        term = "!98plus&!dontrade&147"
        with mock.patch.object(saved, "_numbered_pages", pages), mock.patch.object(
                saved, "_read_tile", mock.AsyncMock(side_effect=["!98plus&!dontrade&148", term])), mock.patch.object(
                saved, "_rename", mock.AsyncMock(return_value=True)) as rename, mock.patch.object(
                saved, "read_favorite", mock.AsyncMock(return_value=term)):
            self.assertTrue(asyncio.run(saved._recover_preview(phone, "PH1 Lucky", term)))
        rename.assert_awaited_once_with(phone, (2, 1), plane, term, label="PH1 Lucky")

    def test_full_drawer_restores_previous_query_after_pin_failure(self):
        phone = SimpleNamespace(name="SE")
        state = {"Safe Trash": "old"}
        async def read(phone, label):
            return state.get(label)
        async def remove(phone, label, *, expected_term):
            self.assertEqual(state.pop(label), expected_term)
            return True
        async def pin(phone, label, term):
            if term == "new":
                raise RuntimeError("pin failed")
            state[label] = term
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(saved.config_paths, "state_dir", return_value=Path(folder)), \
                mock.patch.object(saved, "_drawer_full", mock.AsyncMock(return_value=True)), \
                mock.patch.object(saved, "_recover_preview", mock.AsyncMock(return_value=False)), \
                mock.patch.object(saved, "_matching_preview", mock.AsyncMock(return_value=None)), \
                mock.patch.object(saved, "read_favorite", read), mock.patch.object(saved, "remove_favorite", remove), \
                mock.patch.object(saved, "_pin_named", pin):
            with self.assertRaisesRegex(collect.starter.StepFailed, "restored"):
                asyncio.run(saved.ensure_favorite(phone, "Safe Trash", "new"))
            backup = saved._replacement_path(phone, "Safe Trash")
            self.assertEqual(json.loads(backup.read_text())["status"], "restored")
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(state["Safe Trash"], "old")

    def test_interrupted_capacity_swap_resumes_from_verified_backup(self):
        phone = SimpleNamespace(name="SE")
        state = {"Safe Trash": "old"}
        async def read(phone, label):
            return state.get(label)
        async def remove(phone, label, *, expected_term):
            self.assertEqual(state.pop(label), expected_term)
            return True
        async def pin(phone, label, term):
            state[label] = term
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(saved.config_paths, "state_dir", return_value=Path(folder)), \
                mock.patch.object(saved, "_drawer_full", mock.AsyncMock(return_value=True)), \
                mock.patch.object(saved, "_recover_preview", mock.AsyncMock(return_value=False)), \
                mock.patch.object(saved, "read_favorite", read), mock.patch.object(saved, "remove_favorite", remove):
            with mock.patch.object(saved, "_pin_named", mock.AsyncMock(side_effect=asyncio.CancelledError())):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(saved.ensure_favorite(phone, "Safe Trash", "new"))
            path = saved._replacement_path(phone, "Safe Trash")
            self.assertEqual(json.loads(path.read_text())["old"], "old")
            self.assertNotIn("Safe Trash", state)
            with mock.patch.object(saved, "_pin_named", pin):
                asyncio.run(saved.ensure_favorite(phone, "Safe Trash", "new"))
            self.assertEqual(state["Safe Trash"], "new")
            self.assertEqual(json.loads(path.read_text())["status"], "updated")

    def test_common_prefix_cannot_identify_retained_query(self):
        img = Image.new("RGB", (720, 1600))
        term = collect.build_donor_query("147")
        self.assertFalse(collect._exact_visible_query(img, [box(term[:24], 80, 300)], term))
        self.assertFalse(collect._exact_visible_query(img, [box(term[:-1] + "8", 80, 300)], term))
        self.assertTrue(collect._exact_visible_query(img, [box(term, 80, 300)], term))

    def test_faded_favorite_icons_and_mixed_case_name_dialog(self):
        img = Image.new("RGB", (720, 1600), "white")
        draw = ImageDraw.Draw(img)
        for x in (110, 275):
            draw.ellipse((x-55, 390, x+55, 500), fill=(115, 180, 182))
        boxes = [box("Favorite",30,330), box("Recommended",30,1150)]
        self.assertEqual(len(saved.icons(img, boxes, "favorite")), 2)
        term = "!lucky&147"
        alias = saved.name_for(term)
        label = box(alias, 80, 520)
        self.assertEqual(saved.heading([label], alias), label)
        self.assertEqual(saved.named_tile(img, [*boxes,label], term), label)
        self.assertIsNone(saved.named_tile(img, [*boxes,label], "!lucky&148"))

    def test_keep_two_verified_dex_copies(self):
        term = "!lucky&147"
        image = Image.new("RGB", (720,1600))
        base = [box(term,80,300), box("(2)",340,150), box("CP 420",120,510), box("CP 599",340,510)]
        phone = SimpleNamespace(name="android-one", read=mock.AsyncMock(return_value=(image,base)),
                                tap=mock.AsyncMock())
        with self.assertRaises(collect.ReserveHeld) as caught:
            asyncio.run(collect.choose_filtered_tile(phone,term,"Dratini",keep_copies=2))
        self.assertEqual(caught.exception.count,2)
        phone.tap.assert_not_awaited()

    def test_reserve_rejects_grouped_dex_offer(self):
        term = "!lucky&147,148"
        image = Image.new("RGB", (720,1600))
        base = [box(term,80,300),box("(9)",340,150),box("CP 420",120,510)]
        phone = SimpleNamespace(name="android-one",read=mock.AsyncMock(return_value=(image,base)),tap=mock.AsyncMock())
        with self.assertRaises(collect.starter.StepFailed):
            asyncio.run(collect.choose_filtered_tile(phone,term,"mixed",keep_copies=2))
        phone.tap.assert_not_awaited()

    def test_finds_named_favorite_after_scrolling_past_header(self):
        image = Image.new("RGB", (720, 1600), "white")
        drawer = ImageDraw.Draw(image)
        drawer.ellipse((60, 390, 175, 505), fill=(49, 142, 147))
        term = "!lucky&147"
        alias = box(saved.name_for(term), 80, 510)
        phone = SimpleNamespace(
            swipe=mock.AsyncMock(),
            read=mock.AsyncMock(return_value=(image, [alias, box("Recommended", 30, 1100)])),
        )
        _, _, selected = asyncio.run(saved.find_in_favorites(
            phone, image, [box("Favorite", 30, 280)], term))
        self.assertEqual(selected, alias)
        phone.swipe.assert_awaited_once()

    def test_verified_inventory_uses_recent_without_creating_a_favorite(self):
        image = Image.new("RGB", (720, 1600))
        term = "!lucky&147"
        boxes = [box(term, 80, 300), box("(3)", 340, 150)]
        phone = SimpleNamespace(name="android-one", say=mock.Mock(), look=mock.AsyncMock(return_value=image),
                                save=mock.Mock(return_value="/tmp/favorite-unverified.png"))
        observation = {}
        with mock.patch.object(collect, "_use_query", new_callable=mock.AsyncMock, return_value=False), \
                mock.patch.object(collect, "_wait_query_grid", new_callable=mock.AsyncMock, return_value=(image, boxes)), \
                mock.patch.object(saved, "save_most_recent", new_callable=mock.AsyncMock) as pin:
            result = asyncio.run(collect.prepare_saved_query(phone, term, image, boxes,
                                                             observed=observation, min_count=3))
        self.assertFalse(result)
        self.assertEqual(observation["count"], 3)
        pin.assert_not_awaited()
        phone.save.assert_not_called()
        self.assertEqual(collect._recent_fallback.pop(phone.name), term)

    def test_matching_named_favorite_avoids_writes(self):
        phone = SimpleNamespace(name="phone")
        with mock.patch.object(saved, "read_favorite", mock.AsyncMock(return_value="4ATTACK&4HP")), \
                mock.patch.object(saved, "_pin_named", mock.AsyncMock()) as pin, \
                mock.patch.object(saved, "remove_favorite", mock.AsyncMock()) as remove:
            self.assertEqual(asyncio.run(saved.ensure_favorite(phone, "98%", "4attack&4hp")), "verified")
        pin.assert_not_awaited()
        remove.assert_not_awaited()

    def test_failed_staging_keeps_original_search(self):
        phone = SimpleNamespace(name="phone")
        with mock.patch.object(saved, "_drawer_full", mock.AsyncMock(return_value=False)), \
                mock.patch.object(saved, "read_favorite", mock.AsyncMock(side_effect=["distance1-", None])), \
                mock.patch.object(saved, "_pin_named", mock.AsyncMock(side_effect=collect.starter.StepFailed())), \
                mock.patch.object(saved, "remove_favorite", mock.AsyncMock()) as remove:
            with self.assertRaises(collect.starter.StepFailed):
                asyncio.run(saved.ensure_favorite(phone, "Far Trade", "distance100-"))
        remove.assert_not_awaited()

    def test_changed_source_cannot_be_removed(self):
        phone = SimpleNamespace(name="phone")
        with mock.patch.object(saved, "_find_label", mock.AsyncMock(return_value=(object(), (1,2)))), \
                mock.patch.object(saved, "_read_tile", mock.AsyncMock(return_value="0*,1*,2*&!traded")), \
                mock.patch.object(saved, "_hold", mock.AsyncMock()) as hold:
            with self.assertRaises(collect.starter.StepFailed):
                asyncio.run(saved.remove_favorite(phone, "0*,1*,2*", expected_term="0*,1*,2*"))
        hold.assert_not_awaited()

    def test_retained_exact_android_editor_submits_without_typing(self):
        term = "!98plus&!dontrade&147"
        image = Image.new("RGB", (720, 1600))
        button = box("OK", 625, 1250)
        grid = [box(term, 100, 300), box("(3)", 350, 140)]
        phone = SimpleNamespace(ios=False, name="android", say=mock.Mock(),
                                read=mock.AsyncMock(side_effect=[(image, [button]), (image, grid)]),
                                tap_box=mock.AsyncMock())
        with mock.patch.object(collect.clear_pokemon, "_android_edit_text", new_callable=mock.AsyncMock, return_value=term), \
                mock.patch.object(collect.trade_local, "clear_field", new_callable=mock.AsyncMock) as clear:
            asyncio.run(collect._type_focused_query(phone, term))
        clear.assert_not_awaited()
        phone.tap_box.assert_awaited_once_with(button, image)

    def test_missing_first_tile_count_doesnt_take_second_tiles_ocr(self):
        image = Image.new("RGB", (720,1600), "white")
        self.assertIsNone(saved.recent_tile_count(image, [box("7", 260, 560)], (112,420)))
        self.assertEqual(saved.recent_tile_count(image, [box("5", 90, 560),box("7", 260, 560)],
                                                 (112,420)), 5)

    def test_same_count_different_native_recent_never_authorizes_pin(self):
        image = Image.new("RGB",(720,1600))
        term = "!98plus&!dontrade&147"
        field = box(term,75,310)
        results = [box("(4)",350,140),field]
        point = (112,420)
        phone = SimpleNamespace(tap=mock.AsyncMock(), tap_box=mock.AsyncMock(),
                                read=mock.AsyncMock(return_value=(image,[box("Recent",10,332)])))
        matches = mock.AsyncMock(return_value=False)
        with mock.patch.object(saved,"icons",return_value=[point]), \
                mock.patch.object(saved,"recent_tile_count",return_value=4), \
                mock.patch.object(saved.trade_local,"settle",new_callable=mock.AsyncMock,
                                  return_value=(True,image,results)), \
                mock.patch.object(saved.asyncio,"sleep",new_callable=mock.AsyncMock):
            candidate = asyncio.run(saved._verified_recent(phone,term,4,image,[],matches))
        self.assertIsNone(candidate)
        phone.tap.assert_awaited_once_with(point,image)
        matches.assert_awaited_once()

    def test_clearing_filtered_storage_requires_unfiltered_grid(self):
        image = Image.new("RGB", (720,1600))
        field = box("Q Search",75,308)
        tabs = [box("TAGS",40,82),box("EGGS",560,82),field]
        self.assertIsNone(saved._empty_field(image,[*tabs,box("(3)",350,140)]))
        self.assertEqual(saved._empty_field(image,tabs),field)


class GarbageSplitTests(unittest.TestCase):
    def test_local_trade_never_offers_a_garbage_species(self):
        from types import SimpleNamespace
        from sources import storage_favorites as favorites
        from tests.test_storage_review import matches
        policy = SimpleNamespace(names={d: str(d) for d in range(1, 21)}, trash_species={3, 4, 5, 17})
        clause = favorites.without_garbage(policy)
        for dex in range(1, 21):
            self.assertEqual(matches(clause, dex, set()), dex not in {3, 4, 5, 17}, dex)


class LuckyGapSplitTests(unittest.TestCase):
    def _policy(self):
        from types import SimpleNamespace
        return SimpleNamespace(names={d: str(d) for d in range(1, 21)}, trash_species={3, 4, 5, 17},
                               lucky={6, 7, 12}, receiver="receiver")

    def test_local_lists_never_offer_a_ph1_lucky_gap(self):
        from sources import storage_favorites as favorites
        from tests.test_storage_review import matches
        policy = self._policy()
        trade = favorites.without_species(policy, policy.trash_species | policy.lucky)
        not98 = favorites.without_species(policy, policy.lucky)
        for dex in range(1, 21):
            self.assertEqual(matches(trade, dex, set()), dex not in {3, 4, 5, 17, 6, 7, 12}, dex)
            self.assertEqual(matches(not98, dex, set()), dex not in {6, 7, 12}, dex)

    def test_wanted_applies_the_gap_clause_to_donors_only(self):
        from unittest import mock
        from sources import storage_favorites as favorites
        policy = self._policy()
        policy.favorites = lambda device: {}
        common = {"LocalTrade": "distance-100", "LocalNot98": "distance-100&!pikachu", "FarTrade": "!distance-100"}
        cfg = {"common": common, "donor": {}}
        with mock.patch.object(favorites.config_paths, "default_config") as path, \
                mock.patch.object(favorites, "power_term", return_value="#ToPower"), \
                mock.patch.object(favorites.trading_accounts, "account_for", side_effect=lambda d: d), \
                mock.patch("sources.ender_inventory.lucky_offer_term", return_value="legendary"):
            path.return_value.read_text.return_value = __import__("json").dumps(cfg)
            donor = favorites.wanted("donor", policy)
        self.assertIn("!6-7", donor["LocalNot98"].split("&"))
        self.assertIn("!12", donor["LocalNot98"].split("&"))
        self.assertNotIn("6", donor["FarTrade"])
