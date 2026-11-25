from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests import support as _support
from sources import storage_keep_one as keep, storage_review as review


class KeepOneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = patch.object(review.config_paths, "state_dir", return_value=Path(tmp.name))
        state.start()
        self.addCleanup(state.stop)
        self.phone = SimpleNamespace(name="test")
        self.policy = SimpleNamespace(receiver="test", filters=("!#", "!favorite"), low_raid={1, 2}, safe_species={1, 2})

    async def test_donor_is_rejected_before_any_phone_operation(self):
        with patch.object(keep, "_grid", AsyncMock()) as grid, self.assertRaises(keep.clear.ClearSafetyError):
            await keep.keep_one_duplicates(SimpleNamespace(name="donor"), self.policy, candidates=[1])
        grid.assert_not_awaited()

    def test_original_strong_anchor_is_preserved(self):
        old = review.PREVIOUS_ANCHOR
        review._checkpoint("test", {"1":{"count":1, "day":"2026-11-20", "query":f"1&{old}"}}, predicate=old)
        self.assertEqual(review.load_verified_anchors("test"), {1})
        self.assertEqual(review._evidence("test", review.ANCHOR), {})

    async def test_reserves_one_only_after_two_duplicates_and_new_proof(self):
        responses = [(0, "retained"), (3, "duplicates"), (0, "three"), (2, "two"),
                     (1, "kept")]
        with patch.object(keep, "_grid", AsyncMock(side_effect=responses)) as grid, \
             patch.object(keep, "_tag_best_tile", AsyncMock(return_value="assigned")) as tag:
            row = await keep._one(self.phone, self.policy, 2)
        tag.assert_awaited_once_with(self.phone, "two")
        self.assertEqual(row["status"], "verified one")
        self.assertEqual(grid.await_args_list[4].args[1], "2&#KeepOne&2")
        self.assertEqual(grid.await_count, 5)
        self.assertEqual(review._evidence("test", review.ANCHOR)["2"]["query"], "2&#KeepOne&2")
        self.assertEqual(review.load_verified_anchors("test"), {2})

    async def test_singleton_is_not_touched(self):
        with patch.object(keep, "_grid", AsyncMock(side_effect=[(0,""),(1,"")])), \
             patch.object(keep, "_tag_best_tile", AsyncMock()) as tag:
            row = await keep._one(self.phone, self.policy, 1)
        tag.assert_not_awaited()
        self.assertEqual(row["status"], "fewer than two")
        self.assertEqual(review.load_verified_anchors("test"), set())

    async def test_restart_reconciles_interrupted_tag_without_reselecting(self):
        keep._record("test", 2, "tagging", count=4)
        with patch.object(keep, "_grid", AsyncMock(side_effect=[(1,""),(1,"")])), \
             patch.object(keep, "_tag_best_tile", AsyncMock()) as tag:
            rows = await keep.keep_one_duplicates(self.phone, self.policy, candidates=[2], limit=1)
        tag.assert_not_awaited()
        self.assertEqual(rows[0]["tagged"], 1)

    async def test_multiple_post_tag_matches_are_not_recorded_as_verified(self):
        with patch.object(keep, "_grid", AsyncMock(side_effect=[(0,""),(2,""),(1,""),(2,"")])), \
             patch.object(keep, "_tag_best_tile", AsyncMock(return_value="assigned")), \
             self.assertRaises(keep.clear.ClearSafetyError):
            await keep._one(self.phone, self.policy, 1)
        self.assertFalse(review.load_verified_anchors("test"))
        self.assertEqual(keep._receipts("test")[1]["status"], "tagging")

    async def test_sparse_positive_groups_are_bisected_and_verified_before_pruning(self):
        self.policy.low_raid = self.policy.safe_species = set(range(1,33))
        async def count_group(phone, term, dex, grid=None):
            if dex == 3:
                return (2 if term == f"3&{review.ANCHOR}" else 1), None
            numbers = review.clear.parse_dex_ranges(term.rsplit("&",1)[1])
            return (3 if 29 in numbers else 0) + (1 if 3 in numbers else 0), None
        async def one(phone, policy, dex):
            return {"dex":dex, "status":"already retained", "count":2}
        with patch.object(keep, "_grid", AsyncMock(side_effect=count_group)), \
             patch.object(keep, "_one", AsyncMock(side_effect=one)) as single:
            rows = await keep.keep_one_duplicates(self.phone,self.policy,candidates=range(1,33))
        self.assertEqual({r["dex"] for r in rows}, self.policy.low_raid)
        self.assertIn(29, [c.args[2] for c in single.await_args_list])
        self.assertLessEqual(single.await_count, 3)
        # The one eligible result had a protected partner of the same species.
        row = keep._receipts("test")[3]
        self.assertEqual(row["status"], "already retained")
        self.assertEqual(review.load_verified_anchors("test"), {3})
        self.assertEqual(keep._receipts("test")[1]["isolated_dex"], 3)

    async def test_supplement_only_revisits_previously_unresolved_group_one(self):
        species = {1,2,3,4,5}
        self.policy.low_raid = self.policy.safe_species = species
        for dex in [1,2]:
            keep._record("test",dex,"fewer than two",count=0,group=[1,2],group_count=0)
        for dex in [3,4]:
            keep._record("test",dex,"fewer than two",count=1,group=[3,4])
        async def read(phone,term,dex,grid=None):
            if dex is None:
                queried = review.clear.parse_dex_ranges(term.rsplit("&",1)[-1])
                self.assertLessEqual(queried,{3,4})
                return int(4 in queried),None
            self.assertEqual(dex,4)
            return int(term != f"4&{review.ANCHOR}"),None
        with patch.object(keep,"_grid",AsyncMock(side_effect=read)), \
             patch.object(keep,"_tag_best_tile",AsyncMock()) as tag:
            rows = await keep.keep_one_duplicates(self.phone,self.policy,pooled_ones_only=True,limit=4)
            self.assertEqual(await keep.keep_one_duplicates(self.phone,self.policy,pooled_ones_only=True),[])
        self.assertEqual({r["dex"] for r in rows},{3,4})
        self.assertEqual(keep._receipts("test")[4]["retained_count"],0)
        self.assertFalse(review.load_verified_anchors("test"))
        tag.assert_not_awaited()

    async def test_supplement_does_not_tag_if_more_pokemon_have_arrived(self):
        keep._record("test",1,"fewer than two",count=None,group=[1,2],group_count=1)
        with patch.object(keep,"_grid",AsyncMock(side_effect=[(3,None),(0,None)])), \
             patch.object(keep,"_tag_best_tile",AsyncMock()) as tag:
            rows = await keep.keep_one_duplicates(self.phone,self.policy,pooled_ones_only=True)
        self.assertEqual(rows[0]["status"],"unprotected duplicates")
        self.assertFalse(review.load_verified_anchors("test"))
        tag.assert_not_awaited()

    async def test_post_assignment_read_retry_never_repeats_assignment_or_drops_pending_intent(self):
        def read(phone, term, dex, previous=None, *, require_tiles=False):
            if dex is None:
                return 4, None
            if dex == 2:
                return 1, None
            if term == f"1&{review.ANCHOR}":
                return 0, None
            if term == "1&#KeepOne&1":
                raise keep.clear.ClearSafetyError("temporary unreadable counter")
            return 2, None
        with patch.object(keep, "_read_grid", AsyncMock(side_effect=read)) as reads, \
             patch.object(keep, "_tag_best_tile", AsyncMock(return_value="assigned")) as tag:
            rows = await keep.keep_one_duplicates(self.phone,self.policy,candidates=[1,2])
        self.assertEqual([r["status"] for r in rows], ["read deferred","already retained"])
        tag.assert_awaited_once()
        self.assertEqual(sum(c.args[1]=="1&#KeepOne&1" for c in reads.await_args_list),2)
        self.assertEqual(keep._receipts("test")[1]["status"],"tagging")
        self.assertEqual(review.load_verified_anchors("test"),{2})

    async def test_read_retries_from_current_storage_without_old_grid(self):
        with patch.object(keep, "_read_grid", AsyncMock(side_effect=[
            keep.clear.ClearSafetyError("stale grid"), (1,"good")])) as reads:
            self.assertEqual(await keep._grid(self.phone,"1&favorite",1,"old"),(1,"good"))
        self.assertEqual([c.args[3] for c in reads.await_args_list],["old",None])

    async def test_unverified_evolution_scope_stops_the_pass_before_any_count_or_tile_tap(self):
        self.phone.ios = False
        self.phone.read = AsyncMock(return_value=("image", []))
        self.phone.tap = AsyncMock()
        with patch.object(keep.review, "_filtered_count", return_value=1), \
             patch.object(keep.clear, "evolutionary_line_off", return_value=False), \
             patch.object(keep.clear, "_enter_query_android", AsyncMock()) as enter, \
             patch.object(keep.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(keep.EvolutionScopeUnverified):
                await keep._grid(self.phone,"1&3*&1",1,require_tiles=True)
            with self.assertRaises(keep.EvolutionScopeUnverified):
                await keep._tag_best_tile(self.phone, ("image", []))
            enter.assert_awaited_once()
            self.phone.tap.assert_not_awaited()

    async def test_local_android_header_requires_dimensions_format_and_falls_back_when_rotated(self):
        from PIL import Image
        import struct
        t = keep.gbl_home_recovery.AndroidTarget("test","test")
        i = Image.new("RGB",(100,200), "white")
        self.phone.target = t
        self.phone.look = AsyncMock(return_value=i)
        self.phone._living_screen_size = i.size
        pixels = bytes((10, 100, 120, 255)) * (100*64)
        t._adb = Mock(side_effect=[SimpleNamespace(returncode=0,stdout=struct.pack("<4I",100,200,1,0)+pixels),
                                   SimpleNamespace(returncode=0,stdout=struct.pack("<4I",200,100,1,0)+pixels)])
        with patch.object(keep.clear.gbl_vision,"recognize",return_value=[]):
            header, _ = await keep._android_storage_header(self.phone)
            self.assertEqual(header.size,i.size)
            self.assertEqual(header.getpixel((50,50)),(10,100,120))
            full, _ = await keep._android_storage_header(self.phone)
            self.assertIs(full,i)
            self.phone.look.assert_awaited_once()

    async def test_living_audit_uses_scope_specific_hundo_and_existing_keep_memberships(self):
        ordinary = review.ORDINARY_FORM
        self.policy.living_forms = {1:(ordinary,), 2:(ordinary+"&!galar",ordinary+"&galar")}
        for dex,scope,status,predicate in (
                (1,ordinary,"kept",review.KEPT),
                (2,ordinary+"&galar","preferred",review.PREFERRED)):
            keep._living_record("test",self.phone,dex,scope,status,count=1,query=f"{dex}&{scope}&{predicate}&{dex}")
        self.assertEqual(review.verified_living_species("test",self.policy),{1})
        keep._living_record("test",self.phone,2,review.EVENT_FORMS,"preferred",count=1,
                            query=f"2&{review.EVENT_FORMS}&{review.PREFERRED}&2")
        self.assertEqual(review.verified_living_species("test",self.policy),{1})
        guard=review.receiver_outgoing_filter(self.policy).split("&")
        self.assertIn("!costume",guard)
        self.assertIn("!background",guard)
        self.assertIn("!#KeepOne",guard)
        self.assertEqual(guard[-1],"1-2")
        self.assertIn("!2,galar",guard)

    async def test_entire_catalog_including_non_raid_unrelated_singleton_uses_existing_tag(self):
        ordinary=review.ORDINARY_FORM
        self.policy.living_forms={999:(ordinary,)}
        assigned=False
        async def grid(phone,term,dex,previous=None,require_tiles=False):
            if review.EVENT_FORMS in term:
                return 0,None
            if review.RETAINED in term or review.KEPT in term.split("&"):
                return int(assigned and review.KEPT in term),None
            return 1,"grid"
        async def assign(phone, grid):
            nonlocal assigned
            assigned=True
            return "assigned"
        with patch.object(keep,"_grid",AsyncMock(side_effect=grid)), \
             patch.object(keep,"_tag_best_tile",AsyncMock(side_effect=assign)) as tag:
            rows=await keep.prepare_living_dex(self.phone,self.policy,candidates=[999])
            self.assertEqual(await keep.prepare_living_dex(self.phone,self.policy,candidates=[999]),[])
        tag.assert_awaited_once_with(self.phone,"grid")
        self.assertEqual([r["status"] for r in rows],["tagged","absent"])
        self.assertEqual(review.verified_living_species("test",self.policy),{999})

    async def test_newly_protected_singleton_never_reaches_a_tile_tap(self):
        scope = review.ORDINARY_FORM
        with patch.object(keep,"_grid",AsyncMock(side_effect=[(0,None),(1,None),(0,None)])) as reads, \
             patch.object(keep,"_tag_best_tile",AsyncMock()) as tag, self.assertRaises(keep.ReadUnavailable):
            await keep._living_scope(self.phone,"test",999,scope,prior={})
        selected = reads.await_args.args[1]
        for guard in ("!4*","!#98Plus","!#KeepOne"):
            self.assertIn(guard,selected.split("&"))
        tag.assert_not_awaited()
        self.assertFalse(review.living_evidence("test"))

    async def test_ambiguous_forms_never_release_entire_species(self):
        self.policy.living_forms = {2:()}
        async def grid(phone,term,dex,previous=None,require_tiles=False):
            if review.EVENT_FORMS in term: return 0,None
            if review.PREFERRED in term or review.KEPT in term: return 1,None
            return 4,None
        with patch.object(keep,"_grid",AsyncMock(side_effect=grid)), \
             patch.object(keep,"_tag_best_tile",AsyncMock()) as tag:
            rows=await keep.prepare_living_dex(self.phone,self.policy,candidates=[2])
        tag.assert_not_awaited()
        self.assertEqual(rows[0]["status"],"forms protected")
        self.assertEqual(rows[0]["retained_count"],1)
        self.assertEqual(review.verified_living_species("test",self.policy),set())

    async def test_one_union_search_accepts_existing_perfect_or_tagged_keeper(self):
        ordinary = review.ORDINARY_FORM
        self.policy.living_forms = {123:(ordinary,)}
        expected = f"123&{ordinary}&4*,#98Plus,#KeepOne&123"
        with patch.object(keep, "_grid", AsyncMock(return_value=(1,"grid"))) as query, \
             patch.object(keep, "_tag_best_tile", AsyncMock()) as tag:
            row = await keep._living_scope(self.phone, "test", 123, ordinary, prior={})
        self.assertEqual(row["status"], "retained")
        self.assertEqual(row["query"], expected)
        self.assertEqual(query.await_count, 1)
        tag.assert_not_awaited()
        self.assertTrue(review.verified_living_row(row,123,ordinary))
        self.assertFalse(review.verified_living_row({**row,"query":"123&4*,#98Plus,#KeepOne"},123,ordinary))

    async def test_verified_original_singleton_reuses_dontrade_only_in_its_present_form(self):
        ordinary = review.ORDINARY_FORM + "&!alola"
        alola = review.ORDINARY_FORM + "&alola"
        self.policy.living_forms = {19:(ordinary,alola)}
        with patch.object(keep, "_grid", AsyncMock(return_value=(1,"grid"))) as query, \
             patch.object(keep, "_tag_best_tile", AsyncMock()) as tag:
            row = await keep._living_scope(self.phone, "test", 19, ordinary, prior={}, singleton_tag_day="2026-11-20")
        self.assertEqual(query.await_args.args[1], f"19&{ordinary}&#dontrade&19")
        self.assertEqual(query.await_count, 1)
        tag.assert_not_awaited()
        self.assertTrue(review.verified_living_row(row,19,ordinary))
        self.assertFalse(review.verified_living_row(row,19,alola))
        self.assertFalse(review.verified_living_row({**row,"singleton_tag_day":""},19,ordinary))
        outgoing = review.receiver_outgoing_filter(self.policy).split("&")
        self.assertIn("!19,!alola", outgoing)
        self.assertIn("!#dontrade", outgoing)

    def test_in_place_rename_reads_both_historical_spaced_and_current_exact_proofs(self):
        previous = review.SPACED_ANCHOR
        review._checkpoint("test", {"1":{"count":1, "day":"2026-11-20", "query":"1&#Keep One&1"}}, predicate=previous)
        keep._save_anchor("test",2,"2&#KeepOne&2",1,review.ANCHOR,"test")
        self.assertEqual(review.load_verified_anchors("test"),{1,2})

    def test_group_cp_accepts_only_unique_nonoverlapping_exact_cp(self):
        domains={1:{100,199},2:{108,200},3:{150}}
        self.assertEqual(keep._match_hundo_cp([1,2,3],domains,100),1)
        self.assertIsNone(keep._match_hundo_cp([1,2,3],domains,199))
        self.assertIsNone(keep._match_hundo_cp([1,2,3],domains,101))
        self.assertIsNone(keep._match_hundo_cp([1,2,4],domains,100))

    async def test_conditioned_batch_only_certifies_each_cp_seen_in_both_guarded_grids(self):
        from PIL import Image
        ordinary = review.ORDINARY_FORM
        self.policy.living_forms = {1:(ordinary,), 2:(ordinary,)}
        self.policy.names = {1:"Bulbasaur", 2:"Ivysaur"}
        self.policy.living_labels = {1:{"bulbasaur"}, 2:{"ivysaur"}}
        a, b = SimpleNamespace(text="CP 313"), SimpleNamespace(text="CP 1893")
        image = Image.new("RGB",(700,1400))
        term = review._species_term((ordinary,review.RETAINED),[1,2])
        self.phone.read = AsyncMock(side_effect=[(image,[a,b]),(image,[a,b]),(image,[b])])
        with patch.object(keep, "_grid", AsyncMock(return_value=(2,None))), \
             patch.object(keep, "_exact_live_grid", return_value=True), \
             patch.object(keep.clear, "safe_grid_tiles", side_effect=lambda i,boxes:boxes), \
             patch.object(review.collect_trades,"_tile_species_label",side_effect=["bulbasaur","ivysaur"]), \
             patch.object(keep.asyncio, "sleep", AsyncMock()):
            records = await keep._batch_retained(self.phone,"test",self.policy,[1,2],ordinary,term,2)
        self.assertEqual([r["dex"] for r in records], [2])
        row, = records
        self.assertIn("!1,cp313&!2,cp1893", row["query"])
        self.assertTrue(review.verified_living_row(row,2,ordinary))
        self.assertFalse(review.verified_living_row(row,1,ordinary))
        self.assertFalse(review.verified_living_row({**row,"pairs":[(1,313),(2,313)]},2,ordinary))
        self.assertFalse(review.verified_living_row(row,2,review.EVENT_FORMS))

    async def test_sparse_names_only_exclude_other_species_after_their_own_native_zero(self):
        scope = review.EVENT_FORMS
        numbers = list(range(1,10))
        self.phone.read = AsyncMock(return_value=("image",[]))
        with patch.object(keep,"_exact_live_grid",return_value=True), \
             patch.object(keep,"_nominate_pairs",return_value=[(5,500)]), \
             patch.object(keep,"_grid",AsyncMock(side_effect=[(1,None),(0,None)])) as read:
            self.assertEqual(await keep._batch_absent(self.phone,"test",self.policy,numbers,scope,"group",2),[])
            self.assertEqual(review.living_evidence("test"),{})
            rows = await keep._batch_absent(self.phone,"test",self.policy,numbers,scope,"group",2)
        self.assertEqual({r["dex"] for r in rows},set(numbers)-{5})
        query = review._species_term((scope,),set(numbers)-{5})
        self.assertEqual(read.await_args.args[1],query)
        self.assertTrue(all(r["query"] == query and r["status"] == "absent" and r["count"] == 0 for r in rows))

    async def test_grouped_original_singleton_requires_historical_day_and_preexisting_tag_in_same_form(self):
        from PIL import Image
        scope = review.ORDINARY_FORM
        self.policy.living_forms = {1:(scope,),2:(scope,),3:(scope,)}
        self.policy.living_labels = {1:{"one"},2:{"two"},3:{"three"}}
        days = {1:"2026-11-18",3:"2026-11-19"}
        image = Image.new("RGB",(50,50))
        tiles = [SimpleNamespace(text="CP 100"),SimpleNamespace(text="CP 300")]
        self.phone.read = AsyncMock(return_value=(image,tiles))
        with patch.object(keep,"_exact_live_grid",return_value=True), \
             patch.object(keep,"_nominate_pairs",return_value=[(1,100),(3,300)]) as nominate, \
             patch.object(keep.clear,"safe_grid_tiles",return_value=tiles), \
             patch.object(keep,"_grid",AsyncMock(return_value=(2,None))), \
             patch.object(keep.asyncio,"sleep",AsyncMock()):
            rows = await keep._batch_retained(self.phone,"test",self.policy,[1,2,3],scope,"group",2,singleton_days=days)
        self.assertEqual(nominate.call_args.args[1],[1,3])
        self.assertEqual([r["dex"] for r in rows],[1,3])
        for row in rows:
            dex = row["dex"]
            self.assertEqual(row["singleton_tag_day"],days[dex])
            self.assertIn("&#dontrade&",row["query"])
            self.assertTrue(review.verified_living_row(row,dex,scope))
            self.assertFalse(review.verified_living_row({**row,"singleton_tag_day":""},dex,scope))
            self.assertFalse(review.verified_living_row({**row,"status":"retained"},dex,scope))
            self.assertFalse(review.verified_living_row(row,dex,review.EVENT_FORMS))

    async def test_cached_raid_cp_hints_never_become_holdings_without_current_native_proof(self):
        import json
        ordinary = review.ORDINARY_FORM
        (review.config_paths.state_dir()/"raidscan_test.json").write_text(json.dumps([
            {"search":"001","hundo_cps":[313],"good_cps":[899]},
            {"search":"002","good_cps":[517,"bad"]}, {"search":"003","hundo_cps":[450]},
        ]))
        self.policy.living_forms = {1:(ordinary,), 2:(ordinary,), 3:()}
        with patch.object(keep,"_prove_retained_pairs",AsyncMock(return_value=[])) as confirm:
            self.assertEqual(await keep._batch_raid_hints(self.phone,"test",self.policy,[1,2,3],{}),[])
        confirm.assert_awaited_once()
        self.assertEqual(confirm.await_args.args[3],[(1,313),(2,517)])
        self.assertEqual(review.living_evidence("test"),{})

    async def test_dense_retained_grid_pages_supply_only_hints_for_the_conditioned_query(self):
        scope = review.ORDINARY_FORM
        self.policy.living_forms = {n:(scope,) for n in range(1,7)}
        self.policy.living_labels = {n:{str(n)} for n in self.policy.living_forms}
        frame = SimpleNamespace(width=700,height=1400)
        tile = lambda cp:SimpleNamespace(text=f"CP {cp}",center_x=100,center_y=500)
        first,later = [tile(111),tile(222)], [tile(333),tile(444)]
        self.phone._living_android_fast = True
        self.phone.read = AsyncMock(side_effect=[(frame,first),(frame,first),(frame,later),(frame,later)])
        self.phone.swipe = AsyncMock()
        with patch.object(keep,"_exact_live_grid",return_value=True), \
             patch.object(keep.clear,"safe_grid_tiles",side_effect=lambda i,b:b), \
             patch.object(keep,"_nominate_pairs",side_effect=[[(1,111),(2,222)],[(1,111),(2,222)],
                                                               [(3,333),(4,444)]]), \
             patch.object(keep,"_prove_retained_pairs",AsyncMock(return_value=[])) as confirm, \
             patch.object(keep.asyncio,"sleep",AsyncMock()):
            rows = await keep._batch_retained(self.phone,"test",self.policy,list(range(1,7)),scope,"term",9)
        self.assertEqual(rows,[])
        self.assertEqual(confirm.await_args.args[3],[(1,111),(2,222),(3,333),(4,444)])
        self.assertEqual(self.phone.swipe.await_count,2)
        self.assertFalse(review.living_evidence("test"))

    async def test_fully_identified_group_can_skip_extra_retained_queries_for_remaining_species(self):
        scope=review.ORDINARY_FORM
        self.policy.living_forms={d:(scope,) for d in range(1,5)}
        async def grid(phone,term,dex):
            return (0 if review.EVENT_FORMS in term else 2 if review.RETAINED in term else 7),None
        with patch.object(keep,"_grid",AsyncMock(side_effect=grid)), \
             patch.object(keep,"_batch_retained",AsyncMock(return_value=[{"dex":1},{"dex":2}])), \
             patch.object(keep,"_living_scope",AsyncMock(return_value={"status":"kept"})) as one:
            await keep.prepare_living_dex(self.phone,self.policy,candidates=[1,2,3,4])
        self.assertEqual([c.args[2] for c in one.await_args_list],[3,4])
        self.assertTrue(all(c.kwargs["no_retained"] for c in one.await_args_list))

    async def test_batch_requires_unfiltered_ownership_equal_to_distinct_cp_count(self):
        pairs = [(1,300),(2,450)]
        with patch.object(keep,"_grid",AsyncMock(return_value=(3,None))), \
             patch.object(keep,"_complete_cp_grid",AsyncMock()) as validated:
            self.assertIsNone(await keep._singleton_batch_proof(self.phone,review.ORDINARY_FORM,pairs))
        validated.assert_not_awaited()

    async def test_conditioned_batch_rejects_duplicate_cp_even_with_correct_total(self):
        frame = object()
        good = [SimpleNamespace(text="CP 300"),SimpleNamespace(text="CP 450")]
        duplicate = [good[0],good[0]]
        self.phone.read = AsyncMock(return_value=(frame,duplicate))
        with patch.object(keep,"_grid",AsyncMock(return_value=(2,(frame,good)))), \
             patch.object(keep,"_exact_live_grid",return_value=True), \
             patch.object(keep.clear,"safe_grid_tiles",side_effect=lambda i,b:b), \
             patch.object(keep.asyncio,"sleep",AsyncMock()):
            self.assertIsNone(await keep._complete_cp_grid(self.phone,"term",[(1,300),(2,450)]))

    async def test_unreconciled_multi_tag_preserves_pending_intents_and_never_releases_species(self):
        pairs = [(1,300),(2,450)]
        scope = review.ORDINARY_FORM
        self.policy.living_forms = {1:(scope,),2:(scope,)}
        self.policy.living_labels = {1:{"a"},2:{"b"}}
        grid = ("image",[])
        self.phone.read = AsyncMock(return_value=grid)
        with patch.object(keep,"_exact_live_grid",return_value=True), \
             patch.object(keep,"_nominate_pairs",return_value=pairs), \
             patch.object(keep,"_singleton_batch_proof",AsyncMock(return_value=("all","select",grid))), \
             patch.object(keep,"_tag_selected_keepers",AsyncMock()) as tag, \
             patch.object(keep,"_complete_cp_grid",AsyncMock(return_value=None)), \
             patch.object(keep,"_grid",AsyncMock(return_value=(1,None))), \
             self.assertRaises(keep.clear.ClearSafetyError):
            await keep._batch_singletons(self.phone,"test",self.policy,[1,2],scope,"term",2)
        tag.assert_awaited_once()
        self.assertEqual({row["status"] for row in review.living_evidence("test").values()},{"tagging"})
        self.assertEqual(review.verified_living_species("test",self.policy),set())

    def test_batch_tag_count_cannot_match_transfer_button_or_single_selection(self):
        image = SimpleNamespace(height=2560)
        btn = lambda s,y:SimpleNamespace(text=s,center_y=y)
        two = btn("TAG (2)",2165)
        self.assertIs(keep._selected_count_button(image,[btn("TRANSFER (2)",2383),btn("TAG (1)",2165),
                                                        btn("TAG (2)",205),two],2),two)

    async def test_fast_individual_holds_only_highest_cp_and_never_selects_all(self):
        im = SimpleNamespace(height=1000,width=500)
        def box(text,x=120,y=820):
            return SimpleNamespace(text=text,center_x=x,center_y=y)
        tiles = [box("CP 42",120,280),box("CP 1200",340,520)]
        menu = [box("TAG (1)"),box("SELECT ALL"),box("TRANSFER (1)")]
        sheet = [box("TAG 1 POKÉMON"),box("KeepOne"),box("DONE")]
        self.phone._living_android_fast = self.phone._living_tag_exists = True
        frames = iter([menu,sheet])
        async def settle(test,timeout):
            boxes = next(frames)
            return test(im,boxes),im,boxes
        self.phone.settle = settle
        self.phone.read = AsyncMock(return_value=(im,sheet))
        self.phone.tap_box = AsyncMock()
        with patch.object(keep.clear,"evolutionary_line_off",return_value=True), \
             patch.object(keep.clear,"safe_grid_tiles",return_value=tiles), \
             patch.object(keep.saved,"_hold",AsyncMock()) as hold,patch.object(keep.asyncio,"sleep",AsyncMock()):
            self.assertEqual(await keep._tag_best_tile(self.phone,(im,tiles)),"assigned")
        self.assertEqual(hold.await_args.args[1],[340,520+int(im.height*keep.clear.TILE_RISE)])
        self.assertEqual([call.args[0].text for call in self.phone.tap_box.await_args_list],["TAG (1)","KeepOne","DONE"])

    async def test_multi_tag_proof_only_releases_the_two_exact_scopes_after_native_reconciliation(self):
        from PIL import Image
        scope = review.ORDINARY_FORM
        pairs = [(1,300),(2,450)]
        self.policy.living_forms = {1:(scope,),2:(scope,)}
        self.policy.living_labels = {1:{"a"},2:{"b"}}
        grid = (Image.new("RGB",(50,100)),[])
        self.phone.read = AsyncMock(return_value=grid)
        with patch.object(keep,"_exact_live_grid",return_value=True), \
             patch.object(keep,"_nominate_pairs",return_value=pairs), \
             patch.object(keep,"_singleton_batch_proof",AsyncMock(return_value=("all","select",grid))), \
             patch.object(keep,"_tag_selected_keepers",AsyncMock()), \
             patch.object(keep,"_complete_cp_grid",AsyncMock(return_value=grid)), \
             patch.object(keep,"_grid",AsyncMock(return_value=(0,None))):
            rows = await keep._batch_singletons(self.phone,"test",self.policy,[1,2],scope,"term",2)
        self.assertEqual(len(rows),2)
        self.assertEqual(review.verified_living_species("test",self.policy),{1,2})
        self.assertIn("&#KeepOne&",rows[0]["query"])
        self.assertFalse(review.verified_living_row({**rows[0],"status":"retained"},1,scope))
        self.assertFalse(review.verified_living_row({**rows[0],"population_after":1},1,scope))
        self.assertFalse(review.verified_living_row({**rows[0],"population_query":"!#KeepOne&1"},1,scope))
        self.assertFalse(review.verified_living_row({**rows[0],"group_total":1},1,scope))

    async def test_duplicate_candidates_require_zero_higher_iv_and_zero_higher_cp_before_verified_selection(self):
        pairs = [(1,300),(2,450)]
        scope = review.ORDINARY_FORM
        with patch.object(keep,"_grid",AsyncMock(side_effect=[(0,None),(0,None)])) as reads, \
             patch.object(keep,"_complete_cp_grid",AsyncMock(return_value="grid")) as selection:
            proof = await keep._best_batch_proof(self.phone,self.policy,scope,pairs,2)
        self.assertEqual(proof[0],pairs)
        self.assertIn("4*,#98Plus,#KeepOne,3*",reads.await_args_list[0].args[1].split("&"))
        high = reads.await_args_list[1].args[1].split("&")
        self.assertTrue({"2*","!1,cp301-","!2,cp451-"}.issubset(high))
        selected = selection.await_args.args[1].split("&")
        self.assertTrue({"2*","!4*","!#98Plus","!#KeepOne","!#dontrade","!1,cp300","!2,cp450"}.issubset(selected))

    async def test_single_top_tier_nominee_leaves_lower_tier_group_available_for_independent_proof(self):
        pairs = [(1,300),(3,510)]
        scope = review.ORDINARY_FORM
        proof = (pairs,"selected",("image",[]),"zero higher tiers","zero higher CPs")
        with patch.object(keep,"_grid",AsyncMock(side_effect=[(1,None),(3,("image",[]))])) as reads, \
             patch.object(keep,"_exact_live_grid",return_value=True), \
             patch.object(keep,"_nominate_pairs",return_value=pairs), \
             patch.object(keep,"_best_batch_proof",AsyncMock(return_value=proof)) as check, \
             patch.object(keep,"_assign_batch",AsyncMock(return_value=[{"dex":1},{"dex":3}])) as assign:
            await keep._batch_best_tier(self.phone,"test",self.policy,[1,2,3],scope)
        self.assertEqual([r.args[1].split("&")[2] for r in reads.await_args_list],["3*","2*"])
        check.assert_awaited_once_with(self.phone,self.policy,scope,pairs,2)
        self.assertEqual(assign.await_args.kwargs["higher_tiers"],proof[3])

    async def test_duplicate_candidates_cannot_select_when_a_higher_or_retained_member_exists(self):
        with patch.object(keep,"_grid",AsyncMock(return_value=(1,None))), \
             patch.object(keep,"_complete_cp_grid",AsyncMock()) as selection:
            self.assertIsNone(await keep._best_batch_proof(self.phone,self.policy,review.ORDINARY_FORM,[(1,300),(2,400)],1))
        selection.assert_not_awaited()

    async def test_blocked_nominee_is_removed_only_after_a_new_zero_for_remaining_species(self):
        pairs = [(1,300),(2,450),(3,510)]
        scope = review.ORDINARY_FORM
        self.phone.read = AsyncMock(return_value=("image",[]))
        with patch.object(keep,"_grid",AsyncMock(side_effect=[(1,None),(0,None),(0,None)])) as reads, \
             patch.object(keep,"_exact_live_grid",return_value=True), \
             patch.object(keep,"_nominate_pairs",return_value=[(2,500)]), \
             patch.object(keep,"_complete_cp_grid",AsyncMock(return_value="grid")) as select:
            proof = await keep._best_batch_proof(self.phone,self.policy,scope,pairs,3)
        self.assertEqual(proof[0],[(1,300),(3,510)])
        self.assertEqual(proof[3],reads.await_args_list[1].args[1])
        self.assertEqual(proof[3],review._species_term((scope,review.RETAINED),[1,3]))
        self.assertEqual(select.await_args.args[2],proof[0])

    async def test_higher_cp_hint_requires_another_proven_zero_at_its_improved_cutoff(self):
        pairs = [(1,300),(2,450)]
        self.phone.read = AsyncMock(return_value=("image",[]))
        with patch.object(keep,"_grid",AsyncMock(side_effect=[(0,None),(1,None),(0,None)])) as reads, \
             patch.object(keep,"_exact_live_grid",return_value=True), \
             patch.object(keep,"_nominate_pairs",return_value=[(1,398)]), \
             patch.object(keep,"_complete_cp_grid",AsyncMock(return_value="grid")):
            proof = await keep._best_batch_proof(self.phone,self.policy,review.ORDINARY_FORM,pairs,3)
        self.assertEqual(proof[0],[(1,398),(2,450)])
        self.assertIn("!1,cp399-",reads.await_args_list[-1].args[1].split("&"))

    async def test_duplicate_batch_rejects_extra_tagged_member_outside_selected_cp_grid(self):
        pairs = [(1,300),(2,450)]
        with patch.object(keep,"_tag_selected_keepers",AsyncMock()), \
             patch.object(keep,"_complete_cp_grid",AsyncMock(return_value=("image",[]))), \
             patch.object(keep,"_grid",AsyncMock(return_value=(3,None))) as population, \
             self.assertRaises(keep.clear.ClearSafetyError):
            await keep._assign_batch(self.phone,"test",review.ORDINARY_FORM,pairs,"query",("image",[]),
                                     tier=3,higher_tiers="higher IV",higher_cps="higher CP")
        self.assertIn("#KeepOne",population.await_args.args[1].split("&"))
        self.assertEqual({r["status"] for r in review.living_evidence("test").values()},{"tagging"})

    def test_batch_receipt_needs_same_scope_filtered_query_cp_and_image(self):
        scope=review.ORDINARY_FORM
        path=review.living_journal("test").parent / "shot.png"
        path.parent.mkdir(parents=True)
        row={"method":"matched hundo CP","status":"preferred","count":1,"group":[1,3],
             "historical_cp":106,"observed_cp":106,"shot":str(path),"query":review._species_term((scope,"4*"),[1,3])}
        self.assertFalse(review.verified_living_row(row,3,scope))
        path.touch()
        self.assertTrue(review.verified_living_row(row,3,scope))
        self.assertFalse(review.verified_living_row(row,2,scope))
        self.assertFalse(review.verified_living_row({**row,"observed_cp":105},3,scope))
        self.assertFalse(review.verified_living_row(row,3,review.EVENT_FORMS))
