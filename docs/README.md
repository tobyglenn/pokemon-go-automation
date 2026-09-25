# Pokémon GO Automation

A Python toolkit for coordinating real Android and iOS phones: send and open gifts, add friends, play GO Battle League, trade Pokémon, and transfer selected Pokémon. It combines reusable workflow commands, platform-specific screen workers, and optional coordination across multiple computers.

The repository contains the automation code and example configuration. Your phone identifiers, computer addresses, signing settings, trainer data, calibration files, and run history belong in private directories outside the checkout.

## Start with a workflow

| Workflow | Public entry point | What it does |
| --- | --- | --- |
| Whatever is on screen | [`play.py`](../play.py) | Look at every phone and start the job its screen shows: GBL for the battle card or party picker, berries for a gym defender's feeding card, gifts for the friends list or a friend's profile, catching for an award card, research CLAIM or open encounter. Other screens are left alone. `--plan` only looks. |
| Gifts and gym berries | [`gift.py`](../gift.py) | Each phone's screen picks the job: a phone parked on a gym's feeding screen feeds the defenders, every other phone walks the friend list and opens/sends gifts. `--gifts-only` skips the look. |
| Catching rewards | [`catch.py`](../catch.py) | Work the award queue ("You have N Pokémon left to catch") or a research screen's CLAIM buttons, whichever each phone shows. Android and iPhone. |
| Friends | [`add_friends.py`](../add_friends.py) | Add trainer codes from an explicit list or supported public source, with request history. |
| Friend removal | [`remove_friends.py`](../remove_friends.py) | Remove friends who sent no gift, carry no halo, and sit at or under a heart count (Android). |
| GO Battle League | [`gbl.py`](../gbl.py) | Coordinate battle entry, combat, results, and reward handling across selected phones. |
| A day of GBL | [`gbl_day.py`](../gbl_day.py) | Play a day's whole GBL allotment on the named phones, restarting stalled legs. |
| Trades | [`trade.py`](../trade.py) | Run the trade sequence for a supported pair of phones attached to the same computer. |
| Transfers | [`luckytrash.py`](../luckytrash.py) | Transfer Pokémon from a deliberately prepared selection; execution requires the deletion confirmation option. |

Everything else lives in [`scripts/`](../scripts): the fleet status and dispatcher [`scripts/fleet.py`](../scripts/fleet.py), the berries-only [`scripts/berry.py`](../scripts/berry.py), the GBL team tools, the package build backend, and the older long command names (`send_gifts.py`, `battle_league.py`, `trade_pokemon.py`, `transfer_pokemon.py`, `feed_berries.py`, `pokemon_fleet.py`), kept as compatibility entry points. Run them from the checkout root, e.g. `python scripts/fleet.py status`. See the [workflow guide](workflows.md) for preparation, commands, and platform limits.

## Get running

Install Python and Android Platform Tools separately. iOS control additionally requires macOS, Xcode, Appium, and a working WebDriverAgent setup.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python scripts/fleet.py --help
```

Follow [Getting started](getting-started.md) to create private configuration and calibrate your phones. Review a plan before allowing a command to interact with the game:

```sh
python gift.py --devices all --count 1 --plan
python gbl.py --devices all --count 1 --plan
python luckytrash.py --devices all --count 1 --plan
```

Configured computers participate automatically, and a named phone is sent to whichever of them is actually holding its cable: move a phone between computers and nothing needs reconfiguring. `scripts/fleet.py owners` shows where every configured phone currently is. Use `--local` when deliberately checking only the current computer. Examples are starting points: they do not identify real phones and must be completed before use.

Use an explicit count for initial runs. Gifts default to `--all` with a 100-cycle cap per phone, GBL defaults to five battles, and trades default to one trade. Transfers require an explicit count or `--all` plus `--confirm-delete` for execution. See the workflow guide before increasing a limit.

`--devices all` includes connected Android phones absent from the private registry. Disabled example records do not disable discovery of unregistered phones. Review the plan and use explicit device names for a narrowly scoped first run; worker-side calibration and pair checks still apply. Plans list configured devices without probing phones; they do not enumerate extra unregistered Android phones that execution may discover. Use live fleet status to inspect connected hardware and explicit device selection to constrain the run.

## Understand and extend it

- [Getting started](getting-started.md): prerequisites, installation, and first connection.
- [Configuration](configuration.md): private devices, calibration, paths, and multiple computers.
- [Workflows](workflows.md): preparation and usage for each command.
- [Architecture](architecture.md): how entry points, fleet coordination, and workers connect.
- [File reference](file-reference.md): where the implementation for each feature lives.
- [AI handoff](ai-handoff.md): a reproducible starting point for another coding assistant.
- [Testing](testing.md): distinguish code checks, plans, connected hardware, and completed actions.
- [Privacy](privacy.md): what to exclude from Git and how to review a public release.

These are screen-driven automations. Coordinates, visual detection, game UI changes, and connection quality affect behavior. A passing test suite or successful plan does not establish that a workflow completed on a physical phone. Supervise initial runs and stop when the visible state differs from the expected workflow.

## Attribution

This project builds on the AutoTrader code attributed to `jonaro00`. The original MIT copyright and permission notice are preserved in [LICENSE](LICENSE). Android Platform Tools, Appium, and other external tools are installed separately and are not bundled here. Pokémon GO is a trademark of its respective owners; this is an independent project.
