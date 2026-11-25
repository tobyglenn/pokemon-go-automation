"""Exact receiver Pokédex evidence for collection targets and manual storage review."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from . import clear_pokemon as clear, config_paths


@dataclass(frozen=True)
class DexStatus:
    registered: frozenset[int] = frozenset()
    missing: frozenset[int] = frozenset()
    audit_day: str = ""
    ordinary_tradeable: frozenset[int] = frozenset()


REGIONS = {"KANTO":(1,151), "JOHTO":(152,251), "HOENN":(252,386), "SINNOH":(387,493),
           "UNOVA":(494,649), "KALOS":(650,721), "ALOLA":(722,807), "UNKNOWN":(808,809),
           "GALAR":(810,898), "HISUI":(899,905), "PALDEA":(906,1025)}


def _refresh(receiver, category, status, total, old_counts, eligible, confirmed=frozenset(), confirmed_at=0):
    root = config_paths.state_dir()/"dex-registrations"/receiver
    files = sorted(root.glob(f"*/{category}.json"))
    registered, missing = set(status.registered | confirmed), set(status.missing - confirmed)
    disputed = set()
    audit_day = status.audit_day
    previous_by_region = {key:{d for d in status.missing if key[0] <= d <= key[1]} for key in REGIONS.values()}
    for path in files:
        if status.audit_day and path.parent.name < status.audit_day:
            continue
        data = json.loads(path.read_text())
        if (data["receiver"] != receiver or data["category"] != category or data["date"] != path.parent.name
                or type(data["total"]) is not int or data["total"] < total):
            # A lower lifetime total contradicts the previous trainer evidence.
            return DexStatus(audit_day=status.audit_day)
        prior_missing = missing
        missing = set() if data["total"] > total else set(prior_missing)
        seen, resolved = set(), {}
        for reg in data["regions"]:
            key = (reg["start"],reg["end"])
            header = path.parent/reg["header_shot"]
            if key not in REGIONS.values() or key in seen or not header.is_file():
                continue
            seen.add(key)
            lo,hi = key
            known = {n for n in registered if lo <= n <= hi}
            count = reg["registered_total"]
            if type(count) is not int or not 0 <= count <= hi-lo+1:
                continue
            def entries(field):
                return {int(n) for n, shot in reg.get(field,{}).items() if n.isdecimal() and lo <= int(n) <= hi
                        and isinstance(shot,str) and (path.parent/shot).is_file()}
            positive,negative = entries("registered"), entries("missing")
            conflicts = negative & (known | positive)
            if count == hi-lo+1:
                conflicts.update(negative)
            if conflicts or len(positive | known) > count:
                # A lifetime registration cannot disappear. Withhold any
                # disputed identity from targets and from strict safe trash.
                disputed.update(conflicts if len(positive | known) <= count else positive | known)
                missing.difference_update(previous_by_region[key])
                continue
            if count == hi-lo+1 and not negative:
                # The two-slot Unknown Origin region is often represented
                # only by its directory count. A full region has no gaps.
                positive.update(range(lo,hi+1))
            registered.update(positive)
            locally_missing = set()
            missing.difference_update(previous_by_region[key])
            # Lifetime registrations only grow. If this region grew by
            # exactly its independently verified new registrations, the
            # original absence of every other species still holds.
            before_header = confirmed if confirmed_at <= header.stat().st_mtime else set()
            gained = previous_by_region[key] & (positive | before_header)
            if key in old_counts and count - old_counts[key] == len(gained):
                locally_missing.update(previous_by_region[key])
            if len(positive | known) == count:
                locally_missing.update(d for d in eligible if lo <= d <= hi and d not in registered)
                resolved[key] = count
            locally_missing.update(negative & eligible)
            locally_missing.difference_update(registered)
            previous_by_region[key] = locally_missing
            missing.update(locally_missing)
            old_counts[key] = count
        missing.difference_update(registered)
        total = data["total"]
        national = path.parent/data.get("national_shot","")
        if (not disputed and len(resolved) == len(REGIONS) and sum(resolved.values()) == total
                and len(registered) == total and national.is_file()):
            audit_day = data["date"]
    return DexStatus(frozenset(registered-disputed),frozenset(missing-disputed),audit_day,status.ordinary_tradeable)


def _lucky_owner(device, receiver, identifier):
    label = str(device).strip()
    native = re.fullmatch(r"(.*?)\s*\(([^()]+)\)",label)
    normalize = clear.normalize_species_token
    if native and identifier:
        return normalize(native[2]) == normalize(str(identifier))
    return any(normalize(label) == normalize(str(n)) for n in (receiver,identifier) if n) or (
        native is not None and normalize(native[1]) == normalize(receiver))


def lucky_status(report, votes, receiver, identifier=None):
    """Trust the named trainer's report only for exact, consistently observed Pokédex entries."""
    if not report or not votes or not receiver:
        return DexStatus()
    try:
        data = json.loads(Path(report).read_text())
        if not _lucky_owner(data["summary"]["device"],receiver,identifier):
            return DexStatus()
        listed = {int(s.split()[0]) for s in data["needed_lucky_list"]}
        if listed != clear.parse_dex_ranges(data["needed_lucky_query"]):
            return DexStatus()
        if len(listed) != data["summary"]["total_needed_lucky_tradeable"]:
            return DexStatus()
        observations = json.loads(Path(votes).read_text())
        registered, missing = set(), set()
        for key, rows in observations.items():
            if not key.isdecimal() or not rows:
                continue
            dex = int(key)
            states = {r[0] for r in rows if len(r) >= 3 and r[2]}
            if len(states) != 1 or any(len(r) < 3 or not r[2] for r in rows):
                continue
            captures = {str(r[2]).split("/")[0].replace("UNOVEA","UNOVA") for r in rows}
            if not any(region in captures and lo <= dex <= hi for region,(lo,hi) in REGIONS.items()):
                continue
            if states == {"lucky"} and dex not in listed:
                registered.add(dex)
            elif states == {"none"} and dex in listed:
                missing.add(dex)
        region_counts = {}
        headers = Path(report).parent/"region_headers2.json"
        if headers.is_file():
            for reg in json.loads(headers.read_text()).values():
                if reg.get("region") in REGIONS:
                    region_counts[REGIONS[reg["region"]]] = reg["lucky_count"]
        status = DexStatus(frozenset(registered),frozenset(missing))
        identity = hashlib.sha256(receiver.encode() + b'\0' + Path(report).read_bytes()).hexdigest()[:20]
        ledger = config_paths.state_dir()/"collect-trades"/f"registered-{identity}.jsonl"
        confirmed, confirmed_at = frozenset(), 0
        if ledger.is_file():
            confirmed = frozenset(int(s) for s in ledger.read_text().splitlines() if s.isdecimal()) & status.missing
            confirmed_at = ledger.stat().st_mtime
        # A new direct observation can resolve an entry the old Lucky report
        # omitted. The complete species map contributes only tradability here.
        eligible = listed | perfect_status({"receiver":receiver}).ordinary_tradeable
        return _refresh(receiver,"lucky",status,data["summary"]["total_lucky"],region_counts,eligible,
                        confirmed,confirmed_at)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return DexStatus()


def lucky_gaps(report, votes, receiver, identifier=None):
    return set(lucky_status(report,votes,receiver,identifier).missing)


def _perfect_status(folder, summary):
    """Cross-check the full numbered map with the scanned register and each in-game regional total."""
    numbered = json.loads((folder/"registered.json").read_text())
    votes = json.loads((folder/"votes.json").read_text())
    registered = frozenset(numbered["registered"])
    size = summary["dex_species"]
    if (summary["registered_perfect"] != len(registered) or summary["dex_gaps"] != size-len(registered)
            or any(type(d) is not int or not 1 <= d <= size for d in registered)):
        return DexStatus()
    end = 0
    for start,last,count in numbered["region_checks"]:
        if start != end+1 or last < start or count != sum(start <= d <= last for d in registered):
            return DexStatus()
        end = last
    if not end or end != size:
        return DexStatus()
    maps = list(folder.glob("*-perfect-map.csv"))
    if len(maps) != 1:
        return DexStatus()
    with maps[0].open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != size or {int(r["Dex"]) for r in rows} != set(range(1,size+1)):
        return DexStatus()
    missing, eligible = set(), set()
    for row in rows:
        d = int(row["Dex"])
        if row["Registered in 100% Dex"] != ("Yes" if d in registered else "No"):
            return DexStatus()
        frames = votes.get(str(d), [])
        if row["Trade eligibility"] == "Ordinary trade":
            eligible.add(d)
        if (d not in registered and row["Trade eligibility"] == "Ordinary trade" and frames
                and all(len(obs) >= 3 and obs[0] and obs[1] is False for obs in frames)):
            missing.add(d)
    counts = {(lo,hi):n for lo,hi,n in numbered["region_checks"]}
    status = DexStatus(registered,frozenset(missing),summary["date"],frozenset(eligible))
    return _refresh(summary["receiver"],"perfect",status,len(registered),counts,eligible)


def perfect_status(cfg):
    receiver = cfg.get("receiver")
    if not receiver:
        return DexStatus()
    root = config_paths.state_dir()/"perfect-audit"
    for path in sorted(root.glob("*/summary.json"),reverse=True):
        try:
            summary = json.loads(path.read_text())
            if summary["receiver"] != receiver:
                continue
            if summary["date"] != path.parent.name:
                return DexStatus()
            return _perfect_status(path.parent, summary)
        except (OSError, ValueError, KeyError, TypeError, IndexError, csv.Error):
            return DexStatus()
    return DexStatus()


def perfect_gaps(cfg):
    return set(perfect_status(cfg).missing)
