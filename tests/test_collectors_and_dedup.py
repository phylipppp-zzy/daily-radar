"""Tests for HTTP/XML/JSON/RSS collectors, Latent Space [AINews] splitting, 3-day health alerts, and 7-day dedup."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from daily_radar.cli import main as cli_main
from daily_radar.collectors import (
    HealthCheckSchemaError,
    SourceHealthTracker,
    build_openalex_arxiv_url,
    check_source_health,
    collect_all_sources,
    fetch_arxiv_html_excerpt,
    parse_conference_accepted_json,
    parse_github_trending,
    parse_rss_or_atom,
)
from daily_radar.dedup import deduplicate_candidates, normalize_arxiv_id
from daily_radar.models import DailyIssue, FoldedItem, FourElements, MetricBadge, RadarItem, RawCandidate


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "real_2026_11_feeds"


class _MockFeedServerHandler(BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes, str]] = {}
    hit_counts: dict[str, int] = {}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        self.hit_counts[path] = self.hit_counts.get(path, 0) + 1
        if path == "/flaky" and self.hit_counts[path] == 1:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b"Bad Gateway")
            return
        status, body, ctype = self.routes.get(path, (404, b"Not Found", "text/plain"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class CollectorsAndDedupTest(unittest.TestCase):
    def test_arxiv_html_excerpt_prefers_clean_sections_and_soft_fails(self) -> None:
        _MockFeedServerHandler.hit_counts = {}
        _MockFeedServerHandler.routes = {
            "/html/2611.04073v2": (
                200,
                b"""
                <html><head><title>Metadata noise</title></head><body>
                  <header>Header noise</header><nav>Navigation noise</nav>
                  <article>
                    <h1>Paper title</h1>
                    <h2>Related Work</h2><p>Related-work text.</p>
                    <h2><span>1</span> Introduction</h2>
                    <p>We study co<em>operation</em> &amp; planning.</p>
                    <h3>Motivation</h3><p>We measure teamwork.</p>
                    <h2>2 Methodology</h2>
                    <p>The model uses <math><mi>equation noise</mi></math> shared state.</p>
                    <figure><figcaption>Caption noise</figcaption></figure>
                    <svg><text>SVG noise</text></svg>
                    <script>Script noise</script><style>Style noise</style>
                    <h2>3 Experiments</h2><p>Three trials.</p>
                    <h2>4 Limitations</h2><p>One environment.</p>
                    <h2>5 Conclusion</h2><p>More trials are needed.</p>
                    <h2>References</h2><p>Reference text.</p>
                  </article>
                  <footer>Footer noise</footer>
                </body></html>
                """,
                "text/html",
            ),
            "/html/2611.04074": (
                200,
                b'<div class="ltx_page_header"><div>Chrome noise</div></div><p>Fallback&nbsp;text.</p>',
                "text/html",
            ),
        }
        with ThreadingHTTPServer(("127.0.0.1", 0), _MockFeedServerHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}/html/"
                excerpt = fetch_arxiv_html_excerpt("2611.04073v2", base_url=base_url)
                self.assertEqual(
                    excerpt,
                    "1 Introduction\nWe study cooperation & planning.\n\n"
                    "Motivation\nWe measure teamwork.\n\n"
                    "2 Methodology\nThe model uses shared state.\n\n"
                    "3 Experiments\nThree trials.\n\n"
                    "4 Limitations\nOne environment.\n\n"
                    "5 Conclusion\nMore trials are needed.",
                )
                truncated = fetch_arxiv_html_excerpt("2611.04073v2", base_url=base_url, max_chars=26)
                self.assertEqual(truncated, "1 Introduction\nWe study")
                self.assertEqual(fetch_arxiv_html_excerpt("2611.04074", base_url=base_url), "Fallback text.")
                self.assertEqual(fetch_arxiv_html_excerpt("2611.04075", base_url=base_url), "")
            finally:
                server.shutdown()
                thread.join()

    def test_live_http_collectors_retry_and_ainews_split(self) -> None:
        _MockFeedServerHandler.hit_counts = {}
        _MockFeedServerHandler.routes = {
            "/arxiv.xml": (200, (FIXTURES_DIR / "arxiv-gui-cua.xml").read_bytes(), "application/atom+xml"),
            "/hf.json": (200, (FIXTURES_DIR / "hf-daily-papers.json").read_bytes(), "application/json"),
            "/verl.json": (200, (FIXTURES_DIR / "gh-release-verl.json").read_bytes(), "application/json"),
            "/flaky": (200, (FIXTURES_DIR / "blog-anthropic.xml").read_bytes(), "application/rss+xml"),
            "/latent.xml": (200, (FIXTURES_DIR / "blog-latent-space.xml").read_bytes(), "application/rss+xml"),
            "/dead.xml": (404, b"Gone", "text/plain"),
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockFeedServerHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                tracker = SourceHealthTracker(Path(tmp) / "health.json", alert_threshold_days=3)
                sources_cfg = {
                    "rate_limits": {"default_per_host_interval_sec": 0.0, "arxiv_interval_sec": 0.0},
                    "sources": [
                        {"id": "ax", "name": "arXiv", "kind": "arxiv_api", "section": "papers", "url": f"http://127.0.0.1:{port}/arxiv.xml"},
                        {"id": "hf", "name": "HF", "kind": "hf_daily_papers", "section": "papers", "url": f"http://127.0.0.1:{port}/hf.json"},
                        {"id": "gh", "name": "verl", "kind": "github_releases", "repo": "verl-project/verl", "section": "opensource", "url": f"http://127.0.0.1:{port}/verl.json"},
                        {"id": "anth", "name": "Anthropic", "kind": "rss", "org": "Anthropic", "section": "industry", "url": f"http://127.0.0.1:{port}/flaky"},
                        {"id": "ls", "name": "Latent Space", "kind": "rss", "org": "Latent Space", "split_ainews": True, "section": "industry", "url": f"http://127.0.0.1:{port}/latent.xml"},
                        {"id": "dead", "name": "DeadFeed", "kind": "rss", "section": "industry", "url": f"http://127.0.0.1:{port}/dead.xml"},
                    ],
                }
                # Day 1 & Day 2 failures on dead.xml must NOT alert yet (<3 days), while flaky 502 recovers via retry!
                cands_d1, alerts_d1 = collect_all_sources(sources_cfg, run_date="2026-11-22", health_tracker=tracker)
                self.assertEqual(alerts_d1, [])
                self.assertGreaterEqual(_MockFeedServerHandler.hit_counts["/flaky"], 2)

                _, alerts_d2 = collect_all_sources(sources_cfg, run_date="2026-11-23", health_tracker=tracker)
                self.assertEqual(alerts_d2, [])

                # Day 3 consecutive failure on dead.xml MUST emit an explicit health alert!
                cands_d3, alerts_d3 = collect_all_sources(sources_cfg, run_date="2026-11-24", health_tracker=tracker)
                self.assertEqual(len(alerts_d3), 1)
                self.assertIn("连续 3 天", alerts_d3[0])
                self.assertIn("DeadFeed", alerts_d3[0])

                # Verify Latent Space [AINews] split into 6 standalone items instead of 1 giant newsletter!
                ls_items = [c for c in cands_d3 if c.source_id == "ls"]
                self.assertEqual(len(ls_items), 6)
                qwen_item = next(c for c in ls_items if "Qwen4-Max" in c.title)
                self.assertEqual(qwen_item.url, "https://qwen.ai/blog/qwen4-max")

                # Recovery on Day 4 resets consecutive failure streak
                _MockFeedServerHandler.routes["/dead.xml"] = (
                    200,
                    (FIXTURES_DIR / "blog-anthropic.xml").read_bytes(),
                    "application/rss+xml",
                )
                _, alerts_d4 = collect_all_sources(sources_cfg, run_date="2026-11-25", health_tracker=tracker)
                self.assertEqual(alerts_d4, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_conference_accepted_json_parser(self) -> None:
        sample = json.dumps(
            [
                {
                    "name": "OS-Omni: Cross-Platform Computer Use Benchmark",
                    "abstract": "1,200 tasks across 6 operating systems.",
                    "decision": "Poster",
                    "track": "Evaluations_and_Datasets_Track",
                    "virtualsite_url": "https://neurips.cc/virtual/2026/poster/139359",
                    "authors": [{"fullname": "Tao Yu", "institution": "The University of Hong Kong"}],
                }
            ]
        ).encode("utf-8")
        cands = parse_conference_accepted_json(
            sample,
            {"id": "neurips26", "name": "NeurIPS 2026", "conference_name": "NeurIPS 2026", "conference_slug": "neurips-2026"},
            "2026-12-06",
        )
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].extra["conference_slug"], "neurips-2026")
        self.assertIn("Evaluations_and_Datasets_Track", cands[0].venue)
        self.assertEqual(cands[0].institutions, ["The University of Hong Kong"])

    def test_seven_day_dedup_and_canonical_merge(self) -> None:
        self.assertEqual(normalize_arxiv_id("http://arxiv.org/abs/2611.04073v2"), "2611.04073")
        with tempfile.TemporaryDirectory() as tmp:
            content_dir = Path(tmp)
            # Issue 5 days ago (within 7-day window): should block 2610.03818
            recent_issue = DailyIssue(
                date="2026-11-19",
                weekday_zh="周四",
                generated_at_cst="2026-11-19 09:20:00 CST",
                global_trend_summary=["test"],
                highlights_top3=[],
                featured_items=[],
                folded_items=[
                    FoldedItem(
                        slug="fold-old",
                        section="papers",
                        track="gui-rl",
                        track_label="GUI-RL",
                        title_zh="Control OSWorld",
                        title_en="Control OSWorld: An AI Control Environment for GUI Computer Use Agents",
                        one_line_takeaway="seen 5 days ago",
                        url="https://arxiv.org/abs/2610.03818",
                        published_date="2026-11-19",
                        venue_or_source="arXiv",
                        arxiv_id="2610.03818",
                    )
                ],
            )
            # Issue 10 days ago (outside 7-day window): should NOT block 2601.99999
            expired_issue = DailyIssue(
                date="2026-11-10",
                weekday_zh="周二",
                generated_at_cst="2026-11-10 09:20:00 CST",
                global_trend_summary=["old"],
                highlights_top3=[],
                featured_items=[],
                folded_items=[
                    FoldedItem(
                        slug="fold-very-old",
                        section="papers",
                        track="gui-rl",
                        track_label="GUI-RL",
                        title_zh="Expired Paper",
                        title_en="Expired Paper Outside Seven Day Window",
                        one_line_takeaway="seen 14 days ago",
                        url="https://arxiv.org/abs/2601.99999",
                        published_date="2026-11-10",
                        venue_or_source="arXiv",
                        arxiv_id="2601.99999",
                    )
                ],
            )
            for iss in (recent_issue, expired_issue):
                p = content_dir / "2026" / "11" / f"{iss.date}.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(iss.to_dict()), encoding="utf-8")

            batch = [
                # Aggregator arrives BEFORE canonical arXiv in input list, but canonical arXiv must win!
                RawCandidate(
                    source_id="hf",
                    source_name="HF Daily Papers",
                    section="papers",
                    title="Computer-Use Agents Gain Little Benefit from a World Model When Conditioned on Tutorials",
                    url="https://huggingface.co/papers/2611.04073?utm_source=rss",
                    published_at="2026-11-24",
                    summary_raw="Short HF summary",
                    arxiv_id="2611.04073",
                    github_stars=142,
                    hf_url="https://huggingface.co/papers/2611.04073",
                    extra={"source_kind": "hf_daily_papers"},
                ),
                RawCandidate(
                    source_id="ax",
                    source_name="arXiv API",
                    section="papers",
                    title="Computer-Use Agents Gain Little Benefit from a World Model When Conditioned on Tutorials",
                    url="https://arxiv.org/abs/2611.04073v2",
                    published_at="2026-11-24",
                    summary_raw="Full verbatim arXiv abstract.",
                    authors=["Anh Nguyen"],
                    arxiv_id="2611.04073v2",
                    extra={"source_kind": "arxiv_api"},
                ),
                # Seen 5 days ago -> must be dropped
                RawCandidate(
                    source_id="ax",
                    source_name="arXiv API",
                    section="papers",
                    title="Control OSWorld: An AI Control Environment for GUI Computer Use Agents",
                    url="https://arxiv.org/abs/2610.03818v3",
                    published_at="2026-11-24",
                    summary_raw="Updated typo.",
                    arxiv_id="2610.03818v3",
                    extra={"source_kind": "arxiv_api"},
                ),
                # Seen 14 days ago (>7d window) -> allowed
                RawCandidate(
                    source_id="ax",
                    source_name="arXiv API",
                    section="papers",
                    title="Expired Paper Outside Seven Day Window",
                    url="https://arxiv.org/abs/2601.99999v2",
                    published_at="2026-11-24",
                    summary_raw="Major v2 camera ready.",
                    arxiv_id="2601.99999v2",
                    extra={"source_kind": "arxiv_api"},
                ),
            ]
            kept, dropped = deduplicate_candidates(batch, content_dir=content_dir, target_date="2026-11-24", window_days=7)
            self.assertEqual(dropped, 2)
            self.assertEqual(len(kept), 2)
            primary = next(c for c in kept if c.arxiv_id == "2611.04073")
            self.assertEqual(primary.source_id, "ax")
            self.assertEqual(primary.github_stars, 142)
            self.assertIn("HF Daily Papers", primary.corroborating_sources)

    def test_recency_filter_xml_control_chars_trending_and_gh_repo_dedup(self) -> None:
        # 1. XML with illegal control chars (\x0c, &#x08;) + stale 2023 entry vs fresh 2026-11-24 entry
        dirty_rss = (
            b"\xef\xbb\xbf<?xml version='1.0' encoding='UTF-8'?>\n"
            b"<rss version='2.0'><channel><title>Dirty\x0cFeed</title>"
            b"<item><title>Fresh Agent Post&#x08;</title><link>https://example.org/fresh</link>"
            b"<pubDate>Tue, 24 Nov 2026 01:00:00 GMT</pubDate><description>Fresh\x07 body</description></item>"
            b"<item><title>Ancient 2023 Post</title><link>https://example.org/2023</link>"
            b"<pubDate>Fri, 23 Jun 2023 01:00:00 GMT</pubDate><description>Stale archive</description></item>"
            b"</channel></rss>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            fdir = Path(tmp)
            (fdir / "dirty.xml").write_bytes(dirty_rss)
            (fdir / "trend.html").write_bytes((FIXTURES_DIR / "gh-trending-python.html").read_bytes())
            cfg = {
                "rate_limits": {"max_age_days": 5},
                "sources": [
                    {"id": "dirty", "name": "DirtyRSS", "kind": "rss", "section": "industry", "url": "https://example.org/rss"},
                    {"id": "trend", "name": "GHTrend", "kind": "github_trending", "section": "opensource", "url": "https://github.com/trending"},
                ],
            }
            cands, alerts = collect_all_sources(cfg, run_date="2026-11-24", fixtures_dir=fdir)
            self.assertEqual(alerts, [])
            titles = [c.title for c in cands]
            self.assertIn("Fresh Agent Post", titles)
            self.assertNotIn("Ancient 2023 Post", titles)
            self.assertTrue(any(c.github_repo == "xlang-ai/osworld" and c.github_stars == 4820 for c in cands))

            # Also verify JSON format in parse_github_trending
            json_trend = parse_github_trending(
                json.dumps({"items": [{"full_name": "verl-project/verl", "description": "RL trainer", "stargazers_count": 9000}]}).encode(),
                {"id": "gt", "name": "GT"},
                "2026-11-24",
            )
            self.assertEqual(len(json_trend), 1)
            self.assertEqual(json_trend[0].github_stars, 9000)

            # 2. Cross-day `gh_repo:` deduplication for opensource repos even when URL path differs
            cdir = Path(tmp) / "content"
            day1_issue = DailyIssue(
                date="2026-11-23",
                weekday_zh="周一",
                generated_at_cst="2026-11-23 09:20:00 CST",
                global_trend_summary=["d1"],
                highlights_top3=[],
                featured_items=[
                    RadarItem(
                        slug="vlm-infra-sglang-torchtpu",
                        section="opensource",
                        track="vlm-infra",
                        track_label="VLM/RL底座",
                        subtopics=["inference-engine"],
                        lab_tags=[],
                        institution_tags=["SGLang"],
                        title_zh="SGLang TorchTPU 插件开源",
                        title_en="SGLang TorchTPU Plugin",
                        one_line_pain="针对多模态低延迟推理跨硬件部署痛点，提供原生 PyTorch TPU 后端扩展并保持接口兼容与稳定吞吐表现。",
                        metric_badges=[MetricBadge(text="Native TPU")],
                        openness="code_only",
                        openness_label="仅代码开源",
                        small_compute_friendly=False,
                        small_compute_note="",
                        authors=["sgl-project"],
                        published_date="2026-11-23",
                        venue_or_source="GitHub Trending",
                        url_primary="https://github.com/sgl-project/sglang/tree/main/python",
                        url_github="https://github.com/sgl-project/sglang",
                        abstract_en="Native TPU backend.",
                        four_elements=FourElements("痛点", "机制", "实测", "参考"),
                    )
                ],
                folded_items=[],
            )
            p = cdir / "2026" / "11" / "2026-11-23.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(day1_issue.to_dict()), encoding="utf-8")

            day2_cand = RawCandidate(
                source_id="gt",
                source_name="GitHub Trending",
                section="opensource",
                title="sgl-project/sglang: Completely Different Trending Description Today",
                url="https://github.com/sgl-project/sglang",
                published_at="2026-11-24",
                summary_raw="Fast serving framework.",
                github_repo="sgl-project/sglang",
            )
            kept2, dropped2 = deduplicate_candidates([day2_cand], content_dir=cdir, target_date="2026-11-24", window_days=7)
            self.assertEqual(kept2, [])
            self.assertEqual(dropped2, 1)

    def test_trial_vs_live_state_isolation_and_provenance(self) -> None:
        """Trial runs with --fixtures-dir must isolate state to trial_source_health.json and label run_mode='trial'."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            # Symlink config/ into isolated fake repo root
            (tmp_root / "config").symlink_to(REPO_ROOT / "config")
            state_dir = tmp_root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            real_health = state_dir / "source_health.json"
            sentinel_health = {
                "sources": {
                    "blog-anthropic": {
                        "last_ok_date": "",
                        "consecutive_failed_dates": ["2026-11-24"],
                        "last_error": "HTTPError: HTTP Error 404: Not Found",
                    }
                }
            }
            real_health.write_text(json.dumps(sentinel_health, indent=2), encoding="utf-8")

            content_dir = tmp_root / "content"
            out_dir = tmp_root / "_site"
            rc = cli_main(
                [
                    "--repo-root",
                    str(tmp_root),
                    "run",
                    "--date",
                    "2026-11-24",
                    "--fixtures-dir",
                    str(FIXTURES_DIR),
                    "--content-dir",
                    str(content_dir),
                    "--out-dir",
                    str(out_dir),
                    "--deterministic",
                ]
            )
            self.assertEqual(rc, 0)

            # 1. Live state/source_health.json MUST remain untouched!
            self.assertEqual(json.loads(real_health.read_text(encoding="utf-8")), sentinel_health)
            # 2. Trial state MUST be written to state/trial_source_health.json instead
            trial_health = state_dir / "trial_source_health.json"
            self.assertTrue(trial_health.is_file())
            trial_report = check_source_health(trial_health)
            self.assertTrue(trial_report.is_healthy)
            self.assertGreater(trial_report.total_sources, 0)

            # 3. Generated issue JSON & HTML MUST explicitly label run_mode="trial" and provenance
            issue_data = json.loads((content_dir / "2026" / "11" / "2026-11-24.json").read_text(encoding="utf-8"))
            self.assertEqual(issue_data["run_mode"], "trial")
            self.assertEqual(issue_data["provenance"]["run_mode"], "trial")
            self.assertEqual(issue_data["provenance"]["source_origin"], "local_fixtures")
            self.assertEqual(issue_data["provenance"]["distiller"], "deterministic-verified")
            rendered_html = (out_dir / "daily" / "2026-11-24" / "index.html").read_text(encoding="utf-8")
            self.assertIn('data-run-mode="trial"', rendered_html)
            self.assertIn("TRIAL 演练样本", rendered_html)

            # 4. Reject contradictory --run-mode live + --fixtures-dir (exit 2)
            rc_contra = cli_main(
                [
                    "--repo-root",
                    str(tmp_root),
                    "run",
                    "--date",
                    "2026-11-24",
                    "--run-mode",
                    "live",
                    "--fixtures-dir",
                    str(FIXTURES_DIR),
                    "--content-dir",
                    str(content_dir),
                    "--out-dir",
                    str(out_dir),
                    "--deterministic",
                ]
            )
            self.assertEqual(rc_contra, 2)

    def test_strict_health_check_fail_fast_and_five_error_snapshot_replay(self) -> None:
        """check-health must fail fast on missing/malformed schema fields and accurately report 5 failures & all-green."""
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            # 1. Deterministic 24-source all-green snapshot -> healthy, rc == 0
            all_ok_24 = {
                f"ok-src-{i}": {
                    "last_ok_date": "2026-11-24",
                    "consecutive_failed_dates": [],
                    "last_error": "",
                }
                for i in range(24)
            }
            snap_ok = tdir / "all_ok.json"
            snap_ok.write_text(json.dumps({"sources": all_ok_24}, indent=2), encoding="utf-8")
            ok_report = check_source_health(snap_ok)
            self.assertTrue(ok_report.is_healthy)
            self.assertEqual(ok_report.total_sources, 24)
            self.assertEqual(len(ok_report.ok_sources), 24)
            self.assertEqual(cli_main(["check-health", "--health-file", str(snap_ok)]), 0)

            # 2. Replay the 5-error audit snapshot (19 ok + 5 failing = 24 total)
            sources_24: dict[str, dict[str, object]] = {
                f"ok-src-{i}": {
                    "last_ok_date": "2026-11-24",
                    "consecutive_failed_dates": [],
                    "last_error": "",
                }
                for i in range(19)
            }
            sources_24.update(
                {
                    "blog-anthropic": {
                        "last_ok_date": "",
                        "consecutive_failed_dates": ["2026-11-24"],
                        "last_error": "HTTPError: HTTP Error 404: Not Found",
                        "source_name": "Anthropic Research & Engineering",
                    },
                    "blog-qwen": {
                        "last_ok_date": "",
                        "consecutive_failed_dates": ["2026-11-24"],
                        "last_error": "HTTPError: HTTP Error 404: Not Found",
                        "source_name": "Qwen Official Blog",
                    },
                    "blog-deepmind": {
                        "last_ok_date": "",
                        "consecutive_failed_dates": ["2026-11-24"],
                        "last_error": "ParseError: not well-formed (invalid token)",
                        "source_name": "Google DeepMind Blog",
                    },
                    "arxiv-gui-cua": {
                        "last_ok_date": "",
                        "consecutive_failed_dates": ["2026-11-24"],
                        "last_error": "TimeoutError: The read operation timed out",
                        "source_name": "arXiv Daily (GUI Agent / Computer Use / RL)",
                    },
                    "arxiv-ossl-ood": {
                        "last_ok_date": "",
                        "consecutive_failed_dates": ["2026-11-24"],
                        "last_error": "HTTPError: HTTP Error 429: Too Many Requests",
                        "source_name": "arXiv Daily (Open-Set SSL / VLM OOD / GCD)",
                    },
                }
            )
            snap_5 = tdir / "five_errors.json"
            snap_5.write_text(json.dumps({"sources": sources_24}, indent=2), encoding="utf-8")

            report = check_source_health(snap_5)
            self.assertFalse(report.is_healthy)
            self.assertEqual(report.total_sources, 24)
            self.assertEqual(len(report.ok_sources), 19)
            self.assertEqual(len(report.failing_sources), 5)
            self.assertEqual(
                {f.source_id for f in report.failing_sources},
                {"blog-anthropic", "blog-qwen", "blog-deepmind", "arxiv-gui-cua", "arxiv-ossl-ood"},
            )
            rc_fail = cli_main(["check-health", "--health-file", str(snap_5), "--json"])
            self.assertEqual(rc_fail, 1)

            # 3. Fail-fast on legacy/missing schema, non-UTF-8 bytes, malformed dates, and blank healthy entry
            bad_cases = [
                ("bad_schema.json", json.dumps({"sources": {"blog-anthropic": {"consecutive_failures": 0}}}).encode()),
                ("bad_utf8.json", b"\xff\xfe\x00invalid"),
                (
                    "bad_date.json",
                    json.dumps(
                        {
                            "sources": {
                                "s1": {
                                    "last_ok_date": "not-a-date",
                                    "consecutive_failed_dates": [],
                                    "last_error": "",
                                }
                            }
                        }
                    ).encode(),
                ),
                (
                    "blank_ok.json",
                    json.dumps(
                        {
                            "sources": {
                                "s1": {
                                    "last_ok_date": "",
                                    "consecutive_failed_dates": [],
                                    "last_error": "",
                                }
                            }
                        }
                    ).encode(),
                ),
            ]
            for fname, raw_b in bad_cases:
                p = tdir / fname
                p.write_bytes(raw_b)
                with self.assertRaises(HealthCheckSchemaError):
                    check_source_health(p)
                self.assertEqual(cli_main(["check-health", "--health-file", str(p)]), 2)

            # 4. If mutable gitignored state/source_health.json exists locally, validate schema only (never assume failures)
            local_state = REPO_ROOT / "state" / "source_health.json"
            if local_state.is_file():
                real_report = check_source_health(local_state)
                self.assertEqual(
                    len(real_report.ok_sources) + len(real_report.failing_sources),
                    real_report.total_sources,
                )

    def test_arxiv_openalex_fallback_and_malformed_xml_and_fallback_urls(self) -> None:
        """Verify arXiv Timeout/429 -> OpenAlex S4306400194 fallback, DOI safety, unescaped XML `&`, and multi-feed union."""
        openalex_payload = json.dumps(
            {
                "results": [
                    {
                        "id": "https://openalex.org/W9990001",
                        # Publisher DOI with 4.5-digit substring MUST NOT corrupt real arxiv_id!
                        "doi": "https://doi.org/10.1145/3290605.3300766",
                        "title": "OSWorld-Fallback: Robust GUI Agent Training",
                        "publication_date": "2026-11-24",
                        "ids": {"doi": "https://doi.org/10.1145/3290605.3300766"},
                        "primary_location": {"landing_page_url": "https://arxiv.org/abs/2611.90123"},
                        "authorships": [
                            {
                                "author": {"display_name": "Jane Researcher"},
                                "institutions": [{"display_name": "Test University"}],
                            }
                        ],
                        "abstract_inverted_index": {
                            "GUI": [0],
                            "agent": [1],
                            "RL": [2],
                            "on": [3],
                            "OSWorld": [4],
                            "improves": [5],
                            "by": [6],
                            "+4.2%.": [7],
                        },
                    }
                ]
            }
        ).encode("utf-8")

        deepmind_unescaped_xml = (
            b"   \xef\xbb\xbf<?xml version='1.0' encoding='UTF-8'?>"
            b"<rss version='2.0'><channel><title>DeepMind Research & Engineering</title>"
            b"<item><title>Gemini &amp; World Models & Safety&nbsp;&mdash; Update</title>"
            b"<link>https://deepmind.google/blog/gemini-world-models</link>"
            b"<pubDate>Tue, 24 Nov 2026 08:00:00 GMT</pubDate>"
            b"<description>Covers R&D breakthroughs & evaluations.</description></item>"
            b"</channel></rss>"
        )
        stale_rss = (
            b"<?xml version='1.0' encoding='UTF-8'?>"
            b"<rss version='2.0'><channel><title>Stale Feed</title>"
            b"<item><title>Old 2023 Release</title><link>https://example.org/2023</link>"
            b"<pubDate>Fri, 23 Jun 2023 01:00:00 GMT</pubDate><description>Archived</description></item>"
            b"</channel></rss>"
        )

        # Verify malformed XML with bare `&` and HTML named entities parses cleanly
        dm_cands = parse_rss_or_atom(
            deepmind_unescaped_xml,
            {"id": "blog-deepmind", "name": "Google DeepMind Blog", "org": "Google DeepMind", "section": "industry"},
            "2026-11-24",
        )
        self.assertEqual(len(dm_cands), 1)
        self.assertIn("Gemini & World Models & Safety", dm_cands[0].title)

        # Verify OpenAlex URL targets S4306400194 and bounds both from_publication_date and to_publication_date
        oa_url = build_openalex_arxiv_url(
            {"id": "arxiv-gui-cua", "name": "arXiv GUI", "openalex_query": '"OSWorld" OR "GUI agent"'},
            run_date="2026-11-24",
        )
        self.assertIn("S4306400194", oa_url)
        self.assertIn("to_publication_date%3A2026-11-26", oa_url)

        # Verify unexpected OpenAlex dict lacking 'results' raises ValueError (never silently wipes failure state!)
        from daily_radar.collectors import parse_openalex_works

        with self.assertRaises(ValueError):
            parse_openalex_works(b'{"error": "rate_limited"}', {"id": "ax", "name": "arXiv"}, "2026-11-24")

        # Verify live HTTP fallback: 429 -> OpenAlex, TimeoutError -> OpenAlex, stale primary + extra_urls union
        _MockFeedServerHandler.hit_counts = {}
        _MockFeedServerHandler.routes = {
            "/arxiv-429": (429, b"Too Many Requests", "text/plain"),
            "/openalex": (200, openalex_payload, "application/json"),
            "/stale-rss": (200, stale_rss, "application/rss+xml"),
            "/anth-fresh": (200, (FIXTURES_DIR / "blog-anthropic.xml").read_bytes(), "application/rss+xml"),
            "/deepmind-dirty": (200, deepmind_unescaped_xml, "application/rss+xml"),
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockFeedServerHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tracker = SourceHealthTracker(Path(tmp) / "health.json", alert_threshold_days=3)
                cfg = {
                    "rate_limits": {"default_per_host_interval_sec": 0.0, "arxiv_interval_sec": 0.0},
                    "sources": [
                        {
                            "id": "arxiv-ossl-ood",
                            "name": "arXiv OOD",
                            "kind": "arxiv_api",
                            "section": "papers",
                            "url": f"http://127.0.0.1:{port}/arxiv-429",
                            "openalex_fallback_url": f"http://127.0.0.1:{port}/openalex",
                        },
                        {
                            "id": "blog-anthropic",
                            "name": "Anthropic",
                            "kind": "rss",
                            "org": "Anthropic",
                            "section": "industry",
                            "url": f"http://127.0.0.1:{port}/stale-rss",
                            "extra_urls": [f"http://127.0.0.1:{port}/anth-fresh"],
                        },
                        {
                            "id": "blog-qwen",
                            "name": "Qwen",
                            "kind": "rss",
                            "org": "阿里通义 Qwen",
                            "section": "industry",
                            "url": f"http://127.0.0.1:{port}/stale-rss",
                            "fallback_urls": [f"http://127.0.0.1:{port}/anth-fresh"],
                        },
                        {
                            "id": "blog-deepmind",
                            "name": "DeepMind",
                            "kind": "rss",
                            "org": "Google DeepMind",
                            "section": "industry",
                            "url": f"http://127.0.0.1:{port}/deepmind-dirty",
                        },
                    ],
                }
                cands, alerts = collect_all_sources(cfg, run_date="2026-11-24", health_tracker=tracker)
                self.assertEqual(alerts, [])
                oa_cand = next(c for c in cands if c.source_id == "arxiv-ossl-ood")
                self.assertEqual(oa_cand.arxiv_id, "2611.90123")
                self.assertEqual(oa_cand.extra["fallback_provider"], "openalex:S4306400194")
                self.assertIn("GUI agent RL on OSWorld improves by +4.2%.", oa_cand.summary_raw)
                self.assertTrue(any(c.source_id == "blog-anthropic" for c in cands))
                self.assertTrue(any(c.source_id == "blog-qwen" for c in cands))
                self.assertTrue(any(c.source_id == "blog-deepmind" for c in cands))
                self.assertTrue(check_source_health(Path(tmp) / "health.json").is_healthy)

                # Also test TimeoutError trigger on arxiv-gui-cua -> OpenAlex fallback
                from unittest import mock

                real_fetch = __import__("daily_radar.collectors", fromlist=["fetch_url_bytes"]).fetch_url_bytes

                def _timeout_primary(url: str, **kwargs: object) -> bytes:
                    if "/arxiv-timeout" in url:
                        raise TimeoutError("timed out")
                    return real_fetch(url, **kwargs)

                cfg_to = {
                    "rate_limits": {"default_per_host_interval_sec": 0.0, "arxiv_interval_sec": 0.0},
                    "sources": [
                        {
                            "id": "arxiv-gui-cua",
                            "name": "arXiv GUI",
                            "kind": "arxiv_api",
                            "section": "papers",
                            "url": f"http://127.0.0.1:{port}/arxiv-timeout",
                            "openalex_fallback_url": f"http://127.0.0.1:{port}/openalex",
                        }
                    ],
                }
                with mock.patch("daily_radar.collectors.fetch_url_bytes", side_effect=_timeout_primary):
                    cands_to, alerts_to = collect_all_sources(cfg_to, run_date="2026-11-24", health_tracker=tracker)
                self.assertEqual(alerts_to, [])
                self.assertEqual(len(cands_to), 1)
                self.assertEqual(cands_to[0].arxiv_id, "2611.90123")
                self.assertEqual(cands_to[0].extra["fallback_provider"], "openalex:S4306400194")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
