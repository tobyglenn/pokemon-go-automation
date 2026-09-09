# Handoff to another coding assistant

Read `AGENTS.md`, the README, the workflow guide, and configuration guide before changing code. The checkout is intentionally independent of the original operator's devices. Never reconstruct private values by guessing or searching unrelated personal files.

## Recreate an installation

1. Install the supported Python dependencies and external host tools.
2. Read the generic examples and create a private `POGO_CONFIG_DIR` outside the repository.
3. Ask the operator for missing device and machine configuration, or inspect their expressly authorized attached hardware. Keep those values out of commits and public reports.
4. Calibrate the specific phones and expected game screens.
5. Run fleet status on every configured computer and resolve connection errors.
6. Run workflow plans and inspect the selected devices and pairing.
7. With authorization, perform one supervised action and verify it on the physical phones.

## Suggested implementation prompt

> Work in this Pokémon GO Automation checkout. Read AGENTS.md and docs/architecture.md. Trace the requested workflow from its public entry point through fleet dispatch to its platform worker. Keep personal configuration and runtime artifacts outside Git. Preserve configured multi-computer routing and worker recursion guards. Implement the requested change, test its logic, check live device readiness, and use a plan before any game-changing action. Report code validation, hardware connectivity, and completed actions separately. Run the public-tree privacy check before preparing a commit.

## Divide work among agents

Give one agent ownership of implementation, another ownership of documentation/integration, and another an independent privacy and regression review. Use separate files or isolated worktrees. Share the public CLI and config contract early so documentation matches the code. One coordinator owns live hardware testing; competing agents must not drive the same phone.

## What is deliberately absent

The public repository does not include real serials, UDIDs, signing-team values, SSH destinations, trainer-code lists, device backups, captured inventories, screenshots, or the original operator's run history. A checkout can explain and recreate the system, but it cannot connect to someone else's private fleet without their configuration and access.
