"""Tests for HTTP/XML/JSON/RSS collectors, Latent Space [AINews] splitting, 3-day health alerts, and 7-day dedup."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from daily_radar.collectors import (
    SourceHealthTracker,
    collect_all_sources,
    parse_conference_accepted_json,
    parse_github_trending,
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


if __name__ == "__main__":
    unittest.main()
