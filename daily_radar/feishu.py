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


def _parse_env_file_0600(env_path: Path) -> dict[str, str]:
    text = ensure_mode_0600_read(env_path)
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def _send_via_feishu_app_api(
    card: dict[str, Any],
    cfg: dict[str, Any],
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Send Card JSON 2.0 via Feishu Open API `im/v1/messages` using local 0600 app credentials."""
    env_file_raw = str(cfg.get("env_file") or "").strip()
    env_vars: dict[str, str] = {}
    if env_file_raw:
        env_vars = _parse_env_file_0600(Path(env_file_raw).expanduser())

    app_id = str(cfg.get("app_id") or env_vars.get("FEISHU_APP_ID") or "").strip()
    app_secret = str(cfg.get("app_secret") or env_vars.get("FEISHU_APP_SECRET") or "").strip()
    receive_id_type = str(cfg.get("receive_id_type") or "open_id").strip()
    receive_id = str(cfg.get("receive_id") or env_vars.get("FEISHU_OWNER_OPEN_ID") or "").strip()
    api_base = str(cfg.get("api_base") or "https://open.feishu.cn").rstrip("/")

    if not app_id or not app_secret or not receive_id:
        raise ValueError("Feishu app_api mode requires app_id, app_secret, and receive_id (or env_file).")

    tok_req = urllib.request.Request(
        f"{api_base}/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(tok_req, timeout=timeout) as resp:
        tok_data = json.loads(resp.read().decode("utf-8"))
    if tok_data.get("code", -1) != 0 or not tok_data.get("tenant_access_token"):
        raise RuntimeError(f"Feishu tenant_access_token failed: {tok_data}")
    token = str(tok_data["tenant_access_token"])

    msg_body = json.dumps(
        {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    msg_req = urllib.request.Request(
        f"{api_base}/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
        data=msg_body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(msg_req, timeout=timeout) as resp:
        resp_data = json.loads(resp.read().decode("utf-8"))
    code = resp_data.get("code", -1)
    if code != 0:
        raise RuntimeError(f"Feishu Open API rejected card (code={code}): {resp_data}")
    return resp_data


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
    card = build_feishu_morning_card(
        issue,
        public_base_url=public_base_url,
        pages_degraded_note=pages_degraded_note,
    )

    if not webhook_url_override:
        cfg_dir = config_home or default_user_config_dir()
        webhook_file = cfg_dir / "webhook.json"
        raw_cfg = json.loads(ensure_mode_0600_read(webhook_file))
        if str(raw_cfg.get("mode") or "").strip().lower() == "app_api":
            return _send_via_feishu_app_api(card, raw_cfg, timeout=timeout)
        webhook_url, secret = load_webhook_credentials(config_home)
    else:
        webhook_url = webhook_url_override
        secret = secret_override

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
