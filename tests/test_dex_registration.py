import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests import support as _support
from sources import dex_registration as audit


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        state = patch.object(audit.config_paths,"state_dir",return_value=self.root)
        state.start()
        self.addCleanup(state.stop)

    def test_lifetime_perfect_register_wins_over_missing_storage_and_ancestor(self):
        cfg = {"receiver":"sample-receiver","perfect_trade_dex":[4,5,6]}
        folder = self.root/"perfect-audit"/"2026-11-21"
        folder.mkdir(parents=True)
        (folder/"summary.json").write_text(json.dumps({"receiver":"sample-receiver","date":"2026-11-21",
            "dex_species":6,"registered_perfect":1,"dex_gaps":5}))
        (folder/"registered.json").write_text(json.dumps({"registered":[4],"region_checks":[[1,6,1]]}))
        (folder/"votes.json").write_text(json.dumps({"4":[["kanto.png",True,1]],
            "5":[["kanto.png",False,0]], "6":[["kanto.png",False,0],["other.png",True,1]]}))
        with (folder/"ph1-perfect-map.csv").open("w",newline="") as out:
            writer = csv.writer(out)
            writer.writerow(["Dex","Registered in 100% Dex","Current 4-star copies","Trade eligibility"])
            for n in range(1,7):
                writer.writerow([n,"Yes" if n==4 else "No",0,"Ordinary trade"])
        status = audit.perfect_status(cfg)
        self.assertEqual(status.registered,{4})
        self.assertEqual(status.missing,{5})
        self.assertEqual(status.audit_day,"2026-11-21")
        self.assertFalse(audit.perfect_gaps({"receiver":"another"}))
        (folder/"registered.json").write_text(json.dumps({"registered":[4],"region_checks":[[1,6,2]]}))
        self.assertFalse(audit.perfect_gaps(cfg))

    def test_lucky_requires_account_report_and_corresponding_consistent_frames(self):
        report, votes = self.root/"lucky.json", self.root/"votes.json"
        report.write_text(json.dumps({"summary":{"device":"PH_1 (serial)","total_needed_lucky_tradeable":3,
            "total_lucky":1},"needed_lucky_list":["2 species","3 species","5 species"],"needed_lucky_query":"2-3,5"}))
        votes.write_text(json.dumps({"2":[["none",0,"KANTO/1.png"]],"3":[["none",0,"KANTO/1.png"],
            ["lucky",1,"KANTO/2.png"]],"4":[["lucky",1,"KANTO/1.png"]],"5":[["none",0,"PALDEA/1.png"]]}))
        self.assertEqual(audit.lucky_status(report,votes,"sample-receiver","serial"),audit.DexStatus({4},{2}))
        self.assertFalse(audit.lucky_gaps(report,votes,"another","wrong-serial"))
        self.assertFalse(audit.lucky_gaps(report,None,"sample-receiver","serial"))

    def test_live_total_growth_quarantines_stale_gaps_until_each_region_is_recognized(self):
        status = audit.DexStatus(frozenset({4}),frozenset({7,9,180,182}))
        totals = {(1,151):4,(152,251):3}
        root = self.root/"dex-registrations"/"sample-receiver"
        def manifest(day,lo,hi,count,positive,negative):
            folder=root/day
            folder.mkdir(parents=True)
            (folder/"grid.png").touch()
            row={"start":lo,"end":hi,"registered_total":count,"header_shot":"grid.png",
                 "registered":{str(d):"grid.png" for d in positive},"missing":{str(d):"grid.png" for d in negative}}
            (folder/"lucky.json").write_text(json.dumps({"receiver":"sample-receiver","category":"lucky","date":day,
                "total":8,"regions":[row]}))
        manifest("2026-11-21",1,151,5,[7],[9])
        restored = audit._refresh("sample-receiver","lucky",status,7,dict(totals),set(status.missing))
        self.assertEqual(restored.registered,{4,7})
        self.assertEqual(restored.missing,{9})
        manifest("2026-11-22",152,251,3,[],[])
        restored = audit._refresh("sample-receiver","lucky",status,7,dict(totals),set(status.missing))
        self.assertEqual(restored.missing,{9,180,182})

    def test_only_accounted_regional_growth_restores_its_other_direct_gaps(self):
        folder = self.root/"dex-registrations"/"sample-receiver"/"2026-11-22"
        folder.mkdir(parents=True)
        shot = folder/"sinnoh.png"
        shot.touch()
        row = {"start":387,"end":493,"registered_total":87,"header_shot":shot.name}
        (folder/"lucky.json").write_text(json.dumps({"receiver":"sample-receiver","category":"lucky","date":folder.name,
            "total":827,"regions":[row]}))
        status = audit.DexStatus(frozenset(),frozenset({447,458,459}))
        def calc(confirmed,time=0):
            return audit._refresh("sample-receiver","lucky",status,821,{(387,493):85},set(status.missing),set(confirmed),time)
        self.assertFalse(calc({447}).missing)
        self.assertEqual(calc({447,458}).missing,{459})
        self.assertFalse(calc({447,458},shot.stat().st_mtime+1).missing)

    def test_contradictory_live_entry_is_withheld_from_both_target_and_safe_registered_sets(self):
        folder = self.root/"dex-registrations"/"sample-receiver"/"2026-11-22"
        folder.mkdir(parents=True)
        (folder/"grid.png").touch()
        row = {"start":1,"end":151,"registered_total":5,"header_shot":"grid.png","missing":{"4":"grid.png"}}
        (folder/"lucky.json").write_text(json.dumps({"receiver":"sample-receiver","category":"lucky","date":folder.name,
            "total":6,"regions":[row]}))
        original = audit.DexStatus(frozenset({4}),frozenset({5}))
        current = audit._refresh("sample-receiver","lucky",original,5,{(1,151):4},{4,5})
        self.assertNotIn(4,current.registered | current.missing)
        self.assertFalse(current.missing)
