"""One-use private approvals for specific, verified guaranteed Lucky offers."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta
import json
import os
import secrets
import select
import sqlite3
import sys
import time

from . import config_paths


class ApprovalRequired(RuntimeError):
    """This offer has no usable authorization."""


class ProposalReady(ApprovalRequired):
    def __init__(self, proposal_id):
        self.proposal_id = proposal_id
        super().__init__(f"Guaranteed offer {proposal_id} parked for a specific approval.")


@contextlib.contextmanager
def _db():
    folder = config_paths.state_dir() / "trade" / "guaranteed-approvals"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / "proposals.sqlite3"
    with contextlib.closing(sqlite3.connect(path, timeout=8)) as db:
        os.chmod(path, 0o600)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS proposals (
                      id TEXT PRIMARY KEY, offer TEXT NOT NULL, expires REAL NOT NULL,
                      live_until REAL NOT NULL, status TEXT NOT NULL)""")
        db.execute("BEGIN IMMEDIATE")
        with db:
            yield db


def _row(db, token):
    row = db.execute("SELECT * FROM proposals WHERE id=?", (token,)).fetchone()
    if row is None or time.time() >= row["expires"]:
        raise ApprovalRequired(f"Guaranteed proposal {token!r} is unknown or expired")
    return row


def awaiting_decision(pair):
    """Return an existing live or parked proposal for the same two phones."""
    with _db() as db:
        rows = db.execute("SELECT id, offer FROM proposals WHERE expires > ? "
                          "AND status IN ('pending', 'approved', 'resuming') ORDER BY rowid DESC",
                          (time.time(),))
        for row in rows:
            if set(json.loads(row["offer"])["participants"]) == set(pair):
                return row["id"]
    return None


def describe(offer):
    lines = []
    for device, item in offer["participants"].items():
        form = item.get("form") or ""
        form = f" ({form})" if form and form.casefold() != item["species"].casefold() else ""
        lines.append(f"{device} ({item['trainer']}) gives {item['species']}{form} #{item['dex']}, "
                     f"displayed {item['label']!r}, CP {item['cp']}, HP {item['hp']}, "
                     f"caught {item['caught']}, moves {', '.join(item['moves']) or 'unread'}, "
                     f"{item.get('stock', '?')} eligible. "
                     f"{item.get('reason', '')} Cost: {offer['costs'][device]} Stardust.")
    kind = "special (uses a special trade)" if offer["special"] else "normal"
    return "\n".join(lines + [f"Guaranteed Lucky, {kind}."])


def approve(token):
    """Record this specific choice. Return the offer if the lobby needs reopening."""
    with _db() as db:
        row = _row(db, token)
        if row["status"] not in {"pending", "approved"}:
            raise ApprovalRequired(f"Guaranteed proposal {token} is {row['status']}")
        db.execute("UPDATE proposals SET status='approved' WHERE id=?", (token,))
        offer = json.loads(row["offer"])
        print(f"[guaranteed] Approved {token}.\n{describe(offer)}", flush=True)
        return None if time.time() < row["live_until"] else offer


def decline(token):
    with _db() as db:
        row = _row(db, token)
        if row["status"] not in {"pending", "approved", "resuming"}:
            raise ApprovalRequired(f"Guaranteed proposal {token} is {row['status']}")
        db.execute("UPDATE proposals SET status='declined' WHERE id=?", (token,))
    print(f"[guaranteed] Declined {token}.", flush=True)
    return 0


@contextlib.contextmanager
def claim_for_reselect(token):
    with _db() as db:
        row = _row(db, token)
        if row["status"] != "approved" or time.time() < row["live_until"]:
            raise ApprovalRequired(f"Guaranteed proposal {token} is unavailable for reselection")
        db.execute("UPDATE proposals SET status='resuming' WHERE id=?", (token,))
        offer = json.loads(row["offer"])
    try:
        yield offer
    finally:
        with _db() as db:
            db.execute("UPDATE proposals SET status='closed' WHERE id=? AND status='resuming'", (token,))


def _identity(offer):
    # Counts/reasons/screenshots are presentation evidence; an unrelated newly
    # acquired copy with a different CP does not change this unique individual.
    from .trade_local import trainer_identity
    participants = {}
    for device, p in offer["participants"].items():
        fixed = {k: p[k] for k in ("species", "dex", "form", "label", "cp", "hp", "cp_range",
                                    "hp_range", "caught", "moves", "locality")}
        participants[device] = {**fixed, "trainer": trainer_identity(p["trainer"])}
    return {"special": offer["special"], "costs": offer["costs"], "participants": participants}


def consume(token, current, *, reselected):
    with _db() as db:
        row = _row(db, token)
        expected = "resuming" if reselected else "approved"
        if row["status"] != expected or (not reselected and time.time() > row["live_until"]):
            raise ApprovalRequired(f"Guaranteed proposal {token} has no current one-use approval")
        if _identity(json.loads(row["offer"])) != _identity(current):
            raise ApprovalRequired("Revalidated Pokémon, trainers, class or cost changed. A fresh proposal is required.")
        db.execute("UPDATE proposals SET status='consumed' WHERE id=?", (token,))


def completed(token, receipts):
    """Attach the positive in-game receipt to its one-use human authorization."""
    with _db() as db:
        row = db.execute("SELECT offer FROM proposals WHERE id=? AND status='consumed'", (token,)).fetchone()
        if row is None:
            raise ApprovalRequired(f"No consumed approval matches receipt {token}")
        offer = json.loads(row["offer"])
        offer["receipts"] = receipts
        db.execute("UPDATE proposals SET status='completed', offer=? WHERE id=?", (json.dumps(offer), token))


async def request(offer, *, propose_only=False):
    live = not propose_only and sys.stdin.isatty()
    now = datetime.now()
    expires = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()).timestamp()
    live_until = min(expires, time.time() + 40) if live else 0
    token = secrets.token_hex(8)
    with _db() as db:
        db.execute("INSERT INTO proposals VALUES (?, ?, ?, ?, 'pending')",
                   (token, json.dumps(offer, ensure_ascii=False), expires, live_until))
    print(f"[guaranteed] Proposal {token}\n{describe(offer)}\n"
          f"Approve exactly this exchange: collect_trades.py --approve-guaranteed {token}\n"
          f"Decline: collect_trades.py --decline-guaranteed {token}\n"
          f"Reselection expires {datetime.fromtimestamp(expires):%Y-%m-%d %H:%M %Z} local time.",
          flush=True)
    if live:
        print(f"[guaranteed] For this lobby type APPROVE {token} or DECLINE {token} within 40 seconds.", flush=True)
        while time.time() < live_until:
            with _db() as db:
                status = _row(db, token)["status"]
            if status == "approved":
                return token
            if status != "pending":
                raise ApprovalRequired(f"Guaranteed proposal {token} is {status}")
            if select.select([sys.stdin], [], [], 0)[0]:
                answer = sys.stdin.readline().strip()
                if answer == f"APPROVE {token}":
                    approve(token)
                    return token
                if answer == f"DECLINE {token}":
                    decline(token)
                    raise ApprovalRequired(f"Guaranteed proposal {token} declined")
            await asyncio.sleep(0.3)
    raise ProposalReady(token)
