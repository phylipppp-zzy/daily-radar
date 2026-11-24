"""Tests for verbatim numerical verification (`verify.py`) and secret/confidentiality scanner (`security.py`)."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from daily_radar.models import FourElements, MetricBadge, MetricRow, RadarItem
from daily_radar.pipeline import normalize_pain_point_length
from daily_radar.security import scan_directory, scan_text
from daily_radar.verify import verify_claim_against_source, verify_radar_item


REPO_ROOT = Path(__file__).resolve().parent.parent


class VerifyAndSecurityTest(unittest.TestCase):
    def test_verbatim_number_verifier_passes_real_and_flags_hallucinated(self) -> None:
        source = (
            "Evaluated on 1,200 OSWorld tasks using Qwen2.5-VL-7B on 4 NVIDIA H800 GPUs, "
            "improving success rate by +5.2% to 48.6% while reducing FPR95 to 14.3% (arXiv:2607.25904)."
        )
        ok_real, missing_real = verify_claim_against_source(
            "Qwen2.5-VL-7B 在 1200 个任务上提升 +5.2% 达 48.6%，FPR95 降至 14.3%",
            source,
        )
        self.assertTrue(ok_real)
        self.assertEqual(missing_real, [])

        # "5%" or "9%" inside arXiv ID 2607.25904, and integer prefixes of decimals ("0%" vs "0.05", "5%" vs "+5.2%",
        # "48%" vs "48.6%"), MUST NOT falsely validate hallucinated numbers!
        ok_fake, missing_fake = verify_claim_against_source("OSWorld 提升 +12.7%，成功率达 59%", source)
        self.assertFalse(ok_fake)
        self.assertEqual(missing_fake, ["12.7%", "59%"])

        for bad_claim, tricky_src in (
            ("误报率降至 0%", "trained with learning rate 0.05 on ImageNet"),
            ("提升 5%", "improves success rate by +5.2% on OSWorld"),
            ("成功率 48%", "reaches 48.6% success rate on 1,200 tasks"),
            ("样本数 2607", "see full appendix in 2607.25904v2 for details"),
        ):
            ok_b, miss_b = verify_claim_against_source(bad_claim, tricky_src)
            self.assertFalse(ok_b, f"Hallucinated claim {bad_claim!r} falsely passed against {tricky_src!r}")
            self.assertTrue(len(miss_b) >= 1)

        # Unambiguous decimal-to-percentage equivalence ("85.2%" <-> "0.852") SHOULD pass
        ok_dec, miss_dec = verify_claim_against_source("AUROC 达 85.2%", "achieves 0.852 AUROC across 4 splits")
        self.assertTrue(ok_dec, f"Expected 85.2% <-> 0.852 to verify, got missing={miss_dec}")

        item = RadarItem(
            slug="gui-rl-hallucination-check",
            section="papers",
            track="gui-rl",
            track_label="GUI-RL",
            subtopics=["gui-online-rl"],
            lab_tags=[],
            institution_tags=["Test Lab"],
            title_zh="测试幻觉数字自动标黄核验",
            title_en="Test Numerical Verification on OSWorld",
            one_line_pain=normalize_pain_point_length(
                "针对摘要提炼可能出现幻觉涨点数字的风险，自动逐项比对英文原文并标注未核实数字",
                context_tag="GUI-RL",
            ),
            metric_badges=[
                MetricBadge(text="OSWorld +5.2%"),
                MetricBadge(text="AndroidWorld +9.9%"),
            ],
            openness="code_only",
            openness_label="仅代码开源",
            small_compute_friendly=True,
            small_compute_note="4×H800",
            authors=["Test Author"],
            published_date="2026-11-24",
            venue_or_source="arXiv cs.AI",
            url_primary="https://arxiv.org/abs/2607.25904",
            arxiv_id="2607.25904",
            abstract_en=source,
            four_elements=FourElements(
                pain_point="测试痛点说明。",
                mechanism="测试机制说明。",
                experiments_summary="在 OSWorld 涨点 5.2%，在另一基准虚构涨点 18.4%。",
                repro_and_reference="4×H800 可复现。",
                experiment_rows=[
                    MetricRow(benchmark="OSWorld", metric="SR", value="48.6%", baseline_delta="+5.2%"),
                    MetricRow(benchmark="FakeBench", metric="SR", value="77.3%", baseline_delta="+9.9%"),
                ],
            ),
        )
        verify_radar_item(item)
        self.assertTrue(item.metric_badges[0].verified)
        self.assertFalse(item.metric_badges[1].verified)
        self.assertIn("⚠️ 原文待核", item.metric_badges[1].display_text())
        self.assertTrue(item.four_elements.experiment_rows[0].verified)
        self.assertFalse(item.four_elements.experiment_rows[1].verified)
        self.assertIn("⚠️ 原文待核", item.four_elements.experiment_rows[1].display_value())
        self.assertIn("18.4%", item.four_elements.experiments_summary)
        self.assertIn("⚠️ 原文待核数字", item.four_elements.experiments_summary)

    def test_secret_and_confidentiality_scanner_catches_all_red_lines(self) -> None:
        # Construct sensitive tokens dynamically via token assembly so --include-tests stays 100% clean
        fake_openai = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3"
        fake_gh = "ghp_" + "z9Y8x7W6v5U4t3S2r1Q0p9O8n7M6l5K4j3"
        fake_hook = "https://open." + "feishu.cn/open-apis/bot/v2/hook/11112222-3333-4444-aaaa-bbbbccccdddd"
        fake_ip = ".".join(["192", "168", "10", "45"])
        fake_host = "internal-cluster-node01"
        fake_path = "/home/" + "alice/papers"
        fake_paper = "CVPR2099-" + "SecretProject"

        sample = "\n".join([fake_openai, fake_gh, fake_hook, fake_ip, fake_host, fake_path, fake_paper])
        findings = scan_text(
            sample,
            virtual_path="leak.html",
            extra_terms=["MyPrivateAblationCode", "internal-cluster-node01"],
        )
        rules_hit = {f.rule for f in findings}
        self.assertIn("openai_or_api_key", rules_hit)
        self.assertIn("github_pat", rules_hit)
        self.assertIn("feishu_webhook_url", rules_hit)
        self.assertIn("rfc1918_private_ipv4", rules_hit)
        self.assertIn("internal_machine_or_user_path", rules_hit)
        self.assertIn("unpublished_manuscript_codename", rules_hit)
        self.assertIn("custom_sensitive_term", rules_hit)

        # Binary asset detection inside directory scan
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            (tdir / "paper.pdf").write_bytes(b"%PDF-1.4")
            (tdir / "note.txt").write_text("Contains MyPrivateAblationCode inside.", encoding="utf-8")
            dir_findings = scan_directory(tdir, extra_terms=["MyPrivateAblationCode"])
            dir_rules = {f.rule for f in dir_findings}
            self.assertIn("forbidden_binary_asset", dir_rules)
            self.assertIn("custom_sensitive_term", dir_rules)

    def test_production_repo_and_rendered_site_have_zero_leaks(self) -> None:
        repo_findings = scan_directory(REPO_ROOT, extra_terms=[])
        self.assertEqual(repo_findings, [], f"Repo leaked sensitive strings: {repo_findings}")

        site_dir = REPO_ROOT / "_site"
        self.assertTrue(site_dir.is_dir(), "_site must exist from trial run")
        site_findings = scan_directory(site_dir, extra_terms=[])
        self.assertEqual(site_findings, [], f"_site leaked sensitive strings: {site_findings}")


if __name__ == "__main__":
    unittest.main()
