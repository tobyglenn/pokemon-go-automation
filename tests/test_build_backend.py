"""Synthetic archive metadata tests; no real builder identity is used."""

from __future__ import annotations

from tests import support as _test_support

import gzip
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from scripts import build_backend


class SourceArchiveTests(unittest.TestCase):
    def make_archive(self, path: Path, owner: str) -> None:
        with path.open("wb") as output:
            with gzip.GzipFile(filename="fixture-build-name.tar", mode="wb", fileobj=output, mtime=123) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    member = tarfile.TarInfo("package/README.md")
                    payload = b"Public source contents.\n"
                    member.size = len(payload)
                    member.uid = 4321
                    member.gid = 8765
                    member.uname = owner
                    member.gname = "fixture-group"
                    member.mtime = 9876
                    member.pax_headers = {"mtime": "9876.123", "comment": "fixture metadata"}
                    archive.addfile(member, io.BytesIO(payload))

    def test_metadata_removed_and_contents_preserved(self):
        with tempfile.TemporaryDirectory(prefix="archive-test-") as directory:
            path = Path(directory) / "source.tar.gz"
            self.make_archive(path, "fixture-owner")
            with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "0"}):
                build_backend.normalize_sdist(path)
            header = path.read_bytes()[:10]
            self.assertEqual(header[3], 0, "gzip must not retain a filename or comment")
            self.assertEqual(int.from_bytes(header[4:8], "little"), 0)
            with tarfile.open(path, "r:gz") as archive:
                member = archive.getmembers()[0]
                self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, "root", "root"))
                self.assertEqual(member.mtime, 0)
                self.assertEqual(member.pax_headers, {})
                self.assertEqual(archive.extractfile(member).read(), b"Public source contents.\n")

    def test_different_builder_identities_produce_identical_archives(self):
        with tempfile.TemporaryDirectory(prefix="archive-test-") as directory:
            first = Path(directory) / "first.tar.gz"
            second = Path(directory) / "second.tar.gz"
            self.make_archive(first, "fixture-one")
            self.make_archive(second, "fixture-two")
            with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "321"}):
                build_backend.normalize_sdist(first)
                build_backend.normalize_sdist(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_standard_build_hook_normalizes_the_setuptools_output(self):
        with tempfile.TemporaryDirectory(prefix="archive-test-") as directory:
            path = Path(directory) / "package.tar.gz"
            self.make_archive(path, "fixture-owner")
            with mock.patch.object(build_backend, "_call_setuptools", return_value=path.name) as delegated:
                with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "0"}):
                    result = build_backend.build_sdist(directory, {"example": "value"})
            delegated.assert_called_once_with("build_sdist", directory, {"example": "value"})
            self.assertEqual(result, path.name)
            with tarfile.open(path, "r:gz") as archive:
                self.assertEqual(archive.getmembers()[0].uname, "root")


if __name__ == "__main__":
    unittest.main()
