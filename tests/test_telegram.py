import json
import logging
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from dashboard import alerts as alerts_module
from dashboard.alerts import TelegramError, send_telegram

CFG = {"telegram": {"bot_token": "123:SECRET-TOKEN", "chat_id": "42", "enabled": True}}


class FakeTelegram(BaseHTTPRequestHandler):
    """Stands in for https://api.telegram.org: records requests and answers like the real API."""
    seen = []
    reply = (200, {"ok": True, "result": {}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeTelegram.seen.append({"path": self.path, "body": body, "type": self.headers["Content-Type"]})
        status, payload = FakeTelegram.reply
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class SendTelegramTest(unittest.TestCase):
    def setUp(self):
        FakeTelegram.seen, FakeTelegram.reply = [], (200, {"ok": True, "result": {}})
        self.server = HTTPServer(("127.0.0.1", 0), FakeTelegram)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.original = alerts_module.TELEGRAM_API
        alerts_module.TELEGRAM_API = f"http://127.0.0.1:{self.server.server_port}/bot{{token}}/sendMessage"

    def tearDown(self):
        alerts_module.TELEGRAM_API = self.original
        self.server.shutdown()
        self.server.server_close()

    def test_request_shape(self):
        send_telegram(CFG, "<b>hi</b> & bye")
        req = FakeTelegram.seen[0]
        self.assertEqual(req["path"], "/bot123:SECRET-TOKEN/sendMessage")
        self.assertEqual(req["type"], "application/json")
        self.assertEqual(req["body"], {"chat_id": "42", "text": "<b>hi</b> & bye", "parse_mode": "HTML", "disable_web_page_preview": True})

    def test_api_errors_become_readable_messages_without_the_token(self):
        FakeTelegram.reply = (400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})
        with self.assertRaises(TelegramError) as ctx:
            send_telegram(CFG, "x")
        self.assertEqual(str(ctx.exception), "HTTP 400: Bad Request: chat not found")
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_rejected_without_ok_flag(self):
        FakeTelegram.reply = (200, {"ok": False, "description": "odd"})
        with self.assertRaisesRegex(TelegramError, "odd"):
            send_telegram(CFG, "x")

    def test_network_failure_is_a_telegram_error_without_the_token(self):
        alerts_module.TELEGRAM_API = "http://127.0.0.1:1/bot{token}/sendMessage"
        with self.assertRaises(TelegramError) as ctx:
            send_telegram(CFG, "x")
        self.assertIn("network error", str(ctx.exception))
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_refuses_when_disabled_or_empty(self):
        for tg in ({"bot_token": "t", "chat_id": "1", "enabled": False}, {"bot_token": "", "chat_id": "1", "enabled": True},
                   {"bot_token": "t", "chat_id": "", "enabled": True}):
            with self.assertRaises(TelegramError):
                send_telegram({"telegram": tg}, "x")
        self.assertEqual(FakeTelegram.seen, [])


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
