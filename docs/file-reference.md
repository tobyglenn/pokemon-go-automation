# File reference

Start with the public command for the action you want. Follow it into fleet dispatch, then the platform worker. Installation-specific values belong in private configuration rather than these files.

## Main commands

| File | Use |
| --- | --- |
| [`special_trade.py`](../special_trade.py) | The receiver's daily special trade: a spare legendary for a shiny it is missing, with a partner on the same computer. Start with `--plan`. |
| [`starter.py`](../starter.py) | Public start-of-day command: buddy play, a Blanche training battle with the buddy, free Mega Evolutions, gifts, GBL, then a second buddy round on each phone. Start with `--plan`. |
| [`play.py`](../play.py) | Public screen-driven command: reads every phone and starts GBL, berries, gifts or catching. Start with `--plan`. |
| [`goplus.py`](../goplus.py) | Public Pokémon GO Plus reconnect command for Android phones. Start with `--plan`. |
| [`gift.py`](../gift.py) | Public gifts command; a phone parked on a gym's feeding screen feeds berries instead. Start with `--count 1 --plan`; `--gifts-only` skips the screen read. |
| [`catch.py`](../catch.py) | Public catching command for the award queue, research CLAIM buttons and hand-opened encounters (`--mode encounter`), Android and iPhone. Start with `--dry-run`. |
| [`add_friends.py`](../add_friends.py) | Public friend-request command for Android/iOS request controllers. Supply private trainer-code inputs. |
| [`remove_friends.py`](../remove_friends.py) | Public friend-removal command (Android). Lists only, until `--confirm-remove`. |
| [`gbl.py`](../gbl.py) | Public GBL command. Use `--check` for a detector check or `--count 1 --plan` to inspect dispatch. |
| [`gbl_day.py`](../gbl_day.py) | A day's whole GBL allotment, restarting stalled legs. |
| [`trade.py`](../trade.py) | Public paired-trade command. Specify `--pair DEVICE_A DEVICE_B --count 1 --plan`. |
| [`daily_trade.py`](../daily_trade.py) | Public daily 100-trade command across attached pairs on configured hosts. Start with `--plan` or `--dry-run`. |
| [`priority_trade.py`](../priority_trade.py) | Receiver-first daily trades across the fleet. Start with `--plan`. |
| [`collect_trades.py`](../collect_trades.py) | Collect Lucky Dex gaps for the configured receiver, then trade valuable spares across connected phones. Start with `--plan` or `--dry-run`. |
| [`raidspend.py`](../raidspend.py) | Public raid candy planning command: `--scan` reads each phone's storage; the default plans candy and XL spending per attacking type. Read-only. |
| [`luckytrash.py`](../luckytrash.py) | Public transfer command. Plan first; live deletion requires `--confirm-delete`. |
| [`clear_pokemon.py`](../clear_pokemon.py) | Public safe single-query clearing command with Lucky Dex/evolution/raid auditing and Vision-OCR verification. Start with `--review`, `--plan`, `--dry-run`, or `--open`. |

## Helper scripts

Run from the checkout root, e.g. `python scripts/fleet.py status`; on another computer they run as `python -m scripts.<name>`.

| File | Use |
| --- | --- |
| [`scripts/fleet.py`](../scripts/fleet.py) | Fleet status, owners, and dispatch entry point; every public command ends up here. |
| [`scripts/berry.py`](../scripts/berry.py) | Berries-only command, for `--check` or a run that must not fall back to gifts. |
| [`scripts/excellent_throw.py`](../scripts/excellent_throw.py) | Raw excellent-throw watcher for calibration (`--check`, `--probe-ring`); catching itself is `catch.py --mode encounter`. |
| [`scripts/battle.py`](../scripts/battle.py) | Paired friendly battles. |
| [`scripts/tag_spare_legendaries.py`](../scripts/tag_spare_legendaries.py) | Appraises the special-trade receiver's 3★ legendaries and tags spares `ToTrade` (Attack ≤ 13) or `SilverBottlePot` (two 15s). |
| [`scripts/tag_living_dex.py`](../scripts/tag_living_dex.py) | Scans a single designated phone (`1–1025`) adaptively and tags every sole-copy (`count == 1`) Pokémon as `dontrade`. |
| [`scripts/gbl_evaluator.py`](../scripts/gbl_evaluator.py) | GBL team evaluation from an inventory. |
| [`scripts/inventory_scanner_ios.py`](../scripts/inventory_scanner_ios.py) | iOS storage scan feeding the evaluator. |
| [`scripts/build_backend.py`](../scripts/build_backend.py) | Build-only delegation to setuptools, with source-archive ownership header metadata normalized before release. |
| [`scripts/check_public_tree.py`](../scripts/check_public_tree.py) | Privacy check of staged content and reachable history. |
| [`scripts/raid_prep.py`](../scripts/raid_prep.py) | Supervised raid-attacker preparation (evolve, power up to level 40, tag, Mega Evolve). Spends candy and Stardust: start with `--plan`. Runs on this computer only. |

`scripts/send_gifts.py`, `scripts/battle_league.py`, `scripts/trade_pokemon.py`, `scripts/transfer_pokemon.py`, `scripts/feed_berries.py`, and `scripts/pokemon_fleet.py` are the older long command names, kept for compatibility; they run exactly what `gift.py`, `gbl.py`, `trade.py`, `luckytrash.py`, `scripts/berry.py`, and `scripts/fleet.py` run.

## Coordination and configuration

| File | Responsibility |
| --- | --- |
| [`sources/fleet_entrypoint.py`](../sources/fleet_entrypoint.py) | Public launch routing, configured remote computers, worker execution, and local-only behavior. |
| [`sources/device_owners.py`](../sources/device_owners.py) | Discover which configured computer each phone is attached to, so routing follows the cable rather than the registry's `machine` field. |
| [`sources/pokemon_fleet.py`](../sources/pokemon_fleet.py) | Fleet loading, device selection/readiness, workflow dispatch, pair controllers, and guarded trade/battle coordination. |
| [`sources/config_paths.py`](../sources/config_paths.py) | Resolve external private config and state paths independently of the current working directory. |
| [`sources/fleet_watchdog.py`](../sources/fleet_watchdog.py) | Monitor automation progress and attached hardware; stop stalled or disconnected runs. |
| [`sources/ios_attached_devices.py`](../sources/ios_attached_devices.py) | Discover attached iOS devices for readiness and selection. |
| [`sources/ios_wda_cleanup.py`](../sources/ios_wda_cleanup.py) | WebDriverAgent cleanup, plus what every iOS worker shares: the XCUITest capabilities, the one-retry session start for a preinstalled runner, stale-session detection, and the per-iPhone lock file. |
| [`sources/android_memory.py`](../sources/android_memory.py) | Frees RAM on each Android once per run (catch, gbl, gbl_day, starter) by stopping background apps the game never needs, so the low-memory killer does not take the game mid-throw. `POGO_MEMORY_FREED` lets child runs skip phones already freed. |
| [`sources/android_common.py`](../sources/android_common.py) | ADB plumbing every Android worker shares: the adb binary (the same one the fleet layer uses), the foldable display probe, the swipe tap, screen capture and framebuffer parsing, and pulling/validating the on-phone `AutoTraderConfig.yaml` into private state. |
| [`pokemon_go_automation/`](../pokemon_go_automation/) | Installable package and `pogo` console interface. |

## Workflow workers

| File | Responsibility and entry route |
| --- | --- |
| [`sources/gift_android.py`](../sources/gift_android.py) | Android gift loop and screen recovery; reached through `gift.py`. |
| [`sources/gift_ios.py`](../sources/gift_ios.py) | Appium gift navigation, picker handling, and session lifecycle; reached through `gift.py`. |
| [`sources/luckytrash_android.py`](../sources/luckytrash_android.py) | Android transfer implementation; reached through `luckytrash.py`. |
| [`sources/luckytrash_ios.py`](../sources/luckytrash_ios.py) | iOS transfer implementation and rehearsal/selection handling; reached through `luckytrash.py`. |
| [`sources/berry_android.py`](../sources/berry_android.py) | Android gym/defender berry feeding; reached through `gift.py` and `scripts/berry.py`. |
| [`sources/berry_ios.py`](../sources/berry_ios.py) | Guarded Appium gym-berry feeding; reached through `gift.py` and `scripts/berry.py`. |
| [`sources/trade_android.py`](../sources/trade_android.py) | Legacy Android paired-trade loop and capture/input support. |
| [`sources/trade_ios_android.py`](../sources/trade_ios_android.py) | Mixed-platform trade building blocks and iOS trade support used by fleet routing. |
| [`sources/battle_android.py`](../sources/battle_android.py) | Legacy Android friendly-battle implementation. Use `scripts/battle.py` to enter through supported fleet selection. |
| [`sources/excellent_throw_android.py`](../sources/excellent_throw_android.py) | Android vision-guided throw assistance, used by `catch.py` and `scripts/excellent_throw.py`. |
| [`sources/excellent_throw_ios.py`](../sources/excellent_throw_ios.py) | iOS vision-guided throws for a prepared encounter, used by `catch.py` and `scripts/excellent_throw.py`. |
| [`sources/catch_rewards.py`](../sources/catch_rewards.py) | Reads each phone's screen and runs the award-queue, research or wild-encounter loop; reached through `catch.py`. |
| [`sources/raid_scan.py`](../sources/raid_scan.py) | Fleet dispatcher: scans the selected phones in parallel, writes `raidscan_<device>.json` into private state, and reads a scan back from whichever computer took it. |
| [`sources/raid_scan_android.py`](../sources/raid_scan_android.py) | Android hands for the raid scan: reach storage, confirmed searches, cell taps, safe scrolling and leaving. |
| [`sources/raid_scan_ios.py`](../sources/raid_scan_ios.py) | iOS hands for the raid scan over Appium: the same steps without a BACK key. |
| [`sources/raid_inventory.py`](../sources/raid_inventory.py) | The per-species scan sequence and detail-page parser shared by both platforms: filters, HP, power-up price, moves, candy, XL candy and Mega Energy. |
| [`sources/raid_gamedata.py`](../sources/raid_gamedata.py) | Downloads and caches the PvPoke and PokeMiners game masters and derives the compact raid dataset. |
| [`sources/raid_math.py`](../sources/raid_math.py) | CP/HP, the level-and-IV solver, power-up costs, and the raid DPS/TDO rating. |
| [`sources/raid_meta.py`](../sources/raid_meta.py) | Per-type rankings computed from the game master, and the species list the scan searches. |
| [`sources/raid_plan.py`](../sources/raid_plan.py) | Turns one account's scan into the per-type candy and XL plan and its report. |
| [`sources/raid_prep_android.py`](../sources/raid_prep_android.py), [`sources/raid_prep_ios.py`](../sources/raid_prep_ios.py) | Platform hands for `scripts/raid_prep.py`. They refuse rather than guess: an expected CP that is not on the grid, a power-up dialog that did not open, or a missing confirm button stops the step. |
| [`sources/raid_prep_common.py`](../sources/raid_prep_common.py) | Result types and power-up dialog reads both raid-prep workers share. |
| [`sources/vision_ocr.py`](../sources/vision_ocr.py) | Apple Vision text recognition with bounding boxes, built once into the private state directory. Bounding boxes are what let the candy rows be told apart; bare text lines cannot. |
| [`sources/special_trade.py`](../sources/special_trade.py) | Reads the shiny gap report, picks base-form targets held by attached partners, counts both sides by storage search, trades through `trade_local`, and removes each traded species from the report. |
| [`sources/trade_plan.py`](../sources/trade_plan.py) | Raid/Mega family planner and 3-tier daily trade batch builder (`98Plus` gaps, depth fill, and IV/XL rerolls). |
| [`sources/trade_local.py`](../sources/trade_local.py) | Cross-platform Vision-OCR local trade runner with 100-Stardust and Lucky-Trade guards. |
| [`sources/starter_trade.py`](../sources/starter_trade.py) | Parallel pair storage counter, daily trade history tracker, and trade loop shared by `starter.py` and `daily_trade.py`. |
| [`sources/daily_trade.py`](../sources/daily_trade.py) | Daily 100-trade coordinator for attached pairs; reached through `daily_trade.py`. |
| [`sources/priority_trade.py`](../sources/priority_trade.py) | Budget planner and trade loop for `priority_trade.py`: the receiver priority tiers, then donor-to-donor trades. |
| [`sources/goplus_android.py`](../sources/goplus_android.py) | Reconnect the Pokémon GO Plus on Android: the phone's Bluetooth bond and connection state, the map's accessory disc, the game's pairing prompt and the Android pairing dialog; reached through `goplus.py` and `starter.py --only goplus`. |
| [`sources/goplus_ios.py`](../sources/goplus_ios.py) | Reconnect the Pokémon GO Plus on iPhone, verified by the map's connected indicator; reached through `starter.py --only goplus`. |
| [`sources/collect_trades.py`](../sources/collect_trades.py) | Trade picker and configured-target collector. The daily Lucky Dex and fleet pairing workflow lives in [collector_day.py](../sources/collector_day.py). [collector_proposals.py](../sources/collector_proposals.py) quotes and reselects a single approved guaranteed Lucky exchange. |
| [`sources/remote_target.py`](../sources/remote_target.py) | SSH JSON-RPC `Target` bridge allowing one coordinator to control Android and iOS devices connected to peer computers. |
| [`sources/clear_pokemon.py`](../sources/clear_pokemon.py) | Safe single-query clearing auditor and Vision-OCR transfer loop; reached through `clear_pokemon.py`. |
| [`sources/starter.py`](../sources/starter.py) | The start-of-day steps: mega tag walk, buddy play screen, heart check, and the hand-off to `gift.py` and `gbl_day.py`; reached through `starter.py`. |
| [`sources/screen_router.py`](../sources/screen_router.py) | Classifies each phone's screen and launches the matching command; reached through `play.py`. |
| [`sources/catch_ios.py`](../sources/catch_ios.py) | An iPhone the two catch loops drive like an Android: pixel-to-point taps, Excellent throw then flick. |
| [`sources/catch_awarded_android.py`](../sources/catch_awarded_android.py) | Award-queue loop: reads the award card, starts each encounter, and drives the throw loop; reached through `catch.py`. |
| [`sources/claim_gopass.py`](../sources/claim_gopass.py) | GO Pass / Event Pass claim loop: switches to Showing Claimable Rewards, sweeps unclaimed items with CLAIM ALL when present, and claims and catches each orange `POKÉMON APPEARS!` tile across BASIC and DELUXE columns; reached through `catch.py`. |
| [`sources/claim_research_android.py`](../sources/claim_research_android.py) | Research-claim loop: presses each CLAIM button and works what it pays; reached through `catch.py`. |
| [`sources/remove_friends_android.py`](../sources/remove_friends_android.py) | Android friend removal: reads each friends-list row's hearts, gift line and halo, opens the profile, presses REMOVE FRIEND and confirms only when the dialog names the same friend; reached through `remove_friends.py`. |

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
| [`sources/gbl_evaluator.py`](../sources/gbl_evaluator.py) | League evaluation and team-building support; public utility is `scripts/gbl_evaluator.py`. |
| [`sources/inventory_scanner_ios.py`](../sources/inventory_scanner_ios.py) | iOS inventory scanning support; public utility is `scripts/inventory_scanner_ios.py`. |

## Supporting material

| Path | Use |
| --- | --- |
| [`examples/`](../examples/) | Generic fleet and profile templates. Copy into your private config directory and calibrate before use. |
| [`tests/`](../tests/) | Regression tests using synthetic inputs, fake devices, and generated screen states where applicable. |
| [`pyproject.toml`](../pyproject.toml) | Python build metadata, dependencies, optional platform dependencies, and console entry points. |
| [`AGENTS.md`](../AGENTS.md) | Rules for coding assistants: privacy, fleet dispatch, collaboration, and physical verification. |
| [`docs/CONTRIBUTING.md`](CONTRIBUTING.md) | Contribution and validation expectations. |
| [`docs/LICENSE`](LICENSE) | Preserved MIT license and upstream attribution. |

See [Architecture](architecture.md) for the call layers and [Workflows](workflows.md) for complete preparation and usage examples.
