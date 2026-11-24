"""Tests for 0600 credential checks, Feishu HMAC-SHA256 webhook, 20KB cap, HTTP 200 polling, and CST 10:00 timer."""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from zoneinfo import ZoneInfo

from daily_radar.config import ensure_mode_0600_read, init_user_config_stubs, write_mode_0600_atomic
from daily_radar.feishu import (
    MAX_WEBHOOK_PAYLOAD_BYTES,
    SHANGHAI_TZ,
    build_feishu_morning_card,
    build_webhook_payload,
    gen_feishu_sign,
    poll_pages_http_200,
    send_feishu_webhook,
    wait_until_beijing_time,
)
from daily_radar.models import DailyIssue


REPO_ROOT = Path(__file__).resolve().parent.parent


class _MockFeishuAndPagesHandler(BaseHTTPRequestHandler):
    expected_secret = "unit_test_hmac_secret_99"
    received_payloads: list[dict] = []
    pages_polls = 0
    pages_ready_after = 2
    feishu_return_code = 0

    def do_GET(self) -> None:  # noqa: N802
        type(self).pages_polls += 1
        if type(self).pages_polls < type(self).pages_ready_after:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<html>OK</html>")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        body = json.loads(raw.decode("utf-8"))
        type(self).received_payloads.append(body)

        # Independently verify Feishu HMAC-SHA256 signature on server side!
        ts = body.get("timestamp", "")
        sign = body.get("sign", "")
        expected = base64.b64encode(
            hmac.new(f"{ts}\n{self.expected_secret}".encode("utf-8"), b"", hashlib.sha256).digest()
        ).decode("utf-8")
        if sign != expected:
            resp = json.dumps({"code": 19021, "msg": "sign match fail"}).encode("utf-8")
        else:
            resp = json.dumps({"code": self.feishu_return_code, "msg": "ok"}).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class FeishuAndSchedulerTest(unittest.TestCase):
    def test_mode_0600_credential_enforcement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "daily-radar"
            paths = init_user_config_stubs(cfg_dir)
            for key in ("webhook", "llm", "sensitive_terms"):
                mode = stat.S_IMODE(paths[key].stat().st_mode)
                self.assertEqual(mode, 0o600, f"{key} must be mode 0600, got {oct(mode)}")

            # Loosening permissions to 0644 MUST fail fast with PermissionError!
            os.chmod(paths["webhook"], 0o644)
            with self.assertRaises(PermissionError):
                ensure_mode_0600_read(paths["webhook"])

    def test_feishu_webhook_hmac_card_schema_and_size_guard(self) -> None:
        issue = DailyIssue.from_dict(
            json.loads((REPO_ROOT / "content" / "2026" / "11" / "2026-11-24.json").read_text(encoding="utf-8"))
        )
        # Also test conference special mode header switching
        issue.special_conference_banner = {
            "slug": "aaai-2027",
            "name": "AAAI 2027",
            "ccf_grade": "A",
            "headline": "🎯 AAAI 2027 放榜特刊窗口已开启",
            "url": "conferences/aaai-2027.html",
        }
        card = build_feishu_morning_card(issue, public_base_url="https://phylipppp-zzy.github.io/daily-radar")
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["template"], "carmine")
        self.assertIn("[顶会特刊]", card["header"]["title"]["content"])

        payload = build_webhook_payload(card, secret="sec123", timestamp=1795485600)
        raw_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.assertLess(len(raw_bytes), MAX_WEBHOOK_PAYLOAD_BYTES)
        self.assertEqual(payload["sign"], gen_feishu_sign(1795485600, "sec123"))

        # Live loopback POST with server-side HMAC verification
        _MockFeishuAndPagesHandler.received_payloads = []
        _MockFeishuAndPagesHandler.feishu_return_code = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockFeishuAndPagesHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg_home = Path(tmp)
                write_mode_0600_atomic(
                    cfg_home / "webhook.json",
                    json.dumps(
                        {
                            "webhook_url": f"http://127.0.0.1:{port}/hook",
                            "secret": _MockFeishuAndPagesHandler.expected_secret,
                        }
                    ),
                )
                resp = send_feishu_webhook(
                    issue,
                    public_base_url="https://phylipppp-zzy.github.io/daily-radar",
                    config_home=cfg_home,
                )
                self.assertEqual(resp["code"], 0)
                self.assertEqual(len(_MockFeishuAndPagesHandler.received_payloads), 1)
                sent_card = _MockFeishuAndPagesHandler.received_payloads[0]["card"]
                cols = sent_card["body"]["elements"][-1]["columns"]
                self.assertEqual(
                    cols[0]["elements"][0]["behaviors"][0]["default_url"],
                    "https://phylipppp-zzy.github.io/daily-radar/daily/2026-11-24/index.html",
                )

                # Wrong secret must trigger non-zero code -> RuntimeError!
                write_mode_0600_atomic(
                    cfg_home / "webhook.json",
                    json.dumps({"webhook_url": f"http://127.0.0.1:{port}/hook", "secret": "wrong_secret"}),
                )
                with self.assertRaises(RuntimeError):
                    send_feishu_webhook(
                        issue,
                        public_base_url="https://phylipppp-zzy.github.io/daily-radar",
                        config_home=cfg_home,
                    )
                # CLI configure-webhook: non-https rejected (code 2); https + --verify-send writes 0600 & dispatches latest card
                from unittest.mock import patch
                from daily_radar.cli import main as cli_main

                bad_rc = cli_main(
                    [
                        "--repo-root",
                        str(REPO_ROOT),
                        "configure-webhook",
                        "--config-home",
                        str(cfg_home),
                        "--webhook-url",
                        f"http://127.0.0.1:{port}/hook",
                        "--secret",
                        "s",
                    ]
                )
                self.assertEqual(bad_rc, 2)

                with patch("daily_radar.cli.send_feishu_webhook", return_value={"code": 0}) as mock_send:
                    ok_rc = cli_main(
                        [
                            "--repo-root",
                            str(REPO_ROOT),
                            "configure-webhook",
                            "--config-home",
                            str(cfg_home),
                            "--webhook-url",
                            "https://example.invalid/feishu-hook-verify",
                            "--secret",
                            "sec_verify_0600",
                            "--verify-send",
                        ]
                    )
                    self.assertEqual(ok_rc, 0)
                    self.assertEqual(mock_send.call_count, 1)
                    saved = json.loads((cfg_home / "webhook.json").read_text(encoding="utf-8"))
                    self.assertEqual(saved["secret"], "sec_verify_0600")
        finally:
            server.shutdown()
            server.server_close()

    def test_pages_readiness_polling_and_beijing_10am_clock(self) -> None:
        _MockFeishuAndPagesHandler.pages_polls = 0
        _MockFeishuAndPagesHandler.pages_ready_after = 3
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockFeishuAndPagesHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            fake_now = datetime(2026, 11, 25, 9, 30, 0, tzinfo=SHANGHAI_TZ)
            slept: list[float] = []
            ok = poll_pages_http_200(
                f"http://127.0.0.1:{port}/daily/2026-11-25/index.html",
                deadline_cst_hms="09:58:00",
                poll_interval_sec=5.0,
                now_fn=lambda: fake_now,
                sleep_fn=lambda s: slept.append(s),
            )
            self.assertTrue(ok)
            self.assertEqual(_MockFeishuAndPagesHandler.pages_polls, 3)

            # Timeout at 09:58:00 when page stays 404
            _MockFeishuAndPagesHandler.pages_polls = 0
            _MockFeishuAndPagesHandler.pages_ready_after = 99
            late_now = datetime(2026, 11, 25, 9, 58, 1, tzinfo=SHANGHAI_TZ)
            ok_late = poll_pages_http_200(
                f"http://127.0.0.1:{port}/daily/2026-11-25/index.html",
                deadline_cst_hms="09:58:00",
                now_fn=lambda: late_now,
                sleep_fn=lambda s: None,
            )
            self.assertFalse(ok_late)

            # Verify PST (America/Los_Angeles 17:59:15 = next-day 09:59:15 Asia/Shanghai) passed DIRECTLY (without
            # caller pre-converting to SHANGHAI_TZ!) sleeps exact 45.0s until 10:00:00 CST and polls accurately!
            pst_dt = datetime(2026, 11, 24, 17, 59, 15, tzinfo=ZoneInfo("America/Los_Angeles"))
            waited: list[float] = []
            sec = wait_until_beijing_time("10:00:00", now_fn=lambda: pst_dt, sleep_fn=lambda s: waited.append(s))
            self.assertEqual(sec, 45.0)
            self.assertEqual(waited, [45.0])

            _MockFeishuAndPagesHandler.pages_polls = 0
            _MockFeishuAndPagesHandler.pages_ready_after = 2
            pst_poll_dt = datetime(2026, 11, 24, 17, 30, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
            ok_pst = poll_pages_http_200(
                f"http://127.0.0.1:{port}/daily/2026-11-25/index.html",
                deadline_cst_hms="09:58:00",
                poll_interval_sec=5.0,
                now_fn=lambda: pst_poll_dt,
                sleep_fn=lambda s: None,
            )
            self.assertTrue(ok_pst)
        finally:
            server.shutdown()
            server.server_close()

    def test_feishu_app_api_mode_reads_0600_env_and_sends_card(self) -> None:
        sent_msgs: list[dict] = []

        class _AppAPIHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/open-apis/auth/v3/tenant_access_token/internal":
                    assert body["app_id"] == "cli_test_app"
                    assert body["app_secret"] == "sec_test_app"
                    resp = {"code": 0, "tenant_access_token": "t-mock-token"}
                elif self.path.startswith("/open-apis/im/v1/messages?receive_id_type=open_id"):
                    assert self.headers.get("Authorization") == "Bearer t-mock-token"
                    sent_msgs.append(body)
                    resp = {"code": 0, "data": {"message_id": "om_mock_001"}}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                out = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        server = ThreadingHTTPServer(("127.0.0.1", 0), _AppAPIHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            issue = DailyIssue.from_dict(
                json.loads((REPO_ROOT / "content/2026/11/2026-11-24.json").read_text(encoding="utf-8"))
            )
            with tempfile.TemporaryDirectory() as td:
                cfg_home = Path(td)
                env_file = cfg_home / "bridge.env"
                write_mode_0600_atomic(
                    env_file,
                    "FEISHU_APP_ID=cli_test_app\nFEISHU_APP_SECRET=sec_test_app\nFEISHU_OWNER_OPEN_ID=ou_test_owner\n",
                )
                write_mode_0600_atomic(
                    cfg_home / "webhook.json",
                    json.dumps(
                        {
                            "mode": "app_api",
                            "env_file": str(env_file),
                            "receive_id_type": "open_id",
                            "api_base": f"http://127.0.0.1:{port}",
                        }
                    ),
                )
                resp = send_feishu_webhook(
                    issue,
                    public_base_url="https://phylipppp-zzy.github.io/daily-radar",
                    config_home=cfg_home,
                )
                self.assertEqual(resp["code"], 0)
                self.assertEqual(len(sent_msgs), 1)
                self.assertEqual(sent_msgs[0]["receive_id"], "ou_test_owner")
                self.assertEqual(sent_msgs[0]["msg_type"], "interactive")
                card = json.loads(sent_msgs[0]["content"])
                self.assertEqual(card["schema"], "2.0")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
