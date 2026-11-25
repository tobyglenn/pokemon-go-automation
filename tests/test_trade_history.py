from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
import json

from tests import support as _test_support  # noqa: F401
from sources import trade_history, trading_accounts


class CrossHostTradeHistoryTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.hosts = {"first": {"host": "first"}, "second": {"host": "second"}}
        self.aliases = mock.patch.object(trading_accounts, "aliases", return_value={"spare": "receiver"})
        self.peer = mock.patch.object(trade_history, "_peer_pending", side_effect=self.remote)
        for patch in (self.aliases, self.peer):
            patch.start()
            self.addCleanup(patch.stop)

    @contextmanager
    def coordinator(self, host):
        with mock.patch.object(trade_history.config_paths, "state_dir", return_value=self.root / host):
            yield

    def remote(self, host, _config, action, names, intent):
        directory = self.root / host / "trade" / "pending-confirmations"
        trade_history._pending_action(action, names, intent, directory=directory)

    def claim(self, host, names):
        return trade_history.PendingConfirmation(names, {"pair": list(names)}, routes=(host, self.hosts))

    def test_moving_handset_or_switching_coordinator_cannot_replay(self):
        original = self.claim("first", ["receiver", "donor"])
        with self.coordinator("first"):
            original.reserve()
            original.update(receipts={"receiver": {"lucky": None}})
        with self.coordinator("second"):
            with self.assertRaisesRegex(trade_history.ConfirmationStateError, "prior confirmation"):
                self.claim("second", ["spare", "different-donor"]).reserve()
            paths = trade_history.pending_files(["receiver", "spare"])
            for path in paths:
                record = json.loads(path.read_text())
                self.assertEqual(record["transaction_id"], original.intent["transaction_id"])
                self.assertIsNone(record["receipts"]["receiver"]["lucky"])
        with self.coordinator("first"):
            with original.recording():
                trade_history.record_fleet_trade(["receiver", "donor"])
            event, = (self.root / "first" / "trade").glob("history-*.jsonl")
            self.assertEqual(json.loads(event.read_text())["id"], original.intent["transaction_id"])
            original.release()
        with self.coordinator("second"):
            self.claim("second", ["spare", "different-donor"]).check()

    def test_peer_failure_cannot_leave_an_unused_local_claim(self):
        reservation = self.claim("first", ["receiver", "donor"])
        with self.coordinator("first"), mock.patch.object(
            trade_history, "_peer_pending", side_effect=trade_history.ConfirmationStateError("peer offline")
        ):
            with self.assertRaisesRegex(trade_history.ConfirmationStateError, "peer offline"):
                reservation.reserve()
            self.assertFalse(any(path.exists() for path in trade_history.pending_files(reservation.names)))

    def test_legacy_peer_marker_prevents_claim_from_older_workflow(self):
        folder = self.root / "second" / "trade" / "pending-confirmations"
        folder.mkdir(parents=True)
        legacy, = trade_history.pending_files(["spare"], folder)
        legacy.write_text(json.dumps({"pair": ["spare", "donor"]}))
        with self.coordinator("first"):
            reservation = self.claim("first", ["receiver", "different-donor"])
            with self.assertRaisesRegex(trade_history.ConfirmationStateError, "prior confirmation"):
                reservation.reserve()
            self.assertFalse(any(path.exists() for path in trade_history.pending_files(reservation.names)))
        self.assertTrue(legacy.exists())
