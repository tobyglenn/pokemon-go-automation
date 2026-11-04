#!/usr/bin/env python3
"""Run supervised raid preparation: evolutions, Level 40 power-ups, and Mega tagging.

Supports attached Android and iOS fleet devices, with safety boundaries:
1. Zero Rare Candies or Rare Candy XL consumed.
2. Power-ups capped strictly at Level 40 (avoids Candy XL).
3. Supervised tasks and plans inspected before execution.

Usage:
    python scripts/raid_prep.py --devices all --plan
    python scripts/raid_prep.py --devices oneplus --phase power_up --term "98Plus&Tyranitar" --target-cp 3830
    python scripts/raid_prep.py --devices motog --phase tag --term "98Plus&Lucario" --tag mega
    python scripts/raid_prep.py --tasks-file prep_tasks.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import (  # noqa: E402
    config_paths,
    pokemon_fleet,
    raid_prep_android,
    raid_prep_ios,
    raid_scan_android,
    raid_scan_ios,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("raid_prep")


@dataclass
class PrepTask:
    name: str
    search_term: str
    expected_cp: int | None = None
    evolve_count: int = 0
    target_cp: int | None = None
    tag_name: str | None = None
    mega_evolve: bool = False


def load_tasks(path: Path | None) -> list[PrepTask]:
    if path is None or not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    tasks = []
    for item in data:
        tasks.append(
            PrepTask(
                name=item.get("name", "Unknown"),
                search_term=item.get("search_term", ""),
                expected_cp=item.get("expected_cp"),
                evolve_count=item.get("evolve_count", 0),
                target_cp=item.get("target_cp"),
                tag_name=item.get("tag_name"),
                mega_evolve=item.get("mega_evolve", False),
            )
        )
    return tasks


def run_task(
    worker: raid_prep_ios.IOSRaidPrepWorker | raid_prep_android.AndroidRaidPrepWorker,
    task: PrepTask,
    phase: str,
) -> dict:
    """Run one task's phases on either platform's worker; both share one interface."""
    result = {"task": task.name, "term": task.search_term, "actions": []}
    logger.info("Starting task: %s (%s)", task.name, task.search_term)

    opened = worker.search_and_open(task.search_term, expected_cp=task.expected_cp)
    if not opened:
        result["actions"].append({"action": "open", "status": "failed", "note": "Could not open Pokémon"})
        return result

    # Phase 1: Evolutions
    if phase in ("evolve", "all") and task.evolve_count > 0:
        for stage in range(task.evolve_count):
            logger.info("Evolving %s stage %d/%d", task.name, stage + 1, task.evolve_count)
            evo_res = worker.evolve()
            result["actions"].append(
                {"action": f"evolve_{stage + 1}", "status": "ok" if evo_res.success else "failed", "cp": evo_res.new_cp}
            )
            if not evo_res.success:
                break

    # Phase 2: Power Up to Level 40
    if phase in ("power_up", "all") and task.target_cp:
        logger.info("Powering up %s to target CP %s", task.name, task.target_cp)
        pwr_res = worker.power_up_to_level_40(target_cp=task.target_cp)
        result["actions"].append(
            {
                "action": "power_up",
                "status": "ok" if pwr_res.success else "failed",
                "initial_cp": pwr_res.initial_cp,
                "final_cp": pwr_res.final_cp,
                "stardust": pwr_res.stardust_cost,
                "candy": pwr_res.candy_cost,
            }
        )

    # Phase 3: Tagging
    if phase in ("tag", "all") and task.tag_name:
        logger.info("Tagging %s with '%s'", task.name, task.tag_name)
        tagged = worker.tag_pokemon(task.tag_name)
        result["actions"].append({"action": "tag", "tag": task.tag_name, "status": "ok" if tagged else "failed"})

    # Phase 4: Mega Evolution
    if phase in ("mega_evolve", "all") and task.mega_evolve:
        logger.info("Mega evolving %s", task.name)
        mega_ok = worker.mega_evolve()
        result["actions"].append({"action": "mega_evolve", "status": "ok" if mega_ok else "failed"})

    worker.close_detail()
    return result


# The platform-specific names this module first shipped with.
run_ios_task = run_task
run_android_task = run_task


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--devices", nargs="*", default=["all"], help="Device names or 'all'")
    parser.add_argument(
        "--phase",
        choices=["evolve", "power_up", "tag", "mega_evolve", "all"],
        default="all",
        help="Which prep phase to execute",
    )
    parser.add_argument("--plan", action="store_true", help="Print plan and touch nothing")
    parser.add_argument("--tasks-file", type=Path, default=None, help="JSON file containing task definitions")
    parser.add_argument("--term", help="Ad-hoc single candidate search term")
    parser.add_argument("--expected-cp", type=int, help="Expected initial CP for ad-hoc candidate")
    parser.add_argument("--target-cp", type=int, help="Target Level 40 CP for power-up")
    parser.add_argument("--evolve-count", type=int, default=0, help="Number of evolution stages")
    parser.add_argument("--tag", default="mega", help="Tag name to apply")
    parser.add_argument("--mega-evolve", action="store_true", help="Mega evolve candidate")

    args = parser.parse_args(argv)

    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    wanted = set(args.devices or ["all"])
    specs = [
        spec
        for spec in fleet.devices.values()
        if spec.enabled and ("all" in wanted or spec.name in wanted)
    ]
    unknown = sorted(wanted - {"all"} - {spec.name for spec in specs})
    if unknown:
        print(f"Error: not an enabled fleet device: {', '.join(unknown)}", file=sys.stderr)
        return 2

    # Collect tasks
    tasks = load_tasks(args.tasks_file)
    if args.term:
        tasks.append(
            PrepTask(
                name="Ad-hoc Candidate",
                search_term=args.term,
                expected_cp=args.expected_cp,
                evolve_count=args.evolve_count,
                target_cp=args.target_cp,
                tag_name=args.tag if args.phase in ("tag", "all") else None,
                mega_evolve=args.mega_evolve,
            )
        )

    if not tasks:
        logger.info("No tasks defined. Use --tasks-file or provide --term.")
        return 0

    if args.plan:
        print("\n=== Raid Preparation Plan ===")
        print(f"Phase: {args.phase}")
        print(f"Target Devices: {', '.join(s.name for s in specs)}")
        print(f"Tasks ({len(tasks)}):")
        for idx, t in enumerate(tasks, 1):
            details = []
            if t.evolve_count:
                details.append(f"evolve x{t.evolve_count}")
            if t.target_cp:
                details.append(f"power-up -> CP {t.target_cp}")
            if t.tag_name:
                details.append(f"tag '{t.tag_name}'")
            if t.mega_evolve:
                details.append("mega-evolve")
            print(f"  {idx}. {t.name} [{t.search_term}] -> {', '.join(details) or 'inspect'}")
        print("\nPlan mode: touched nothing.")
        return 0

    # Execution requires live device attachments
    for spec in specs:
        logger.info("Connecting to fleet device: %s (%s)", spec.name, spec.platform)
        if spec.platform == "ios":
            phone = raid_scan_ios.IOSPhone(spec.identifier)
            worker = raid_prep_ios.IOSRaidPrepWorker(phone)
            for t in tasks:
                res = run_task(worker, t, args.phase)
                logger.info("Result for %s: %s", t.name, res)
        elif spec.platform == "android":
            phone = raid_scan_android.AndroidPhone(spec.identifier)
            worker = raid_prep_android.AndroidRaidPrepWorker(phone)
            for t in tasks:
                res = run_task(worker, t, args.phase)
                logger.info("Result for %s: %s", t.name, res)

    return 0


if __name__ == "__main__":
    sys.exit(main())
