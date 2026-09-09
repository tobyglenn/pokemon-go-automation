# File reference

Start with the public command for the action you want. Follow it into fleet dispatch, then the platform worker. Installation-specific values belong in private configuration rather than these files.

## Main commands

| File | Use |
| --- | --- |
| [`send_gifts.py`](../send_gifts.py) | Public gift-opening/sending command. Start with `--count 1 --plan`. |
| [`add_friends.py`](../add_friends.py) | Public friend-request command and Android/iOS request controllers. Supply private trainer-code inputs. |
| [`battle_league.py`](../battle_league.py) | Public GBL command. Use `--check` for a detector check or `--count 1 --plan` to inspect dispatch. |
| [`trade_pokemon.py`](../trade_pokemon.py) | Public paired-trade command. Specify `--pair DEVICE_A DEVICE_B --count 1 --plan`. |
| [`transfer_pokemon.py`](../transfer_pokemon.py) | Public transfer command. Plan first; live deletion requires `--confirm-delete`. |
| [`feed_berries.py`](../feed_berries.py) | Public gym-berry command with a per-phone spending budget. |
| [`fleet.py`](../fleet.py) | Fleet status and dispatch entry point. |

The legacy names `gift.py`, `gbl.py`, `trade.py`, `luckytrash.py`, `berry.py`, and `pokemon_fleet.py` provide compatibility. The clearer names do not replace the underlying algorithms or their safeguards.

## Coordination and configuration

| File | Responsibility |
| --- | --- |
| [`sources/fleet_entrypoint.py`](../sources/fleet_entrypoint.py) | Public launch routing, configured remote computers, worker execution, and local-only behavior. |
| [`sources/pokemon_fleet.py`](../sources/pokemon_fleet.py) | Fleet loading, device selection/readiness, workflow dispatch, pair controllers, and guarded trade/battle coordination. |
| [`sources/config_paths.py`](../sources/config_paths.py) | Resolve external private config and state paths independently of the current working directory. |
| [`sources/fleet_watchdog.py`](../sources/fleet_watchdog.py) | Monitor automation progress and attached hardware; stop stalled or disconnected runs. |
| [`sources/ios_attached_devices.py`](../sources/ios_attached_devices.py) | Discover attached iOS devices for readiness and selection. |
| [`sources/ios_wda_cleanup.py`](../sources/ios_wda_cleanup.py) | Handle WebDriverAgent cleanup associated with iOS sessions. |
| [`pokemon_go_automation/`](../pokemon_go_automation/) | Installable package and `pogo` console interface. |

## Workflow workers

| File | Responsibility and entry route |
| --- | --- |
| [`sources/gift_android.py`](../sources/gift_android.py) | Android gift loop and screen recovery; reached through `send_gifts.py`. |
| [`sources/gift_ios.py`](../sources/gift_ios.py) | Appium gift navigation, picker handling, and session lifecycle; reached through `send_gifts.py`. |
| [`sources/luckytrash_android.py`](../sources/luckytrash_android.py) | Android transfer implementation; reached through `transfer_pokemon.py`. |
| [`sources/luckytrash_ios.py`](../sources/luckytrash_ios.py) | iOS transfer implementation and rehearsal/selection handling; reached through `transfer_pokemon.py`. |
| [`sources/berry_android.py`](../sources/berry_android.py) | Android gym/defender berry feeding; reached through `feed_berries.py`. |
| [`sources/berry_ios.py`](../sources/berry_ios.py) | Guarded Appium gym-berry feeding; reached through `feed_berries.py`. |
| [`sources/trade_android.py`](../sources/trade_android.py) | Legacy Android paired-trade loop and capture/input support. |
| [`sources/trade_ios_android.py`](../sources/trade_ios_android.py) | Mixed-platform trade building blocks and iOS trade support used by fleet routing. |
| [`sources/battle_android.py`](../sources/battle_android.py) | Legacy Android friendly-battle implementation. Use `battle.py` to enter through supported fleet selection. |
| [`sources/excellent_throw_android.py`](../sources/excellent_throw_android.py) | Android vision-guided throw assistance, exposed through `excellent_throw.py`. |
| [`sources/excellent_throw_ios.py`](../sources/excellent_throw_ios.py) | iOS vision-guided throws for a prepared encounter, exposed through `excellent_throw.py`. |

## GBL implementation

| File | Responsibility |
| --- | --- |
| [`sources/gbl_android.py`](../sources/gbl_android.py) | Android battle entry, combat input, results, and rewards. |
| [`sources/gbl_ios.py`](../sources/gbl_ios.py) | iOS GBL state handling and Appium input. |
| [`sources/gbl_day.py`](../sources/gbl_day.py) | Longer daily-run orchestration, caps, and progress; public entry is `gbl_day.py`. |
| [`sources/gbl_strategy.py`](../sources/gbl_strategy.py) | Shared strategy decisions and per-profile settings. |
| [`sources/gbl_meta.py`](../sources/gbl_meta.py) | PvPoke-backed roster ranking and GBL party preparation. Its caches and roster data belong in private state. |
| [`sources/gbl_vision.py`](../sources/gbl_vision.py) | macOS Vision OCR bridge for menu and opponent recognition. |
| [`sources/gbl_home_recovery.py`](../sources/gbl_home_recovery.py) | Recover from the game map to the GBL entry screen. |
| [`sources/gbl_evaluator.py`](../sources/gbl_evaluator.py) | League evaluation and team-building support; public utility is `gbl_evaluator.py`. |
| [`sources/inventory_scanner_ios.py`](../sources/inventory_scanner_ios.py) | iOS inventory scanning support; public utility is `inventory_scanner_ios.py`. |

## Supporting material

| Path | Use |
| --- | --- |
| [`examples/`](../examples/) | Generic fleet and profile templates. Copy into your private config directory and calibrate before use. |
| [`tests/`](../tests/) | Regression tests using synthetic inputs, fake devices, and generated screen states where applicable. |
| [`scripts/check_public_tree.py`](../scripts/check_public_tree.py) | Privacy scan of staged content and reachable history; supports an external private denylist. |
| [`pyproject.toml`](../pyproject.toml) | Python build metadata, dependencies, optional platform dependencies, and console entry points. |
| [`build_backend.py`](../build_backend.py) | Build-only delegation to setuptools, with source-archive ownership and header metadata normalized before release. |
| [`AGENTS.md`](../AGENTS.md) | Rules for coding assistants: privacy, fleet dispatch, collaboration, and physical verification. |
| [`CONTRIBUTING.md`](../CONTRIBUTING.md) | Contribution and validation expectations. |
| [`LICENSE`](../LICENSE) | Preserved MIT license and upstream attribution. |

See [Architecture](architecture.md) for the call layers and [Workflows](workflows.md) for complete preparation and usage examples.
