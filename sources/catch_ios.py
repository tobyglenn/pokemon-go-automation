"""An iPhone for the award-queue and research-claim loops.

`catch_awarded_android` and `claim_research_android` were written against an
Android phone: a frame and a tap share one coordinate space, and a throw is
`excellent_throw_android.run_once`.  On an iPhone the screenshot is in pixels
and WebDriverAgent taps in logical points, so this wrapper takes the loops'
pixel coordinates and converts them itself.  The loops never learn which kind
of phone they are driving.

The session and the throw are the ones a GO Battle League reward catch already
uses on the SE: `gbl_ios.IOSGBLDevice` for the connection, the excellent-throw
routine while it will lock the circle, and the stepped flick once it refuses.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from PIL import Image

from . import excellent_throw_android, excellent_throw_ios, gbl_ios, pokemon_fleet

# As on Android: an encounter counts as loaded after two agreeing reads, or one
# when the caller already knows a Pokemon is standing there.
ENCOUNTER_POLL = 0.5
# `excellent_throw_android.wait_for_throw_result`'s timings.  A ball that comes
# back inside the window means the Pokemon broke out; one that stays gone means
# the encounter is over, caught or fled.
RESULT_SETTLE = 2.2
RESULT_WINDOW = 6.0
RESULT_POLL = 0.3
# GBL's flick falls short on the SE: eleven balls at a CP 450 Eevee on
# 24 Sep 2026 all dropped in front of it.  Its 0.46h is only ~307 points on
# the SE's 667-point screen, and a throw's power is its speed, so this one
# rises further in less time -- ~410 points in 200ms.
FLICK_RISE = 0.62
FLICK_STEP_MS = 20


class _CatchScreens:
    """What `excellent_throw_ios.clear_catch_screens` needs of a phone."""

    def __init__(self, phone: "IOSCatchPhone") -> None:
        self.label = phone.label
        self.viewport = phone.logical
        self.screenshot = phone.session._screenshot
        self.tap = phone.session.tap


class IOSCatchPhone:
    platform = "ios"

    def __init__(self, session: gbl_ios.IOSGBLDevice, name: str, frame_size: tuple[int, int]) -> None:
        self.session = session
        self.label = name
        session.label = name
        self.logical = (int(session.viewport["width"]), int(session.viewport["height"]))
        # The loops work in the frame's own pixels, so that is the viewport
        # they are shown.  Every touch is scaled back to points on the way out.
        self.viewport = frame_size
        self.thrower: gbl_ios.ExcellentThrower | None = None
        # Set once the excellent routine has refused.  The SE's circle does not
        # sit concentric with its target ring, so a refusal is a property of the
        # phone rather than of one Pokemon, and asking again costs a minute and
        # a berry per throw to reach the same answer.
        self.flicking = False

    @classmethod
    def connect(cls, spec: pokemon_fleet.DeviceSpec) -> "IOSCatchPhone":
        profile = pokemon_fleet.load_appium_profile(spec)
        config = gbl_ios.RuntimeConfig(
            appium_server_url=profile["server_url"],
            ios_device=profile["device"],
            coordinates={},
            delay_modifier=0.0,
            battles=1,
        )
        session = gbl_ios.IOSGBLDevice.connect(config)
        try:
            frame = session._screenshot()
        except Exception:
            session.driver.quit()
            raise
        return cls(session, spec.name, frame.size)

    def close(self) -> None:
        if self.thrower is not None:
            self.thrower.close()
            self.thrower = None
        try:
            self.session.driver.quit()
        except Exception:
            pass

    def _points(self, x: float, y: float) -> list[int]:
        return [
            round(x * self.logical[0] / self.viewport[0]),
            round(y * self.logical[1] / self.viewport[1]),
        ]

    async def capture_frame(self) -> tuple[Image.Image, float]:
        image = await self.session.screenshot()
        if image is None:
            raise gbl_ios.IOSGBLError(f"Could not read {self.label}'s screen")
        self.viewport = image.size
        return image, time.monotonic()

    async def tap(self, point: list[int]) -> None:
        await self.session.tap(self._points(*point))

    async def input_swipe(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int) -> None:
        await self.session.scroll(self._points(x0, y0), self._points(x1, y1), duration_ms / 1000)

    async def press_back(self) -> None:
        # There is no BACK on an iPhone, and the screens this is asked to leave
        # draw their way out somewhere different each time -- the GO Snapshot
        # shutter sits where a close button would.  Nothing is pressed; the
        # caller counts the failure and stops if the screen stays.
        print(f"[{self.label}] No BACK key on an iPhone; leaving this screen alone")

    def ball_visible(self, image: Image.Image) -> bool:
        return excellent_throw_ios.locate_throw_ball(image, self.logical) is not None

    async def clear_catch_screens(self, artifact_dir: Path) -> None:
        await excellent_throw_ios.clear_catch_screens(_CatchScreens(self), artifact_dir)

    async def run_once(
        self,
        *,
        wait_seconds: float | None,
        artifact_dir: Path,
        dry_run: bool,
        use_nanab: bool = False,
        ring_hold: bool = False,
        lone_ball_reading: bool = False,
    ) -> bool:
        """One ball: True when the encounter ended, as the Android routine says.

        `ring_hold` is Android's circle-measuring hold; the excellent routine
        here always measures, so it has nothing to switch.
        """
        del ring_hold
        image = await self.wait_for_encounter(wait_seconds, lone_ball_reading)
        if image is None:
            raise excellent_throw_android.NoEncounterError(
                f"No encounter ball within {wait_seconds or 0:g}s"
            )
        image.save(artifact_dir / "encounter.jpg", "JPEG", quality=72)
        if dry_run:
            print(f"[{self.label}] Dry-run: encounter detected, no touch sent")
            return False
        if not await self.throw(image, artifact_dir, use_nanab):
            # Most often a berry left in hand, which the flick has just swapped
            # back for a ball.  The loop counts it and looks again.
            raise excellent_throw_android.AndroidExcellentThrowError("no ball in hand to throw")
        return await self.throw_result(artifact_dir)

    async def wait_for_encounter(self, wait_seconds: float | None, lone_reading: bool) -> Image.Image | None:
        needed = 1 if lone_reading else 2
        deadline = time.monotonic() + (wait_seconds if wait_seconds is not None else 25.0)
        seen = 0
        while True:
            image, _ts = await self.capture_frame()
            if self.ball_visible(image):
                seen += 1
                if seen >= needed:
                    return image
            else:
                seen = 0
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(ENCOUNTER_POLL)

    async def throw(self, image: Image.Image, artifact_dir: Path, use_nanab: bool) -> bool:
        """The excellent routine while it will lock the circle, then the flick.

        Says whether a ball went.  The excellent routine stays open after it
        refuses a lock, because its Nanab feed is what the flick uses too --
        Android feeds one on every fresh encounter, and the SE had stopped.
        """
        if self.thrower is None and not self.flicking:
            self.thrower = gbl_ios.open_excellent_thrower(self.session)
            if self.thrower is None:
                self.flicking = True
        if self.thrower is not None and not self.flicking:
            directory = artifact_dir / "excellent"
            directory.mkdir(parents=True, exist_ok=True)
            try:
                await excellent_throw_ios.run_once(
                    self.thrower.thrower,
                    self.thrower.stream,
                    wait_seconds=gbl_ios.EXCELLENT_THROW_WAIT,
                    artifact_dir=directory,
                    dry_run=False,
                    use_nanab=use_nanab,
                    berry=self.thrower.berry,
                )
                return True
            except (
                excellent_throw_ios.ExcellentThrowError,
                gbl_ios.WebDriverException,
                OSError,
                ValueError,
            ) as exc:
                print(f"[{self.label}] Excellent throw refused ({exc}); flicking from here on")
                self.flicking = True
                # Whatever it refused on is usually a sheet over the ball, so
                # the flick aims off a fresh frame, not the one from before.
                fresh = await self.session.screenshot()
                if fresh is not None:
                    image = fresh
                # The refused attempt may already have fed this encounter.
                use_nanab = False
        if use_nanab and self.thrower is not None:
            image = await self.feed_nanab(image, artifact_dir)
        return await gbl_ios.throw_ball(
            self.session, image, rise=FLICK_RISE, step_ms=FLICK_STEP_MS
        )

    async def feed_nanab(self, image: Image.Image, artifact_dir: Path) -> Image.Image:
        """Feed a Nanab before a flick; hand back the frame to throw from."""
        thrower = self.thrower.thrower
        ball = excellent_throw_ios.locate_throw_ball(image, thrower.viewport)
        if ball is None:
            return image
        try:
            await excellent_throw_ios.use_nanab_berry(
                thrower, ball, image, artifact_dir, berry=self.thrower.berry
            )
        except (excellent_throw_ios.ExcellentThrowError, gbl_ios.WebDriverException, OSError) as exc:
            print(f"[{self.label}] Nanab not fed ({exc}); throwing without one")
        fresh = await self.wait_for_encounter(8.0, lone_reading=True)
        return image if fresh is None else fresh

    async def throw_result(self, artifact_dir: Path) -> bool:
        await asyncio.sleep(RESULT_SETTLE)
        deadline = time.monotonic() + RESULT_WINDOW
        returned = 0
        last: Image.Image | None = None
        while time.monotonic() < deadline:
            last, _ts = await self.capture_frame()
            if self.ball_visible(last):
                returned += 1
                if returned >= 2:
                    last.save(artifact_dir / "result-encounter-returned.jpg", "JPEG", quality=72)
                    print(f"[{self.label}] Pokemon still in encounter; re-aiming")
                    return False
            else:
                returned = 0
            await asyncio.sleep(RESULT_POLL)
        if last is not None:
            last.save(artifact_dir / "result-encounter-ended.jpg", "JPEG", quality=72)
        print(f"[{self.label}] Encounter ball stayed gone; catch/encounter completed")
        await self.clear_catch_screens(artifact_dir)
        return True
