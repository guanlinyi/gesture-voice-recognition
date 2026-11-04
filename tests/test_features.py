"""Synthetic landmarks and shipped numeric references; never read source media."""
from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np

from steering_control.gesture.features import Descriptor, SampleLibrary, describe

ROOT = Path(__file__).resolve().parents[1]


class FeatureTests(unittest.TestCase):
    def test_210_unique_distances_ignore_translation_rotation_and_uniform_scale(self):
        points = np.random.default_rng(7).normal(size=(21, 3))
        def landmarks(values):
            return [SimpleNamespace(x=x, y=y, z=z) for x, y, z in values]
        rotation = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
        first = describe(landmarks(points), landmarks(points))
        changed = describe(landmarks(points), landmarks((points @ rotation) * 2 + 10))
        self.assertEqual(first.vector.shape, (210,))
        np.testing.assert_allclose(first.vector, changed.vector, atol=1e-12)

    def test_invalid_or_absent_landmarks_do_not_produce_features(self):
        self.assertIsNone(describe(None))
        zero = [SimpleNamespace(x=0., y=0., z=0.) for _ in range(21)]
        self.assertIsNone(describe(zero, zero))
        zero[0].x = float("nan")
        self.assertIsNone(describe(zero, zero))

    def test_all_shipped_samples_are_valid_and_match_their_original_label(self):
        library = SampleLibrary.load(ROOT / "assets/output_json/reference_library.json")
        counts = {}
        for demonstration in library.demonstrations:
            for feature in demonstration["features"]:
                result = library.predict(Descriptor(feature["space"], np.asarray(feature["vector"])))
                self.assertEqual(result.label, demonstration["label"])
                self.assertEqual(result.distance, 0.)
            counts[demonstration["label"]] = len(demonstration["features"])
        self.assertEqual(counts, {"握拳": 20, "OK手势": 36, "手掌展开": 17})
