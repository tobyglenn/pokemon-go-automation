# Workflow guide

Run these commands from an installed checkout. The examples use public aliases and bounded counts. Use your own private fleet file to select real devices. `--help` is the authoritative option list for the installed version.

## Whatever is on screen — `play.py`

Park each phone on the screen of the job you want, then run one command. Every configured computer looks at its attached phones (screenshots only) and starts:

- `gbl_day.py` on the GO Battle League card, the league page or the party picker;
- `scripts/berry.py` on a gym defender's berry-feeding card -- open a defender first, the gym overview itself is left alone;
- `gift.py --all --gifts-only` on the friends list or a friend's profile;
- `catch.py` on an award card, a research CLAIM button or an open wild encounter.

A phone on any other screen, a trade's Pokémon picker included, is named and left alone; a phone another command already holds is skipped. Phones on the same screen share one command, and different jobs run side by side. `--plan` reads and reports without starting anything; `--spend N` caps berries; `--local` keeps it to this computer.

```sh
python play.py --plan
python play.py --devices android-one ios-one
```

## Gifts and gym berries — `gift.py`

Use an explicit `--count` for a supervised run. If neither `--count` nor `--all` is supplied, the public entry point chooses `--all`, capped at 100 cycles per phone by `--max-cycles`.

**Screen routing:** before anything is tapped, each phone's screen is read. A phone parked on a gym's feeding screen (a defender open with its berry button showing) feeds the defenders instead, exactly as `scripts/berry.py` would, under `--spend`/`--spend-device` budgets; every other phone runs the gift loop. An open catch encounter passes the feeding-screen test on its own, so a ball in hand sends the phone to the gifts instead. On an iPhone the berry worker connects first and steps aside, touching nothing, when the phone is not on a feeding screen. `--gifts-only` skips the look and gifts from every phone.

**Purpose:** open and send gifts while moving through the Friends interface. The platform workers recognize expected states, handle picker transitions, and stop or recover when the visible state differs.

**Prepare:** connect and unlock the phone, open the Friends interface, and verify the appropriate gift profile/calibration. Gift availability and daily game limits still apply.

**Row selection:** before opening the next friend, both platform workers read the friends list from a screenshot. A friend pinned to the top by a remote trade awaiting a response is stepped over — that row has no gift to open and cannot receive one, so tapping it repeats forever. Of the remaining rows, a gift is only opened when the friend's row is drawn without the pale blue halo around their avatar; sending is unaffected and continues for every friend who can receive a gift, for as long as the gift bag holds any. A gift left unopened is closed first, because tapping the friend's row opens the gift full screen and the send buttons are on the profile behind it. Set `OPEN_FROM_HALOED_ROWS` in `sources/gift_android.py` or `sources/gift_ios.py` to `True` to open from the haloed rows instead. An unreadable screen, a part-scrolled list, or `--no-guard` falls back to tapping the configured `NEXT_FRIEND_BTN` point and opening whatever it finds.

```sh
python gift.py --devices all --count 1 --plan
python gift.py --devices all --count 1
python gift.py --devices all --count 1 --spend 10   # a phone on a gym feeds at most 10 berries
```

Implementation: `sources/gift_android.py` and `sources/gift_ios.py`, selected by the fleet controller; berries as below. Compatibility name: `scripts/send_gifts.py`.

## Add friends — `add_friends.py`

**Purpose:** submit trainer codes using Android or iOS screen interaction, track attempts, and avoid repeating codes already recorded in the chosen history.

**Prepare:** keep trainer codes and history in private files. Start with one code and a visible Friends/Add Friend interface. An accepted input or attempted request is not proof that a friendship was accepted by another trainer.

```sh
python add_friends.py --devices all --count 1 --codes-file "$POGO_CONFIG_DIR/trainer-codes.txt" --plan
python add_friends.py --devices all --count 1 --codes-file "$POGO_CONFIG_DIR/trainer-codes.txt"
```

Set `POGO_CONFIG_DIR` in your shell before using the path above. You can also pass `--codes-file trainer-codes.txt`; the relative path resolves under each selected computer's own private configuration directory. Absolute paths inside the coordinator's configuration directory are rebased for remote workers, but the code-list files must already exist there. Absolute paths outside that directory are forwarded unchanged; use `--local` for a file available only on this computer. No file synchronization is performed. Individual codes and a public source are also supported; consult `--help` and review the intended code list.

Implementation: the controllers and request loop in `add_friends.py`, using shared fleet selection and platform connections.

## GO Battle League — `gbl.py`

The default is five battles per selected phone. Set `--count 1` for the initial supervised run; `gbl_day.py` is a separate longer-running workflow.

**Purpose:** coordinate league entry, party selection, combat input, result screens, and rewards. Each selected phone participates in its own matchmaking battle. This differs from the paired friendly battle command.

**Prepare:** verify the league/team configuration and enter the expected GBL screen. Screen recognition, reward state, the available cup list, and game daily limits all affect progress.

On Android, season information articles are recognized before league cards. The worker closes them with Android Back and reads the screen again; league names in the article are never treated as selectable cards. Recovery is bounded if the article will not close.

```sh
python gbl.py --devices all --count 1 --plan
python gbl.py --devices all --check
python gbl.py --devices all --count 1
```

Implementation: `sources/gbl_android.py`, `sources/gbl_ios.py`, and shared vision, strategy, metadata, and home-recovery modules. `gbl_day.py` coordinates longer daily runs; begin with the bounded command above. Compatibility name: `gbl.py`.

## Trades — `trade.py`

The default count is 100 trades. An explicit pair runs on its owning computer; both devices must belong to that same computer.

**Purpose:** execute the repeated trade confirmation sequence for a supported pair attached to the same computer.

**Prepare:** identify the intended pair, complete one manual trade, and prepare the selection/search that should be reused. Check what both phones are offering before starting. Trade cost and game restrictions remain in effect.

```sh
python trade.py --pair android-one android-two --count 1 --plan
python trade.py --pair android-one android-two --count 1
```

Implementation: the paired controllers and trade loop in `sources/pokemon_fleet.py` support Android/Android, iOS/iOS, and mixed pairs when both devices are configured and calibrated. This is implementation support, not evidence that every pair type has been verified on live phones. The older `sources/trade_android.py` and `sources/trade_ios_android.py` workers remain in the codebase. Compatibility name: `trade.py`.

## Transfers — `luckytrash.py`

Select either `--count N` or `--all` explicitly. `--all` has a default limit of 500 transfers per phone through `--max-transfers`. Execution also requires `--confirm-delete`; a plan does not authorize or perform a transfer.

**Purpose:** transfer selected Pokémon using the existing LuckyTrash workers. Transfers remove Pokémon from the account; the program cannot undo that action.

**Prepare:** manually review the search/tag/selection, protected Pokémon, and transfer limit. Start with a plan and inspect the actual phone. The execution command below includes the required deletion confirmation and should only be used after that review.

```sh
python luckytrash.py --devices all --count 1 --plan
python luckytrash.py --devices all --count 1 --confirm-delete
```

Implementation: `sources/luckytrash_android.py` and `sources/luckytrash_ios.py`. Compatibility name: `luckytrash.py`. A new alias does not change the underlying selection or protection rules.

## Gym berries only — `scripts/berry.py`

`gift.py` already feeds any phone parked on a gym. This berries-only command is for `--check` (a read-only detector check) and for a run that must not fall back to gifts. Prepare the gym/defender interface and review the berry budget. Implementations are `sources/berry_android.py` and `sources/berry_ios.py`; compatibility name `scripts/feed_berries.py`.

```sh
python scripts/berry.py --devices all --spend 1 --plan
python scripts/berry.py --devices all --check
```

## Catching rewards — `catch.py`

Works whichever reward queue each phone is parked on, on Android and iPhone:

- the award card ("A Pokémon has appeared! You have N Pokémon left to catch"): START ENCOUNTER, feed a Nanab, throw, repeat until the queue empties;
- a research screen's orange CLAIM buttons: press each one and work what it pays out -- items are dismissed, Pokémon are thrown at.

An encounter already open is caught first, then the screen is read again. A screen showing none of these is left alone. `--mode award` or `--mode research` skips the read (research looks further down a list than the read does); `--dry-run` reports what it sees without touching the phone. On an iPhone the Excellent routine throws until it first refuses to lock the circle, then a plain flick for the rest of the run; there is no BACK key, so a screen that only BACK would leave is reported and left alone. `--mode encounter` catches wild Pokémon you open by hand, one after another, with the same throws and Nanab. It runs on this computer unless `--allmachines` is given.

```sh
python catch.py --dry-run
python catch.py --devices ios-one --throws 30
python catch.py --mode encounter --count 5
```

## Raid candy planning — `raidspend.py`

Answers, per account and per attacking type: what the best raid attackers are, what you can field today, which individual your candy and XL candy should go into and what it costs, which Megas this account cannot use yet, and which top attackers it does not own. Read-only: the scan never powers anything up, teaches a move, Mega Evolves or transfers.

```sh
# Scan: routed to whichever computer each phone is plugged into; phones run in parallel.
python raidspend.py --scan --devices all
python raidspend.py --scan --devices android-one --resume   # finish a cut-off run

# Plan: reads each scan back from the computer that took it.
python raidspend.py --devices all
python raidspend.py --devices android-three --json
```

A phone opts in with a `raidscan` entry under its `operations:` in the private fleet config — see [`examples/pokemon-fleet.yaml`](../examples/pokemon-fleet.yaml). It needs no calibration.

**Rankings are computed, not typed in.** `sources/raid_gamedata.py` downloads PvPoke's game master (species, forms, Megas, movepools, Elite TM moves, shadow eligibility) and PokeMiners' decoded game master (gyms-and-raids move stats, CP multipliers, power-up costs including per-species overrides, first Mega Evolution prices), caches both in private state for a day, and `sources/raid_math.py` rates every form, shadow and Mega with its best moveset of each type using the GamePress comprehensive DPS estimate, folded into (DPS³ × TDO)^¼. Ratings are shown as a percentage of the type's #1 at level 40 with perfect IVs. Megas and non-Megas are listed separately, because a party fields one Mega.

**What the scan reads.** Per species in any type's top ten same-type attackers, at 60% or more of that type's #1 — about a hundred species — (`sources/raid_meta.py`; searched by zero-padded Pokédex number, which the game matches exactly), `sources/raid_inventory.py` runs the storage filters `094`, `094&shadow`, `094&4*` and `094&3*,4*` — the `3*` band is 82–98%, so a 98% individual is in it — then opens the best two or three individuals. From each detail page it reads CP, max HP, the next power-up's stardust and candy price, the moves, and the species' candy, XL candy, Mega Energy and the account's stardust. Every search is confirmed against the text the search field shows; a search that silently did not take would otherwise leave the previous species' results on screen.

**How the plan is made** (`sources/raid_plan.py`):

- **Level and IVs are solved, not guessed.** CP and HP are exact functions of base stats, IVs and level; the power-up price pins the level to a band (and is the only on-screen sign of a shadow or purified individual), and the star filter pins the IV sum. Every combination that fits is kept, and the plan uses the weakest one, so a range is never oversold.
- **Costs use the species' own tables.** Shadow ×1.2, purified ×0.9, and per-species overrides — Eternatus costs 100–890 XL *per power-up* above level 40, which is why it is never an XL recommendation.
- **A Mega counts only if this account can use it.** The species' detail page must show a Mega Energy cell at all, and the energy must cover the first Mega Evolution. Otherwise it is listed as locked, with the reason.
- **Only same-type attackers count.** A Mega Dragonite firing Hyper Beam rates as a Normal attacker on paper; nobody builds counters that way. Normal is listed but never ranked among the best spends.
- **A spend has to buy levels.** An individual already as high as its candy allows is not a spend; one that is strong but on the wrong moves is listed as a TM to teach instead.
- **The spend goes to the individual that ends up strongest**, given how far its own candy and XL take it, not to the one with the most candy. An attacker that tops out below 55% of the type's #1 is never recommended; a moveset that needs a TM is marked.

**Rare Candy and Rare Candy XL** are the default output. They turn into any species' candy, so they only belong where the species' own pool runs out first: an attacker the account can already max on its own candy is never listed. Each target is measured against the best the account can build in that slot with no rare candy, so a target only appears if it ends up stronger. Two lists, ranked by points gained per 100 spent: Rare Candy (to L40), and Rare Candy XL (to L50). A third list gives one target per type. Targets needing more than 400 of either (Eternatus needs about 6,500 XL) are left out. `--full` adds the per-type power-up plan using each species' own candy. The bag's rare candy count is not read.

Stardust is reported, not budgeted.

### Reading a phone screen

- **Scrolling never starts on a control.** A drag begins on the WEIGHT/HEIGHT caption read off the same screenshot, after two reads agree the page has stopped sliding into place. A drag that landed on the Mega pips once opened the Mega Level page, whose LEVEL UP spends Mega Energy; that page is recognised by its title and only ever closed.
- **iOS.** The detail page's close button is found by its colour; the search field is emptied with the native text box's own "Clear text" button.

### Reading a phone screen

Navigation safety is the part worth knowing before pointing this at a live account.

- **BACK is sent at most once per step, and only guarded.** Android's BACK on the map raises "Do you want to exit Pokémon GO?", whose default action is OK. So a retreat sends one press, reads the result, and cancels the prompt if that is what came up. A cancelled prompt is also useful as a signal: the game only offers it from the root screen, so it means the handset is on the map and the menu button will work.
- **The game is never force-stopped.** Doing that tore down a live Party Play session and raised a prompt only the account owner could answer.
- **Two dialogs are cleared on standing authority** — the weekly Adventure Sync card (CLAIM REWARD!) and the Continue Party Play prompt (REJOIN PARTY). Anything else with no recognisable way out is reported, never guessed at. A dialog carrying a word about buying, spending, deleting or transferring is never auto-confirmed even when it has an OK button.
- **Nothing is typed blind.** Per-device positions are derived from the screen: the storage search field sits at 0.148 of screen height on a 720×1600 handset and 0.161 on a 1316×2560 one, so it is located from the tab row above it. The main menu's POKÉMON caption is also not the button — the game draws it above a circular icon, and tapping the caption only closes the menu.
- **Foldables need an explicit display.** A foldable reports two displays, and `screencap` with no `-d` writes a warning onto stdout ahead of the framebuffer, so every screen read fails. The id `screencap` wants is the long SurfaceFlinger unique id (`-d 0` answers "Display Id '0' is not valid"), chosen from whichever display is active so folding the handset shut follows the handover to the cover display.

Two parser details worth recording, both measured rather than assumed. A Mega-capable species lays its four stats out in a 2×2 grid where the neighbouring cell is Mega Energy, so an XL candy count read carelessly becomes a few hundred phantom levels of headroom; and the floating close button truncates the XL label to `CANDY X`. And CP on the detail page is drawn under the Pokémon's sprite — on a Zacian the sprite covers the leading digit outright — so CP is read from the grid, where it prints above the sprite.

## Additional tools

- `scripts/fleet.py status`: read-only connectivity and configuration status.
- `scripts/fleet.py owners`: read-only survey of every configured computer's USB buses, showing which one each phone is attached to, flagging any whose `machine` field disagrees, and naming a host whose probe did not answer. Run it after moving a cable; routing already follows what it reports.
- `scripts/battle.py`: paired friendly battles, with its own supported platforms and visual calibration.
- `scripts/excellent_throw.py`: the raw excellent-throw watcher, kept for its `--check` and `--probe-ring` calibration; day-to-day catching is `catch.py --mode encounter`.
- `remove_friends.py`: removes Android friends who have sent no gift, carry no blue halo and have at most `--hearts` hearts (default 0, up to 3). Run it with the phone on the FRIENDS list sorted by friendship level, lowest first. Without `--confirm-remove` it only lists who would go; with it, each removal is checked against the name in the unfriend dialog, and the removed names are written to the private state directory.
- `scripts/gbl_evaluator.py`: inspect GBL strategy/evaluation inputs and outputs.
- `scripts/inventory_scanner_ios.py`: inspect iOS inventory; store account-derived output privately.
- `recover_home.py`: recovery utility where provided; review `--help` before allowing screen changes.

A workflow plan uses configured devices and does not probe attached phones. At execution time, `--devices all` can additionally discover unregistered Android phones. Run live fleet status to inspect attached hardware, or name devices explicitly when the run should be limited to a particular set.

## Stop and verify

Use Ctrl+C to interrupt a supervised run. Check every participating computer for leftover workers and confirm the phones stopped receiving input. Review the visible outcome separately from the command's exit status. Read [Testing](testing.md) before claiming an action completed.
