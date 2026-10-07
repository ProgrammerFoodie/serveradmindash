import json
import unittest

from dashboard import config


def example():
    return json.loads(config.EXAMPLE_PATH.read_text())


class IdleMinutesTest(unittest.TestCase):
    def test_example_config_is_valid_and_sets_fifteen_minutes(self):
        cfg = example()
        config.validate(cfg)
        self.assertEqual(cfg["auth"]["idle_minutes"], 15)

    def test_a_config_written_before_the_setting_existed_still_loads(self):
        cfg = example()
        del cfg["auth"]["idle_minutes"]
        config.validate(cfg)

    def test_bad_values_are_rejected(self):
        for bad in ("15", True, None, 0, 0.5, -1, 1441, [15]):
            cfg = example()
            cfg["auth"]["idle_minutes"] = bad
            with self.assertRaises(config.ConfigError, msg=repr(bad)):
                config.validate(cfg)


if __name__ == "__main__":
    unittest.main()
