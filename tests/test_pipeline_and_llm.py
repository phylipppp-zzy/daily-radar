"""Tests for dual-track scoring, boundary matching, quiet-day empty state, conference trigger, and LLM failover."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from daily_radar.config import RepoConfig
from daily_radar.models import RawCandidate, assert_no_pr_cliche, visible_char_len
from daily_radar.pipeline import (
    DualChannelLLMClient,
    build_daily_issue,
    evaluate_conference_calendar,
    normalize_pain_point_length,
    score_and_partition_candidates,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


class _FailoverLLMHandler(BaseHTTPRequestHandler):
    fail_backup = False
    primary_hits = 0
    backup_hits = 0

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/primary"):
            type(self).primary_hits += 1
            self.send_response(429)
            self.end_headers()
            self.wfile.write(b'{"error": "rate limit exceeded"}')
            return

        type(self).backup_hits += 1
        if self.fail_backup:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b"Bad Gateway")
            return

        payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "title_zh": "备用通道成功接管：OSWorld 在线 RL 判分核验",
                                "one_line_pain": "针对主通道限流场景验证双通道自动切换，同时检验稀疏奖励任务中的环境状态判分准确性",
                                "metric_badges": ["成功率 52.4%"],
                                "pain_point": "主通道返回 429 时若无备用通道会导致原始链接刷屏。",
                                "mechanism": "自动切换备用兼容端点并执行原文实验数字核验。",
                                "experiments_summary": "在 OSWorld 基准取得 52.4% 成功率（4×H800）。",
                                "repro_and_reference": "4 张 80G 卡即可复现。",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class PipelineAndLLMTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_cfg = RepoConfig.load(REPO_ROOT)

    def test_word_boundary_and_provenance_no_false_attribution(self) -> None:
        # HKUST paper mentioning Qwen2.5-VL-7B and AndroidControl must NOT match HKU (lab-hku-xlang),
        # must NOT match lab-qwen, and must NOT match 'trl' or 'control' substrings!
        cand = RawCandidate(
            source_id="ax",
            source_name="arXiv",
            section="papers",
            title="CLIPN-V2: Learnable No Prompts for Out-of-Distribution Detection",
            url="https://arxiv.org/abs/2611.88888",
            published_at="2026-11-24",
            summary_raw=(
                "We study zero-shot out-of-distribution detection with CLIP and compare feature control "
                "against Qwen2.5-VL-7B on AndroidControl."
            ),
            authors=["Xiaomeng Li"],
            institutions=["The Hong Kong University of Science and Technology (HKUST)"],
            arxiv_id="2611.88888",
        )
        feat, _ = score_and_partition_candidates([cand], self.repo_cfg, issue_date="2026-11-24")
        self.assertEqual(len(feat), 1)
        sc = feat[0]
        self.assertEqual(sc.track, "ossl-ood")
        self.assertIn("lab-hkust", sc.lab_ids)
        self.assertNotIn("lab-hku-xlang", sc.lab_ids)
        self.assertNotIn("lab-qwen", sc.lab_ids)

    def test_quiet_day_empty_state_never_pads_noise(self) -> None:
        off_topic = [
            RawCandidate(
                source_id="rss",
                source_name="Generic Blog",
                section="industry",
                title="Protein Folding Molecular Dynamics on Supercomputers",
                url="https://example.org/protein",
                published_at="2026-11-24",
                summary_raw="Purely about wet-lab crystallography and molecular dynamics.",
            )
        ]
        issue = build_daily_issue(off_topic, self.repo_cfg, issue_date="2026-11-24")
        self.assertEqual(issue.featured_items, [])
        self.assertEqual(issue.folded_items, [])
        self.assertEqual(issue.highlights_top3, [])
        self.assertIn("不凑数", issue.global_trend_summary[0])

    def test_conference_special_banner_and_countdown(self) -> None:
        # On 2026-11-24, AAAI 2027 notification (2026-11-30) is 6 days away -> upcoming note, no active banner
        banner_24, note_24 = evaluate_conference_calendar(self.repo_cfg.conferences, "2026-11-24")
        self.assertIsNone(banner_24)
        self.assertIsNotNone(note_24)
        self.assertIn("AAAI 2027", note_24 or "")

        # On 2026-11-30 (AAAI 2027 notification day) and 2026-12-16 (ICLR 2027 notification day) -> active banner!
        banner_30, _ = evaluate_conference_calendar(self.repo_cfg.conferences, "2026-11-30")
        self.assertIsNotNone(banner_30)
        assert banner_30 is not None
        self.assertEqual(banner_30["slug"], "aaai-2027")

        banner_iclr, _ = evaluate_conference_calendar(self.repo_cfg.conferences, "2026-12-18")
        self.assertIsNotNone(banner_iclr)
        assert banner_iclr is not None
        self.assertEqual(banner_iclr["slug"], "iclr-2027")

    def test_dual_channel_llm_failover_429_to_backup(self) -> None:
        _FailoverLLMHandler.fail_backup = False
        _FailoverLLMHandler.primary_hits = 0
        _FailoverLLMHandler.backup_hits = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FailoverLLMHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            client = DualChannelLLMClient(
                {
                    "primary": {
                        "name": "primary-429",
                        "base_url": f"http://127.0.0.1:{port}/primary/v1",
                        "api_key": "tok1",
                        "distill_model": "m1",
                    },
                    "backup": {
                        "name": "backup-200",
                        "base_url": f"http://127.0.0.1:{port}/backup/v1",
                        "api_key": "tok2",
                        "distill_model": "m2",
                    },
                }
            )
            cand = RawCandidate(
                source_id="ax",
                source_name="arXiv",
                section="papers",
                title="OSWorld Online RL Verifier with State Probes",
                url="https://arxiv.org/abs/2611.77777",
                published_at="2026-11-24",
                summary_raw=(
                    "We train a GUI agent with online RL and state verifier on OSWorld using 4 NVIDIA H800 GPUs, "
                    "reaching 52.4% success rate."
                ),
                authors=["Alice Chen"],
                institutions=["Shanghai Jiao Tong University"],
                arxiv_id="2611.77777",
            )
            issue = build_daily_issue(
                [cand], self.repo_cfg, issue_date="2026-11-24", llm_client=client, fetch_fulltext=False,
            )
            self.assertEqual(_FailoverLLMHandler.primary_hits, 1)
            self.assertEqual(_FailoverLLMHandler.backup_hits, 1)
            self.assertEqual(issue.llm_channel_used, "backup-200")
            self.assertEqual(len(issue.featured_items), 1)
            self.assertTrue(issue.featured_items[0].metric_badges[0].verified)

            # When both channels fail, fail fast with RuntimeError instead of silently emitting raw junk!
            _FailoverLLMHandler.fail_backup = True
            with self.assertRaises(RuntimeError):
                client.complete_json([{"role": "user", "content": "test"}])
        finally:
            server.shutdown()
            server.server_close()

    def test_reviewer_evidence_and_offline_boundaries(self) -> None:
        cand = RawCandidate(
            source_id="ax", source_name="arXiv", section="papers", title="GUI Agent Online RL on OSWorld",
            url="https://arxiv.org/abs/2611.77777", published_at="2026-11-24", arxiv_id="2611.77777",
            summary_raw="We evaluate a GUI agent trained with online RL on OSWorld.",
            extra={"reported_baseline": "41.3%"},
        )
        critiques = ["界面元素重排后，状态判分是否仍然可靠？", "基线是否使用相同的交互预算？", "同一基座下移除判分器的消融结果如何？"]
        client = Mock(spec=DualChannelLLMClient, last_channel_used="reviewer")
        client.complete_json.return_value = {
            "verdict_label": "基线仍待核实", "verdict_reason": "任务成功率的分母明确，基线的交互预算仍待核对。",
            "critique_points": critiques, "metric_badges": ["成功率 62.7%", "基线 41.3%"],
            "experiment_rows": [{"benchmark": "OSWorld", "metric": "成功率", "value": "62.7%"}],
            "evidence_boundary": "已通读全文",
        }
        excerpt = "On OSWorld, success across all test tasks was 62.7%."
        with patch("daily_radar.collectors.fetch_arxiv_html_excerpt", return_value=excerpt) as fetch:
            issue = build_daily_issue([cand], self.repo_cfg, issue_date=cand.published_at, llm_client=client)
            fetch.assert_called_once_with(cand.arxiv_id)
            self.assertIn(excerpt, client.complete_json.call_args.args[0][1]["content"])
            item = issue.featured_items[0]
            review = item.four_elements
            self.assertEqual(review.critique_points, critiques)
            self.assertEqual(review.verdict_label, "基线仍待核实")
            self.assertEqual(review.verdict_reason, client.complete_json.return_value["verdict_reason"])
            self.assertEqual(review.evidence_boundary, "已结合 arXiv HTML 正文片段与摘要研判")
            self.assertTrue(all(b.verified for b in item.metric_badges))
            self.assertTrue(review.experiment_rows[0].verified)

            fetch.reset_mock()
            client.complete_json.return_value["critique_points"] = "malformed"
            for args in ({"fetch_fulltext": False}, {"source_origin": "local_fixtures"}):
                with self.subTest(args=args):
                    issue = build_daily_issue(
                        [cand], self.repo_cfg, issue_date=cand.published_at, llm_client=client, **args,
                    )
                    item = issue.featured_items[0]
                    self.assertEqual(item.four_elements.evidence_boundary, "仅基于原文摘要与元数据研判，正文消融与附录算力细节待核原文")
                    self.assertEqual(len(item.four_elements.critique_points), 3)
                    self.assertFalse(item.metric_badges[0].verified)
                    self.assertTrue(item.metric_badges[1].verified)
                    self.assertNotIn(excerpt, client.complete_json.call_args.args[0][1]["content"])

            offline = build_daily_issue([cand], self.repo_cfg, issue_date=cand.published_at).featured_items[0]
            self.assertEqual(offline.four_elements.evidence_boundary, "确定性离线抽取模式（未调用大模型）")
            self.assertEqual(len(offline.four_elements.critique_points), 3)
            fetch.assert_not_called()

    def test_pain_point_length_and_pr_cliche_ban(self) -> None:
        for raw in (
            "短句痛点说明",
            "针对长轨迹 GUI 在线强化学习中稀疏奖励导致优势估计方差爆炸的痛点，引入子目标锚点正则显著稳定训练收敛速度并消除崩溃",
            "这是一段超长描述：" + "在复杂多模态桌面交互场景下模型频繁被弹窗干扰误触导致失败，" * 4,
        ):
            norm = normalize_pain_point_length(raw, context_tag="GUI-RL")
            n = visible_char_len(norm)
            self.assertGreaterEqual(n, 45, f"Too short ({n}): {norm}")
            self.assertLessEqual(n, 60, f"Too long ({n}): {norm}")

        with self.assertRaises(ValueError):
            assert_no_pr_cliche("本文提出了创新框架并取得了优异性能", "test_field")

    def test_p1_strict_10pct_cap_hf_papers_openness_and_feedback_cli(self) -> None:
        from daily_radar.cli import record_feedback_token
        import tempfile

        # 1. Ten P1 items with zero P0 items: 10% cap MUST allow at most 1 P1 item (never 3!)
        p1_batch = [
            RawCandidate(
                source_id="ax",
                source_name="arXiv",
                section="papers",
                title=f"VLM Post-Training KV Cache Async RL Study #{i}",
                url=f"https://arxiv.org/abs/2611.900{i:02d}",
                published_at="2026-11-24",
                summary_raw="Studies verl, vllm, sglang, kv cache, and async rl post-training.",
                arxiv_id=f"2611.900{i:02d}",
            )
            for i in range(10)
        ]
        feat_p1, fold_p1 = score_and_partition_candidates(p1_batch, self.repo_cfg, issue_date="2026-11-24")
        self.assertEqual(len(feat_p1), 1, f"P1 10% cap violated: expected 1 featured from 10 P1 items, got {len(feat_p1)}")
        self.assertEqual(len(fold_p1), 9)

        # 2. HF Daily Papers discussion link (`huggingface.co/papers/...`) + GitHub repo without weights
        # must be classified `code_only`, NEVER `code_and_weights`!
        cand_no_weights = RawCandidate(
            source_id="ax",
            source_name="arXiv",
            section="papers",
            title="Prompt-Injection Defense for GUI Agent Computer-Use",
            url="https://arxiv.org/abs/2611.71234",
            published_at="2026-11-24",
            summary_raw="Defends GUI agent against prompt-injection on OSWorld. Code released.",
            github_repo="test-org/gui-defense",
            hf_url="https://huggingface.co/papers/2611.71234",
            arxiv_id="2611.71234",
        )
        issue_nw = build_daily_issue([cand_no_weights], self.repo_cfg, issue_date="2026-11-24")
        self.assertEqual(len(issue_nw.featured_items), 1)
        self.assertEqual(issue_nw.featured_items[0].openness, "code_only")
        self.assertIn("gui-security", issue_nw.featured_items[0].subtopics)

        # 3. Feedback token ingestion (`#雷达反馈 👎 ...`) penalizes matching noise candidates
        with tempfile.TemporaryDirectory() as tmp:
            fb_file = Path(tmp) / "feedback.jsonl"
            entry = record_feedback_token(
                "#雷达反馈 👎 2026-11-24/gui-rl-2611-04073 [gui-rl]",
                content_dir=REPO_ROOT / "content",
                feedback_file=fb_file,
                extra_keywords=["world model"],
            )
            self.assertEqual(entry["vote"], "down")
            self.assertIn("world model", entry["keywords"])
            base_f, _ = score_and_partition_candidates([cand_no_weights], self.repo_cfg, issue_date="2026-11-25")
            record_feedback_token(
                "#雷达反馈 👎 2026-11-24/gui-rl-2611-71234 [gui-rl]",
                content_dir=REPO_ROOT / "content",
                feedback_file=fb_file,
                extra_keywords=["prompt-injection", "osworld"],
            )
            penalized_f, _ = score_and_partition_candidates(
                [cand_no_weights], self.repo_cfg, issue_date="2026-11-25", feedback_file=fb_file
            )
            self.assertLess(
                penalized_f[0].raw_score if penalized_f else 0.0,
                base_f[0].raw_score,
            )

    def test_agy_cli_gemini_primary_channel(self) -> None:
        from unittest.mock import patch
        import os
        import subprocess
        import tempfile

        fake_stdout = json.dumps(
            {
                "conversation_id": "conv-ephemeral-123",
                "status": "SUCCESS",
                "response": '```json\n{"title_zh": "Gemini 提炼测试", "one_line_pain": "测试"}\n```',
            },
            ensure_ascii=False,
        )
        client = DualChannelLLMClient(
            {
                "primary": {
                    "name": "gemini-agy-primary",
                    "provider": "agy_cli",
                    "agy_bin": "/bin/echo",
                    "screen_model": "gemini-4-hydrogen-medium",
                    "distill_model": "gemini-4-neon-medium",
                    "timeout_sec": 30,
                }
            }
        )
        with patch.dict(
            os.environ,
            {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:49999", "ANTIGRAVITY_CSRF_TOKEN": "tok-test"},
            clear=False,
        ), patch(
            "daily_radar.pipeline.subprocess.run",
            return_value=subprocess.CompletedProcess(args=["agy"], returncode=0, stdout=fake_stdout, stderr=""),
        ) as mock_run, patch.object(
            DualChannelLLMClient, "_cleanup_agy_conversation", return_value=True
        ) as mock_cleanup:
            out = client.complete_json([{"role": "user", "content": "hi"}], stage="distill")
        self.assertEqual(out["title_zh"], "Gemini 提炼测试")
        self.assertEqual(client.last_channel_used, "gemini-agy-primary")
        called_cmd = mock_run.call_args[0][0]
        self.assertIn("gemini-4-neon-medium", called_cmd)
        self.assertEqual(mock_run.call_args.kwargs.get("cwd"), tempfile.gettempdir())
        mock_cleanup.assert_called_once_with("conv-ephemeral-123")

    def test_agy_cli_reuses_single_conversation_when_ls_env_absent(self) -> None:
        from unittest.mock import patch
        import os
        import subprocess

        fake_stdout = json.dumps(
            {
                "conversation_id": "conv-systemd-001",
                "status": "SUCCESS",
                "response": '{"title_zh": "复用会话", "one_line_pain": "测试"}',
            },
            ensure_ascii=False,
        )
        client = DualChannelLLMClient(
            {
                "primary": {
                    "name": "gemini-agy-primary",
                    "provider": "agy_cli",
                    "agy_bin": "/bin/echo",
                    "distill_model": "gemini-4-neon-medium",
                    "timeout_sec": 30,
                }
            }
        )
        clean_env = {k: v for k, v in os.environ.items() if k not in ("ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN")}
        with patch.dict(os.environ, clean_env, clear=True), patch(
            "daily_radar.pipeline.subprocess.run",
            return_value=subprocess.CompletedProcess(args=["agy"], returncode=0, stdout=fake_stdout, stderr=""),
        ) as mock_run:
            client.complete_json([{"role": "user", "content": "first"}], stage="distill")
            client.complete_json([{"role": "user", "content": "second"}], stage="distill")
        first_cmd = mock_run.call_args_list[0][0][0]
        second_cmd = mock_run.call_args_list[1][0][0]
        self.assertNotIn("--conversation", first_cmd)
        self.assertIn("--conversation", second_cmd)
        self.assertIn("conv-systemd-001", second_cmd)


if __name__ == "__main__":
    unittest.main()
