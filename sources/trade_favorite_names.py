"""Public role names and private labels for the matching in-game favorites."""

from __future__ import annotations

import json

from . import config_paths

LUCKY = "ReceiverLucky"
HUNDO = "ReceiverPerfect"
PRIORITY = "ReceiverPriority"
ROLES = (LUCKY, HUNDO, PRIORITY)


def _key(value: str) -> str:
    return "".join(value.casefold().split())


def _load() -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    path = config_paths.default_config("trade-favorites.json")
    if not path.is_file():
        return {}, {}
    data = json.loads(path.read_text())
    labels = data.get("labels", {}) if isinstance(data, dict) else None
    previous = data.get("aliases", {}) if isinstance(data, dict) else None
    if (not isinstance(labels, dict) or not isinstance(previous, dict)
            or (labels.keys() | previous.keys()) - set(ROLES)
            or any(not isinstance(value, str) or not value.strip() for value in labels.values())
            or any(not isinstance(values, list) or any(not isinstance(item, str) or not item.strip()
                       for item in values) for values in previous.values())):
        raise ValueError("Invalid trade-favorites.json labels or aliases")
    owners: dict[str, str] = {}
    for role in ROLES:
        for value in (role, labels.get(role, role), *previous.get(role, ())):
            key = _key(value)
            if key in owners and owners[key] != role:
                raise ValueError("Trade favorites must have distinct labels across roles")
            owners[key] = role
    return {role: value.strip() for role, value in labels.items()}, {
        role: tuple(v.strip() for v in values) for role, values in previous.items()
    }


def label(canonical: str) -> str:
    """The configured label used for new or synchronized favorites."""
    if canonical not in ROLES:
        return canonical
    labels, _ = _load()
    return labels.get(canonical, canonical)


def names(canonical: str) -> tuple[str, ...]:
    """Accept the configured label first, followed by known former labels."""
    if canonical not in ROLES:
        return (canonical,)
    labels, previous = _load()
    return tuple(dict.fromkeys((labels.get(canonical, canonical), canonical, *previous.get(canonical, ()))))


def role_for(name: str) -> str:
    key = _key(name)
    for role in ROLES:
        if key in {_key(candidate) for candidate in names(role)}:
            return role
    return name
