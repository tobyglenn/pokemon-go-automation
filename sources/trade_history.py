"""Durable events in the fleet's per-day trade journal."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import NamedTemporaryFile
from urllib.parse import quote
from uuid import uuid4

from . import config_paths

_TRANSACTION = ContextVar("trade_receipt_transaction", default=None)


class ConfirmationStateError(RuntimeError):
    """A prior confirmation or unavailable journal needs reconciliation."""


def _routes():
    from . import fleet_entrypoint
    if not config_paths.default_config("pokemon-fleet.yaml").is_file():
        return None, {}
    machines = fleet_entrypoint.machine_configs()
    return fleet_entrypoint.local_machine(machines), machines


def _accounts(names):
    from . import trading_accounts
    aliases = trading_accounts.aliases()
    canonical = sorted({aliases.get(name, name) for name in names})
    all_names = sorted(set(canonical) | {name for name, account in aliases.items() if account in canonical})
    return canonical, all_names


def pending_files(names, directory=None) -> list[Path]:
    directory = directory or config_paths.state_dir() / "trade" / "pending-confirmations"
    return [directory / (quote(name, safe="") + ".json") for name in sorted(names)]


@contextmanager
def _locked_pending(directory: Path):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_intent(path: Path, payload: dict) -> None:
    with NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def _pending_action(action, names, intent=None, *, directory=None):
    """Run one host's account-marker operation while holding its state lock."""
    guard, aliases = _accounts(names)
    if not guard or guard != sorted(names):
        raise ConfirmationStateError("hosts disagree on canonical trading accounts")
    paths = pending_files(aliases, directory)
    directory = paths[0].parent
    with _locked_pending(directory):
        existing = [path for path in paths if path.exists()]
        if action in {"check", "reserve"} and existing:
            raise ConfirmationStateError(f"reconcile the prior confirmation: {existing[0]}")
        if action == "check":
            return
        if action == "reserve":
            for path in paths:
                _write_intent(path, intent)
        elif action in {"update", "release"}:
            matched = [path for path in existing
                       if json.loads(path.read_text()).get("transaction_id") == intent["transaction_id"]]
            if action == "update" and len(matched) != len(paths):
                raise ConfirmationStateError("confirmation markers changed before evidence could be saved")
            for path in matched:
                if action == "update":
                    _write_intent(path, intent)
                else:
                    path.unlink()
        else:
            raise ValueError(f"unknown confirmation journal action {action!r}")
        _sync_directory(directory)


def _peer_pending(host, config, action, names, intent):
    from . import fleet_entrypoint
    request = {"action": action, "names": names, "intent": intent}
    try:
        command = fleet_entrypoint.remote_shell_command(host, config, ["-m", "sources.trade_history", "--pending"])
        result = subprocess.run(command, input=json.dumps(request), capture_output=True, text=True,
                                timeout=20, check=False)
        if result.returncode:
            raise ConfirmationStateError(result.stderr.strip() or f"worker exited {result.returncode}")
        reply = json.loads(result.stdout)
        if reply != {"ok": True}:
            raise ConfirmationStateError(reply.get("error", "invalid acknowledgement") if isinstance(reply, dict)
                                         else "invalid acknowledgement")
    except (OSError, ValueError, subprocess.SubprocessError, ConfirmationStateError) as exc:
        raise ConfirmationStateError(f"{host}: could not {action} confirmation state: {exc}") from exc


class PendingConfirmation:
    """Mirror one exchange's durable account reservation before any confirmation tap."""

    def __init__(self, names, intent=None, *, routes=None):
        self.names, _ = _accounts(names)
        if len(self.names) != len(names):
            raise ConfirmationStateError("both handsets resolve to the same trading account")
        self.intent = {**(intent or {}), "transaction_id": uuid4().hex}
        local, machines = _routes() if routes is None else routes
        # Every coordinator uses the same host ordering. A competing exchange
        # cannot take a contradictory reservation on one of the peer hosts.
        self.hosts = [(host, None if host == local else machines[host]) for host in sorted(machines)]
        if not self.hosts:
            self.hosts = [("local", None)]

    def _apply(self, action, host, config):
        if config is None:
            _pending_action(action, self.names, self.intent)
        else:
            _peer_pending(host, config, action, self.names, self.intent)

    def check(self):
        for host, config in self.hosts:
            self._apply("check", host, config)

    def reserve(self):
        attempted = []
        try:
            for host, config in self.hosts:
                attempted.append((host, config))
                self._apply("reserve", host, config)
        except BaseException:
            # No confirmation was dispatched. Release only our own markers,
            # even if the remote reservation succeeded before a timeout.
            for host, config in reversed(attempted):
                try:
                    self._apply("release", host, config)
                except Exception:
                    pass  # Unreachable markers fail closed on future runs.
            raise

    def update(self, **evidence):
        self.intent.update(evidence)
        # Keep a coordinator copy of the evidence even if a peer disappeared.
        for host, config in sorted(self.hosts, key=lambda item: item[1] is not None):
            self._apply("update", host, config)

    def release(self):
        for host, config in reversed(self.hosts):
            self._apply("release", host, config)

    @contextmanager
    def recording(self):
        token = _TRANSACTION.set(self.intent["transaction_id"])
        try:
            yield
        finally:
            _TRANSACTION.reset(token)


def append_event(payload, now=None, directory=None):
    now = now or datetime.now()
    directory = directory or config_paths.state_dir() / "trade"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {"at": now.isoformat(timespec="microseconds"), **payload}
    if (transaction := _TRANSACTION.get()) is not None:
        record.setdefault("id", transaction)
    fd = os.open(directory / f"history-{now.date()}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab", buffering=0) as out:
        out.write((json.dumps(record) + "\n").encode())
        os.fsync(out.fileno())
    _sync_directory(directory)


def current_day_counts(names, day=None):
    from . import collector_day
    _, machines = _routes()
    return collector_day.day_counts(names, machines, day or date.today())


def has_budget(names, cap, day=None):
    counts = current_day_counts(names, day)
    return (not pair_capped(names, counts)
            and all(counts[name] < cap for name in names))


def pair_capped(names, counts):
    accounts, _ = _accounts(names)
    return tuple(accounts) in counts.capped_pairs


def record_fleet_trade(pair):
    append_event({"id": _TRANSACTION.get() or uuid4().hex, "pair": list(pair), "give": {},
                  "family": "", "reason": "trade.py", "evidence": "both post-trade screens"})


def record_pair_limit(pair):
    # The game can display the same limit on both screens when only one trainer is capped.
    append_event({"pair": [], "capped_pair": sorted(pair), "reason": "in-game daily trading limit"})


def _worker():
    try:
        request = json.load(sys.stdin)
        _pending_action(request["action"], request["names"], request["intent"])
    except (OSError, ValueError, KeyError, TypeError, ConfirmationStateError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
    else:
        print(json.dumps({"ok": True}))


if __name__ == "__main__":
    if sys.argv[1:] != ["--pending"]:
        raise SystemExit("Internal confirmation journal worker")
    _worker()
