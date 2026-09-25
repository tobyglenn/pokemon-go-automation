# Working on Pokémon GO Automation

Read docs/README.md and docs/architecture.md before changing a workflow. Public entry points should describe the user action; reusable Android/iOS workers live in sources/.

## Privacy

All actual device, machine, trainer, signing, and account data belongs in private configuration outside this repository. Never copy an operator's working folder wholesale. Do not commit logs, screenshots, friend histories, inventory exports, backups, SDK binaries, or local environments. Keep templates fictional and run the public-tree privacy check before preparing a commit or release.

## Fleet behavior

Public workflows route across all configured computers and their connected devices by default. Preserve an explicit local-only option and an internal worker marker to prevent recursive delegation. Direct worker modules must follow the supported fleet-routing contract. Validate pair locality before either device receives a trade or friendly-battle action.

## Verification

Unit tests and mocks do not establish physical device operation. Check live fleet status on every configured computer, inspect existing workers, and execute the actual command's plan. Use specific process identities when stopping confirmed leaks. Do not stop unrelated active runs.

Keep code validation, plans, connected hardware, and completed game actions distinct in reports. An offline device is unverified. Supervised game-changing checks require authorization and a deliberately prepared selection; never use transfers or trades as an incidental refactor smoke test.

## Collaboration

Assign bounded tasks with clear file ownership. One coordinator owns phone access and release decisions. Share changes to CLI flags, configuration schema, and filenames with documentation and website maintainers. Preserve upstream attribution and compatibility wrappers unless a deliberate migration is documented.
