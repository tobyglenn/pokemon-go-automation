"""Small, lazy-loading public command dispatcher."""
from __future__ import annotations
import argparse
import importlib
from importlib import resources
import os
from pathlib import Path
import sys

COMMANDS = {"gifts": "send_gifts", "friends": "add_friends", "gbl": "battle_league",
            "trades": "trade_pokemon", "transfers": "transfer_pokemon", "berries": "feed_berries",
            "gbl-day": "gbl_day", "status": "fleet"}

def init_config(directory: Path | None = None) -> int:
    from sources.config_paths import private_config_dir
    target = directory.expanduser().resolve() if directory else private_config_dir()
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    templates = resources.files("pokemon_go_automation").joinpath("templates")
    created = []
    for template in templates.iterdir():
        if template.name.endswith(".yaml") and template.name != "machines.example.yaml":
            path = target / template.name
            try:
                with path.open("x") as handle:
                    handle.write(template.read_text())
                path.chmod(0o600)
                created.append(template.name)
            except FileExistsError:
                pass
    print(f"Private configuration: {target}")
    print(f"Created {len(created)} templates; existing files were preserved.")
    print("Set your identifiers, calibrate coordinates, then enable devices in pokemon-fleet.yaml.")
    return 0

def main(arguments=None) -> int:
    argv = list(sys.argv[1:] if arguments is None else arguments)
    parser = argparse.ArgumentParser(prog="pogo", description="Pokémon GO automation across your configured devices and hosts.")
    parser.add_argument("command", nargs="?", choices=[*COMMANDS, "init-config"])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    if not argv or argv[0] in {"-h", "--help"}:
        parser.print_help()
        print("Commands default to all configured hosts. Use --local for one host and --plan to preview.")
        return 0
    args = parser.parse_args(argv)
    try:
        if args.command == "init-config":
            setup = argparse.ArgumentParser(prog="pogo init-config")
            setup.add_argument("--directory", type=Path)
            return init_config(setup.parse_args(args.arguments).directory)
        module = importlib.import_module(COMMANDS[args.command])
        forwarded = (["status"] if args.command == "status" else []) + args.arguments
        return module.main(forwarded)
    except KeyboardInterrupt:
        print("Automation cancelled", file=sys.stderr)
        return 130
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
