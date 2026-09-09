"""Synthetic privacy boundaries; these tests never use a real private inventory."""

from __future__ import annotations

from tests import support as _test_support

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_public_tree.py"
SPEC = importlib.util.spec_from_file_location("public_tree_guard", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def synthetic_home_path() -> str:
    # Assemble a fictional test fixture; no personal path is stored in source.
    return "/" + "Users" + "/" + "fixture-owner" + "/project"


class PatternTests(unittest.TestCase):
    def test_generic_hosts_aliases_and_loopback_are_allowed(self):
        for text in (
            "ssh_host: worker.example",
            "ssh_host: YOUR_SSH_ALIAS",
            "controller worker android-one ios-two",
            "http://127.0.0.1:4723",
            "contact: test@example.com",
        ):
            with self.subTest(text=text):
                self.assertEqual(guard.content_problems(text.encode(), []), [])

    def test_private_patterns_are_rejected(self):
        fixtures = (
            synthetic_home_path(),
            ".".join(map(str, (192, 168, 23, 45))),
            "0" * 8 + "-" + "0" * 16,
            "ANDROID" + "123456789012345",
            "-" * 5 + "BEGIN " + "OPENSSH PRIVATE KEY" + "-" * 5,
            "0123" + "9874" + "5678",
        )
        for fixture in fixtures:
            with self.subTest(size=len(fixture)):
                self.assertTrue(guard.content_problems(fixture.encode(), []))

    def test_external_literals_are_matched_without_substring_false_positives(self):
        self.assertTrue(guard.content_problems(b"host: fixture-private-host", ["fixture-private-host"]))
        self.assertEqual(guard.content_problems(b"image.tobytes()", ["tob"]), [])

    def test_runtime_files_and_live_configuration_are_rejected(self):
        for name in ("logs/run.log", "config/device.yaml", "private/data.txt", ".env", "capture.png"):
            with self.subTest(name=name):
                self.assertTrue(guard.path_problems(name))
        self.assertEqual(guard.path_problems("config/device.example.yaml"), [])
        self.assertEqual(guard.path_problems("examples/pokemon-fleet.yaml"), [])

    def test_only_obvious_synthetic_trainer_codes_are_exempt(self):
        self.assertTrue(guard.synthetic_trainer_code("1234 5678 9012"))
        self.assertTrue(guard.synthetic_trainer_code("1111-2222-3333"))
        self.assertFalse(guard.synthetic_trainer_code("0123" + "9874" + "5678"))


class GitBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="public-tree-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q")

    def git(self, *arguments):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
             "-c", "user.name=Privacy Test", "-c", "user.email=test@example.com",
             "-C", str(self.root), *arguments],
            capture_output=True, text=True, check=True,
        )

    def audit(self, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), *arguments],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_empty_index_fails_closed(self):
        self.assertEqual(self.audit().returncode, 2)

    def test_clean_index_passes(self):
        (self.root / "README.md").write_text("Public fixture.\n")
        self.git("add", "README.md")
        self.assertEqual(self.audit().returncode, 0)

    def test_clean_working_copy_cannot_hide_staged_leak(self):
        path = self.root / "README.md"
        path.write_text(synthetic_home_path())
        self.git("add", "README.md")
        path.write_text("Safe working copy.\n")
        self.assertEqual(self.audit().returncode, 1)

    def test_deleted_historical_leak_remains_blocked(self):
        path = self.root / "README.md"
        path.write_text(synthetic_home_path())
        self.git("add", "README.md")
        self.git("commit", "-qm", "Synthetic fixture")
        path.write_text("Safe current version.\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "Remove fixture")
        self.assertEqual(self.audit().returncode, 1)

    def test_commit_messages_are_scanned(self):
        (self.root / "README.md").write_text("Public fixture.\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", synthetic_home_path())
        self.assertEqual(self.audit().returncode, 1)

    def test_symlink_is_rejected_without_following_it(self):
        (self.root / "link").symlink_to("missing-external-file")
        self.git("add", "link")
        self.assertEqual(self.audit().returncode, 1)

    def test_private_identifier_in_filename_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="external-deny-fixture-") as temporary:
            deny = Path(temporary) / "deny.json"
            deny.write_text(json.dumps(["fixture-private-host"]))
            (self.root / "fixture-private-host.md").write_text("Public contents.\n")
            self.git("add", ".")
            self.assertEqual(self.audit("--deny-file", str(deny)).returncode, 1)

    def test_deny_list_cannot_be_placed_inside_the_checkout(self):
        path = self.root / "deny.json"
        path.write_text(json.dumps(["fixture-private-host"]))
        result = self.audit("--working-tree", "--deny-file", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("outside the repository", result.stderr)


if __name__ == "__main__":
    unittest.main()
