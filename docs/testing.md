# Testing and live verification

## Four distinct results

| Result | What it establishes | What it does not establish |
| --- | --- | --- |
| Unit/configuration checks | Logic, validation, dispatch, and regression behavior under the tested inputs. | That a real phone is connected or working. |
| A plan | Configuration and command construction for configured devices, without probing phones. | Live connectivity, newly discovered unregistered Android phones, or an accepted game action. |
| Live status/check | The observed device/connection or screen state at that time. | Completion of a gift, battle, trade, or transfer. |
| Supervised action verification | The observed outcome on the participating physical phones. | That every platform and future game UI will behave identically. |

## Code validation

From the repository checkout:

```sh
python -m pip install -e '.[ios,dev]'
export POGO_CONFIG_DIR="$(mktemp -d)"
export POGO_STATE_DIR="$(mktemp -d)"
python -m unittest discover -s tests -t .
python -m build
python scripts/check_public_tree.py
```

The temporary directories isolate tests from your real fleet configuration and runtime state. Run these commands in a separate shell from live automation. The GitHub Actions workflow uses fresh directories under the runner's temporary directory, runs the same regression suite and package build, and scans tracked files plus reachable history before installing dependencies. These checks use synthetic test data and do not establish live phone behavior.

Use the repository's test and CI commands with the required optional platform dependencies installed. Tests using fake devices or generated frames are useful for regression coverage, but describe them as such.

Before committing, run the public-tree scan documented in [Privacy](privacy.md). Inspect package contents as well as Git's tracked files so a built wheel or source archive cannot accidentally include local configuration.

## Hardware verification

1. Check fleet status on every configured computer.
2. Record connected, disconnected, and uncalibrated devices privately.
3. Inspect active worker processes. Stop confirmed leaked workers by their specific process identities; do not indiscriminately terminate another active run.
4. Execute the actual public command with `--plan` and verify machine/device coverage.
5. Use a read-only screen check where the workflow supports it.
6. For authorized game-changing tests, prepare the phone, use a small count, observe the result, and check cleanup on every computer.

Transfers are destructive and trades change account inventory. Refactoring authorization is not a reason to perform either action as a casual smoke test. Their plans, dispatch, guards, and tests can be checked without removing or exchanging Pokémon.

## Private validation record

Keep a local record of the tested revision, date, host environment, dependency versions, selected devices, commands, outcomes, and remaining failures. Redact identifiers and account content before sharing. Public release notes should state limitations accurately—for example, an offline iPhone means iOS live behavior was not verified in that session.
