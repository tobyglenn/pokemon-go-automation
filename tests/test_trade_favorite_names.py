import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sources import trade_favorite_names as favorites


class TradeFavoriteNamesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / "trade-favorites.json"
        config = patch.object(favorites.config_paths, "default_config", return_value=self.config)
        config.start()
        self.addCleanup(config.stop)

    def test_canonical_roles_work_without_private_config(self):
        self.assertEqual(favorites.label(favorites.LUCKY), "ReceiverLucky")
        self.assertEqual(favorites.names(favorites.HUNDO), ("ReceiverPerfect",))
        self.assertEqual(favorites.role_for("SafeTrash"), "SafeTrash")

    def test_configured_label_and_previous_alias_keep_their_role(self):
        self.config.write_text(json.dumps({
            "labels": {favorites.LUCKY: "Existing Lucky"},
            "aliases": {favorites.LUCKY: ["Old Lucky"]},
        }))
        self.assertEqual(favorites.label(favorites.LUCKY), "Existing Lucky")
        self.assertEqual(favorites.names(favorites.LUCKY), ("Existing Lucky", "ReceiverLucky", "Old Lucky"))
        self.assertEqual(favorites.role_for("old lucky"), favorites.LUCKY)

    def test_ambiguous_private_names_are_rejected(self):
        self.config.write_text(json.dumps({
            "labels": {favorites.LUCKY: "My Favorite", favorites.HUNDO: "MyFavorite"},
        }))
        with self.assertRaisesRegex(ValueError, "distinct"):
            favorites.label(favorites.LUCKY)
