"""Map a trainer's alternate phone to the same daily allowance and receiver role."""
from __future__ import annotations

import json
from dataclasses import replace
from . import config_paths


def aliases() -> dict[str, str]:
    file = config_paths.default_config("trading-accounts.json")
    if not file.exists():
        return {}
    values = json.loads(file.read_text())["aliases"]
    if not isinstance(values, dict) or any(not isinstance(v, str) or not v or v in values for v in values.values()):
        raise ValueError("Trading account aliases must refer directly to canonical accounts")
    return values


def account_for(name):
    return aliases().get(name, name)


def transports(owners, mapping):
    """Prefer the primary handset whenever both phones for one trainer are attached."""
    selected = {}
    for phone in owners:
        account = mapping.get(phone, phone)
        if account not in selected or phone == account:
            selected[account] = phone
    return selected


def route_fleet(fleet, owners, mapping):
    """Preserve canonical device specs for account policy and route I/O to the attached handset."""
    selected = transports(owners, mapping)
    alternate = {account: phone for account, phone in selected.items() if phone != account}
    return (replace(fleet, transports=alternate) if alternate != getattr(fleet, "transports", {}) else fleet,
            {account: owners[phone] for account, phone in selected.items()})
