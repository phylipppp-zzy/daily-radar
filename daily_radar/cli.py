"""Unified CLI for Daily Research Radar (`python3 -m daily_radar <subcommand>`)."""

from __future__ import annotations

import argparse
from datetime import datetime
from functools import partial
import getpass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from daily_radar.collectors import (
    HealthCheckSchemaError,
    SourceHealthTracker,
    check_source_health,
    collect_all_sources,
)
from daily_radar.config import (
    RepoConfig,
    default_user_config_dir,
    init_user_config_stubs,
    write_mode_0600_atomic,
)
from daily_radar.dedup import deduplicate_candidates
from daily_radar.feishu import (
    SHANGHAI_TZ,
    build_feishu_morning_card,
    build_webhook_payload,
    poll_pages_http_200,
    send_feishu_webhook,
    wait_until_beijing_time,
)
from daily_radar.models import DailyIssue
from daily_radar.pipeline import DualChannelLLMClient, build_daily_issue
from daily_radar.renderer import build_static_site
from daily_radar.security import load_extra_sensitive_terms, scan_directory
from daily_radar.verify import verify_radar_item


def _today_cst() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def _write_issue_json(content_dir: Path, issue: DailyIssue) -> Path:
    yyyy, mm, _ = issue.date.split("-")
    out_path = content_dir / yyyy / mm / f"{issue.date}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(issue.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_path


def cmd_init_config(args: argparse.Namespace) -> int:
    cfg_home = Path(args.config_home).expanduser() if args.config_home else None
    created = init_user_config_stubs(cfg_home)
    print(json.dumps({k: str(v) for k, v in created.items()}, ensure_ascii=False, indent=2))
    return 0


def cmd_configure_webhook(args: argparse.Namespace) -> int:
    cfg_home = Path(args.config_home).expanduser() if args.config_home else default_user_config_dir()
    init_user_config_stubs(cfg_home)
    url = args.webhook_url or getpass.getpass("请输入飞书群自定义机器人 Webhook URL（输入隐藏）：").strip()
    secret = args.secret if args.secret is not None else getpass.getpass("请输入飞书自定义机器人签名密钥 Secret（输入隐藏）：").strip()
    if not url.startswith("https://"):
        print("错误：Webhook URL 必须以 https:// 开头。", file=sys.stderr)
        return 2
    webhook_path = cfg_home / "webhook.json"
    write_mode_0600_atomic(
        webhook_path,
        json.dumps({"webhook_url": url, "secret": secret}, ensure_ascii=False, indent=2) + "\n",
    )
    print(f"已写入 0600 凭据文件：{webhook_path}")
    if getattr(args, "verify_send", False):
        repo_root = Path(args.repo_root).resolve()
        repo_cfg = RepoConfig.load(repo_root)
        issue_files = sorted((repo_root / "content").rglob("*.json"))
        if not issue_files:
            print("提示：content/ 下暂无期刊 JSON，已跳过验证推送。", file=sys.stderr)
            return 0
        latest_issue = DailyIssue.from_dict(json.loads(issue_files[-1].read_text(encoding="utf-8")))
        base_url = str(repo_cfg.profile["site"]["public_base_url"])
        resp = send_feishu_webhook(latest_issue, public_base_url=base_url, config_home=cfg_home)
        print(f"已向新建飞书群发送验证早报卡片（{latest_issue.date}）：{json.dumps(resp, ensure_ascii=False)}")
    return 0


def cmd_scan_secrets(args: argparse.Namespace) -> int:
    target = Path(args.target or args.repo_root).resolve()
    cfg_home = Path(args.config_home).expanduser() if args.config_home else None
    extra_terms = load_extra_sensitive_terms(cfg_home)
    findings = scan_directory(target, include_tests=bool(args.include_tests), extra_terms=extra_terms)
    if findings:
        print(f"ERROR: Secret/confidentiality scan detected {len(findings)} violation(s):", file=sys.stderr)
        for f in findings:
            print(f"  - [{f.rule}] {f.path}:{f.line_no} ({f.excerpt})", file=sys.stderr)
        return 1
    print(f"OK: Secret & zero-binary scan passed on {target} (0 findings).")
    return 0


def cmd_verify_numbers(args: argparse.Namespace) -> int:
    issue_path = Path(args.issue_json).resolve()
    issue = DailyIssue.from_dict(json.loads(issue_path.read_text(encoding="utf-8")))
    unverified_total = 0
    for item in issue.featured_items:
        verify_radar_item(item)
        for b in item.metric_badges:
            if not b.verified:
                unverified_total += 1
        for r in item.four_elements.experiment_rows:
            if not r.verified:
                unverified_total += 1
    if args.write:
        issue_path.write_text(json.dumps(issue.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "issue": issue.date,
                "featured_items": len(issue.featured_items),
                "flagged_unverified_claims": unverified_total,
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_build_site(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    content_dir = Path(args.content_dir).resolve() if args.content_dir else (repo_root / "content")
    config_dir = Path(args.config_dir).resolve() if args.config_dir else (repo_root / "config")
    out_dir = Path(args.out_dir).resolve() if args.out_dir else (repo_root / "_site")

    repo_cfg = RepoConfig.load(repo_root, config_dir=config_dir)
    summary = build_static_site(
        content_dir=content_dir,
        repo_cfg=repo_cfg,
        out_dir=out_dir,
        hot_years_override=args.hot_years,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_send_feishu(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    repo_cfg = RepoConfig.load(repo_root)
    issue_path = Path(args.issue_json).resolve()
    issue = DailyIssue.from_dict(json.loads(issue_path.read_text(encoding="utf-8")))
    base_url = str(repo_cfg.profile["site"]["public_base_url"])

    if args.dry_run:
        card = build_feishu_morning_card(issue, public_base_url=base_url)
        payload = build_webhook_payload(card, secret=args.secret or "dry_run_secret", timestamp=1795485600)
        encoded_len = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        print(json.dumps({"bytes": encoded_len, "payload": payload}, ensure_ascii=False, indent=2))
        return 0

    cfg_home = Path(args.config_home).expanduser() if args.config_home else None
    resp = send_feishu_webhook(issue, public_base_url=base_url, config_home=cfg_home)
    print(json.dumps(resp, ensure_ascii=False))
    return 0


def cmd_check_health(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    health_file = Path(args.health_file).resolve() if args.health_file else (repo_root / "state" / "source_health.json")
    expected_ids: list[str] | None = None
    if args.strict_sources:
        repo_cfg = RepoConfig.load(repo_root)
        expected_ids = [
            str(s["id"]) for s in repo_cfg.sources.get("sources", []) if s.get("enabled", True)
        ]

    try:
        report = check_source_health(health_file, expected_source_ids=expected_ids)
    except HealthCheckSchemaError as exc:
        print(f"ERROR: Source health schema validation failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        status = "OK" if report.is_healthy else "FAIL"
        print(
            f"SOURCE HEALTH {status}: ok={len(report.ok_sources)}/{report.total_sources}, "
            f"failed={len(report.failing_sources)}, missing={len(report.missing_sources)} "
            f"({report.state_file})"
        )
        for fail in report.failing_sources:
            print(
                f"  - FAIL {fail.source_id} ({fail.source_name}): "
                f"consecutive_failed_dates={fail.consecutive_failed_dates} "
                f"last_ok_date={fail.last_ok_date or '<never>'} "
                f"last_error={fail.last_error}"
            )
        for miss in report.missing_sources:
            print(f"  - MISSING {miss}: not present in {report.state_file}")

    return 0 if report.is_healthy else 1


def cmd_run(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    repo_cfg = RepoConfig.load(repo_root)
    content_dir = Path(args.content_dir).resolve() if args.content_dir else (repo_root / "content")
    out_dir = Path(args.out_dir).resolve() if args.out_dir else (repo_root / "_site")
    state_dir = repo_root / "state"
    issue_date = args.date or _today_cst()
    fixtures_dir = Path(args.fixtures_dir).resolve() if args.fixtures_dir else None
    cfg_home = Path(args.config_home).expanduser() if args.config_home else None
    if args.run_mode == "live" and fixtures_dir is not None:
        print(
            "ERROR: Contradictory flags: --run-mode live cannot be combined with --fixtures-dir.",
            file=sys.stderr,
        )
        return 2
    run_mode = str(args.run_mode or ("trial" if fixtures_dir is not None else "live"))
    if args.health_file:
        health_state_file = Path(args.health_file).resolve()
    elif run_mode == "trial" or fixtures_dir is not None:
        health_state_file = state_dir / "trial_source_health.json"
    else:
        health_state_file = state_dir / "source_health.json"

    health_tracker = SourceHealthTracker(
        state_file=health_state_file,
        alert_threshold_days=int(repo_cfg.sources.get("rate_limits", {}).get("consecutive_failure_alert_days", 3)),
    )

    raw_candidates, health_alerts = collect_all_sources(
        repo_cfg.sources,
        run_date=issue_date,
        health_tracker=health_tracker,
        fixtures_dir=fixtures_dir,
    )
    deduped_candidates, merged_count = deduplicate_candidates(
        raw_candidates,
        content_dir=content_dir,
        target_date=issue_date,
        window_days=int(repo_cfg.profile.get("budgets", {}).get("dedup_window_days", 7)),
    )

    llm_client = None if args.deterministic else DualChannelLLMClient.from_user_config(cfg_home)
    source_origin = "local_fixtures" if fixtures_dir is not None else "live_web"
    issue = build_daily_issue(
        deduped_candidates,
        repo_cfg,
        issue_date=issue_date,
        llm_client=llm_client,
        source_health_alerts=health_alerts,
        feedback_file=state_dir / "feedback.jsonl",
        run_mode=run_mode,
        source_origin=source_origin,
    )

    issue_file = _write_issue_json(content_dir, issue)

    # Mandatory pre-publish secret & confidentiality scan before any build or git commit!
    extra_terms = load_extra_sensitive_terms(cfg_home)
    findings = scan_directory(content_dir, extra_terms=extra_terms)
    if findings:
        raise RuntimeError(f"Secret scan blocked generated issue {issue_file}: {findings}")

    site_summary = build_static_site(content_dir=content_dir, repo_cfg=repo_cfg, out_dir=out_dir)
    out_findings = scan_directory(out_dir, extra_terms=extra_terms)
    if out_findings:
        raise RuntimeError(f"Secret scan blocked rendered _site: {out_findings}")

    if args.push_git:
        subprocess.run(["git", "add", str(issue_file.relative_to(repo_root))], cwd=repo_root, check=True)
        diff_chk = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_root)
        if diff_chk.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", f"chore(content): daily radar {issue_date}"],
                cwd=repo_root,
                check=True,
            )
            for attempt in range(3):
                push_proc = subprocess.run(["git", "push", "origin", "main"], cwd=repo_root)
                if push_proc.returncode == 0:
                    break
                if attempt == 2:
                    raise subprocess.CalledProcessError(push_proc.returncode, ["git", "push", "origin", "main"])
                time.sleep(2.0 * (2**attempt))

    degraded_note = ""
    base_url = str(repo_cfg.profile["site"]["public_base_url"]).rstrip("/")
    if args.wait_pages:
        daily_page_url = f"{base_url}/daily/{issue_date}/index.html"
        deadline_hms = str(repo_cfg.profile["site"].get("pages_ready_deadline_cst", "09:58:00"))
        ok_200 = poll_pages_http_200(daily_page_url, deadline_cst_hms=deadline_hms)
        if not ok_200:
            degraded_note = f"GitHub Pages 截至 {deadline_hms} 仍在部署构建中，若点击 404 请稍候 2 分钟刷新。"

    if args.wait_until:
        wait_until_beijing_time(args.wait_until, now_fn=lambda: datetime.now(SHANGHAI_TZ))

    if args.send_feishu:
        send_feishu_webhook(
            issue,
            public_base_url=base_url,
            config_home=cfg_home,
            pages_degraded_note=degraded_note,
        )

    print(
        json.dumps(
            {
                "date": issue_date,
                "run_mode": run_mode,
                "health_state_file": str(health_state_file),
                "raw_candidates": len(raw_candidates),
                "deduped_candidates": len(deduped_candidates),
                "merged_or_skipped": merged_count,
                "featured_count": len(issue.featured_items),
                "folded_count": len(issue.folded_items),
                "issue_json": str(issue_file),
                "site": site_summary,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


FEEDBACK_TOKEN_RE = re.compile(
    r"#雷达反馈\s*(👍|👎|up|down|\+1|-1)\s*(\d{4}-\d{2}-\d{2})/([a-z0-9-]+)(?:\s*\[([^\]]+)\])?",
    re.IGNORECASE,
)


def record_feedback_token(
    token_text: str,
    *,
    content_dir: Path,
    feedback_file: Path,
    extra_keywords: list[str] | None = None,
) -> dict[str, object]:
    """Parse `#雷达反馈 👍 YYYY-MM-DD/slug [track]` copied from detail pages into `state/feedback.jsonl`."""
    m = FEEDBACK_TOKEN_RE.search(token_text.strip())
    if not m:
        raise ValueError(
            f"Unrecognized feedback token format: {token_text!r} "
            "(expected '#雷达反馈 👍 YYYY-MM-DD/<slug> [<track>]')"
        )
    raw_vote, issue_date, slug, track_hint = m.group(1), m.group(2), m.group(3), (m.group(4) or "").strip()
    vote = "up" if raw_vote in ("👍", "up", "+1") else "down"

    keywords: list[str] = [k.strip().lower() for k in (extra_keywords or []) if k.strip()]
    yyyy, mm, _ = issue_date.split("-")
    issue_file = content_dir / yyyy / mm / f"{issue_date}.json"
    if issue_file.is_file():
        issue = DailyIssue.from_dict(json.loads(issue_file.read_text(encoding="utf-8")))
        matched_item = next((it for it in issue.featured_items if it.slug == slug), None)
        if matched_item is not None:
            from daily_radar.dedup import title_tokens

            for sub in matched_item.subtopics:
                if sub not in keywords:
                    keywords.append(sub)
            for tok in sorted(title_tokens(matched_item.title_en))[:6]:
                if len(tok) >= 4 and tok not in keywords:
                    keywords.append(tok)
    if not keywords and track_hint:
        keywords.append(track_hint.lower())

    entry: dict[str, object] = {
        "date": issue_date,
        "slug": slug,
        "vote": vote,
        "track": track_hint,
        "keywords": keywords,
    }
    feedback_file.parent.mkdir(parents=True, exist_ok=True)
    with feedback_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def cmd_record_feedback(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    content_dir = Path(args.content_dir).resolve() if args.content_dir else (repo_root / "content")
    feedback_file = Path(args.feedback_file).resolve() if args.feedback_file else (repo_root / "state" / "feedback.jsonl")
    extra_kw = [x.strip() for x in args.keywords.split(",") if x.strip()] if args.keywords else []
    entry = record_feedback_token(
        args.token,
        content_dir=content_dir,
        feedback_file=feedback_file,
        extra_keywords=extra_kw,
    )
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    return 0


def cmd_serve_local(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    site_dir = Path(args.site_dir).resolve() if args.site_dir else (repo_root / "_site")
    handler = partial(SimpleHTTPRequestHandler, directory=str(site_dir))
    with ThreadingHTTPServer((args.host, args.port), handler) as httpd:
        print(f"Serving {site_dir} at http://{args.host}:{args.port}/ (Ctrl+C to stop)")
        httpd.serve_forever()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="daily-radar", description="Daily Research & Industry Radar CLI")
    p.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Path to daily-radar repository root",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_init = sub.add_parser("init-config", help="Initialize 0600 credential stubs in ~/.config/daily-radar")
    sp_init.add_argument("--config-home", default="")
    sp_init.set_defaults(func=cmd_init_config)

    sp_wh = sub.add_parser("configure-webhook", help="Interactively save Feishu webhook URL & HMAC secret (mode 0600)")
    sp_wh.add_argument("--config-home", default="")
    sp_wh.add_argument("--webhook-url", default="")
    sp_wh.add_argument("--secret", default=None)
    sp_wh.add_argument(
        "--verify-send",
        action="store_true",
        help="Immediately send the latest issue card to verify the new Feishu group webhook",
    )
    sp_wh.set_defaults(func=cmd_configure_webhook)

    sp_scan = sub.add_parser("scan-secrets", help="Scan directory for secrets, private IPs/paths, and binary files")
    sp_scan.add_argument("--target", default="")
    sp_scan.add_argument("--config-home", default="")
    sp_scan.add_argument("--include-tests", action="store_true")
    sp_scan.set_defaults(func=cmd_scan_secrets)

    sp_ver = sub.add_parser("verify-numbers", help="Verify numerical claims in an issue JSON against verbatim source")
    sp_ver.add_argument("issue_json")
    sp_ver.add_argument("--write", action="store_true")
    sp_ver.set_defaults(func=cmd_verify_numbers)

    sp_build = sub.add_parser("build-site", help="Build 3-tier static HTML site from content/ JSON files")
    sp_build.add_argument("--content-dir", default="")
    sp_build.add_argument("--config-dir", default="")
    sp_build.add_argument("--out-dir", default="")
    sp_build.add_argument("--hot-years", type=int, default=None)
    sp_build.set_defaults(func=cmd_build_site)

    sp_feishu = sub.add_parser("send-feishu", help="Send (or --dry-run preview) Feishu morning briefing card")
    sp_feishu.add_argument("issue_json")
    sp_feishu.add_argument("--config-home", default="")
    sp_feishu.add_argument("--dry-run", action="store_true")
    sp_feishu.add_argument("--secret", default="")
    sp_feishu.set_defaults(func=cmd_send_feishu)

    sp_health = sub.add_parser(
        "check-health",
        help="Strictly validate source_health.json schema (fail-fast) and report failing sources",
    )
    sp_health.add_argument("--health-file", default="")
    sp_health.add_argument(
        "--strict-sources",
        action="store_true",
        help="Also verify every enabled source in config/sources.yaml is present in the health snapshot",
    )
    sp_health.add_argument("--json", action="store_true", help="Emit machine-readable JSON report")
    sp_health.set_defaults(func=cmd_check_health)

    sp_run = sub.add_parser("run", help="Run full daily pipeline: collect -> dedup -> filter -> verify -> scan -> build")
    sp_run.add_argument("--date", default="")
    sp_run.add_argument("--content-dir", default="")
    sp_run.add_argument("--out-dir", default="")
    sp_run.add_argument("--fixtures-dir", default="")
    sp_run.add_argument("--config-home", default="")
    sp_run.add_argument("--health-file", default="", help="Override source health JSON path (auto-isolated for trial runs)")
    sp_run.add_argument("--run-mode", choices=("live", "trial"), default="", help="Explicitly label issue provenance as live or trial")
    sp_run.add_argument("--deterministic", action="store_true")
    sp_run.add_argument("--push-git", action="store_true")
    sp_run.add_argument("--wait-pages", action="store_true")
    sp_run.add_argument("--wait-until", default="")
    sp_run.add_argument("--send-feishu", action="store_true")
    sp_run.set_defaults(func=cmd_run)

    sp_fb = sub.add_parser("record-feedback", help="Record `#雷达反馈 👍/👎 ...` token into state/feedback.jsonl")
    sp_fb.add_argument("token", help="Feedback token string copied from item detail page")
    sp_fb.add_argument("--content-dir", default="")
    sp_fb.add_argument("--feedback-file", default="")
    sp_fb.add_argument("--keywords", default="", help="Optional comma-separated keywords override")
    sp_fb.set_defaults(func=cmd_record_feedback)

    sp_serve = sub.add_parser("serve-local", help="Serve rendered _site/ locally for browser preview")
    sp_serve.add_argument("--site-dir", default="")
    sp_serve.add_argument("--host", default="127.0.0.1")
    sp_serve.add_argument("--port", type=int, default=8765)
    sp_serve.set_defaults(func=cmd_serve_local)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
