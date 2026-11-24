"""Zero-dependency Feishu Custom Bot Webhook (HMAC-SHA256 + Card JSON 2.0) & Beijing 10:00 dispatcher."""

from __future__ import annotations

import base64
from datetime import datetime, time as dt_time
import hashlib
import hmac
import json
from pathlib import Path
import time
from typing import Any, Callable
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from daily_radar.config import default_user_config_dir, ensure_mode_0600_read
from daily_radar.models import DailyIssue


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
# Feishu custom bot webhook enforces a 20 KB card JSON cap; keep a safe margin at 19,000 UTF-8 bytes.
MAX_WEBHOOK_PAYLOAD_BYTES = 19_000


def gen_feishu_sign(timestamp: int, secret: str) -> str:
    """Compute Feishu Custom Bot HMAC-SHA256 signature: base64(HMAC-SHA256(key=f'{timestamp}\\n{secret}', msg=b''))."""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), msg=b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _open_url_button(label: str, url: str, btn_type: str = "default", elem_id: str = "btn_1") -> dict[str, Any]:
    return {
        "tag": "button",
        "element_id": elem_id,
        "type": btn_type,
        "text": {"tag": "plain_text", "content": label},
        "behaviors": [
            {
                "type": "open_url",
                "default_url": url,
                "pc_url": "",
                "ios_url": "",
                "android_url": "",
            }
        ],
    }


def build_feishu_morning_card(
    issue: DailyIssue,
    *,
    public_base_url: str,
    pages_degraded_note: str = "",
) -> dict[str, Any]:
    """Build the 30-second briefing Card JSON 2.0 matching §2.2 specification."""
    base = public_base_url.rstrip("/")
    daily_url = f"{base}/daily/{issue.date}/index.html"
    archive_url = f"{base}/archive/index.html"

    has_special = bool(issue.special_conference_banner)
    prefix = "[顶会特刊] " if has_special else ""
    header_title = f"📡 {prefix}每日科研雷达 · {issue.date}（{issue.weekday_zh}）"
    header_template = "carmine" if has_special else ("orange" if pages_degraded_note else "blue")

    stats = issue.stats_by_track()
    stats_line = (
        f"**GUI-RL 精选 {stats['gui-rl']} 篇** ｜ "
        f"**OSSL/OOD 精选 {stats['ossl-ood']} 篇** ｜ "
        f"**开源与业界 {stats['opensource_and_industry'] + stats['vlm-infra']} 条**"
        f"（另折叠泛相关 {len(issue.folded_items)} 条）"
    )

    if issue.highlights_top3:
        highlights_md = "\n".join(line[:110] for line in issue.highlights_top3[:3])
    else:
        highlights_md = "• 今日各监控信源无命中核心阈值的高优新增，总览页如实留空不凑数。"

    elements: list[dict[str, Any]] = []
    if pages_degraded_note:
        elements.append({"tag": "markdown", "content": f"⚠️ **{pages_degraded_note}**"})
    if issue.special_conference_banner:
        elements.append({"tag": "markdown", "content": f"**{issue.special_conference_banner['headline']}**"})

    elements.append({"tag": "markdown", "content": stats_line})
    elements.append({"tag": "markdown", "content": highlights_md})

    if issue.source_health_alerts:
        alert_short = issue.source_health_alerts[0][:100]
        elements.append({"tag": "markdown", "content": f"⚠️ 信源健康提醒：{alert_short}"})

    elements.append(
        {
            "tag": "column_set",
            "flex_mode": "flow",
            "horizontal_spacing": "8px",
            "horizontal_align": "left",
            "columns": [
                {
                    "tag": "column",
                    "width": "auto",
                    "elements": [
                        _open_url_button("打开今日总览页（5–10 分钟）", daily_url, "primary", "btn_daily")
                    ],
                },
                {
                    "tag": "column",
                    "width": "auto",
                    "elements": [_open_url_button("历史归档与检索", archive_url, "default", "btn_archive")],
                },
            ],
        }
    )

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": header_template,
            "title": {"tag": "plain_text", "content": header_title},
        },
        "body": {"elements": elements},
    }
    return card


def build_webhook_payload(
    card: dict[str, Any],
    *,
    secret: str = "",
    timestamp: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": card,
    }
    if secret:
        ts = timestamp if timestamp is not None else int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = gen_feishu_sign(ts, secret)

    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= MAX_WEBHOOK_PAYLOAD_BYTES, (
        f"Feishu webhook payload ({len(encoded)} bytes) exceeds {MAX_WEBHOOK_PAYLOAD_BYTES}-byte safety cap!"
    )
    return payload


def load_webhook_credentials(config_home: Path | None = None) -> tuple[str, str]:
    cfg_dir = config_home or default_user_config_dir()
    webhook_file = cfg_dir / "webhook.json"
    raw = json.loads(ensure_mode_0600_read(webhook_file))
    url = str(raw.get("webhook_url") or "").strip()
    secret = str(raw.get("secret") or "").strip()
    if not url:
        raise ValueError(
            f"webhook_url is empty in {webhook_file}. Run `python3 -m daily_radar configure-webhook` first."
        )
    return url, secret


def send_feishu_webhook(
    issue: DailyIssue,
    *,
    public_base_url: str,
    config_home: Path | None = None,
    webhook_url_override: str = "",
    secret_override: str = "",
    pages_degraded_note: str = "",
    timeout: float = 15.0,
) -> dict[str, Any]:
    if webhook_url_override:
        webhook_url = webhook_url_override
        secret = secret_override
    else:
        webhook_url, secret = load_webhook_credentials(config_home)

    card = build_feishu_morning_card(
        issue,
        public_base_url=public_base_url,
        pages_degraded_note=pages_degraded_note,
    )
    payload = build_webhook_payload(card, secret=secret)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp_data = json.loads(resp.read().decode("utf-8"))

    code = resp_data.get("code", resp_data.get("StatusCode", 0))
    if code != 0:
        raise RuntimeError(f"Feishu webhook rejected card (code={code}): {resp_data}")
    return resp_data


def poll_pages_http_200(
    page_url: str,
    *,
    deadline_cst_hms: str = "09:58:00",
    poll_interval_sec: float = 10.0,
    now_fn: Callable[[], datetime] = lambda: datetime.now(SHANGHAI_TZ),
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Poll the deployed GitHub Pages daily overview URL until HTTP 200 or Beijing deadline (default 09:58:00)."""
    now = now_fn().astimezone(SHANGHAI_TZ)
    h, m, s = [int(x) for x in deadline_cst_hms.split(":")]
    deadline = datetime.combine(now.date(), dt_time(h, m, s), tzinfo=SHANGHAI_TZ)

    while True:
        req = urllib.request.Request(
            page_url,
            headers={
                "User-Agent": "DailyResearchRadar-ReadinessProbe/0.1",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()

        cur = now_fn().astimezone(SHANGHAI_TZ)
        if cur >= deadline:
            return False
        remain = (deadline - cur).total_seconds()
        sleep_fn(min(poll_interval_sec, max(0.2, remain)))


def wait_until_beijing_time(
    target_hms: str = "10:00:00",
    *,
    now_fn: Callable[[], datetime] = lambda: datetime.now(SHANGHAI_TZ),
    sleep_fn: Callable[[float], None] = time.sleep,
) -> float:
    """Sleep until exact HH:MM:SS Asia/Shanghai if invoked earlier on the same day. Returns seconds slept."""
    now = now_fn().astimezone(SHANGHAI_TZ)
    h, m, s = [int(x) for x in target_hms.split(":")]
    target = datetime.combine(now.date(), dt_time(h, m, s), tzinfo=SHANGHAI_TZ)
    delta = (target - now).total_seconds()
    if delta <= 0:
        return 0.0
    sleep_fn(delta)
    return delta
