from tests import support as _test_support  # noqa: F401

import asyncio
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from sources import catch_awarded_android, catch_rewards, excellent_throw_android, legendary_pokemon, throw_tuning


def fake_device(label="test-phone", config=None):
    return excellent_throw_android.AndroidDevice(
        mock.Mock(shell=mock.AsyncMock()),
        serial=f"serial-for-{label}",
        label=label,
        viewport=(720, 1600),
        config=config or excellent_throw_android.load_config(None, excellent_throw_android.parse_args([])),
    )


class ThrowTuningTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name)

    def test_meltan_gets_no_premium_berry(self):
        self.assertEqual(legendary_pokemon.legendary_named(["Meltan"]), "Meltan")
        self.assertFalse(legendary_pokemon.premium_berry_worthy("Meltan"))
        self.assertTrue(legendary_pokemon.premium_berry_worthy("Kyogre"))
        self.assertFalse(legendary_pokemon.premium_berry_worthy(None))


    def test_parse_answer_takes_last_json_and_drops_unknown_knobs(self):
        text = 'thinking {"x": 1}\n{"changes": {"throw_rise": 0.5, "bogus": 3, "throw_ms": "fast"}, "reason": "r"}'
        changes, reason = throw_tuning.parse_answer(text)
        self.assertEqual(changes, {"throw_rise": 0.5})
        self.assertEqual(reason, "r")


    def test_parse_answer_without_json_is_empty(self):
        self.assertEqual(throw_tuning.parse_answer("no idea"), ({}, ""))


    def test_clamp_limits_step_and_range(self):
        knob = throw_tuning.KNOBS["throw_rise"]
        step = (knob.high - knob.low) * throw_tuning.MAX_STEP
        self.assertEqual(throw_tuning.clamp_change("throw_rise", 0.46, 0.9), round(0.46 + step, 4))
        self.assertEqual(throw_tuning.clamp_change("throw_rise", 0.31, 0.0), knob.low)
        self.assertTrue(isinstance(throw_tuning.clamp_change("throw_ms", 140, 150), int))


    def test_apply_values_keeps_the_other_device_and_defaults_unchanged(self):
        device = fake_device()
        other = fake_device("other-phone", device.config)
        baseline = throw_tuning.current_values(other)
        throw_tuning.apply_values(device, {"throw_rise": 0.5, "reach_step": 0.1, "throw_ms": 160})
        self.assertEqual(device.throw_aim.throw_rise, 0.5)
        self.assertEqual(catch_awarded_android.throw_reach(2, device.throw_aim.reach_step), 1.2)
        self.assertEqual(excellent_throw_android.PROVEN_THROW_RISE, baseline["throw_rise"])
        self.assertEqual(throw_tuning.current_values(other), baseline)
        ball = excellent_throw_android.excellent_throw_ios.BallDetection(1.0, 360, 1300, 110)
        _, tuned = excellent_throw_android.proven_throw_points(ball, device.viewport, aim=device.throw_aim)
        _, untuned = excellent_throw_android.proven_throw_points(ball, other.viewport, aim=other.throw_aim)
        self.assertLess(tuned[1], untuned[1])

    def test_saved_tuning_drops_malformed_and_out_of_range_values(self):
        device = fake_device()
        (self.tmp / "overrides.json").write_text(
            '{"throw_rise": 0.50, "reach_step": 90, "aim_deadband": "none", "throw_ms": true}'
        )
        throw_tuning.SolTuner(device, self.tmp)
        self.assertEqual(device.throw_aim.throw_rise, 0.50)
        self.assertEqual(device.throw_aim.reach_step, excellent_throw_android.DEFAULT_THROW_AIM.reach_step)
        self.assertEqual(device.throw_aim.aim_deadband, excellent_throw_android.DEFAULT_THROW_AIM.aim_deadband)


    def test_tuner_reviews_and_applies_between_encounters(self):
        device = fake_device()
        answer = '{"changes": {"throw_rise": 0.48}, "reason": "short"}'
        baseline = device.throw_aim.throw_rise
        tuner = throw_tuning.SolTuner(device, self.tmp, every=2, runner=lambda prompt: answer)
        tuner.record(True, 3)
        self.assertIsNone(tuner._thread)
        tuner.record(False, 6)
        tuner._thread.join(5)
        self.assertEqual(device.throw_aim.throw_rise, baseline)
        tuner.apply_pending()
        self.assertEqual(device.throw_aim.throw_rise, 0.48)
        self.assertTrue((self.tmp / "overrides.json").exists())
        self.assertEqual((self.tmp / "journal.jsonl").read_text().count("\n"), 2)


    def test_failed_review_never_raises(self):
        def broken(prompt):
            raise RuntimeError("network down")

        tuner = throw_tuning.SolTuner(fake_device(), self.tmp, every=1, runner=broken)
        tuner.record(True, 1)
        tuner._thread.join(5)
        tuner.apply_pending()
        self.assertIn("network down", (self.tmp / "sol-reviews.log").read_text())


class CatchTuningIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_catchers_send_only_their_numeric_telemetry(self):
        one = fake_device("private-label-one")
        two = fake_device("private-label-two", one.config)
        first_threw = asyncio.Event()
        second_threw = asyncio.Event()
        prompts = []
        tuners = []
        real_tuner = throw_tuning.SolTuner

        def tuner(*args, **kwargs):
            instance = real_tuner(*args, **kwargs)
            tuners.append(instance)
            return instance

        def review(prompt):
            prompts.append(prompt)
            return '{"changes": {}}'

        async def encounter(phone, _args, _directory):
            if phone is one:
                await phone.straight_throw([360, 1300], [360, 560], 140)
                first_threw.set()
                await second_threw.wait()
            else:
                await first_threw.wait()
                for _ in range(3):
                    await phone.straight_throw([360, 1300], [360, 560], 140)
                second_threw.set()
            print("unrelated runtime line: /private/artifacts/host/account")
            return True

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(throw_tuning, "SolTuner", side_effect=tuner), \
                mock.patch.object(throw_tuning, "run_sol", side_effect=review), \
                mock.patch.object(catch_awarded_android, "catch_one", side_effect=encounter), \
                mock.patch.object(catch_awarded_android, "clear_catch_screens", mock.AsyncMock()), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            args = catch_rewards.parse_args([
                "--mode", "encounter", "--count", "1", "--sol-tune", "--sol-every", "1",
                "--artifacts", directory,
            ])
            results = await asyncio.gather(*(catch_rewards.run_phone(phone, args) for phone in (one, two)))
            for instance in tuners:
                instance._thread.join(5)
            self.assertIs(sys.stdout, output)
        self.assertEqual(results, [0, 0])
        self.assertEqual(len(prompts), 2)
        entries = []
        for prompt in prompts:
            for value in (one.label, two.label, one.serial, two.serial, directory, "/private/artifacts/host/account"):
                self.assertNotIn(value, prompt)
            entries.append(json.loads(next(line for line in prompt.splitlines() if line.startswith('{"ended"'))))
        self.assertEqual(sorted(entry["balls"] for entry in entries), [1, 3])
        for entry in entries:
            self.assertEqual(set(entry), {"ended", "balls", "settings"})
            self.assertEqual(set(entry["settings"]), set(throw_tuning.KNOBS))


if __name__ == '__main__':
    unittest.main()
