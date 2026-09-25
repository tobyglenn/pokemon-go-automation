# Configuration

## Public code, private installation

Keep these two areas separate:

```text
pokemon-go-automation/                 # Git checkout
  examples/                           # generic templates
  sources/                            # reusable implementations
  docs/                               # instructions

~/.config/pokemon-go-automation/       # private, outside Git
  pokemon-fleet.yaml                  # your computers and device aliases
  ...                                # your Appium and calibration profiles
```

`POGO_CONFIG_DIR` overrides the default private configuration directory. Relative configuration references should point to other files in that directory. Prefer portable relative references over embedding one operator's home directory in code.

## Fleet inventory

`pokemon-fleet.yaml` describes the configured devices and their workflow-specific profiles. Start from the shipped example and preserve its schema. Each alias should identify one intended device. Supply Android serials or iOS UDIDs privately; these are not interchangeable with an arbitrary human-readable alias.

Android discovery supplements the registry: `--devices all` can add connected phones whose serials have no configured record. A disabled record is respected for its own matching device, but disabled placeholder records do not block discovery of other phones. Use explicit device selection when only a particular phone should participate. Workflow workers verify required on-device calibration when connecting, and paired operations still require both selected devices on one computer. Plans use configured devices without probing phones; extra unregistered Android phones can appear only when execution performs discovery.

Keep device-specific facts out of Python source: serials, UDIDs, trainer names and codes, machine hostnames, SSH destinations, Appium signing settings, and personal filesystem paths. The project should work for another operator by changing configuration, not by editing the controller.

## Multiple computers

Set `POGO_MACHINE` to the current computer's configured machine name, or provide exactly one matching `hostnames` entry (or `local: true`). The machine names are configuration aliases; they are not hardcoded hardware models.

A device profile's `machine` field is a fallback, not the routing decision. Before delegating an explicitly named device, the coordinator enumerates its own USB buses: a phone attached here runs here, whatever the field says. A phone this computer demonstrably does not have goes to the only other configured computer, or, with more than two, to the one whose probe reports it; peers are probed only when that is genuinely ambiguous, so a two-computer fleet never wakes a sleeping host to route. The `machine` field decides only what a probe could not: a bus that failed to answer, an unplugged phone, or discovery turned off with `POGO_DEVICE_DISCOVERY=off`. A device with neither an answer nor a `machine` field is an error naming both. `fleet.py owners` prints the whole picture and marks every field that disagrees with a cable. Trade and battle pairs are judged by where the phones actually are, so two phones on one computer pair even if their fields say otherwise.

The optional `machines` map records each computer's local hostnames or SSH destination, its checkout directory, Python executable, and private configuration directory. The coordinator uses this information to route work. All configured computers participate by default; `--local` limits a command to the current computer. Worker mode prevents recursive delegation.

`examples/machines.example.yaml` is a fragment to merge into each private fleet file; `pogo init-config` does not install it automatically. A remote machine entry needs `ssh_host` and `project_dir`; `python` defaults to `.venv/bin/python`, and `config_dir` defaults to `~/.config/pokemon-go-automation`. Set private SSH aliases and locations there.

A machine may also set `path_prefix`, a list of directories to prepend to its inherited `PATH`. For example, `path_prefix: ["~/Library/Android/sdk/platform-tools"]` makes an Android SDK installation available without editing shell startup files. Home-directory references expand on the computer running that worker.

For `add_friends.py`, relative `--codes-file` paths resolve under each computer's `POGO_CONFIG_DIR`; relative `--history` paths resolve under its `POGO_STATE_DIR`. Absolute code-list paths inside the coordinator's configuration directory are rebased to the worker's configuration directory, and history paths inside the coordinator's state directory are rebased to the worker's state directory. Place the required private files on each computer yourself: the launcher does not synchronize files. Absolute paths outside those private roots, and options such as `--trace` and `--output`, are forwarded unchanged. They must be accessible on each selected computer, or use `--local` for a file that exists only on the current computer.

Install the same code and dependencies on each computer. Place each computer's private files on that computer. A code deployment must not overwrite private configuration or copy it into Git. Review the plan before starting a distributed run.

Trade pairs must be attached to the same computer. A network connection between computers does not make two phones a valid physical trade pair for this implementation.

## Calibration

Android profiles contain measured button positions and workflow settings. Several legacy Android workers read `AutoTraderConfig.yaml` directly from the phone. iOS workflows use their Appium and action-specific profiles. Review the example file and worker that consume a setting before changing it.

Coordinates belong to a screen layout. Copying coordinates from another phone is only a starting point, even when the models appear similar. Confirm the actual display size, in-game screen, button position, and detection result.

## Private runtime data

Screenshots, traces, friend-request histories, inventories, OCR results, device discovery output, and logs can contain account or device information. Keep them outside Git, and use a private output directory when a command offers an output or trace option. Review `git status --short` before each commit, including after debugging.

See [Privacy](privacy.md) for the release check and [Workflows](workflows.md) for each profile's role.

`POGO_STATE_DIR` sets the private runtime directory and defaults to `~/.local/state/pokemon-go-automation`. Keep histories, screenshots, inventories, and other runtime output there instead of the checkout.
