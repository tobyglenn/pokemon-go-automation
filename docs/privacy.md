# Privacy and public releases

The goal is separation, not masking a few strings. A public checkout must never require the original operator's private fleet to explain or run its code.

## Never commit

- Device serials, UDIDs, wireless device addresses, private hostnames, or SSH destinations.
- Personal Appium profiles, signing identifiers, absolute home paths, or completed calibration files.
- Trainer codes, account names, friend histories, inventory exports, screenshots, OCR output, and run logs.
- Environment files, credentials, keys, device backups, virtual environments, SDK binaries, or debug archives.

Store configuration under `POGO_CONFIG_DIR` outside the checkout. Keep runtime data in a separate private directory. Generic examples should use clearly fictional aliases and empty/placeholding values.

## Review before a public push

1. Inspect `git status --short` and every proposed tracked file.
2. Run `python scripts/check_public_tree.py` using the options shown by its `--help` output.
3. Review staged content, commit history, and packaged distribution contents. An ignore file does not remove data already committed.
4. Search for known private identifiers using an external denylist. Do not commit that denylist: its contents are themselves private.
5. Review documentation, test fixtures, comments, image assets, and filenames as carefully as Python configuration.
6. Check author metadata and release attachments before publication.

A scanner is a review aid, not a guarantee. Use both generic pattern checks and an independent review against the private source inventory.

Before the first staging operation, use `python scripts/check_public_tree.py --working-tree`. This strict mode includes ignored files and will flag local environments, build output, and bytecode caches. Use the staged mode for the final release review. For a release, stage the intended files and run `python scripts/check_public_tree.py`; the default checks staged content and every reachable commit, including commit messages. `--deny-file /path/outside/checkout/private-identifiers.json` adds a JSON list of private literals without embedding them in the repository. Keep that file private and outside the checkout.

## Reporting a problem

Do not paste an exposed credential or personal identifier into a public issue. Privately notify the repository maintainer with the affected path and type of exposure. Remove the sensitive value from the public surface and rotate any affected credential; deleting the latest file alone does not erase Git history or existing copies.
