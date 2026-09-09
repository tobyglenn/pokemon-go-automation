from __future__ import annotations

from tests import support as _test_support

import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import yaml
from PIL import Image, ImageDraw

from sources import excellent_throw_ios as thrower


VIEWPORT = (375, 667)


def synthetic_ball(radius: int) -> Image.Image:
    image = Image.new("RGB", VIEWPORT, (65, 145, 75))
    draw = ImageDraw.Draw(image)
    center = (VIEWPORT[0] // 2, round(VIEWPORT[1] * 0.81))
    box = (
        center[0] - radius,
        center[1] - radius,
        center[0] + radius,
        center[1] + radius,
    )
    draw.ellipse(box, fill=(235, 235, 235), outline=(20, 20, 20), width=5)
    draw.pieslice(box, 180, 360, fill=(220, 35, 55))
    draw.rectangle((center[0] - radius, center[1] - 3, center[0] + radius, center[1] + 3), fill=(15, 15, 15))
    return image


def _stream_frame(image: Image.Image, timestamp: float) -> thrower.StreamFrame:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    return thrower.StreamFrame(timestamp, buffer.getvalue())


def marked_target(target_radius: int, ring_radius: int) -> Image.Image:
    """A white target circle with a coloured one inside it, at any size.

    The shape the tracker reads.  At 49/20 it is the real catch circle; at
    15/8 it is what a Pokemon's own markings offer on a frame with no ring
    drawn, and the tracker cannot tell them apart by anything but size.
    """
    image = Image.new("RGB", VIEWPORT, (40, 90, 45))
    draw = ImageDraw.Draw(image)
    center_x, center_y = 187, 330
    draw.ellipse(
        (
            center_x - target_radius,
            center_y - target_radius,
            center_x + target_radius,
            center_y + target_radius,
        ),
        outline=(235, 235, 235),
        width=3,
    )
    draw.ellipse(
        (
            center_x - ring_radius,
            center_y - ring_radius,
            center_x + ring_radius,
            center_y + ring_radius,
        ),
        outline=(240, 180, 25),
        width=4,
    )
    return image


def crowded_target(
    target_radius: int, center: tuple[int, int], blob_radius: int = 12
) -> Image.Image:
    """One real ring, plus the swarm of small blobs a species' face offers.

    The ring sits deliberately off the coarse search grid, which steps 8 in x
    and 6 in y, so it can only be reached by refining a nearby seed.
    """
    image = Image.new("RGB", VIEWPORT, (40, 90, 45))
    draw = ImageDraw.Draw(image)
    center_x, center_y = center
    draw.ellipse(
        (
            center_x - target_radius,
            center_y - target_radius,
            center_x + target_radius,
            center_y + target_radius,
        ),
        outline=(235, 235, 235),
        width=3,
    )
    for offset_x, offset_y in ((-14, -18), (-4, -22), (8, -16), (-16, -6), (10, -4)):
        blob_x, blob_y = center_x + offset_x, center_y + offset_y
        draw.ellipse(
            (
                blob_x - blob_radius,
                blob_y - blob_radius,
                blob_x + blob_radius,
                blob_y + blob_radius,
            ),
            outline=(245, 245, 245),
            width=3,
        )
    return image


def synthetic_ring(
    center_y: int, radius: int, outer_radius: int = 50, center_x: int = 187
) -> Image.Image:
    image = Image.new("RGB", VIEWPORT, (40, 90, 45))
    draw = ImageDraw.Draw(image)
    outer_box = (
        center_x - outer_radius,
        center_y - outer_radius,
        center_x + outer_radius,
        center_y + outer_radius,
    )
    draw.ellipse(outer_box, outline=(225, 225, 225), width=3)
    box = (
        center_x - radius,
        center_y - radius,
        center_x + radius,
        center_y + radius,
    )
    draw.ellipse(box, outline=(175, 250, 25), width=4)
    return image


class EncounterGuardTests(unittest.TestCase):
    def test_large_encounter_ball_is_accepted(self) -> None:
        self.assertTrue(thrower.is_encounter(synthetic_ball(52)))

    def test_small_map_ball_is_rejected(self) -> None:
        self.assertFalse(thrower.is_encounter(synthetic_ball(23)))

    def test_white_encounter_transition_is_rejected(self) -> None:
        self.assertFalse(thrower.is_encounter(Image.new("RGB", VIEWPORT, "white")))

    def test_ball_center_is_measured_for_touch_down(self) -> None:
        detection = thrower.locate_throw_ball(synthetic_ball(52), VIEWPORT)
        self.assertIsNotNone(detection)
        assert detection is not None
        self.assertAlmostEqual(detection.center_x, 187, delta=3)
        self.assertAlmostEqual(detection.center_y, 540, delta=4)

    def test_tall_phone_ball_bounce_stays_in_same_encounter(self) -> None:
        prior = thrower.BallDetection(1.0, 220, 768, 80)
        bounced = thrower.BallDetection(1.0, 220, 891, 88)
        self.assertTrue(thrower.same_encounter_ball(prior, bounced, (440, 956)))

    def test_different_ball_geometry_breaks_encounter_stability(self) -> None:
        prior = thrower.BallDetection(1.0, 220, 850, 82)
        shifted = thrower.BallDetection(1.0, 260, 850, 55)
        self.assertFalse(thrower.same_encounter_ball(prior, shifted, (440, 956)))


class RingAnalysisTests(unittest.TestCase):
    def test_sequence_finds_changing_catch_circle(self) -> None:
        frames = [synthetic_ring(330, radius) for radius in (42, 34, 27, 20, 14, 42)]
        lock = thrower.analyze_ring_sequence(frames, VIEWPORT)
        self.assertAlmostEqual(lock.center_y, 330, delta=3)
        self.assertGreaterEqual(lock.maximum_radius, 40)
        self.assertGreater(lock.current_radius, 35)

    def test_high_off_center_flying_target_is_measured(self) -> None:
        frames = [
            synthetic_ring(135, radius, outer_radius=47, center_x=172)
            for radius in (36, 30, 24, 18, 13)
        ]
        lock = thrower.analyze_ring_sequence(frames, VIEWPORT)
        self.assertAlmostEqual(lock.center_x, 172, delta=3)
        self.assertAlmostEqual(lock.center_y, 135, delta=3)
        self.assertAlmostEqual(lock.maximum_radius, 47, delta=4)

    def test_fast_tracker_reads_live_locked_ring(self) -> None:
        prior = thrower.RingLock(172, 135, 47, 30, 0.8)
        measured = thrower.fast_current_ring(
            synthetic_ring(135, 12, outer_radius=47, center_x=172),
            prior,
            VIEWPORT,
        )
        self.assertIsNotNone(measured)
        assert measured is not None
        self.assertAlmostEqual(measured.ratio, 12 / 47, delta=0.06)

    def test_centered_white_target_does_not_require_ring_component(self) -> None:
        image = Image.new("RGB", VIEWPORT, (40, 90, 45))
        draw = ImageDraw.Draw(image)
        draw.ellipse((150, 293, 224, 367), outline=(225, 225, 225), width=3)
        targets = thrower.target_candidates(image, viewport=VIEWPORT, limit=3)
        self.assertTrue(targets)
        self.assertAlmostEqual(targets[0].center_x, 187, delta=4)
        self.assertAlmostEqual(targets[0].center_y, 330, delta=4)

    def test_small_markings_do_not_crowd_out_the_ring_that_is_there(self) -> None:
        """The android-one failure: a ring scoring higher than anything else, lost.

        Every one of the six refinement seeds landed on the Pokemon's head, so
        the eight returned candidates were all 10-25px blobs.  The caller threw
        them away for being under the radius floor and reported no ring at all,
        while the real 41px ring sat on the frame the whole time.  Asking for
        the floor up front is what keeps it.
        """
        image = crowded_target(44, (188, 331))
        floor = round(VIEWPORT[0] * thrower.MINIMUM_TARGET_RADIUS_WIDTH)

        targets = thrower.target_candidates(
            image, viewport=VIEWPORT, limit=8, minimum_radius=floor
        )

        self.assertTrue(targets, "the ring on the frame came back as nothing")
        self.assertAlmostEqual(targets[0].center_x, 188, delta=4)
        self.assertAlmostEqual(targets[0].center_y, 331, delta=4)
        self.assertAlmostEqual(targets[0].radius, 44, delta=4)

    def test_the_floor_is_never_below_the_detectors_own_smallest(self) -> None:
        image = crowded_target(44, (188, 331))

        targets = thrower.target_candidates(
            image, viewport=VIEWPORT, limit=8, minimum_radius=4
        )

        self.assertTrue(all(target.radius >= 8 for target in targets))

    def test_a_distant_arc_does_not_win_over_the_ring(self) -> None:
        """What spreading the seeds buys, on top of the radius floor.

        With the floor alone the android-two locked onto a bright arc near the top
        of the frame instead of the ring around the Pokemon.  Six seeds bunched
        on that arc left nothing looking anywhere else.
        """
        image = crowded_target(44, (188, 331))
        draw = ImageDraw.Draw(image)
        draw.arc((140, 30, 236, 126), start=200, end=340, fill=(250, 250, 250), width=4)
        floor = round(VIEWPORT[0] * thrower.MINIMUM_TARGET_RADIUS_WIDTH)

        targets = thrower.target_candidates(
            image, viewport=VIEWPORT, limit=8, minimum_radius=floor
        )

        self.assertTrue(targets)
        self.assertAlmostEqual(targets[0].center_y, 331, delta=6)

    def test_a_ring_free_frame_does_not_seed_on_the_pokemons_markings(self) -> None:
        """The newest frame of a hold usually has no ring left to read.

        `target_candidates` still answers it, with 9-25px blobs off the
        species' own face and leaves, and those blobs carry a smaller coloured
        component inside them, so a reading comes back.  A live SE run measured
        8 over 15 that way -- the numbers this synthetic reproduces -- and the
        next hold's candidate filter then held it to that junk maximum_radius
        for the rest of the attempt: thirty holds, never in the band, four
        Great Balls spent on flicks.  The real 49px ring was on the frames
        immediately before it the whole time.
        """
        frames = [
            _stream_frame(marked_target(49, 20), 1.0),
            _stream_frame(marked_target(15, 8), 2.0),
        ]
        measured = thrower.measure_ring(frames, None, VIEWPORT)
        self.assertIsNotNone(measured)
        assert measured is not None
        self.assertAlmostEqual(measured.maximum_radius, 48, delta=4)
        self.assertAlmostEqual(measured.ratio, 18 / 48, delta=0.08)

    def test_the_gate_is_radius_alone_because_score_does_not_separate(self) -> None:
        """Junk scores as high as the real ring, so score cannot be the gate.

        Over 168 captured hold frames the markings reached 1.06 while the true
        ring dropped to 0.83.  Radius is the whole of the separation: 9-25
        against 45-54.
        """
        junk = thrower.target_candidates(marked_target(15, 8), viewport=VIEWPORT, limit=1)
        real = thrower.target_candidates(marked_target(49, 20), viewport=VIEWPORT, limit=1)
        self.assertTrue(junk and real)
        self.assertGreaterEqual(junk[0].score, real[0].score * 0.9)
        self.assertLess(
            junk[0].radius, VIEWPORT[0] * thrower.MINIMUM_TARGET_RADIUS_WIDTH
        )
        self.assertGreaterEqual(
            real[0].radius, VIEWPORT[0] * thrower.MINIMUM_TARGET_RADIUS_WIDTH
        )

    def test_coloured_arcs_without_white_target_are_refused(self) -> None:
        frames: list[Image.Image] = []
        for radius in (42, 34, 27, 20, 14):
            image = Image.new("RGB", VIEWPORT, (40, 90, 45))
            draw = ImageDraw.Draw(image)
            draw.ellipse(
                (187 - radius, 250 - radius, 187 + radius, 250 + radius),
                outline=(240, 180, 25),
                width=4,
            )
            frames.append(image)
        with self.assertRaisesRegex(thrower.ExcellentThrowError, "white target"):
            thrower.analyze_ring_sequence(frames, VIEWPORT)

    def test_curve_path_spins_then_releases_right_for_clockwise_curve(self) -> None:
        ball = thrower.BallDetection(0.9, 187, 540, 52)
        lock = thrower.RingLock(187, 330, 50, 15, 0.9)
        config = SimpleNamespace(
            spin_radius_width=0.065,
            spin_turns=2.0,
            spin_segment_ms=24,
            curve_direction="clockwise",
            curve_offset_width=0.22,
            end_offset_height=0.045,
            throw_duration_ms=140,
        )
        start, path = thrower.curve_throw_path(ball, lock, VIEWPORT, config)
        self.assertEqual(start, [187, 540])
        self.assertGreaterEqual(len(path), 5)
        # Clockwise curves left, so flick aims right (187 + 82 = 269)
        self.assertEqual(path[-1], (269, 300, 140))
        self.assertGreater(max(x for x, _, _ in path[:-1]), 187)
        self.assertLess(min(x for x, _, _ in path[:-1]), 187)

    def test_curve_path_spins_then_releases_left_for_counterclockwise_curve(self) -> None:
        ball = thrower.BallDetection(0.9, 187, 540, 52)
        lock = thrower.RingLock(187, 330, 50, 15, 0.9)
        config = SimpleNamespace(
            spin_radius_width=0.065,
            spin_turns=2.0,
            spin_segment_ms=24,
            curve_direction="counterclockwise",
            curve_offset_width=0.22,
            end_offset_height=0.045,
            throw_duration_ms=140,
        )
        start, path = thrower.curve_throw_path(ball, lock, VIEWPORT, config)
        self.assertEqual(start, [187, 540])
        # Counterclockwise curves right, so flick aims left (187 - 82 = 105)
        self.assertEqual(path[-1], (105, 300, 140))

    def test_straight_throw_aims_at_measured_circle_center(self) -> None:
        ball = thrower.BallDetection(0.9, 187, 540, 52)
        lock = thrower.RingLock(172, 330, 50, 15, 0.9)
        config = SimpleNamespace(end_offset_height=0.045)
        start, end = thrower.straight_throw_points(ball, lock, VIEWPORT, config)
        self.assertEqual(start, [187, 540])
        self.assertEqual(end, [172, 315])

    def test_attack_motion_score_ignores_still_frame_and_sees_lunge(self) -> None:
        still = Image.new("RGB", VIEWPORT, (45, 115, 60))
        draw = ImageDraw.Draw(still)
        draw.ellipse((145, 285, 229, 375), fill=(220, 170, 90))
        attack = Image.new("RGB", VIEWPORT, (45, 115, 60))
        draw = ImageDraw.Draw(attack)
        draw.ellipse((125, 255, 249, 395), fill=(220, 170, 90))
        lock = thrower.RingLock(187, 330, 50, 13, 0.9)
        self.assertEqual(
            thrower.pokemon_motion_score(still, still, lock, VIEWPORT), 0.0
        )
        self.assertGreater(
            thrower.pokemon_motion_score(still, attack, lock, VIEWPORT), 0.04
        )

    def test_target_tracker_follows_moving_pokemon_center(self) -> None:
        prior = Image.new("RGB", VIEWPORT, (45, 115, 60))
        current = Image.new("RGB", VIEWPORT, (45, 115, 60))
        ImageDraw.Draw(prior).ellipse((92, 142, 108, 158), fill=(225, 170, 80))
        ImageDraw.Draw(current).ellipse((104, 148, 120, 164), fill=(225, 170, 80))
        center = thrower.track_target_center(prior, current, 100, 150, 45, VIEWPORT)
        self.assertAlmostEqual(center[0], 112, delta=2)
        self.assertAlmostEqual(center[1], 156, delta=2)


class ConfigTests(unittest.TestCase):
    def test_loads_device_and_tuning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ios.yaml").write_text(
                yaml.safe_dump(
                    {
                        "server_url": "http://127.0.0.1:4723",
                        "device": {
                            "udid": "UDID",
                            "team_id": "TEAM",
                            "wda_bundle_id": "WDA",
                            "mjpeg_server_port": 9100,
                        },
                    }
                )
            )
            (root / "throw.yaml").write_text(
                yaml.safe_dump(
                    {
                        "appium_config": "ios.yaml",
                        "ring": {"target_ratio_min": 0.24, "target_ratio_max": 0.36},
                        "throw": {"duration_ms": 140},
                    }
                )
            )
            config = thrower.load_runtime_config(root / "throw.yaml")
            self.assertEqual(config.ios_device["udid"], "UDID")
            self.assertEqual(config.throw_duration_ms, 140)
            self.assertEqual(config.curve_direction, "clockwise")
            self.assertEqual(config.curve_offset_width, 0.09)
            self.assertEqual(config.attack_motion_threshold, 0.04)
            self.assertEqual(config.max_ring_attempts, 80)

    def test_default_watcher_has_no_throw_limit(self) -> None:
        self.assertIsNone(thrower.parse_args([]).throws)

    def test_config_is_not_chosen_until_a_phone_is_read(self) -> None:
        self.assertIsNone(thrower.parse_args([]).config)


def write_profile_pair(root: Path, name: str, udid: str) -> Path:
    (root / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "server_url": "http://127.0.0.1:4723",
                "device": {
                    "udid": udid,
                    "team_id": "TEAM",
                    "wda_bundle_id": "WDA",
                    "mjpeg_server_port": 9100,
                },
            }
        )
    )
    throw_path = root / f"excellent-throw-{name}.yaml"
    throw_path.write_text(yaml.safe_dump({"appium_config": f"{name}.yaml"}))
    return throw_path


class ThrowWindowTests(unittest.TestCase):
    config = SimpleNamespace(attack_quiet_threshold=0.025, attack_motion_threshold=0.040)

    def state(self, score: float, offset: int) -> str:
        return thrower.throw_window_state(score, offset, 26, self.config)

    def test_centered_still_target_is_ready(self) -> None:
        self.assertEqual(self.state(0.004, 5), "ready")

    def test_still_but_off_center_target_waits(self) -> None:
        self.assertEqual(self.state(0.004, 60), "still")

    def test_attacking_target_is_never_ready(self) -> None:
        self.assertEqual(self.state(0.052, 0), "attacking")

    def test_idle_sway_is_restless_rather_than_ready(self) -> None:
        self.assertEqual(self.state(0.030, 0), "restless")

    def test_tolerance_edge_counts_as_centered(self) -> None:
        self.assertEqual(self.state(0.004, 26), "ready")


class RingSizingTests(unittest.TestCase):
    def test_first_hold_teaches_the_shrink_rate(self) -> None:
        # Full circle down to 0.60 over a 0.20s hold.
        self.assertAlmostEqual(
            thrower.updated_shrink_rate(1.0, 0.60, 0.20, None), 2.0, places=3
        )

    def test_a_circle_that_reset_to_full_does_not_teach_a_rate(self) -> None:
        self.assertEqual(thrower.updated_shrink_rate(0.20, 0.95, 0.20, 3.0), 3.0)

    def test_next_hold_asks_for_exactly_the_shortfall(self) -> None:
        # 0.60 now, band midpoint 0.24, losing 2.0 ratio per second of holding.
        self.assertAlmostEqual(
            thrower.next_hold_seconds(0.60, 0.24, 2.0, 0.08), 0.18, places=3
        )

    def test_overshoot_steps_in_small_holds_until_the_reset(self) -> None:
        self.assertEqual(thrower.next_hold_seconds(0.10, 0.24, 2.0, 0.08), 0.08)

    def test_unknown_rate_steps_instead_of_guessing(self) -> None:
        self.assertEqual(thrower.next_hold_seconds(0.90, 0.24, None, 0.08), 0.08)

    def test_a_hold_is_never_longer_than_the_cap(self) -> None:
        self.assertEqual(
            thrower.next_hold_seconds(0.99, 0.24, 0.1, 0.08),
            thrower.MAXIMUM_HOLD_SECONDS,
        )


class ConfigSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.se = write_profile_pair(self.root, "se", "UDID-SE")
        self.main = write_profile_pair(self.root, "main", "UDID-MAIN")

    def select_with(self, attached: dict[str, str]) -> Path:
        return self.select_configs_with(attached)[0]

    def select_configs_with(self, attached: dict[str, str]) -> list[Path]:
        original_dirs = thrower.config_paths.search_dirs
        original_devices = thrower.ios_attached_devices.attached_devices
        thrower.config_paths.search_dirs = lambda relative_to=None: [self.root]
        thrower.ios_attached_devices.attached_devices = lambda: attached
        try:
            return thrower.select_configs()
        finally:
            thrower.config_paths.search_dirs = original_dirs
            thrower.ios_attached_devices.attached_devices = original_devices

    def test_plugged_in_phone_picks_its_own_profile(self) -> None:
        self.assertEqual(self.select_with({"UDID-MAIN": "USB"}), self.main.resolve())
        self.assertEqual(self.select_with({"UDID-SE": "USB"}), self.se.resolve())

    def test_wifi_only_phone_is_not_treated_as_attached(self) -> None:
        with self.assertRaises(thrower.ExcellentThrowError) as caught:
            self.select_with({"UDID-SE": "Network"})
        self.assertIn("UDID-SE over Network", str(caught.exception))

    def test_no_phone_asks_for_a_cable(self) -> None:
        with self.assertRaises(thrower.ExcellentThrowError) as caught:
            self.select_with({})
        self.assertIn("data cable", str(caught.exception))

    def test_two_attached_phones_select_both_profiles(self) -> None:
        selected = self.select_configs_with({"UDID-SE": "USB", "UDID-MAIN": "USB"})
        self.assertEqual(set(selected), {self.se.resolve(), self.main.resolve()})

    def test_unreadable_profile_is_skipped_rather_than_fatal(self) -> None:
        (self.root / "excellent-throw-broken.yaml").write_text("appium_config: [")
        self.assertEqual(self.select_with({"UDID-MAIN": "USB"}), self.main.resolve())



if __name__ == "__main__":
    unittest.main()
