# Pokémon GO Automation

A Python toolkit for coordinating real Android and iOS phones: send and open gifts, add friends, play GO Battle League, trade Pokémon, and transfer selected Pokémon. It combines reusable workflow commands, platform-specific screen workers, and optional coordination across multiple computers.

The repository contains the automation code and example configuration. Your phone identifiers, computer addresses, signing settings, trainer data, calibration files, and run history belong in private directories outside the checkout.

## Start with a workflow

| Workflow | Public entry point | What it does |
| --- | --- | --- |
| Gifts | [`send_gifts.py`](send_gifts.py) | Walk the friend list and open/send gifts with screen checks and bounded runs. |
| Friends | [`add_friends.py`](add_friends.py) | Add trainer codes from an explicit list or supported public source, with request history. |
| GO Battle League | [`battle_league.py`](battle_league.py) | Coordinate battle entry, combat, results, and reward handling across selected phones. |
| Trades | [`trade_pokemon.py`](trade_pokemon.py) | Run the trade sequence for a supported pair of phones attached to the same computer. |
| Transfers | [`transfer_pokemon.py`](transfer_pokemon.py) | Transfer Pokémon from a deliberately prepared selection; execution requires the deletion confirmation option. |
| Gym berries | [`feed_berries.py`](feed_berries.py) | Feed defenders using an explicit berry budget. |
| Fleet status | [`fleet.py`](fleet.py) | Inspect configuration and connected hardware before running a workflow. |

The original `gift.py`, `gbl.py`, `trade.py`, `luckytrash.py`, `berry.py`, and `pokemon_fleet.py` names remain as compatibility entry points. See the [workflow guide](docs/workflows.md) for preparation, commands, and platform limits.

## Get running

Install Python and Android Platform Tools separately. iOS control additionally requires macOS, Xcode, Appium, and a working WebDriverAgent setup.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python fleet.py --help
```

Follow [Getting started](docs/getting-started.md) to create private configuration and calibrate your phones. Review a plan before allowing a command to interact with the game:

```sh
python send_gifts.py --devices all --count 1 --plan
python battle_league.py --devices all --count 1 --plan
python transfer_pokemon.py --devices all --count 1 --plan
```

Configured computers participate automatically. Use `--local` when deliberately checking only the current computer. Examples are starting points: they do not identify real phones and must be completed before use.

Use an explicit count for initial runs. Gifts default to `--all` with a 100-cycle cap per phone, GBL defaults to five battles, and trades default to one trade. Transfers require an explicit count or `--all` plus `--confirm-delete` for execution. See the workflow guide before increasing a limit.

`--devices all` includes connected Android phones absent from the private registry. Disabled example records do not disable discovery of unregistered phones. Review the plan and use explicit device names for a narrowly scoped first run; worker-side calibration and pair checks still apply. Plans list configured devices without probing phones; they do not enumerate extra unregistered Android phones that execution may discover. Use live fleet status to inspect connected hardware and explicit device selection to constrain the run.

## Understand and extend it

- [Getting started](docs/getting-started.md): prerequisites, installation, and first connection.
- [Configuration](docs/configuration.md): private devices, calibration, paths, and multiple computers.
- [Workflows](docs/workflows.md): preparation and usage for each command.
- [Architecture](docs/architecture.md): how entry points, fleet coordination, and workers connect.
- [File reference](docs/file-reference.md): where the implementation for each feature lives.
- [AI handoff](docs/ai-handoff.md): a reproducible starting point for another coding assistant.
- [Testing](docs/testing.md): distinguish code checks, plans, connected hardware, and completed actions.
- [Privacy](docs/privacy.md): what to exclude from Git and how to review a public release.

These are screen-driven automations. Coordinates, visual detection, game UI changes, and connection quality affect behavior. A passing test suite or successful plan does not establish that a workflow completed on a physical phone. Supervise initial runs and stop when the visible state differs from the expected workflow.

## Attribution

This project builds on the AutoTrader code attributed to `jonaro00`. The original MIT copyright and permission notice are preserved in [LICENSE](LICENSE). Android Platform Tools, Appium, and other external tools are installed separately and are not bundled here. Pokémon GO is a trademark of its respective owners; this is an independent project.
