"""Public commands fan out to the configured hosts; workers stay local."""
from __future__ import annotations
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
from typing import Sequence
from . import config_paths, fleet_watchdog, pokemon_fleet

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALL_MACHINES_FLAG = "--allmachines"
DEVICE_OPERATIONS = {"gifts", "berries", "gbl", "gbl_day", "delete", "trade", "battle"}

def has_option(arguments: Sequence[str], option: str) -> bool:
    return any(value == option or value.startswith(f"{option}=") for value in arguments)


def _multi_option_values(arguments: Sequence[str], option: str) -> list[str] | None:
    values = list(arguments)
    for index, value in enumerate(values):
        if value == option or value.startswith(option + "="):
            result = [value.split("=", 1)[1]] if "=" in value else []
            for following in values[index + 1:]:
                if following.startswith("-"):
                    break
                result.append(following)
            return result
    return None

def _replace_multi_option(arguments: Sequence[str], option: str, values: Sequence[str]) -> list[str]:
    result = list(arguments)
    for index, value in enumerate(result):
        if value == option or value.startswith(option + "="):
            end = index + 1
            while end < len(result) and not result[end].startswith("-"):
                end += 1
            return [*result[:index], option, *values, *result[end:]]
    return [option, *values, *result]


VALUE_OPTIONS = {
    "--count",
    "--android-count",
    "--max-cycles",
    "--spend",
    "--spend-device",
    "--trace",
    "--output",
    "--step",
    "--start-step",
    "--config",
    "--max-transfers",
    "--delay-modifier",
    "--pair",
    "--battles",
}


def _extract_positional_devices(arguments: Sequence[str]) -> tuple[list[str], list[str]]:
    """Separate positional device names from flags/options when --devices is omitted."""
    devices: list[str] = []
    other: list[str] = []
    i = 0
    while i < len(arguments):
        arg = arguments[i]
        if arg.startswith("-"):
            other.append(arg)
            if "=" in arg:
                i += 1
                continue
            if arg in VALUE_OPTIONS and i + 1 < len(arguments) and not arguments[i + 1].startswith("-"):
                other.append(arguments[i + 1])
                i += 1
        elif arg in {"selection", "from-selection", "skip-trade-btn"}:
            other.append("--selection")
        else:
            devices.append(arg)
        i += 1
    return devices, other


def operation_arguments(operation: str, arguments: Sequence[str]) -> list[str]:
    """Apply safe public-command defaults while preserving explicit choices."""
    forwarded = list(arguments)
    if operation in DEVICE_OPERATIONS and not has_option(forwarded, "--devices") and not has_option(forwarded, "--pair"):
        positional_devices, options = _extract_positional_devices(forwarded)
        if positional_devices:
            if operation in {"trade", "battle"} and len(positional_devices) == 2:
                forwarded = ["--pair", *positional_devices, *options]
            else:
                forwarded = ["--devices", *positional_devices, *options]
        else:
            forwarded = ["--devices", "all", *options]
    if operation == "gifts":
        if not has_option(forwarded, "--count") and not has_option(forwarded, "--all"):
            forwarded.append("--all")
    return forwarded



def apply_registry_override(arguments: Sequence[str]) -> list[str]:
    """Choose the coordinator registry; remote hosts use their configured directory."""
    result = list(arguments)
    for index, value in enumerate(result):
        if value == "--config":
            if index + 1 == len(result):
                raise pokemon_fleet.FleetError("--config needs a fleet YAML path")
            os.environ["POGO_FLEET_CONFIG"] = str(Path(config_paths.expand_user_path(result[index + 1])).resolve())
            del result[index:index + 2]
            break
        if value.startswith("--config="):
            os.environ["POGO_FLEET_CONFIG"] = str(Path(config_paths.expand_user_path(value.split("=", 1)[1])).resolve())
            del result[index]
            break
    return result

def machine_configs() -> dict[str, dict]:
    path = config_paths.default_config("pokemon-fleet.yaml")
    root = pokemon_fleet.load_yaml(path)
    raw = root.get("machines")
    if raw is None:
        return {"local": {"local": True}}
    if not isinstance(raw, dict) or not raw:
        raise pokemon_fleet.FleetError("machines must be a nonempty mapping, or omit it for a single host")
    result = {}
    for name, config in raw.items():
        if not isinstance(name, str) or not name or not isinstance(config, dict):
            raise pokemon_fleet.FleetError("Each machine needs a name and configuration mapping")
        if config.get("enabled", True):
            result[name] = config
    if not result:
        raise pokemon_fleet.FleetError("No machines are enabled")
    return result

def _local_machine(machines: dict[str, dict] | None = None) -> str:
    machines = machine_configs() if machines is None else machines
    override = os.environ.get("POGO_MACHINE")
    if override:
        if override not in machines:
            raise pokemon_fleet.FleetError("POGO_MACHINE does not name an enabled machine")
        return override
    hostname = socket.gethostname().split(".", 1)[0].lower()
    matches = [name for name, config in machines.items()
               if config.get("local") is True or hostname in
               [str(value).split(".", 1)[0].lower() for value in config.get("hostnames", [])]]
    if len(matches) != 1:
        raise pokemon_fleet.FleetError("Set POGO_MACHINE or configure exactly one matching machine hostname (or local: true)")
    return matches[0]

def apply_local_environment() -> None:
    """Prepend configured dependency directories without changing shell dotfiles."""
    if not config_paths.default_config("pokemon-fleet.yaml").is_file():
        return
    machines = machine_configs()
    local = _local_machine(machines)
    prefixes = machines[local].get("path_prefix", [])
    if not isinstance(prefixes, list) or not all(isinstance(value, str) for value in prefixes):
        raise pokemon_fleet.FleetError("machine.path_prefix must be a list of directories")
    if prefixes:
        entries = [str(config_paths.expand_user_path(value)) for value in prefixes]
        entries.extend(os.environ.get("PATH", "").split(os.pathsep))
        os.environ["PATH"] = os.pathsep.join(dict.fromkeys(entries))

def _machine_for_device(name: str, machines: dict[str, dict]) -> str:
    if name.startswith("android:"):
        return _local_machine(machines)
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    if name not in fleet.devices:
        raise pokemon_fleet.FleetError(f"Unknown configured device: {name}")
    machine = fleet.devices[name].config.get("machine")
    if machine in machines:
        return machine
    if len(machines) == 1:
        return next(iter(machines))
    raise pokemon_fleet.FleetError(f"Device {name} needs a machine field for explicit cross-host selection")

def route_machine_arguments(arguments: Sequence[str], *, dynamic_all: bool = True) -> dict[str, list[str]]:
    machines = machine_configs()
    forwarded = list(arguments)
    pair = _multi_option_values(forwarded, "--pair")
    if pair is not None:
        if len(pair) != 2:
            raise pokemon_fleet.FleetError("--pair needs exactly two devices")
        owners = {_machine_for_device(name, machines) for name in pair}
        if len(owners) != 1:
            raise pokemon_fleet.FleetError("A trade or battle pair must be connected to the same host")
        return {owners.pop(): forwarded}
    devices = _multi_option_values(forwarded, "--devices")
    if devices is None:
        return {name: forwarded[:] for name in machines}
    if devices == ["all"]:
        if "--allow-empty" not in forwarded:
            forwarded.append("--allow-empty")
        return {name: forwarded[:] for name in machines}
    if not devices or "all" in devices:
        raise pokemon_fleet.FleetError("Use --devices all by itself")
    routed: dict[str, list[str]] = {}
    for name in machines:
        selected = [device for device in devices if _machine_for_device(device, machines) == name]
        if selected:
            routed[name] = _replace_multi_option(forwarded, "--devices", selected)
    return routed

def _commands_for_machines(script_name: str, arguments: Sequence[str]) -> dict[str, list[str]]:
    arguments = apply_registry_override(arguments)
    machines = machine_configs()
    local = _local_machine(machines)
    routed = route_machine_arguments(arguments)
    commands: dict[str, list[str]] = {}
    for name, forwarded in routed.items():
        config = machines[name]
        module = Path(script_name).stem
        worker = ["-u", "-m", module, "--local", *forwarded]
        if name == local:
            commands[name] = [sys.executable, *worker]
            continue
        ssh_host = config.get("ssh_host")
        project_dir = config.get("project_dir")
        if not isinstance(ssh_host, str) or not ssh_host or ssh_host.startswith("-"):
            raise pokemon_fleet.FleetError(f"Machine {name} needs ssh_host (an SSH config alias is recommended)")
        if not isinstance(project_dir, str) or not project_dir:
            raise pokemon_fleet.FleetError(f"Machine {name} needs project_dir pointing to its installed checkout")
        python = config.get("python", ".venv/bin/python")
        config_dir = config.get("config_dir", "~/.config/pokemon-go-automation")
        env = ["env", f"POGO_CONFIG_DIR={config_dir}", f"POGO_MACHINE={name}"]
        # A leading ~/ is intentionally expanded by the remote shell; all other text is quoted.
        remote_dir = ('"$HOME"/' + shlex.quote(project_dir[2:])) if project_dir.startswith("~/") else shlex.quote(project_dir)
        remote = shlex.join([*env, str(python), *worker])
        prefixes = config.get("path_prefix", [])
        if not isinstance(prefixes, list) or not all(isinstance(value, str) for value in prefixes):
            raise pokemon_fleet.FleetError("machine.path_prefix must be a list of directories")
        remote_prefix = ""
        if prefixes:
            encoded = [('"$HOME"/' + shlex.quote(value[2:])) if value.startswith("~/") else shlex.quote(value) for value in prefixes]
            remote_prefix = "export PATH=" + ":".join(encoded) + ':"$PATH"; '
        commands[name] = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                          "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                          ssh_host, remote_prefix + f"cd {remote_dir} && exec {remote}"]
    return commands

def run_all_machines(script_name: str, arguments: Sequence[str]) -> int:
    commands = _commands_for_machines(script_name, arguments)
    apply_local_environment()
    processes: dict[str, subprocess.Popen] = {}
    try:
        for machine, command in commands.items():
            print(f"[{machine}] {Path(script_name).stem}", flush=True)
            processes[machine] = subprocess.Popen(command, cwd=PROJECT_ROOT)
        statuses = {machine: process.wait() for machine, process in processes.items()}
    except BaseException:
        for process in processes.values():
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
        for process in processes.values():
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
        raise
    failed = {machine: code for machine, code in statuses.items() if code}
    if failed:
        raise pokemon_fleet.FleetError("Host command failed: " + ", ".join(f"{name}={code}" for name, code in failed.items()))
    return 0

def local_requested(arguments: Sequence[str]) -> bool:
    return "--local" in arguments or os.environ.get("POKEMON_FLEET_CHILD") == "1"

def clean_scope_flags(arguments: Sequence[str]) -> list[str]:
    return [value for value in arguments if value not in {"--local", ALL_MACHINES_FLAG}]

def run_operation(operation: str, script_name: str, arguments: Sequence[str] | None = None) -> int:
    original = apply_registry_override(list(sys.argv[1:] if arguments is None else arguments))
    local = local_requested(original)
    forwarded = operation_arguments(operation, clean_scope_flags(original))
    if not local and not any(value in {"--help", "-h"} for value in forwarded):
        return run_all_machines(script_name, forwarded)
    if not any(value in {"--help", "-h"} for value in forwarded):
        apply_local_environment()
    config = config_paths.default_config("pokemon-fleet.yaml")
    # --config selects the fleet registry and is accepted after any public command.
    for index, value in enumerate(forwarded[:]):
        if value == "--config" and index + 1 < len(forwarded):
            config = Path(config_paths.expand_user_path(forwarded[index + 1]))
            del forwarded[index:index + 2]
            break
        if value.startswith("--config="):
            config = Path(config_paths.expand_user_path(value.split("=", 1)[1]))
            del forwarded[index]
            break
    sys.argv = ["fleet.py", "--config", str(config), operation, *forwarded]
    if "--plan" not in forwarded and operation != "status" and "--help" not in forwarded:
        fleet_watchdog.install()
    return pokemon_fleet.main()

def direct_module_operation(operation: str, script_name: str) -> int | None:
    """Platform modules are fleet entrypoints unless the controller marked a worker."""
    if os.environ.get("POKEMON_FLEET_CHILD") == "1":
        return None
    try:
        return run_operation(operation, script_name)
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
