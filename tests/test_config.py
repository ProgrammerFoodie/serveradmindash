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


class AdminSwitchesTest(unittest.TestCase):
    def test_example_config_is_valid_with_every_switch_on(self):
        cfg = example()
        config.validate(cfg)
        self.assertEqual(config.admin_switches(cfg), {name: True for name in config.ADMIN_SWITCHES})

    def test_missing_block_or_key_means_off(self):
        cfg = example()
        del cfg["admin"]
        config.validate(cfg)
        self.assertEqual(config.admin_switches(cfg), {name: False for name in config.ADMIN_SWITCHES})
        cfg["admin"] = {"power": True}
        config.validate(cfg)
        self.assertEqual([n for n, on in config.admin_switches(cfg).items() if on], ["power"])

    def test_only_a_real_true_turns_a_tool_on(self):
        for odd in ("true", 1, "yes", [True], None, {}):
            self.assertFalse(config.admin_switches({"admin": {"power": odd}})["power"], repr(odd))
        self.assertFalse(any(config.admin_switches({"admin": "everything"}).values()))

    def test_bad_blocks_are_rejected(self):
        for bad in ("on", ["power"], {"power": "yes"}, {"power": 1}, {"powre": True}, {"power": None}):
            cfg = example()
            cfg["admin"] = bad
            with self.assertRaises(config.ConfigError, msg=repr(bad)):
                config.validate(cfg)


if __name__ == "__main__":
    unittest.main()
