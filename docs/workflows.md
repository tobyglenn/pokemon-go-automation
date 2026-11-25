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

## Start the day — `starter.py`

During a live run, each phone switches its display off as soon as its final scheduled work finishes (pass `--no-sleep` to leave displays on). The summary includes the observed display state. Plans and dry runs leave the displays alone.

When multiple computers are configured and `special` is scheduled (default), `starter.py` finishes the pre-special steps (`buddy`, `battle`) across every computer first without sleeping their displays, runs `special_trade.py` once from the coordinator across all configured computers over SSH (so the receiver can trade with partner phones plugged into peer computers without device-lock conflicts), and then runs the remaining steps (`mega`, `bag`, `gifts`, `gbl`, `buddy-again`) across every computer. With `--local`, `special` stays restricted to phones attached to the current computer.

On iPhone, starter keeps one authorized Appium session through buddy work, trade inventory, gifts, battle legs and display sleep. Approve iOS automation when the first session opens. If the phone disconnects or iOS revokes authorization, reconnecting can require approval again.

Start with every phone on the map. The default order is:

1. **buddy** gives up to three berries, takes one snapshot, and pets the buddy. The routine checks the buddy profile for the corresponding hearts.
2. **battle** attempts a Blanche training battle for the buddy's Battle Together heart.
3. **special** checks for trades using the private special-trade configuration. Use `--skip special` to omit this step.
4. **mega** checks the `mega` tag and evolves Pokémon whose button shows the Mega Energy icon without a cost. Each evolution replaces the previous one.
5. **bag** discards all held Potions, Super Potions, Hyper Potions, Revives, Razz Berries, Nanab Berries, and Pinap Berries. Use `--skip bag` to leave item counts alone.
6. **gifts** opens the friends list and runs `gift.py --all --gifts-only` for that phone.
7. **gbl** returns to the map and runs `gbl_day.py`. A recorded daily cap skips this step on that phone until midnight. `--replay-gbl` runs it anyway.
8. **buddy-again** revisits berry feeding, playing, and the snapshot.

Add `--trade` to schedule the ordinary trade step between gifts and GBL.

Buttons are found by their words (Vision OCR) and colours, not per-phone coordinates. A step that cannot reach its screen is reported and the phone moves on to the next one. `--dry-run` walks the mega and buddy screens and reports, but evolves, feeds, photographs and pets nothing (gifts and GBL are skipped); `--plan` only lists phones and steps; `--skip`/`--only` choose steps; `--tag` names another tag.

```sh
python starter.py --plan
python starter.py --devices android-one --dry-run --only mega buddy
python starter.py
```

## Daily special trade — `special_trade.py`

One phone (the *receiver*) fills its shiny gaps by special trade. The private settings file `special-trade.json` names the receiver, its shiny gap report, the fleet phone that now holds each account the report names, and the spare legendaries the receiver gives:

```json
{"receiver": "android-three", "report": "~/.local/state/pokemon-go-automation/devices/gap_report.md",
 "holders": {"PhoneA": "android-one", "PhoneB": "android-two"},
 "give": ["zamazenta", "zacian", "necrozma", "mewtwo", "kyurem", "rayquaza"]}
```

Only base forms held by another phone are traded for; an evolved form is never a target, because its shiny comes from evolving the lower stage. By default, partner phones attached to **any** configured computer can participate over SSH via `sources/remote_target.py`; pass `--local` to restrict to devices plugged into the current computer, `--all` to run all remaining special trades allowed today (during multi-special events) across all partners until zero remain, or `--ignore-today` to check the receiver live instead of trusting `today.json`. The partner gives a shiny of the species, found by searching its Pokédex number (excluding `!dontrade`, `!shadow`, and `!traded`). The receiver gives its lowest-CP spare legendary that is not tagged, `dontrade`, favorited, costume, purified, buddy, shiny, lucky, 4★, shadow, or already traded. Only once none of those is left does it give from its `ToTrade` tag (setting `spare_tag`), lowest CP first, with the same never-give filters (`!dontrade`, `!favorite`, `!costume`, `!purified`, `!buddy1-5`, `!shiny`, `!lucky`, `!4*`, `!shadow`, `!traded`), and explicitly refuses Armored Mewtwo (even though `!costume` does not filter it) or any `dontrade`-tagged card before tapping `NEXT`. The search is narrowed by CP until the lowest one shows in the top rows. The game's "Do you want to make a Special Trade?" card is answered YES. A trade costing more than `--max-stardust` (default 40,000) is refused (skipping to the next partner/species rather than aborting other phones). Each completed trade removes its species from the report and is logged to `special-trade/traded.jsonl` in the state directory. The run stops when the receiver has no special trades left. When no attached partner has anything left to give, a macOS notification names the phone to plug in next.

```sh
python special_trade.py --list    # the report's targets; touches no phone
python special_trade.py --plan    # count/roster on the phones, trade nothing
python special_trade.py --all     # run all remaining special trades today across all computers
python special_trade.py
```

`scripts/tag_spare_legendaries.py` fills those tags on the receiver. A legendary with two stats at 15 (not a hundo) gets `SilverBottlePot` (the game caps tag names at 15 letters). An untagged 3★ whose Attack is 13 or below (excluding `!dontrade`, `!favorite`, `!costume`, `!purified`, `!buddy1-5`, Armored Mewtwo, shiny, lucky, 4★, shadow, traded) gets `ToTrade`. Search cannot tell Attack 14 from 11–13, so each candidate is appraised and its Attack bar measured; borderline readings are left alone. Each decision is logged to `special-trade/iv-tags.jsonl`. `--dry-run` appraises and tags nothing.

To maintain a Living Dex on a single designated phone without affecting other devices, `scripts/tag_living_dex.py --device <name>` scans Pokédex numbers `1–1025` adaptively and tags every sole-copy species (`count == 1`) with `dontrade`:

```sh
python scripts/tag_living_dex.py --device android-three --dry-run
python scripts/tag_living_dex.py --device android-three
```

```sh
python scripts/tag_spare_legendaries.py --dry-run --limit 5
python scripts/tag_spare_legendaries.py --device android-three
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

## Daily high-value trading — `daily_trade.py`

Runs up to `--trades` (default `100`) trades per day on each computer's attached pair of configured phones (`sources/daily_trade.py`, `sources/trade_plan.py`, `sources/starter_trade.py`), using the ~93 expanded raid/Mega/Shadow-line families (`--core-only` restricts to the 45 core families):

1. **Tier 1 (`98Plus == 0`)**: families either phone has zero `98Plus` copies of. Few-copies species (`< 3` spares) are logged as Lucky Trade suggestions rather than auto-traded.
2. **Tier 2 (`0 < 98Plus < --target-good`, default `6`)**: deepens `98Plus` coverage, scheduling mirror trades within the family first.
3. **Tier 3**: fills the remaining daily budget up to 100 with high-value mirror and cross-family IV/XL rerolls.

Completed trades are recorded per day under `state_dir()/trade/history-YYYY-MM-DD.jsonl` so interrupted runs resume cleanly within the daily cap.

```sh
python daily_trade.py --plan
python daily_trade.py --dry-run
python daily_trade.py --reuse-counts
```

## Evening routine — `ender.py`

Run `python ender.py` on the fleet coordinator. Both hosts do buddy and gift work, then one coordinator pairs the connected trainers. Each two-sided completion in the daily journals consumes one of each account’s 100 trades. The receiver and its alternate handset share a counter. An in-game daily-limit notice also closes the affected pair for that date.

Trading uses the same engine as [`priority_trade.py`](#receiver-first-daily-trades--priority_tradepy): the receiver takes donors' XXL/XXS, ReceiverLucky and ReceiverPerfect stock first, then the donors trade their valuable spares and Far/Local lists with each other until as many accounts as possible reach 100. Every pick opens the handset's current pinned favorite, so the evening never depends on an inventory snapshot from an earlier day. Guaranteed Lucky trades are skipped, never spent; request one with `collect_trades.py --propose-guaranteed`.

After trading, Ender opens the pinned `SafeTrash` favorite (checked against the live storage policy) for manual review, records its result grid and switches the display off. You decide which Pokémon to transfer. `python ender.py --plan` describes this routine offline.

### Saved storage searches

Run `python storage_favorites.py` on the coordinator to reconcile favorite searches. Use `--plan` to list each account's searches. The five shared searches are **98%**, **FarTrade**, **FarNot98**, **LocalTrade** and **LocalNot98**. The 98% search includes candidates with two perfect stats. Appraise the third stat before applying 98Plus. All outgoing and disposal searches use the game’s native `!4*` filter. The private `storage-favorites.json` holds the shared definitions and donor extras.

The three donors use all twelve slots. The remaining seven are SafeTrash, Non-Raid, ReceiverLucky, ReceiverPerfect, XXL/XXS, LuckyOffer and ToPower. The receiver uses the five common searches, its own SafeTrash, Non-Raid and ToPower, and its TradeEvolve, NearLowIV, PlainNot98 and TradeReturns searches. A sync backs up and removes retired searches first (TradedLucky and HundoReview on donors, LocalSpare on the receiver) so the new ones fit under the cap. `trading-accounts.json` maps alternate handsets to one trainer for inventory policy, trade pairing and daily limits. To sync favorites on a specific handset, pass its physical device name to `--devices`.

The configurable defaults for receiver-specific favorites are `ReceiverLucky`, `ReceiverPerfect`, and `ReceiverPriority`. To keep existing in-game labels, copy [the example](../examples/trade-favorites.example.json) to `trade-favorites.json` in your private configuration and put those exact labels under `labels`. Use `aliases` to recognize previous labels during synchronization.

Every list except 98% and ToPower carries `!#ToPower`, so nothing tagged for investment can be traded or transferred from a saved search.

| Favorite | Pokémon shown for manual review |
| --- | --- |
| SafeTrash | The transfer list: garbage only. Garbage is a species with no raid, Mega, Max Battle or notable PvP use (Nymble, Bidoof); a Scorbunny or a Magikarp never qualifies. Any IV short of 98% counts, because trading rerolls IVs; native 4★, the 98Plus tag and two-perfect-stat spreads (possible unappraised 98s) stay out, as do tagged, favorite, Lucky, shiny, Legendary, shadow, size, costume, background, buddy and vintage Pokémon. An untraded donor copy must have been caught within 100 km and have a verified registration in the receiver’s Lucky Pokédex. Priority targets stay out. On the receiver a verified retained copy is required and the return pools stay out. |
| Non-Raid | A wider second look, not a transfer list: Pokémon up to 3★ (two-perfect-stat spreads excluded) of species with no raid or Max Battle use. Check each before transferring. On the receiver it requires a kept representative. |
| ToPower | The Pokémon to put candy into: the account’s Rare Candy and Rare Candy XL targets and each type’s best own-candy spend from its latest `raidspend.py` scan, written as `#ToPower,<dex>&#ToPower,<cp>`, so anything you tag ToPower appears too. An individual leaves the list once a power-up changes its CP; tag it ToPower to keep it. Rerun the scan and the sync to refresh. |
| LuckyOffer | Eligible Legendary spares for an individually approved Lucky Friends trade. Setup checks the raid shortlist against each donor’s saved search. It verifies eligible individuals by species, form and CP, then records their positions and the result count in each partner’s actual picker. Ender checks that picker receipt before selecting an offer. |
| ReceiverLucky, ReceiverPerfect | Tradeable donor candidates whose exact species is missing from the receiver’s in-game Lucky or Perfect Pokédex. A missing evolution qualifies only that evolution. Trading rerolls the IVs. Priority species remain protected by the trash filters and routed by the trader. |
| XXL/XXS | Untraded XXL and XXS donor Pokémon, which can still be traded to the receiver. |
| FarTrade, FarNot98 | Untraded Pokémon caught more than 100 km away: trading one earns bonus candy. FarTrade is 0–2★; FarNot98 is any IV short of 98%. |
| LocalTrade, LocalNot98 | Untraded Pokémon caught within 100 km. LocalTrade is the trade list: useful species only (never garbage), any IV short of 98%, two-perfect-stat spreads excluded. LocalNot98 is the wider original: every species short of 98%. On donors both also leave out every ReceiverLucky species, so those go only to the receiver. On the receiver LocalTrade also needs a verified retained copy, so it stays empty until `--keep-one` has covered useful species. |

Living Dex preparation runs explicitly on the receiver. `python storage_favorites.py --keep-one all` checks the catalog and saves progress between runs. An existing `KeepOne` specimen stays protected. The native `98Plus` tag or a verified hundo can provide the retained copy for the audited form. Receiver outgoing lists require verified coverage and protect costumes, backgrounds and unresolved forms. Donor trade and review searches allow eligible costumes and last copies according to their individual purposes. The receiver’s `TradeReturns` favorite supplies its side of an ordinary trade.

## Receiver-first daily trades — `priority_trade.py`

```bash
python priority_trade.py --plan
python priority_trade.py
```

**Purpose:** bring as many accounts as possible to the 100-trade daily cap, filling the receiver's needs first.

The receiver trades with the donors first. Across all donors the donor offers its **XXL/XXS** search, then **ReceiverLucky**, then **ReceiverPerfect**; a lower tier never takes a receiver slot while a higher tier still has stock on any donor. The receiver sends curated return fodder and keeps its `trade_keep_last` reserve. The receiver's lane and a donor-to-donor lane run at the same time, so all four phones trade at once; the lanes swap phones only between trades. Donors trade with each other from their **valuable spares** first (the collector's daily-value lists: pseudo-legendary families and curated valuable species, at least two copies kept, ReceiverLucky species removed), then **FarTrade**, **FarNot98**, **LocalNot98**, and **LocalTrade** last. Each pinned list is opened as its verified favorite rather than typed, so long searches are not garbled by keyboard input; an unverified favorite falls back to typing. A pair that fails for a passing reason rests 10 minutes and is retried. ReceiverLucky species never appear in a donor's LocalTrade or LocalNot98 (see storage favorites below), so they only ever go to the receiver.

A budget planner chooses which donor feeds the receiver so the donors can still finish their own caps with each other. With every account at 0 trades, one donor gives the receiver its 100 and the other two trade 100 with each other, so all four can cap. Three is the success threshold; real stock decides. The plan is recomputed after every trade and whenever a search runs dry, and it stays with the open pair when that costs nothing. Today's counts come from both Macs' trade journals. Guaranteed Lucky trades are skipped, never spent. `--plan` reads counts and favorite terms only and touches no phone. Options: `--local`, `--trades N`, `--receiver`, `--exclude-devices`, `--shots`. Exit status is 0 when at least three attached accounts (fewer if phones are offline) reach the cap.

## Lucky Dex and daily fleet trades — `collect_trades.py`

Use `--finish-night` to leave each phone on its filtered storage list for manual review and switch its screen off. Set `"finish_night": true` in the private collector configuration for full daily runs. The collector opens the same `SafeTrash` filter as Ender. You choose any transfers yourself. Temporary trade-picker queries can reuse a matching Recent search after verifying the full text.

The default command reads the designated receiver’s private Lucky Dex report (`lucky_report` in the referenced `clear-pokemon.json`). Every selected donor offers protected, tradeable matches for that report. The receiver offers pseudo-legendary families first, then other curated valuable species. During collection, free peer pairs can exchange valuable spares from outside the pending Lucky Dex list. Each such phone keeps its entire share of the receiver’s remaining daily budget available as a potential donor. When the receiver’s budget or the available Lucky Dex matches run out, eligible pairs throughout the fleet exchange valuable spares until each phone reaches its daily cap or runs out of eligible partners or offers. Phones on different computers can pair through the SSH device bridge.

The coordinator checks every configured computer’s trade history and charges each verified exchange to both participants. Collector history includes the offered Pokédex number for each phone and the result screenshots and Lucky status captured for the exchange. The default cap is 100 per phone. It reports each shortfall and returns an unsuccessful exit status if a selected phone finishes below the requested daily cap. A Lucky roll is a separate outcome. A confirmed Lucky receipt paired with the donor’s exact Dex-number offer retires that species from the remaining audited targets for this receiver and report.

Searches retain the collector’s donor and return exclusions. Ordinary offers on both phones require an exact Pokédex-number search with at least three eligible copies and more than the phone’s configured `trade_keep_last` reserve. Nicknames can help order the searches. Every guaranteed Lucky pair is held for specific approval, including during PH1 gap collection. When ordinary collection encounters a pair listed in `guaranteed_proposal_pairs` outside the designated receiver, it releases the pair’s locks and prepares one ranked proposal for those two phones. A terminal can accept or decline it by ID. Unattended collection parks the proposal and continues eligible ordinary trades. Ordinary donor and return searches exclude the configured vintage years. The existing configured-target workflows remain available with `--targets-only`, `--rolycoly-only`, and `--size-only`.

```sh
python collect_trades.py --plan
python collect_trades.py --dry-run
python collect_trades.py
python collect_trades.py --size-only --trades 20
```

### Reviewing and accepting a guaranteed Lucky trade

Before a live Legendary inventory scan, both phones must have room under the ordinary daily cap, show the Lucky Friend badge on the other trainer’s profile, enter the trade picker and show a positive Special Trade allowance. An unreadable check skips the pair. Eligible pairs reuse fresh cached recommendations. When evidence needs updating, previously discovered candidate species are checked first, with a wider search if needed. An approved exchange expires the two traded species on both accounts.

A separate proposal compares live-verified legendary raid attackers in both directions. It considers what the receiving account owns, including its perfect and powered-up attackers. Configure the ordered phone pairs in `guaranteed_proposal_pairs` in the private collector JSON. Running with a named pair restricts the review to those two phones.

```sh
python collect_trades.py --propose-guaranteed --plan
python collect_trades.py --propose-guaranteed
python collect_trades.py --propose-guaranteed android-one android-two
python collect_trades.py --approve-guaranteed PROPOSAL_ID
python collect_trades.py --decline-guaranteed PROPOSAL_ID
```

The plan ranks offers from the inventory snapshot. When pairs share a phone, it chooses distinct outgoing individuals and ranks the later exchange against ownership projected after the earlier one. If verified stock cannot support both exchanges, the plan and live review label them as mutually exclusive alternatives. A single named pair gets its best independent match. Live proposal runs refresh missing or stale evidence on just the named phones. Approval and decline use the original proposal and bypass fleet inventory ranking. A live proposal verifies both Lucky Friends profiles, each eligible individual, each remaining Special Trade allowance, and the Stardust quote. It prints a one-use ID and closes the lobby. Multiple pairs can share one phone. Reviewing their quotes leaves that phone’s allowance available. Accepting one exchange can defer another to a later day.

Accepting an ID reopens only that pair and checks the species, form, current CP, identity from the selection screen, signed-in trainers, guarantee and cost against the original quote. The command ends after that exchange. A changed offer needs a fresh proposal. Unused proposals expire at local midnight.

## Transfers — `luckytrash.py`

Select either `--count N` or `--all` explicitly. `--all` has a default limit of 500 transfers per phone through `--max-transfers`. Execution also requires `--confirm-delete`; a plan does not authorize or perform a transfer.

**Purpose:** transfer selected Pokémon using the existing LuckyTrash workers. Transfers remove Pokémon from the account; the program cannot undo that action.

**Prepare:** manually review the search/tag/selection, protected Pokémon, and transfer limit. Start with a plan and inspect the actual phone. The execution command below includes the required deletion confirmation and should only be used after that review.

```sh
python luckytrash.py --devices all --count 1 --plan
python luckytrash.py --devices all --count 1 --confirm-delete
```

Implementation: `sources/luckytrash_android.py` and `sources/luckytrash_ios.py`. Compatibility name: `luckytrash.py`. A new alias does not change the underlying selection or protection rules.

## Safe audited clearing — `clear_pokemon.py`

Reads `clear-pokemon.json` from the private configuration directory (see `examples/clear-pokemon.example.json`), audits the dex list against Lucky Dex gaps, evolution chains, Mega evolutions, and raid attackers, enforces all safety exclusions (`!#`, `!98Plus`, `!dontrade`, `!shiny`, `!4*`, `!favorite`, `!lucky`, `!legendary`, `!mythical`, `!ultra beasts`, `!costume`, `!shadow`, `!dynamax`, `!gigantamax`, `!background`, `!xxs`, `!xxl`), and runs the full audited filter in a single verified storage query across every connected Android and iOS device.

Every transfer verifies the active search filter, the detail page's species name and lack of protected tags/Mega/Lucky/XXS/XXL state, the exact `TRANSFER` menu label, the confirmation dialog wording, absence of any second confirmation dialog, and a drop in the storage result count. `--open` leaves the verified filtered list open on screen on every device for visual review without deleting anything; `--count` and `--all` require `--confirm-delete`.

```sh
python clear_pokemon.py --review
python clear_pokemon.py --plan
python clear_pokemon.py --dry-run
python clear_pokemon.py --open
python clear_pokemon.py --count 5 --confirm-delete
```

## Gym berries only — `scripts/berry.py`

`gift.py` already feeds any phone parked on a gym. This berries-only command is for `--check` (a read-only detector check) and for a run that must not fall back to gifts. Prepare the gym/defender interface and review the berry budget. Implementations are `sources/berry_android.py` and `sources/berry_ios.py`; compatibility name `scripts/feed_berries.py`.

```sh
python scripts/berry.py --devices all --spend 1 --plan
python scripts/berry.py --devices all --check
```

## Catching rewards — `catch.py`

Works whichever reward queue each phone is parked on, on Android and iPhone:

- the award card ("A Pokémon has appeared! You have N Pokémon left to catch"): START ENCOUNTER, feed a Nanab, throw, repeat until the queue empties;
- a GO Pass / Event Pass screen (BASIC and DELUXE columns): press CLAIM ALL once if unclaimed item tiles are showing, flip the top-right toggle to *Showing Claimable Rewards*, then tap each orange `POKÉMON APPEARS!` tile (left and right columns) and catch what appears (feeding a Pinap berry by default when in stock, falling back to Nanab, or Silver Pinap + Ultra Ball for legendaries; override with `--berry` or `--no-nanab`);
- a research screen's orange CLAIM buttons: press each one and work what it pays out -- items are dismissed, Pokémon are thrown at.

An encounter already open is caught first, then the screen is read again. A screen showing none of these is left alone. `--mode award`, `--mode pass` or `--mode research` skips the read (research looks further down a list than the read does); `--dry-run` reports what it sees without touching the phone. On an iPhone the Excellent routine throws until it first refuses to lock the circle, then a plain flick for the rest of the run; there is no BACK key, so a screen that only BACK would leave is reported and left alone. `--mode encounter` catches wild Pokémon you open by hand, one after another, with the same throws and Nanab. It runs on this computer unless `--allmachines` is given.

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

## Pokémon GO Plus reconnect — `starter.py --only goplus`

`starter.py --only goplus` checks the attached Android phones and iPhones on every configured computer. It leaves live connections alone. On a disconnected phone it returns Pokémon GO to the map, taps the Go Plus icon, confirms the accessory pairing prompt if it appears, and verifies the reconnect. Android requires a live Bluetooth connection in the game's client table and an accessory bond. iPhone verification uses the green Go Plus indicator on the game map. The step never runs as part of a normal start of day; schedule it on its own.

`ender.py --goplus-only` runs the same reconnect, for an hourly scheduler such as a LaunchAgent. It uses the fleet's device locks and leaves the game on the map. The accessory must be in range and available to connect. If the game asks for the accessory's own button, press it on the accessory.

```sh
python ender.py --goplus-only --plan
python starter.py --only goplus --plan
python starter.py --only goplus --devices sample-android-a sample-android-b --goplus-timeout 90
python goplus.py --plan
```

## Stop and verify

Use Ctrl+C to interrupt a supervised run. Check every participating computer for leftover workers and confirm the phones stopped receiving input. Review the visible outcome separately from the command's exit status. Read [Testing](testing.md) before claiming an action completed.
