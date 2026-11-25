"""Share one Appium session between a local starter and its sequential iPhone workers."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import ios_wda_cleanup


def _key(udid: str) -> str:
    return "POGO_STARTER_IOS_" + udid.encode().hex()


def directory(udid: str) -> Path | None:
    value = os.environ.get(_key(udid))
    return Path(value) if value else None


def _client(record):
    # Import only on iOS hosts. Appium initializes its normal command mappings
    # while start_session binds this client to the session starter already owns.
    from appium import webdriver
    from appium.options.ios import XCUITestOptions

    class AttachedDriver(webdriver.Remote):
        def start_session(self, capabilities, browser_profile=None):
            self.session_id = record["id"]
            self.caps = record["caps"]

        def quit(self):
            self.command_executor.close()

        def end_session(self):
            super().quit()

    return AttachedDriver(command_executor=record["server"], options=XCUITestOptions())


def _end(record):
    from selenium.common.exceptions import InvalidSessionIdException

    try:
        _client(record).end_session()
    except InvalidSessionIdException:
        pass  # Appium already expired this exact session.


@contextmanager
def reserve(spec):
    """Keep the public device lock until teardown. Children still lock each step."""
    from . import pokemon_fleet

    key = _key(spec.identifier)
    if key in os.environ:
        raise RuntimeError(f"{spec.name} already belongs to a starter run")
    with (
        tempfile.TemporaryDirectory(prefix="pogo-starter-") as tmp,
        pokemon_fleet.acquire_device_locks([spec], reserve_for=Path(tmp)),
    ):
        record_path = Path(tmp) / "session.json"
        os.environ[key] = tmp
        try:
            yield
        finally:
            try:
                if record_path.exists():
                    _end(json.loads(record_path.read_text()))
            finally:
                del os.environ[key]


def connect(udid, server_url, create):
    """Called under this phone's step lock, including in gift and battle workers."""
    folder = directory(udid)
    if folder is None:
        return create()
    from selenium.common.exceptions import InvalidSessionIdException, WebDriverException
    from urllib3.exceptions import NewConnectionError, MaxRetryError

    path = folder / "session.json"
    server = server_url.rstrip("/")
    if path.exists():
        record = json.loads(path.read_text())
        if record["server"] != server:
            raise RuntimeError("Starter iPhone workers must use the same Appium server")
        client = _client(record)
        try:
            try:
                client.get_window_rect()
            except (NewConnectionError, MaxRetryError):
                # Appium itself went away. Check the same session again after
                # bringing the configured server back before creating another.
                from . import pokemon_fleet
                pokemon_fleet.ensure_appium_server(server)
                client.get_window_rect()
        except WebDriverException as exc:
            client.quit()
            message = str(exc).lower()
            lost_runner = any(reason in message for reason in (
                "socket hang up", "econnrefused", "wda process is not running",
                "webdriveragent is not running", "instrumentation process is not running",
            ))
            if not (isinstance(exc, InvalidSessionIdException)
                    or ios_wda_cleanup.stale_wda_error(exc) or lost_runner):
                raise
            _end(record)
            path.unlink()
            print("Starter iPhone session expired or lost UI access; reconnecting", flush=True)
        except BaseException:
            client.quit()
            raise
        else:
            return client

    # Use the worker's existing startup, including normal iOS authorization.
    driver = create()
    try:
        record = {"id": driver.session_id, "caps": driver.capabilities, "server": server}
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps(record))
        pending.replace(path)
    except BaseException:
        driver.quit()
        raise
    driver.command_executor.close()
    print("Opened shared starter iPhone session", flush=True)
    return _client(record)
