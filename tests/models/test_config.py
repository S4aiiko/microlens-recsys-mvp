from __future__ import annotations

import copy
import unittest

from recsys.models.config import load_model_config
from recsys.models.errors import ModelInputError

from ._support import model_config


class ModelConfigTests(unittest.TestCase):
    def test_enabled_decay_rejects_bool_and_numeric_string(self) -> None:
        for invalid in (True, "604800", 0, -1, 1.5):
            config = model_config()
            config["dssm"]["time_decay"] = {
                "enabled": True,
                "half_life_seconds": invalid,
            }
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ModelInputError, "positive integer"),
            ):
                load_model_config(config)

    def test_dense_feature_declaration_rejects_duplicates(self) -> None:
        config = copy.deepcopy(model_config())
        features = config["deepfm"]["dense_features"]
        features.append(features[0])
        with self.assertRaisesRegex(ModelInputError, "frozen six"):
            load_model_config(config)

    def test_evaluation_integer_fields_reject_coercion_and_duplicates(self) -> None:
        for invalid in (True, "200", 0, -1, 1.5):
            config = model_config()
            config["evaluation"]["maximum_badcases"] = invalid
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ModelInputError, "positive integer"),
            ):
                load_model_config(config)
        config = model_config()
        config["evaluation"]["k"] = [5, 10, 10]
        with self.assertRaisesRegex(ModelInputError, "duplicates"):
            load_model_config(config)

    def test_activity_segments_are_optional_but_frozen_when_declared(self) -> None:
        config = model_config()
        load_model_config(config)
        config["evaluation"]["activity_segments"] = {
            "cold_start": {"minimum_history": 0, "maximum_history": 2}
        }
        with self.assertRaisesRegex(ModelInputError, "frozen segment boundaries"):
            load_model_config(config)


if __name__ == "__main__":
    unittest.main()
