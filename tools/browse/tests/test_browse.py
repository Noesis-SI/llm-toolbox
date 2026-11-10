"""Tests for the browse tool, against local pages served by http.server.

Run from tools/browse:  uv run --project . python -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))

from browse import BrowseError, detect_block, get_text, run_steps, screenshot  # noqa: E402
from browse.core import validate_steps  # noqa: E402


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = partial(_Quiet, directory=str(HERE / "site"))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()


class DetectBlockTests(unittest.TestCase):
    def test_status_and_title_and_widget(self):
        self.assertEqual(detect_block(403, "Example", ""), "HTTP 403")
        self.assertIn("title", detect_block(200, "Attention Required! | Cloudflare", ""))
        self.assertIn("widget", detect_block(200, "Home", '<div class="g-recaptcha"></div>'))
        self.assertIsNone(detect_block(200, "Home", "<p>hello</p>"))


class ValidateStepsTests(unittest.TestCase):
    def test_accepts_valid_steps(self):
        steps = [{"action": "fill", "selector": "#q", "value": "x"}, {"action": "press", "key": "Enter"}]
        self.assertEqual(validate_steps(steps), steps)

    def test_rejects_unknown_action_and_missing_field(self):
        with self.assertRaises(BrowseError) as cm:
            validate_steps([{"action": "download", "selector": "a"}])
        self.assertEqual(cm.exception.code, "bad_steps")
        with self.assertRaises(BrowseError):
            validate_steps([{"action": "fill", "selector": "#q"}])  # no value
        with self.assertRaises(BrowseError):
            validate_steps({"action": "click"})  # not a list


class BrowserTests(_Server):
    def test_text_sees_javascript_rendered_content(self):
        result = get_text(f"{self.base}/index.html", timeout=20)
        self.assertEqual(result["title"], "Rendered page")
        self.assertIn("RENDERED-BY-JS", result["text"])
        self.assertEqual(result["status"], 200)
        self.assertFalse(result["truncated"])

    def test_text_truncates(self):
        result = get_text(f"{self.base}/index.html", max_chars=5, timeout=20)
        self.assertEqual(len(result["text"]), 5)
        self.assertTrue(result["truncated"])

    def test_form_fill_click_wait_and_extract(self):
        steps = [
            {"action": "fill", "selector": "#q", "value": "fiber"},
            {"action": "click", "selector": "#go"},
            {"action": "wait_for", "selector": "p.hit"},
            {"action": "extract", "selector": "p.hit"},
        ]
        result = run_steps(f"{self.base}/form.html", steps, timeout=20)
        self.assertEqual(result["steps_done"], 4)
        self.assertEqual(result["extracted"], [{"selector": "p.hit", "texts": ["Result for fiber"]}])
        self.assertIn("Result for fiber", result["text"])

    def test_press_key_on_field(self):
        steps = [
            {"action": "fill", "selector": "#q", "value": "x"},
            {"action": "press", "key": "Enter", "selector": "#q"},
            {"action": "extract", "selector": "#q"},
        ]
        result = run_steps(f"{self.base}/form.html", steps, timeout=20)
        self.assertEqual(result["extracted"][0]["texts"], [""])  # input has no inner text; step ran

    def test_missing_selector_reports_step_timeout(self):
        with self.assertRaises(BrowseError) as cm:
            run_steps(f"{self.base}/form.html",
                      [{"action": "wait_for", "selector": "#does-not-exist"}], timeout=1)
        self.assertEqual(cm.exception.code, "step_timeout")
        self.assertEqual(cm.exception.details["step"], 0)

    def test_screenshot_writes_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "shot.png")
            result = screenshot(f"{self.base}/index.html", out, timeout=20)
            with open(out, "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
            self.assertEqual(result["path"], out)

    def test_stops_at_challenge_page(self):
        with self.assertRaises(BrowseError) as cm:
            get_text(f"{self.base}/blocked.html", timeout=20)
        self.assertEqual(cm.exception.code, "blocked")
        self.assertIn("does not solve CAPTCHAs", cm.exception.message)

    def test_rejects_non_http_url(self):
        with self.assertRaises(BrowseError) as cm:
            get_text("file:///etc/passwd")
        self.assertEqual(cm.exception.code, "usage_error")


class CliTests(_Server):
    def _cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(SRC))
        proc = subprocess.run([sys.executable, "-m", "browse.cli", *args],
                              capture_output=True, text=True, timeout=120, env=env)
        return proc.returncode, json.loads(proc.stdout)

    def test_text_contract_success(self):
        code, out = self._cli("text", f"{self.base}/index.html", "--timeout", "20")
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertIn("RENDERED-BY-JS", out["data"]["text"])

    def test_blocked_contract_failure(self):
        code, out = self._cli("text", f"{self.base}/blocked.html", "--timeout", "20")
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "blocked")
        self.assertTrue(out["error"]["hint"])

    def test_bad_usage_is_exit_2(self):
        code, out = self._cli("run", f"{self.base}/form.html")  # --steps missing
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "usage_error")


if __name__ == "__main__":
    unittest.main()
