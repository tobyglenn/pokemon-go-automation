"""Setuptools hooks with anonymous, reproducible source-archive metadata."""

from __future__ import annotations

import gzip
import os
from pathlib import Path
import tarfile
import tempfile

def _call_setuptools(hook, *arguments):
    # Only the isolated builder needs setuptools. Metadata regression tests can
    # import the normalization helper using the standard library alone.
    from setuptools import build_meta
    return getattr(build_meta, hook)(*arguments)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return _call_setuptools("build_wheel", wheel_directory, config_settings, metadata_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _call_setuptools("build_editable", wheel_directory, config_settings, metadata_directory)


def get_requires_for_build_wheel(config_settings=None):
    return _call_setuptools("get_requires_for_build_wheel", config_settings)


def get_requires_for_build_sdist(config_settings=None):
    return _call_setuptools("get_requires_for_build_sdist", config_settings)


def get_requires_for_build_editable(config_settings=None):
    return _call_setuptools("get_requires_for_build_editable", config_settings)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _call_setuptools("prepare_metadata_for_build_wheel", metadata_directory, config_settings)


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return _call_setuptools("prepare_metadata_for_build_editable", metadata_directory, config_settings)


def normalize_sdist(path: Path) -> None:
    """Remove builder identities and original timestamps from archive headers.

    Members stream between compressed archives without writing extracted files
    to disk. Gzip receives no original filename header.
    SOURCE_DATE_EPOCH can supply a release timestamp; otherwise use epoch zero.
    """
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    if epoch < 0 or epoch > 0xFFFFFFFF:
        raise ValueError("SOURCE_DATE_EPOCH must fit an unsigned 32-bit timestamp")
    mode = path.stat().st_mode & 0o777
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".public-sdist-", suffix=".tar.gz", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            with tarfile.open(path, "r:gz") as source:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=epoch) as compressed:
                    with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as target:
                        for member in source.getmembers():
                            member.uid = 0
                            member.gid = 0
                            member.uname = "root"
                            member.gname = "root"
                            member.mtime = epoch
                            member.pax_headers = {}
                            stream = source.extractfile(member) if member.isfile() else None
                            try:
                                target.addfile(member, stream)
                            finally:
                                if stream is not None:
                                    stream.close()
        temporary.chmod(mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_sdist(sdist_directory, config_settings=None):
    filename = _call_setuptools("build_sdist", sdist_directory, config_settings)
    normalize_sdist(Path(sdist_directory) / filename)
    return filename
