import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class FrontendTest(unittest.TestCase):
    def test_helper_functions(self):
        run = subprocess.run(["node", str(ROOT / "tests/js/helpers.mjs")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_overview_renders_and_its_rows_open_and_close(self):
        run = subprocess.run(["node", str(ROOT / "tests/js/overview_smoke.mjs")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_agents_tab_renders_empty_and_populated_data(self):
        run = subprocess.run(["node", str(ROOT / "tests/js/agents_smoke.mjs")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_the_confirm_dialog_asks_for_the_typed_word_before_it_can_be_confirmed(self):
        run = subprocess.run(["node", str(ROOT / "tests/js/confirm_smoke.mjs")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_the_power_card_asks_before_it_acts_and_follows_the_restart(self):
        run = subprocess.run(["node", str(ROOT / "tests/js/power_smoke.mjs")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_the_users_tab_filters_shows_details_and_survives_odd_data(self):
        run = subprocess.run(["node", str(ROOT / "tests/js/users_smoke.mjs")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_forms_and_account_actions_check_everything_and_confirm_the_dangerous_ones(self):
        run = subprocess.run(["node", str(ROOT / "tests/js/forms_smoke.mjs")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_the_folder_picker_navigates_explains_and_only_returns_allowed_choices(self):
        run = subprocess.run(["node", str(ROOT / "tests/js/folderpicker_smoke.mjs")], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_every_script_parses(self):
        for path in sorted((ROOT / "static").rglob("*.js")):
            run = subprocess.run(["node", "--input-type=module", "--check"], input=path.read_text(), capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, f"{path.name}: {run.stderr}")

    def test_no_inline_styles_or_scripts(self):
        # The CSP forbids them, so a stray one would silently do nothing in the browser.
        for path in sorted((ROOT / "static").glob("*.html")):
            text = path.read_text()
            self.assertNotIn("style=", text, path.name)
            self.assertNotIn("<script>", text, path.name)
            self.assertNotIn("onclick=", text, path.name)
        for path in sorted((ROOT / "static/js").rglob("*.js")):
            self.assertNotIn("innerHTML", path.read_text(), f"{path.name}: use text nodes, never HTML strings")


if __name__ == "__main__":
    unittest.main()
