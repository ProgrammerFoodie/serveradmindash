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
