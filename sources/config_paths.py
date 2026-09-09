"""Resolve private configuration independently from source and working directory."""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("POGO_CONFIG_DIR", "~/.config/pokemon-go-automation")).expanduser()

class ConfigPathError(RuntimeError):
    """A configured path is malformed or missing."""

def expand_user_path(value: Any) -> Any:
    return os.path.expanduser(os.path.expandvars(value)) if isinstance(value, str) else value

def private_config_dir() -> Path:
    return Path(expand_user_path(os.environ.get("POGO_CONFIG_DIR", str(CONFIG_DIR)))).resolve()

def search_dirs(relative_to: Path | str | None = None) -> list[Path]:
    ordered = ([Path(relative_to)] if relative_to is not None else []) + [private_config_dir()]
    return list(dict.fromkeys(path.expanduser().resolve() for path in ordered))

def candidates(name: str, relative_to: Path | str | None = None) -> list[Path]:
    return [directory / name for directory in search_dirs(relative_to)]

def find_config(value: Any, relative_to: Path | str | None = None, label: str = "config") -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigPathError(f"{label} must be a path")
    value_path = Path(expand_user_path(value))
    if value_path.is_absolute():
        if value_path.is_file():
            return value_path.resolve()
        raise ConfigPathError(f"{label} not found: {value_path}")
    for path in candidates(str(value_path), relative_to):
        if path.is_file():
            return path.resolve()
    raise ConfigPathError(f"{label} not found: {value}. Run `pogo init-config` and configure {private_config_dir()}")

def default_config(name: str) -> Path:
    if name == "pokemon-fleet.yaml" and os.environ.get("POGO_FLEET_CONFIG"):
        return Path(expand_user_path(os.environ["POGO_FLEET_CONFIG"])).resolve()
    return private_config_dir() / name

def scale_point_to_viewport(
    point: list[int] | tuple[int, int],
    viewport: dict[str, int],
    baseline: tuple[int, int] = (375, 667),
) -> list[int]:
    """Scale a logical point calibrated on `baseline` (e.g. 375x667 iPhone SE) to active `viewport`."""
    vw, vh = viewport.get("width", baseline[0]), viewport.get("height", baseline[1])
    if vw == baseline[0] and vh == baseline[1]:
        return list(point)
    scale_x = vw / float(baseline[0])
    scale_y = vh / float(baseline[1])
    return [int(round(point[0] * scale_x)), int(round(point[1] * scale_y))]


def state_dir() -> Path:
    """Private captures, histories and caches never default into the checkout."""
    return Path(expand_user_path(os.environ.get("POGO_STATE_DIR", "~/.local/state/pokemon-go-automation"))).resolve()

def temporary_file(name: str) -> Path:
    """Keep pulled on-device calibration files in a private runtime directory."""
    directory = state_dir() / "tmp"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / name
