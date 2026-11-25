"""Tests for 3-tier static HTML site generation, full link integrity, external assets, zoteroctl JSON, and cold-tiering."""

from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import tempfile
import unittest

from daily_radar.config import RepoConfig
from daily_radar.models import RadarItem
from daily_radar.renderer import build_static_site


REPO_ROOT = Path(__file__).resolve().parent.parent


class _DOMInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []
        self.inline_style_tags = 0
        self.ids: set[str] = set()
        self.card_slugs: list[str] = []
        self.has_return_overview = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        amap = dict(attrs)
        if amap.get("id"):
            self.ids.add(str(amap["id"]))
        if tag == "style":
            self.inline_style_tags += 1
        elif tag == "a" and amap.get("href"):
            self.links.append(str(amap["href"]))
            if amap.get("data-item-slug"):
                self.card_slugs.append(str(amap["data-item-slug"]))
            if amap.get("data-return-overview"):
                self.has_return_overview = True
        elif tag == "link" and amap.get("rel") == "stylesheet" and amap.get("href"):
            self.stylesheets.append(str(amap["href"]))
        elif tag == "script" and amap.get("src"):
            self.scripts.append(str(amap["src"]))


class RendererAndSiteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_cfg = RepoConfig.load(REPO_ROOT)
        cls.site_dir = REPO_ROOT / "_site"

    def test_critique_fields_round_trip_and_render_only_when_present(self) -> None:
        source = REPO_ROOT / "content" / "2026" / "11" / "2026-11-24.json"
        issue_data = json.loads(source.read_text(encoding="utf-8"))
        reviewed, legacy = issue_data["featured_items"][:2]
        critique = {
            "verdict_label": "基准有参考价值",
            "verdict_reason": "可借鉴评测划分，训练增益仍待控制变量。",
            "critique_points": ["界面布局变化后还能泛化吗？", "同算力基线的方差是多少？", "固定数据后微调的增益还有多少？"],
            "evidence_boundary": "仅基于摘要与元数据研判",
        }
        for key in critique:
            legacy["four_elements"].pop(key, None)
        legacy_item = RadarItem.from_dict(legacy)
        self.assertEqual(legacy_item.four_elements.verdict_label, "")
        self.assertEqual(legacy_item.four_elements.verdict_reason, "")
        self.assertEqual(legacy_item.four_elements.critique_points, [])
        self.assertEqual(legacy_item.four_elements.evidence_boundary, "")
        legacy_item.four_elements.critique_points.append("检查默认列表是否独立")
        self.assertEqual(RadarItem.from_dict(legacy).four_elements.critique_points, [])

        reviewed["four_elements"].update(critique)
        restored = RadarItem.from_dict(RadarItem.from_dict(reviewed).to_dict())
        self.assertEqual({key: getattr(restored.four_elements, key) for key in critique}, critique)

        with tempfile.TemporaryDirectory() as tmp:
            content_dir = Path(tmp) / "content"
            content_file = content_dir / "2026" / "11" / source.name
            content_file.parent.mkdir(parents=True)
            content_file.write_text(json.dumps(issue_data, ensure_ascii=False), encoding="utf-8")
            site_dir = Path(tmp) / "site"
            build_static_site(content_dir=content_dir, repo_cfg=self.repo_cfg, out_dir=site_dir)
            day_dir = site_dir / "daily" / issue_data["date"]
            overview = (day_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn('class="badge verdict-pill">研判：基准有参考价值</span>', overview)
            detail = (day_dir / f"item-{reviewed['slug']}.html").read_text(encoding="utf-8")
            self.assertIn('<section id="el-critique" class="four-el-card critique-card">', detail)
            self.assertIn('<a class="nav-pill" href="#el-critique">⑤ 审稿批判</a>', detail)
            self.assertIn('<span class="verdict-pill">基准有参考价值</span>', detail)
            self.assertIn(f"<p>{critique['verdict_reason']}</p>", detail)
            points = "".join(f"<li>{point}</li>" for point in critique["critique_points"])
            self.assertIn(f'<ol class="critique-list">{points}</ol>', detail)
            self.assertIn(f'<footer class="evidence-boundary">{critique["evidence_boundary"]}</footer>', detail)
            legacy_detail = (day_dir / f"item-{legacy['slug']}.html").read_text(encoding="utf-8")
            self.assertNotIn("el-critique", legacy_detail)

    def test_every_html_file_has_valid_relative_links_and_external_assets(self) -> None:
        html_files = sorted(self.site_dir.rglob("*.html"))
        self.assertGreaterEqual(len(html_files), 70)

        for hf in html_files:
            inspector = _DOMInspector()
            inspector.feed(hf.read_text(encoding="utf-8"))

            # §4.3 Rule 3: strictly external fingerprinted CSS & JS, zero inline <style> tags!
            self.assertEqual(inspector.inline_style_tags, 0, f"Inline <style> found in {hf}")
            self.assertEqual(len(inspector.stylesheets), 1, f"Missing external CSS in {hf}")
            self.assertEqual(len(inspector.scripts), 1, f"Missing external JS in {hf}")

            for ref in inspector.stylesheets + inspector.scripts + inspector.links:
                if ref.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                clean_ref = ref.split("#")[0].split("?")[0]
                if not clean_ref:
                    continue
                # Must never start with absolute '/' which would break GitHub Project Pages (/daily-radar/)!
                self.assertFalse(clean_ref.startswith("/"), f"Root-relative URL '{ref}' in {hf} breaks Project Pages!")
                resolved = (hf.parent / clean_ref).resolve()
                self.assertTrue(resolved.is_file(), f"Broken relative link '{ref}' in {hf} -> {resolved}")

    def test_tier1_tier2_tier3_structure_and_zoteroctl_command(self) -> None:
        # Root index.html mirrors latest overview (2026-11-24) with depth-0 links and zero placeholder/chopped text
        root_html = (self.site_dir / "index.html").read_text(encoding="utf-8")
        root_ins = _DOMInspector()
        root_ins.feed(root_html)
        self.assertEqual(len(root_ins.card_slugs), 7)
        self.assertNotIn("前沿技术动态速览", root_html)
        self.assertNotIn("自动合成大；", root_html)

        # Conference year isolation & §6.3 subproblem trend breakdown check:
        # ICML 2026 paper SE-GA MUST NOT leak into icml-2027.html cards, while neurips-2026.html MUST show trend stats!
        icml27_html = (self.site_dir / "conferences" / "icml-2027.html").read_text(encoding="utf-8")
        icml27_ins = _DOMInspector()
        icml27_ins.feed(icml27_html)
        self.assertEqual(icml27_ins.card_slugs, [])
        self.assertNotIn("item-gui-rl-2605-16883", icml27_html)
        neurips26_html = (self.site_dir / "conferences" / "neurips-2026.html").read_text(encoding="utf-8")
        self.assertIn("录用趋势与子问题统计", neurips26_html)
        self.assertIn("DIBench", neurips26_html)

        # Tier-2 detail page checks (both academic preprint AND opensource computerProgram)
        detail_file = self.site_dir / "daily" / "2026-11-24" / "item-gui-rl-2611-04073.html"
        self.assertTrue(detail_file.is_file())
        det_text = detail_file.read_text(encoding="utf-8")
        det_ins = _DOMInspector()
        det_ins.feed(det_text)
        self.assertTrue(det_ins.has_return_overview)
        for el_id in ("el-pain", "el-mech", "el-exp", "el-repro", "zoteroctl-cmd-code", "bibtex-code"):
            self.assertIn(el_id, det_ins.ids)

        # BibTeX special-char escaping check (`%` and `&` in title/venue must be escaped as `\%` and `\&`)
        import html as html_mod
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import os
        import subprocess
        import threading

        sega_html = (self.site_dir / "daily" / "2026-11-24" / "item-gui-rl-2605-16883.html").read_text(encoding="utf-8")
        verl_file = next((self.site_dir / "daily" / "2026-11-24").glob("item-vlm-infra-verl*.html"))
        verl_html = verl_file.read_text(encoding="utf-8")
        bib_match = re.search(r'<pre id="bibtex-code"[^>]*>(.*?)</pre>', verl_html, re.DOTALL)
        self.assertIsNotNone(bib_match)
        assert bib_match is not None
        verl_bib = html_mod.unescape(bib_match.group(1))
        self.assertIn(r"\&", verl_bib)

        # End-to-end execution of the REAL `zoteroctl` CLI (when present in sibling workspace) against a strict
        # Zotero v3 mock HTTP server enforcing valid fields AND valid creatorTypes per itemType!
        valid_creator_types = {
            "preprint": {"author", "contributor"},
            "conferencePaper": {"author", "editor", "contributor"},
            "computerProgram": {"programmer", "contributor"},
            "blogPost": {"author", "commenter", "contributor"},
        }
        templates = {
            "preprint": {"itemType": "preprint", "title": "", "creators": [], "abstractNote": "", "repository": "", "archiveID": "", "date": "", "url": "", "DOI": "", "extra": ""},
            "computerProgram": {"itemType": "computerProgram", "title": "", "creators": [], "abstractNote": "", "date": "", "url": "", "rights": "", "extra": ""},
        }
        created_items: list[dict] = []

        class _StrictZoteroMock(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/items/new"):
                    itype = self.path.split("itemType=", 1)[1].split("&")[0]
                    body = json.dumps(templates[itype]).encode("utf-8")
                    self.send_response(200)
                else:
                    body = b"[]"
                    self.send_response(200)
                    self.send_header("Total-Results", "0")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                objs = json.loads(raw.decode("utf-8"))
                for obj in objs:
                    allowed = valid_creator_types[obj["itemType"]]
                    for cr in obj.get("creators", []):
                        if cr.get("creatorType") not in allowed:
                            self.send_response(400)
                            self.end_headers()
                            self.wfile.write(f"Invalid creatorType {cr.get('creatorType')}".encode())
                            return
                    created_items.append(obj)
                resp = json.dumps({"success": {"0": "ZOTKEY01"}, "failed": {}, "unchanged": {}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        real_zoteroctl = REPO_ROOT.parent / "zoteroctl" / "zoteroctl"
        self.assertTrue(real_zoteroctl.is_file(), "Expected real zoteroctl executable in sibling workspace")

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _StrictZoteroMock)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            env = dict(os.environ)
            env["ZOTEROCTL_API_BASE"] = f"http://127.0.0.1:{port}"
            env["ZOTERO_API_KEY"] = "mock_key"
            env["ZOTERO_LIBRARY_ID"] = "1"
            env["ZOTERO_LIBRARY_TYPE"] = "user"
            env["PATH"] = f"{real_zoteroctl.parent}:{env.get('PATH', '')}"

            for page_html in (det_text, verl_html, sega_html):
                m = re.search(r'<pre id="zoteroctl-cmd-code"[^>]*>(.*?)</pre>', page_html, re.DOTALL)
                self.assertIsNotNone(m)
                assert m is not None
                # Append `--apply` inside the subshell before closing `)` and verify caller's EXIT trap survives!
                cmd_preview = html_mod.unescape(m.group(1)).strip()
                self.assertTrue(cmd_preview.startswith("(") and cmd_preview.endswith(")"))
                cmd_apply = cmd_preview[:-1] + " --apply)"
                wrapped = f"caller_trap_intact=0; trap 'caller_trap_intact=1' EXIT; {cmd_apply}; test \"$caller_trap_intact\" = 0 && trap -p EXIT | grep -q caller_trap_intact"
                subprocess.run(["bash", "-c", wrapped], check=True, env=env, capture_output=True, text=True)

            self.assertEqual(len(created_items), 3)
            self.assertEqual(created_items[0]["itemType"], "preprint")
            self.assertEqual(created_items[0]["archiveID"], "arXiv:2611.04073")
            self.assertEqual(created_items[1]["itemType"], "computerProgram")
            self.assertEqual(created_items[1]["creators"][0]["creatorType"], "programmer")
        finally:
            srv.shutdown()
            srv.server_close()

        # Tier-3 search index shard check (computed dynamically from content/2026/*.json)
        years_json = json.loads((self.site_dir / "data" / "years.json").read_text(encoding="utf-8"))
        self.assertIn("2026", years_json["years"])
        shard_2026 = json.loads((self.site_dir / "data" / "index-2026.json").read_text(encoding="utf-8"))
        expected_2026_items = sum(
            len(json.loads(p.read_text(encoding="utf-8")).get("featured_items", []))
            for p in (REPO_ROOT / "content" / "2026").rglob("*.json")
        )
        self.assertEqual(len(shard_2026), expected_2026_items)

        # Provenance badges: trial (2026-11-24) vs live (2026-11-25) must be visually & structurally distinct
        trial_overview = (self.site_dir / "daily" / "2026-11-24" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-run-mode="trial"', trial_overview)
        self.assertIn("TRIAL 演练样本", trial_overview)
        self.assertIn("trial-verified-distiller", trial_overview)
        live_overview = (self.site_dir / "daily" / "2026-11-25" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-run-mode="live"', live_overview)
        self.assertIn("LIVE 实采", live_overview)
        self.assertIn("gemini-agy-primary", live_overview)

    def test_cold_archive_tiering_mode_omits_old_detail_files(self) -> None:
        # With hot_years_override=0, only latest day (0 days diff) is hot; 2026-11-22 & 2026-11-23 become cold
        # and route through /daily/item.html?date=...&slug=... (§4.3 Rule 5)!
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "_site_cold"
            summary = build_static_site(
                content_dir=REPO_ROOT / "content",
                repo_cfg=self.repo_cfg,
                out_dir=out,
                hot_years_override=0,
            )
            self.assertEqual(summary["detail_pages_rendered"], 7)
            self.assertTrue((out / "daily" / "item.html").is_file())
            self.assertFalse(any((out / "daily" / "2026-11-22").glob("item-*.html")))
            old_overview = (out / "daily" / "2026-11-22" / "index.html").read_text(encoding="utf-8")
            self.assertIn("../../daily/item.html?date=2026-11-22&amp;slug=", old_overview)


if __name__ == "__main__":
    unittest.main()
