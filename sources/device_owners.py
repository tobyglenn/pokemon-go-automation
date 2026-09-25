"""Which computer a phone is plugged into, discovered rather than declared.

`pokemon-fleet.yaml` lets each device record the `machine` it hangs off, and
that tag is right until someone moves a cable.  Then `--devices razr` routes to
the computer the tag names, that host's own connectivity check reports
`Android not connected/authorized`, and the phone is sitting in a USB port on
the other Mac.  Worse in the other direction: a worker asked for a phone tagged
to a peer tries to delegate back and dies with `Machine <name> needs ssh_host`,
because the local entry has no SSH destination and needs none.

So the tag stops being the answer and becomes the fallback.  The USB buses know
the truth; ask them.

Two rules shape the probing:

* **A probe that fails never reads as "no phones here."**  That lie is what let
  a watchdog kill a live trade (see `pokemon_fleet.adb_states`).  Each snapshot
  records which of its probes actually answered, and a host whose probe did not
  answer is left undecided, so the configured tag still governs.
* **Nothing is probed that does not have to be.**  Routing asks the local buses
  first, in-process, and with exactly one other computer configured a phone
  that is demonstrably not here is on that one -- no SSH, no wake of a sleeping
  Mac.  Remote hosts are probed only to break a genuine three-way ambiguity, or
  when `fleet.py owners` asks for the whole picture on purpose.

Set `POGO_DEVICE_DISCOVERY=off` to route on the configured tags alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import subprocess
import sys
from typing import Any, Iterable, Sequence

from . import pokemon_fleet

PROBE_MODULE = "sources.device_owners"
# The remote probe runs two USB enumerations behind an SSH connect that may be
# waiting on a wake-on-LAN, so it gets its own budget on top of the machine's
# connect timeout rather than sharing one number with it.
REMOTE_PROBE_SECONDS = 45
PLATFORMS = ("android", "ios")
_DISCOVERY_OFF = {"off", "0", "no", "false"}


@dataclass(frozen=True)
class Snapshot:
    """What one computer can see on its buses, and what it failed to look at."""

    machine: str
    android: dict[str, str] = field(default_factory=dict)
    ios: frozenset[str] = frozenset()
    probed: frozenset[str] = frozenset()
    attempted: frozenset[str] = frozenset()
    errors: tuple[str, ...] = ()

    def looked_at(self, platform: str) -> bool:
        return platform in self.probed

    def merged(self, other: "Snapshot") -> "Snapshot":
        """Fold a second, narrower probe of the same computer into this one."""
        return Snapshot(
            machine=self.machine,
            android={**self.android, **other.android} if "android" in other.probed else dict(self.android),
            ios=other.ios if "ios" in other.probed else self.ios,
            probed=self.probed | other.probed,
            attempted=self.attempted | other.attempted,
            errors=self.errors + other.errors,
        )

    def holds(self, platform: str, identifier: str) -> bool:
        if not identifier or not self.looked_at(platform):
            return False
        if platform == "android":
            return self.android.get(identifier) == "device"
        return identifier in self.ios

    def as_dict(self) -> dict[str, Any]:
        return {
            "machine": self.machine,
            "android": dict(self.android),
            "ios": sorted(self.ios),
            "probed": sorted(self.probed),
            "attempted": sorted(self.attempted),
            "errors": list(self.errors),
        }


def snapshot_from_dict(machine: str, payload: dict[str, Any], attempted: Sequence[str] = PLATFORMS) -> Snapshot:
    android = payload.get("android")
    ios = payload.get("ios")
    errors = payload.get("errors")

    def platforms(key: str) -> frozenset[str]:
        value = payload.get(key)
        return frozenset(str(name) for name in value if name in PLATFORMS) if isinstance(value, list) else frozenset()

    return Snapshot(
        machine=machine,
        android={str(key): str(value) for key, value in android.items()} if isinstance(android, dict) else {},
        ios=frozenset(str(value) for value in ios) if isinstance(ios, list) else frozenset(),
        probed=platforms("probed"),
        attempted=platforms("attempted") or frozenset(attempted),
        errors=tuple(str(value) for value in errors) if isinstance(errors, list) else (),
    )


def discovery_enabled() -> bool:
    return os.environ.get("POGO_DEVICE_DISCOVERY", "on").strip().lower() not in _DISCOVERY_OFF


def local_snapshot(machine: str = "local", platforms: Sequence[str] = PLATFORMS) -> Snapshot:
    """Enumerate this computer's own buses, recording which probes answered."""
    android: dict[str, str] = {}
    ios: frozenset[str] = frozenset()
    probed: set[str] = set()
    errors: list[str] = []
    if "android" in platforms:
        try:
            android = pokemon_fleet.adb_states_or_raise()
            probed.add("android")
        except pokemon_fleet.ProbeError as error:
            errors.append(f"android: {error}")
    if "ios" in platforms:
        try:
            ios = frozenset(pokemon_fleet.ios_connected_udids_or_raise())
            probed.add("ios")
        except pokemon_fleet.ProbeError as error:
            errors.append(f"ios: {error}")
    return Snapshot(
        machine=machine,
        android=android,
        ios=ios,
        probed=frozenset(probed),
        attempted=frozenset(platforms),
        errors=tuple(errors),
    )


def remote_snapshot(machine: str, config: dict, platforms: Sequence[str] = PLATFORMS) -> Snapshot:
    """Run this same module over SSH inside the machine's own checkout."""
    from . import fleet_entrypoint  # deferred: that module reaches back here while routing

    attempted = frozenset(platforms)
    worker = ["-m", PROBE_MODULE, "--json", "--platforms", ",".join(platforms)]
    try:
        command = fleet_entrypoint.remote_shell_command(machine, config, worker)
    except pokemon_fleet.FleetError as error:
        return Snapshot(machine=machine, attempted=attempted, errors=(str(error),))
    timeout = fleet_entrypoint.connect_timeout(machine, config) + REMOTE_PROBE_SECONDS
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return Snapshot(machine=machine, attempted=attempted, errors=(f"probe did not run: {error}",))
    if result.returncode != 0:
        # An older checkout answers `No module named sources.device_owners` here,
        # which is worth surfacing verbatim: it means that host needs a deploy.
        detail = (result.stderr or result.stdout).strip().splitlines()
        return Snapshot(
            machine=machine,
            attempted=attempted,
            errors=(f"probe failed: {detail[-1] if detail else result.returncode}",),
        )
    try:
        payload = json.loads(result.stdout)
    except ValueError as error:
        return Snapshot(machine=machine, attempted=attempted, errors=(f"probe returned no JSON: {error}",))
    if not isinstance(payload, dict):
        return Snapshot(machine=machine, attempted=attempted, errors=("probe returned no JSON object",))
    return snapshot_from_dict(machine, payload, platforms)


class Discovery:
    """One pass of bus enumeration, memoized for the life of a command.

    Deliberately not cached to disk: a cache that outlives a cable change
    reintroduces exactly the stale-routing bug this module exists to remove.
    """

    def __init__(self, machines: dict[str, dict] | None = None, local: str | None = None) -> None:
        from . import fleet_entrypoint  # deferred: see remote_snapshot

        self.machines = fleet_entrypoint.machine_configs() if machines is None else machines
        self.local = fleet_entrypoint.local_machine(self.machines) if local is None else local
        self._snapshots: dict[str, Snapshot] = {}

    def snapshot(self, machine: str, platforms: Sequence[str] = PLATFORMS) -> Snapshot:
        """This computer's view, probing only the buses not asked about yet.

        Routing one Android phone must not pay for the iOS enumeration, which
        spends up to twelve seconds in `devicectl` before it answers.  A probe
        that already failed counts as asked: it will fail the same way again.
        """
        cached = self._snapshots.get(machine)
        wanted = tuple(name for name in platforms if cached is None or name not in cached.attempted)
        if not wanted:
            return cached  # type: ignore[return-value]
        if machine == self.local:
            from . import fleet_entrypoint  # deferred: see remote_snapshot

            # A machine's `path_prefix` is usually where its adb lives; without it
            # the probe reports "could not run adb devices" and decides nothing.
            fleet_entrypoint.apply_local_environment()
            fresh = local_snapshot(machine, wanted)
        else:
            fresh = remote_snapshot(machine, self.machines[machine], wanted)
        self._snapshots[machine] = fresh if cached is None else cached.merged(fresh)
        return self._snapshots[machine]

    def probed(self) -> dict[str, Snapshot]:
        return dict(self._snapshots)

    def machine_holding(self, platform: str, identifier: str, configured: str = "") -> str | None:
        """The configured computer with this phone on it, or None if undecided.

        `configured` is the device's `machine` tag, which is evidence rather
        than an answer: it stands only while the buses have not contradicted it.
        Returning None hands the decision back to it.
        """
        if not identifier or not discovery_enabled():
            return None
        here = self.snapshot(self.local, (platform,))
        if here.holds(platform, identifier):
            return self.local
        if not here.looked_at(platform):
            return None  # nothing was actually enumerated; decide nothing
        others = [name for name in self.machines if name != self.local]
        if not others:
            return self.local
        if configured in others:
            # This computer has disproven nothing about a peer, and asking costs
            # an SSH wake to learn what that host's own check reports anyway.
            return None
        if len(others) == 1:
            # The tag was this computer and the bus says otherwise, or there is
            # no tag: either way one candidate is left, so no probe can add to it.
            return others[0]
        for name in others:
            if self.snapshot(name, (platform,)).holds(platform, identifier):
                return name
        return None

    def machine_for_spec(self, spec: pokemon_fleet.DeviceSpec) -> str | None:
        configured = spec.config.get("machine")
        return self.machine_holding(
            spec.platform, spec.identifier, configured if isinstance(configured, str) else ""
        )


@dataclass(frozen=True)
class OwnerRow:
    """One device's configured home against the computer actually holding it."""

    name: str
    platform: str
    identifier: str
    configured: str
    attached_to: str | None

    @property
    def mismatched(self) -> bool:
        return bool(self.attached_to) and bool(self.configured) and self.attached_to != self.configured

    @property
    def state(self) -> str:
        if not self.attached_to:
            return "not attached to any configured computer"
        if self.mismatched:
            return f"attached to {self.attached_to}, configured {self.configured}"
        return f"attached to {self.attached_to}"


def survey(fleet: pokemon_fleet.FleetConfig, machines: dict[str, dict] | None = None) -> tuple[list[OwnerRow], dict[str, Snapshot]]:
    """Probe every configured computer and match what they see to the registry.

    Unlike routing, this asks every host on purpose -- it is the report an
    operator reads after moving cables, so an unprobed Mac is a finding.
    """
    discovery = Discovery(machines)
    for machine in discovery.machines:
        discovery.snapshot(machine)
    snapshots = discovery.probed()
    rows: list[OwnerRow] = []
    for name, spec in sorted(fleet.devices.items()):
        identifier = spec.identifier
        holder = next(
            (machine for machine, snapshot in snapshots.items() if snapshot.holds(spec.platform, identifier)),
            None,
        )
        rows.append(
            OwnerRow(
                name=name,
                platform=spec.platform,
                identifier=identifier,
                configured=str(spec.config.get("machine", "") or ""),
                attached_to=holder,
            )
        )
    return rows, snapshots


def unregistered_serials(fleet: pokemon_fleet.FleetConfig, snapshots: dict[str, Snapshot]) -> dict[str, list[str]]:
    """Authorized Android phones no registry entry claims, per computer."""
    registered = {spec.identifier for spec in fleet.devices.values() if spec.platform == "android"}
    found: dict[str, list[str]] = {}
    for machine, snapshot in snapshots.items():
        extra = sorted(
            serial for serial, state in snapshot.android.items() if state == "device" and serial not in registered
        )
        if extra:
            found[machine] = extra
    return found


def print_survey(
    rows: Iterable[OwnerRow],
    snapshots: dict[str, Snapshot],
    unregistered: dict[str, list[str]],
    as_json: bool = False,
) -> None:
    rows = list(rows)
    if as_json:
        print(json.dumps(
            {
                "devices": [
                    {
                        "name": row.name,
                        "platform": row.platform,
                        "configured": row.configured,
                        "attached_to": row.attached_to,
                        "mismatched": row.mismatched,
                    }
                    for row in rows
                ],
                "machines": {machine: snapshot.as_dict() for machine, snapshot in snapshots.items()},
                "unregistered": unregistered,
            },
            indent=2,
            sort_keys=True,
        ))
        return
    width = max((len(row.name) for row in rows), default=6)
    print(f"Probed {len(snapshots)} configured computer(s) for attached phones")
    for row in rows:
        marker = "!" if row.mismatched else " "
        print(f"{marker} {row.name.ljust(width)}  {row.platform:<7}  {row.state}")
    for machine, snapshot in sorted(snapshots.items()):
        missed = [platform for platform in PLATFORMS if not snapshot.looked_at(platform)]
        if missed:
            print(f"\n{machine}: no answer for {', '.join(missed)} -- those devices routed on their configured machine")
        for error in snapshot.errors:
            print(f"{machine}: {error}")
    for machine, serials in sorted(unregistered.items()):
        print(f"\n{machine}: connected but unregistered: {', '.join(f'android:{serial}' for serial in serials)}")
    if any(row.mismatched for row in rows):
        print("\nRouting follows the attached column, so a `!` row works as it stands;")
        print("correct the `machine` field on every computer's copy to fix the fallback.")


def main(arguments: Sequence[str] | None = None) -> int:
    """Probe this computer only; `fleet.py owners` is the fleet-wide report."""
    argv = list(sys.argv[1:] if arguments is None else arguments)
    as_json = "--json" in argv
    platforms = PLATFORMS
    for index, value in enumerate(argv):
        chosen = value.split("=", 1)[1] if value.startswith("--platforms=") else (
            argv[index + 1] if value == "--platforms" and index + 1 < len(argv) else None
        )
        if chosen is not None:
            platforms = tuple(name for name in chosen.split(",") if name in PLATFORMS) or PLATFORMS
            break
    machine = os.environ.get("POGO_MACHINE", "this computer")
    snapshot = local_snapshot(machine, platforms)
    if as_json:
        print(json.dumps(snapshot.as_dict(), sort_keys=True))
        return 0
    for serial, state in sorted(snapshot.android.items()):
        print(f"android:{serial} {state}")
    for udid in sorted(snapshot.ios):
        print(f"ios:{udid} attached")
    for error in snapshot.errors:
        print(f"probe error {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
