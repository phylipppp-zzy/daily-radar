"""3-Tier Static Multi-Page Site Generator for Daily Research Radar."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
from typing import Any

from daily_radar.config import RepoConfig
from daily_radar.models import DailyIssue, RadarItem
from daily_radar.zotero_cmd import build_bibtex, build_zoteroctl_command


STATIC_SRC_DIR = Path(__file__).resolve().parent / "static"


def _esc(val: Any) -> str:
    return html.escape(str(val or ""), quote=True)


def _rel(depth: int, target_from_root: str) -> str:
    clean = target_from_root.lstrip("/")
    if depth <= 0:
        return clean
    prefix = "/".join([".."] * depth)
    return f"{prefix}/{clean}"


def _root_attr(depth: int) -> str:
    if depth <= 0:
        return "."
    return "/".join([".."] * depth)


def _item_rel_path(issue_date: str, slug: str, *, is_hot: bool = True) -> str:
    if is_hot:
        return f"daily/{issue_date}/item-{slug}.html"
    return f"daily/item.html?date={issue_date}&slug={slug}"


@dataclass
class AssetManifest:
    css_rel: str
    js_rel: str


def _emit_fingerprinted_assets(out_dir: Path) -> AssetManifest:
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    css_bytes = (STATIC_SRC_DIR / "app.css").read_bytes()
    js_bytes = (STATIC_SRC_DIR / "app.js").read_bytes()
    css_hash = hashlib.sha256(css_bytes).hexdigest()[:8]
    js_hash = hashlib.sha256(js_bytes).hexdigest()[:8]

    css_name = f"app.{css_hash}.css"
    js_name = f"app.{js_hash}.js"
    (assets_dir / css_name).write_bytes(css_bytes)
    (assets_dir / js_name).write_bytes(js_bytes)
    return AssetManifest(css_rel=f"assets/{css_name}", js_rel=f"assets/{js_name}")


def _render_shell(
    *,
    page_title: str,
    site_title: str,
    depth: int,
    assets: AssetManifest,
    body_html: str,
    page_kind: str,
    issue_date: str = "",
    item_slug: str = "",
    prev_date_href: str = "",
    next_date_href: str = "",
    all_dates_desc: list[str] | None = None,
) -> str:
    root_attr = _root_attr(depth)
    css_href = _rel(depth, assets.css_rel)
    js_src = _rel(depth, assets.js_rel)
    home_href = _rel(depth, "index.html")
    archive_href = _rel(depth, "archive/index.html")
    topics_href = _rel(depth, "topics/index.html")
    conf_href = _rel(depth, "conferences/index.html")

    date_nav_html = ""
    if all_dates_desc:
        prev_cls = "btn" if prev_date_href else "btn btn-disabled"
        next_cls = "btn" if next_date_href else "btn btn-disabled"
        options = []
        for d in all_dates_desc:
            sel = " selected" if d == issue_date else ""
            opt_href = _rel(depth, f"daily/{d}/index.html")
            options.append(f'<option value="{_esc(opt_href)}"{sel}>{_esc(d)}</option>')
        date_nav_html = (
            '<div class="date-nav">'
            f'<a class="{prev_cls}" href="{_esc(prev_date_href or "#")}" title="前一期">←</a>'
            f'<select id="issue-date-jump" class="date-select" aria-label="选择历史日期">{"".join(options)}</select>'
            f'<a class="{next_cls}" href="{_esc(next_date_href or "#")}" title="后一期">→</a>'
            "</div>"
        )

    extra_attrs = f' data-page-kind="{_esc(page_kind)}"'
    if issue_date:
        extra_attrs += f' data-issue-date="{_esc(issue_date)}"'
    if item_slug:
        extra_attrs += f' data-item-slug="{_esc(item_slug)}"'

    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-root="{_esc(root_attr)}"{extra_attrs}>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{_esc(page_title)} — {_esc(site_title)}</title>
  <link rel="stylesheet" href="{_esc(css_href)}">
</head>
<body>
  <header class="site-header">
    <div class="header-inner">
      <div class="brand-group">
        <a class="brand-title" href="{_esc(home_href)}">📡 {_esc(site_title)}</a>
        {date_nav_html}
      </div>
      <nav class="nav-links" aria-label="主导航">
        <a class="nav-pill" href="{_esc(archive_href)}">归档</a>
        <a class="nav-pill" href="{_esc(topics_href)}">课题</a>
        <a class="nav-pill" href="{_esc(conf_href)}">会议</a>
        <button type="button" class="nav-pill" data-open-search="1">搜索 (/)</button>
        <button type="button" id="theme-toggle-btn" class="nav-pill" title="切换浅色/深色模式">◐</button>
      </nav>
    </div>
  </header>

  {body_html}

  <div id="search-backdrop" class="search-backdrop" role="dialog" aria-modal="true" aria-label="站内检索">
    <div class="search-dialog">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
        <strong>站内即时检索（按年分片懒加载）</strong>
        <button type="button" id="search-close-btn" class="btn">关闭 (Esc)</button>
      </div>
      <input id="search-input" class="search-input" type="search" placeholder="搜索标题、OSWorld、NegLabel、verl、机构或作者…">
      <label style="font-size:13px;color:var(--text-muted);display:inline-flex;align-items:center;gap:6px;">
        <input type="checkbox" id="search-all-years"> 跨年搜索全部历史分片
      </label>
      <ul id="search-results" class="search-results"></ul>
    </div>
  </div>

  <div id="radar-toast" class="toast" role="status" aria-live="polite"></div>
  <script defer src="{_esc(js_src)}"></script>
</body>
</html>
"""


def _render_summary_card(
    item: RadarItem,
    *,
    issue_date: str,
    depth: int,
    is_hot: bool = True,
    extra_conf_tags: str = "",
) -> str:
    """Render Tier-1 summary card exposing strictly the 5 essential scannable elements (§3.2)."""
    detail_href = _rel(depth, _item_rel_path(issue_date, item.slug, is_hot=is_hot))

    # Element 2: Track & institution tags
    tag_pills = [f'<span class="badge badge-track">{_esc(item.track_label)}</span>']
    for inst in item.institution_tags[:2]:
        tag_pills.append(f'<span class="badge">{_esc(inst)}</span>')
    if item.conference_track_tier:
        tag_pills.append(f'<span class="badge badge-amber">{_esc(item.conference_track_tier)}</span>')

    # Element 4 & 5: 1-2 core metric badges + openness & small-compute badges
    footer_pills: list[str] = []
    for mb in item.metric_badges[:2]:
        cls = "badge" if mb.verified else "badge badge-amber"
        footer_pills.append(f'<span class="{cls}">{_esc(mb.display_text())}</span>')

    if item.openness == "code_and_weights":
        footer_pills.append(f'<span class="badge badge-open-full">{_esc(item.openness_label)}</span>')
    elif item.openness == "code_only":
        footer_pills.append(f'<span class="badge badge-open-code">{_esc(item.openness_label)}</span>')
    else:
        footer_pills.append(f'<span class="badge">{_esc(item.openness_label)}</span>')

    if item.small_compute_friendly:
        footer_pills.append('<span class="badge badge-open-full">4–8卡可复现</span>')

    conf_attr = f' data-conf-tags="{_esc(extra_conf_tags)}"' if extra_conf_tags else ""
    return (
        f'<a class="summary-card" href="{_esc(detail_href)}" data-item-slug="{_esc(item.slug)}"{conf_attr}>'
        "<div>"
        f'<div class="card-tags-row">{"".join(tag_pills)}</div>'
        f'<h3 class="card-title-zh">{_esc(item.title_zh)}</h3>'
        f'<p class="card-title-en">{_esc(item.title_en)}</p>'
        f'<p class="card-pain">{_esc(item.one_line_pain)}</p>'
        "</div>"
        f'<div class="card-footer-badges">{"".join(footer_pills)}</div>'
        "</a>"
    )


def _render_overview_page(
    issue: DailyIssue,
    repo_cfg: RepoConfig,
    assets: AssetManifest,
    *,
    depth: int,
    all_dates_desc: list[str],
    prev_date: str | None,
    next_date: str | None,
    is_hot: bool,
    is_root_home: bool = False,
) -> str:
    site_title = str(repo_cfg.profile["site"]["title"])
    sections_cfg = repo_cfg.profile["sections"]

    prev_href = _rel(depth, f"daily/{prev_date}/index.html") if prev_date else ""
    next_href = _rel(depth, f"daily/{next_date}/index.html") if next_date else ""
    permalink_href = _rel(depth, f"daily/{issue.date}/index.html")
    weekly_iso = date.fromisoformat(issue.date).isocalendar()
    weekly_slug = f"{weekly_iso.year}-W{weekly_iso.week:02d}"
    weekly_href = _rel(depth, f"weekly/{weekly_slug}.html")

    permalink_html = (
        '<div class="permalink-bar">'
        f'<span>当前期次：<strong>{_esc(issue.date)}（{_esc(issue.weekday_zh)}）</strong> · 生成于 {_esc(issue.generated_at_cst)}</span>'
        f'<span><a href="{_esc(permalink_href)}">本期永久链接</a> · <a href="{_esc(weekly_href)}">本周汇总 ({_esc(weekly_slug)})</a></span>'
        "</div>"
    )

    special_html = ""
    if issue.special_conference_banner:
        b = issue.special_conference_banner
        b_href = _rel(depth, b.get("url", "conferences/index.html"))
        special_html = (
            '<div class="special-banner">'
            f'<span>{_esc(b["headline"])}</span>'
            f'<a class="btn" href="{_esc(b_href)}">进入会议特刊 →</a>'
            "</div>"
        )

    health_html = ""
    if issue.source_health_alerts:
        rows = "".join(f"<div>⚠️ {_esc(a)}</div>" for a in issue.source_health_alerts)
        health_html = f'<div class="special-banner">{rows}</div>'

    trend_p = "".join(f"<p>{_esc(line)}</p>" for line in issue.global_trend_summary)
    trend_html = f'<section class="trend-box"><h2>⚡ 今日 30 秒全局风向</h2>{trend_p}</section>'

    jump_pills = []
    for sec in sections_cfg:
        sec_id = str(sec["id"])
        sec_count = sum(1 for it in issue.featured_items if it.section == sec_id)
        jump_pills.append(
            f'<a class="nav-pill" href="#sec-{_esc(sec_id)}">{_esc(sec["short_label"])} ({sec_count})</a>'
        )
    jumps_html = f'<div class="section-jumps">{"".join(jump_pills)}</div>'

    sections_html_parts: list[str] = []
    for sec in sections_cfg:
        sec_id = str(sec["id"])
        sec_items = [it for it in issue.featured_items if it.section == sec_id]
        sec_folded = [fi for fi in issue.folded_items if fi.section == sec_id]

        if sec_items:
            cards_str = "".join(
                _render_summary_card(it, issue_date=issue.date, depth=depth, is_hot=is_hot) for it in sec_items
            )
            grid_html = f'<div class="card-grid">{cards_str}</div>'
        else:
            grid_html = '<div class="empty-state">今日该板块无高优新增（严格降噪，不凑数）。</div>'

        folded_html = ""
        if sec_folded:
            li_rows = []
            for fi in sec_folded:
                li_rows.append(
                    f'<li><span class="badge badge-track">{_esc(fi.track_label)}</span> '
                    f'<a href="{_esc(fi.url)}" target="_blank" rel="noopener"><strong>{_esc(fi.title_zh)}</strong></a> '
                    f'— <span style="color:var(--text-muted)">{_esc(fi.one_line_takeaway)}</span></li>'
                )
            folded_html = (
                '<details class="folded-panel">'
                f"<summary>展开其余 {len(sec_folded)} 条泛相关速览（单行折叠）</summary>"
                f'<ul class="folded-list">{"".join(li_rows)}</ul>'
                "</details>"
            )

        sections_html_parts.append(
            f'<section id="sec-{_esc(sec_id)}" class="radar-section">'
            '<div class="section-header">'
            f'<h2>{_esc(sec["title"])}</h2>'
            f'<span class="section-count">精选 {len(sec_items)} 条 · 折叠 {len(sec_folded)} 条</span>'
            "</div>"
            f"{grid_html}{folded_html}"
            "</section>"
        )

    body_html = (
        '<main class="container">'
        f"{permalink_html}{special_html}{health_html}{trend_html}{jumps_html}"
        f'{"".join(sections_html_parts)}'
        "</main>"
    )

    page_title = f"{issue.date} 每日总览" if not is_root_home else f"最新一期 ({issue.date})"
    return _render_shell(
        page_title=page_title,
        site_title=site_title,
        depth=depth,
        assets=assets,
        body_html=body_html,
        page_kind="overview",
        issue_date=issue.date,
        prev_date_href=prev_href,
        next_date_href=next_href,
        all_dates_desc=all_dates_desc,
    )


def _render_detail_page(
    issue: DailyIssue,
    item: RadarItem,
    repo_cfg: RepoConfig,
    assets: AssetManifest,
    *,
    prev_item: RadarItem | None,
    next_item: RadarItem | None,
    all_dates_desc: list[str],
) -> str:
    depth = 2
    site_title = str(repo_cfg.profile["site"]["title"])
    overview_href = "index.html"
    prev_item_href = f"item-{prev_item.slug}.html" if prev_item else ""
    next_item_href = f"item-{next_item.slug}.html" if next_item else ""

    bibtex_str = build_bibtex(item)
    zoteroctl_cmd = build_zoteroctl_command(item, apply=False)
    fb_up_token = f"#雷达反馈 👍 {issue.date}/{item.slug} [{item.track}]"
    fb_down_token = f"#雷达反馈 👎 {issue.date}/{item.slug} [{item.track}]"

    # Topic & lab links
    tag_links = [
        f'<a class="badge badge-track" href="{_esc(_rel(depth, f"topics/{item.track}.html"))}">{_esc(item.track_label)}</a>'
    ]
    for sub in item.subtopics:
        tag_links.append(f'<a class="badge" href="{_esc(_rel(depth, f"topics/{sub}.html"))}">{_esc(sub)}</a>')
    for lab_id in item.lab_tags:
        tag_links.append(f'<a class="badge" href="{_esc(_rel(depth, f"topics/{lab_id}.html"))}">{_esc(lab_id)}</a>')

    authors_str = "、".join(item.authors) if item.authors else item.venue_or_source
    inst_str = " / ".join(item.institution_tags) if item.institution_tags else "原文未单列单位"
    corroboration_str = (
        f"<div>多源热度交叉印证：{_esc('、'.join(item.corroborating_sources))}</div>"
        if item.corroborating_sources
        else ""
    )
    stars_str = f" · GitHub ★ {item.github_stars}" if item.github_stars is not None else ""
    lic_str = f" · 协议：{item.license_name}" if item.license_name else ""

    fe = item.four_elements
    rows_table_html = ""
    if fe.experiment_rows:
        trs = []
        for r in fe.experiment_rows:
            val_cls = "" if r.verified else ' style="color:var(--amber-fg);font-weight:600;"'
            trs.append(
                "<tr>"
                f"<td>{_esc(r.benchmark)}</td>"
                f"<td>{_esc(r.metric)}</td>"
                f"<td{val_cls}>{_esc(r.display_value())}</td>"
                f"<td>{_esc(r.baseline_delta)}</td>"
                f"<td>{_esc(r.setup_note)}</td>"
                "</tr>"
            )
        rows_table_html = (
            '<table class="metric-table">'
            "<thead><tr><th>评测基准</th><th>指标</th><th>实测结果（自动核验）</th><th>对比基线增量</th><th>实验设置备注</th></tr></thead>"
            f'<tbody>{"".join(trs)}</tbody>'
            "</table>"
        )

    ext_buttons = [
        f'<a class="btn btn-primary" href="{_esc(item.url_primary)}" target="_blank" rel="noopener">一手原文直达 ↗</a>'
    ]
    if item.url_pdf:
        ext_buttons.append(f'<a class="btn" href="{_esc(item.url_pdf)}" target="_blank" rel="noopener">arXiv PDF</a>')
    if item.url_arxiv_html:
        ext_buttons.append(
            f'<a class="btn" href="{_esc(item.url_arxiv_html)}" target="_blank" rel="noopener">arXiv HTML (手机友好)</a>'
        )
    if item.url_github:
        ext_buttons.append(f'<a class="btn" href="{_esc(item.url_github)}" target="_blank" rel="noopener">GitHub 仓库</a>')
    if item.url_hf:
        ext_buttons.append(f'<a class="btn" href="{_esc(item.url_hf)}" target="_blank" rel="noopener">Hugging Face</a>')

    prev_btn_html = (
        f'<a id="detail-prev-link" class="btn" href="{_esc(prev_item_href)}">← 上一篇：{_esc(prev_item.title_zh[:18])}…</a>'
        if prev_item
        else '<span class="btn btn-disabled">已是首篇</span>'
    )
    next_btn_html = (
        f'<a id="detail-next-link" class="btn" href="{_esc(next_item_href)}">下一篇：{_esc(next_item.title_zh[:18])}… →</a>'
        if next_item
        else '<span class="btn btn-disabled">已是末篇</span>'
    )

    mobile_prev_html = (
        f'<a class="btn" href="{_esc(prev_item_href)}">← 上一篇</a>'
        if prev_item
        else '<span class="btn btn-disabled">首篇</span>'
    )
    mobile_next_html = (
        f'<a class="btn" href="{_esc(next_item_href)}">下一篇 →</a>'
        if next_item
        else '<span class="btn btn-disabled">末篇</span>'
    )

    body_html = f"""<main class="container">
  <div style="margin-bottom:12px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
    <a class="btn" href="{_esc(overview_href)}" data-return-overview="1">← 返回 {_esc(issue.date)} 总览（保持原阅读位置）</a>
    <span style="font-size:13px;color:var(--text-muted);">快捷键：Esc 返回 · ←/→ 切篇 · C 复制 zoteroctl</span>
  </div>

  <div class="detail-layout">
    <article class="detail-main">
      <header class="detail-header">
        <div class="card-tags-row">{"".join(tag_links)}</div>
        <h1 class="detail-h1">{_esc(item.title_zh)}</h1>
        <p class="detail-en-sub">{_esc(item.title_en)}</p>
        <div class="meta-list">
          <div><strong>作者：</strong>{_esc(authors_str)}</div>
          <div><strong>单位/机构：</strong>{_esc(inst_str)}</div>
          <div><strong>发布日期与渠道：</strong>{_esc(item.published_date)} · {_esc(item.venue_or_source)} · {_esc(item.openness_label)}{_esc(stars_str)}{_esc(lic_str)}</div>
          {corroboration_str}
        </div>
      </header>

      <section id="el-pain" class="four-el-card">
        <h2>① 真实痛点与翻车场景</h2>
        <p>{_esc(fe.pain_point)}</p>
      </section>

      <section id="el-mech" class="four-el-card">
        <h2>② 具体方法机制</h2>
        <p>{_esc(fe.mechanism)}</p>
      </section>

      <section id="el-exp" class="four-el-card">
        <h2>③ 实验基准数字与对比口径</h2>
        <div class="spec-grid">
          <div class="spec-pill"><strong>基座模型与参数量</strong>{_esc(fe.backbone_and_params)}</div>
          <div class="spec-pill"><strong>训练算力与耗时</strong>{_esc(fe.gpu_and_duration)}</div>
          <div class="spec-pill"><strong>评测随机种子数</strong>{_esc(fe.seeds_note)}</div>
        </div>
        <p>{_esc(fe.experiments_summary)}</p>
        {rows_table_html}
      </section>

      <section id="el-repro" class="four-el-card">
        <h2>④ 公开技术参考与复现成本评估</h2>
        <p>{_esc(fe.repro_and_reference)}</p>
      </section>

      <details id="en-abstract-details" class="abstract-box">
        <summary>展开/收起原文英文摘要对照（用于核验原始措辞与数字）</summary>
        <div class="abstract-content">{_esc(item.abstract_en)}</div>
      </details>

      <nav class="prev-next-nav" aria-label="相邻精选切换">
        {prev_btn_html}
        {next_btn_html}
      </nav>
    </article>

    <aside class="detail-sidebar" aria-label="科研动作与目录栏">
      <div class="sidebar-group">
        <h3>四要素快速跳转</h3>
        <div class="action-row-wrap">
          <a class="nav-pill" href="#el-pain">① 痛点</a>
          <a class="nav-pill" href="#el-mech">② 机制</a>
          <a class="nav-pill" href="#el-exp">③ 实验数字</a>
          <a class="nav-pill" href="#el-repro">④ 复现参考</a>
        </div>
      </div>

      <div class="sidebar-group">
        <h3>一手外部链接</h3>
        <div class="action-stack">{"".join(ext_buttons)}</div>
      </div>

      <div class="sidebar-group">
        <h3>文献入库与引用</h3>
        <div class="action-stack">
          <button type="button" id="copy-zoteroctl-btn" class="btn btn-primary" data-copy-target="zoteroctl-cmd-code" data-copy-label=" zoteroctl 预览入库命令">一键复制 zoteroctl 命令 (C)</button>
          <button type="button" class="btn" data-copy-target="bibtex-code" data-copy-label=" BibTeX 引用">一键复制 BibTeX</button>
        </div>
        <details class="code-details">
          <summary>查看 zoteroctl / BibTeX 命令源码</summary>
          <pre id="zoteroctl-cmd-code" class="code-pre">{_esc(zoteroctl_cmd)}</pre>
          <pre id="bibtex-code" class="code-pre">{_esc(bibtex_str)}</pre>
        </details>
      </div>

      <div class="sidebar-group">
        <h3>打分校准反馈（复制发回飞书）</h3>
        <div class="action-row-wrap">
          <button type="button" class="btn" data-copy-text="{_esc(fb_up_token)}" data-copy-label="正样本反馈口令">👍 有价值</button>
          <button type="button" class="btn" data-copy-text="{_esc(fb_down_token)}" data-copy-label="降噪负样本口令">👎 噪音少推</button>
        </div>
      </div>
    </aside>
  </div>
</main>

<div class="mobile-bottom-bar">
  <a class="btn" href="{_esc(overview_href)}" data-return-overview="1">← 返回总览</a>
  {mobile_prev_html}
  {mobile_next_html}
</div>"""

    return _render_shell(
        page_title=item.title_zh,
        site_title=site_title,
        depth=depth,
        assets=assets,
        body_html=body_html,
        page_kind="detail",
        issue_date=issue.date,
        item_slug=item.slug,
        all_dates_desc=all_dates_desc,
    )


def _render_archive_page(
    issues_desc: list[DailyIssue],
    weekly_map: dict[str, list[DailyIssue]],
    repo_cfg: RepoConfig,
    assets: AssetManifest,
) -> str:
    depth = 1
    site_title = str(repo_cfg.profile["site"]["title"])
    all_dates_desc = [iss.date for iss in issues_desc]

    heat_cells = []
    for iss in reversed(issues_desc):
        n = len(iss.featured_items)
        lvl = "heat-lvl-2" if n >= 6 else ("heat-lvl-1" if n >= 1 else "")
        sp = " heat-special" if iss.special_conference_banner else ""
        href = _rel(depth, f"daily/{iss.date}/index.html")
        heat_cells.append(
            f'<a class="heat-cell {lvl}{sp}" href="{_esc(href)}" title="{_esc(iss.date)}：精选 {n} 条">'
            f'{_esc(iss.date[5:])} ({n})</a>'
        )

    weekly_pills = []
    for w_slug in sorted(weekly_map.keys(), reverse=True):
        w_href = _rel(depth, f"weekly/{w_slug}.html")
        total_feat = sum(len(x.featured_items) for x in weekly_map[w_slug])
        weekly_pills.append(f'<a class="btn" href="{_esc(w_href)}">📅 {_esc(w_slug)} 周报汇总（{total_feat} 篇精选）</a>')

    by_ym: dict[str, list[DailyIssue]] = defaultdict(list)
    for iss in issues_desc:
        by_ym[iss.date[:7]].append(iss)

    ym_blocks = []
    for ym, ym_issues in sorted(by_ym.items(), reverse=True):
        lis = []
        for iss in ym_issues:
            href = _rel(depth, f"daily/{iss.date}/index.html")
            lead = iss.global_trend_summary[0] if iss.global_trend_summary else ""
            lis.append(
                f'<li><a href="{_esc(href)}"><strong>{_esc(iss.date)}（{_esc(iss.weekday_zh)}）</strong></a> '
                f'<span class="badge badge-open-full">精选 {len(iss.featured_items)}</span> '
                f'<span class="badge">折叠 {len(iss.folded_items)}</span>'
                f'<div style="font-size:14px;color:var(--text-muted);margin-top:4px;">{_esc(lead)}</div></li>'
            )
        ym_blocks.append(f'<section class="radar-section"><h2>{_esc(ym)}</h2><ul class="archive-list">{"".join(lis)}</ul></section>')

    body_html = (
        '<main class="container">'
        '<section class="four-el-card">'
        "<h1>永久历史归档与阅读热力日历</h1>"
        '<p style="color:var(--text-muted)">点击任意日期方格直达当日总览页；金色外框代表含顶会放榜特刊。</p>'
        f'<div class="heatmap-grid">{"".join(heat_cells)}</div>'
        "</section>"
        '<section class="four-el-card">'
        "<h2>周度精选汇总（辅助编写个人科研周报）</h2>"
        f'<div class="action-row-wrap">{"".join(weekly_pills)}</div>'
        "</section>"
        f'{"".join(ym_blocks)}'
        "</main>"
    )
    return _render_shell(
        page_title="历史归档与周度汇总",
        site_title=site_title,
        depth=depth,
        assets=assets,
        body_html=body_html,
        page_kind="archive",
        all_dates_desc=all_dates_desc,
    )


def _render_weekly_page(
    week_slug: str,
    week_issues: list[DailyIssue],
    repo_cfg: RepoConfig,
    assets: AssetManifest,
    *,
    hot_dates: set[str],
    all_dates_desc: list[str],
) -> str:
    depth = 1
    site_title = str(repo_cfg.profile["site"]["title"])
    cards = []
    for iss in sorted(week_issues, key=lambda x: x.date, reverse=True):
        for it in iss.featured_items:
            cards.append(_render_summary_card(it, issue_date=iss.date, depth=depth, is_hot=(iss.date in hot_dates)))

    dates_covered = f"{min(x.date for x in week_issues)} ~ {max(x.date for x in week_issues)}"
    cards_html = (
        f'<div class="card-grid">{"".join(cards)}</div>'
        if cards
        else '<div class="empty-state">本周暂无高优精选条目。</div>'
    )
    body_html = (
        '<main class="container">'
        '<div class="trend-box">'
        f"<h2>📅 {_esc(week_slug)} 周度精选汇总（{_esc(dates_covered)}）</h2>"
        f"<p>共收录 {len(week_issues)} 期日报、合计 {len(cards)} 条高信噪比精选，供周报撰写与组会复盘直接引用。</p>"
        "</div>"
        f"{cards_html}"
        "</main>"
    )
    return _render_shell(
        page_title=f"{week_slug} 周度精选",
        site_title=site_title,
        depth=depth,
        assets=assets,
        body_html=body_html,
        page_kind="weekly",
        all_dates_desc=all_dates_desc,
    )


def _render_topics_pages(
    issues_desc: list[DailyIssue],
    repo_cfg: RepoConfig,
    assets: AssetManifest,
    out_dir: Path,
    *,
    hot_dates: set[str],
) -> None:
    depth = 1
    topics_dir = out_dir / "topics"
    topics_dir.mkdir(parents=True, exist_ok=True)
    site_title = str(repo_cfg.profile["site"]["title"])
    all_dates_desc = [iss.date for iss in issues_desc]

    # Collect metadata definitions for tracks, subtopics, and benchmark labs
    tag_meta: dict[str, dict[str, str]] = {}
    for t_id, t_cfg in repo_cfg.profile["tracks"].items():
        tag_meta[t_id] = {"title": f"{t_cfg['display_label']} · {t_cfg['title']}", "group": "核心方向"}
        for s_id, s_cfg in t_cfg.get("subtopics", {}).items():
            tag_meta[s_id] = {"title": f"{s_cfg['label']} ({s_id})", "group": "细分技术子方向"}
    for lab in repo_cfg.profile.get("tracked_labs", []):
        tag_meta[str(lab["id"])] = {
            "title": f"{lab['label']} — {lab['note']}",
            "group": "重点追踪高校与工业界实验室",
        }

    tag_items: dict[str, list[tuple[str, RadarItem]]] = defaultdict(list)
    for iss in issues_desc:
        for it in iss.featured_items:
            tag_items[it.track].append((iss.date, it))
            for sub in it.subtopics:
                tag_items[sub].append((iss.date, it))
            for lab_id in it.lab_tags:
                tag_items[lab_id].append((iss.date, it))

    # Render /topics/index.html
    groups: dict[str, list[str]] = defaultdict(list)
    for tag_id, meta in tag_meta.items():
        cnt = len(tag_items.get(tag_id, []))
        groups[meta["group"]].append(
            f'<a class="btn" href="{_esc(tag_id)}.html">{_esc(meta["title"].split("—")[0].strip())} ({cnt})</a>'
        )

    group_sections = []
    for g_name in ("核心方向", "细分技术子方向", "重点追踪高校与工业界实验室"):
        pills = "".join(groups.get(g_name, []))
        group_sections.append(
            f'<section class="four-el-card"><h2>{_esc(g_name)}</h2><div class="action-row-wrap">{pills}</div></section>'
        )

    idx_html = _render_shell(
        page_title="课题与标杆实验室专题索引",
        site_title=site_title,
        depth=depth,
        assets=assets,
        body_html=f'<main class="container"><h1>课题路线与重点实验室纵向追踪</h1>{"".join(group_sections)}</main>',
        page_kind="topics-index",
        all_dates_desc=all_dates_desc,
    )
    (topics_dir / "index.html").write_text(idx_html, encoding="utf-8")

    # Render individual /topics/<tag>.html
    for tag_id, meta in tag_meta.items():
        entries = tag_items.get(tag_id, [])
        cards = [
            _render_summary_card(it, issue_date=d_str, depth=depth, is_hot=(d_str in hot_dates))
            for d_str, it in entries
        ]
        cards_block = (
            f'<div class="card-grid">{"".join(cards)}</div>'
            if cards
            else '<div class="empty-state">当前归档窗口内该标签暂无命中精选条目，后续命中将自动归入此时间线。</div>'
        )
        t_html = _render_shell(
            page_title=meta["title"],
            site_title=site_title,
            depth=depth,
            assets=assets,
            body_html=(
                '<main class="container">'
                '<div class="trend-box">'
                f'<h2>🏷️ {_esc(meta["title"])}</h2>'
                f'<p>累计收录精选条目 {len(entries)} 篇 · <a href="index.html">返回全部课题与实验室索引</a></p>'
                "</div>"
                f"{cards_block}"
                "</main>"
            ),
            page_kind="topic-detail",
            all_dates_desc=all_dates_desc,
        )
        (topics_dir / f"{tag_id}.html").write_text(t_html, encoding="utf-8")


def _render_conferences_pages(
    issues_desc: list[DailyIssue],
    repo_cfg: RepoConfig,
    assets: AssetManifest,
    out_dir: Path,
    *,
    hot_dates: set[str],
) -> None:
    depth = 1
    conf_dir = out_dir / "conferences"
    conf_dir.mkdir(parents=True, exist_ok=True)
    site_title = str(repo_cfg.profile["site"]["title"])
    all_dates_desc = [iss.date for iss in issues_desc]
    ref_date = date.fromisoformat(all_dates_desc[0]) if all_dates_desc else date.today()

    subtopic_zh: dict[str, str] = {}
    for t_cfg in repo_cfg.profile.get("tracks", {}).values():
        for s_id, s_cfg in t_cfg.get("subtopics", {}).items():
            subtopic_zh[s_id] = str(s_cfg.get("label", s_id))

    conf_items: dict[str, list[tuple[str, RadarItem]]] = defaultdict(list)
    for iss in issues_desc:
        for it in iss.featured_items:
            hay = f"{it.conference_slug} {it.venue_or_source}".lower()
            for conf in repo_cfg.conferences.get("conferences", []):
                c_slug = str(conf["slug"])
                primary_title = str(conf["name"]).lower().split("&")[0].split("+")[0].strip()
                exact_year_match = bool(
                    primary_title
                    and re.search(r"\b20\d{2}\b", primary_title)
                    and re.search(rf"(?<![a-z0-9]){re.escape(primary_title)}(?![a-z0-9])", hay)
                )
                if it.conference_slug == c_slug or exact_year_match:
                    conf_items[c_slug].append((iss.date, it))

    rows_html = []
    for conf in repo_cfg.conferences.get("conferences", []):
        c_slug = str(conf["slug"])
        notif_str = str(conf.get("notification_date", ""))
        sub_str = str(conf.get("submission_deadline", ""))
        countdown_badge = ""
        try:
            nd = date.fromisoformat(notif_str)
            diff = (nd - ref_date).days
            if diff >= 0:
                countdown_badge = f'<span class="badge badge-amber">距放榜 {diff} 天 ({_esc(notif_str)})</span>'
            else:
                countdown_badge = f'<span class="badge">放榜日 {_esc(notif_str)}</span>'
        except ValueError:
            countdown_badge = f'<span class="badge">{_esc(notif_str)}</span>'

        cnt = len(conf_items.get(c_slug, []))
        rows_html.append(
            "<tr>"
            f'<td><a href="{_esc(c_slug)}.html"><strong>{_esc(conf["name"])}</strong></a> ({cnt} 篇关联)</td>'
            f'<td><span class="badge badge-track">{_esc(conf["ccf_grade"])}</span></td>'
            f"<td>{_esc(sub_str)}</td>"
            f"<td>{countdown_badge}</td>"
            f'<td>{_esc(conf.get("conference_dates", ""))}</td>'
            f'<td>{_esc(conf.get("focus", ""))}</td>'
            "</tr>"
        )

        # Render /conferences/<conf-slug>.html with §6.3 subproblem trend breakdown
        matched = conf_items.get(c_slug, [])
        track_counts: dict[str, int] = defaultdict(int)
        sub_counts: dict[str, int] = defaultdict(int)
        open_count = 0
        c_cards = []
        for d_str, it in matched:
            track_counts[it.track_label] += 1
            for sub in it.subtopics:
                sub_counts[subtopic_zh.get(sub, sub)] += 1
            if it.openness in ("code_and_weights", "code_only"):
                open_count += 1
            tags_parts = ["all"]
            tier_low = f"{it.conference_track_tier} {it.venue_or_source}".lower()
            if "oral" in tier_low or "spotlight" in tier_low:
                tags_parts.append("oral")
            if "e&d" in tier_low or "evaluations" in tier_low or "findings" in tier_low or "workshop" in tier_low:
                tags_parts.append("ed_findings")
            if it.openness in ("code_and_weights", "code_only"):
                tags_parts.append("opensource")
            if it.small_compute_friendly:
                tags_parts.append("small_gpu")
            c_cards.append(
                _render_summary_card(
                    it,
                    issue_date=d_str,
                    depth=depth,
                    is_hot=(d_str in hot_dates),
                    extra_conf_tags=" ".join(tags_parts),
                )
            )

        trend_stats_line = ""
        if matched:
            track_str = "、".join(f"{k} {v} 篇" for k, v in track_counts.items())
            sub_str_top = "、".join(
                f"{k} {v} 篇" for k, v in sorted(sub_counts.items(), key=lambda kv: kv[1], reverse=True)[:4]
            )
            trend_stats_line = (
                f"<p><strong>录用趋势与子问题统计：</strong>当前共收录双课题高相关工作 {len(matched)} 篇"
                f"（{track_str}；已开源代码/权重 {open_count} 篇），细分覆盖：{sub_str_top}。</p>"
            )

        cards_html = (
            f'<div class="card-grid">{"".join(c_cards)}</div>'
            if c_cards
            else '<div class="empty-state">放榜或论文集发布后，双课题录用筛选特刊将自动聚合至本页。</div>'
        )
        detail_conf_html = _render_shell(
            page_title=f"{conf['name']} 录用追踪特刊",
            site_title=site_title,
            depth=depth,
            assets=assets,
            body_html=(
                '<main class="container">'
                '<div class="trend-box">'
                f'<h2>🎯 {_esc(conf["name"])}（CCF 第七版：{_esc(conf["ccf_grade"])}）</h2>'
                f'<p>{_esc(conf.get("focus", ""))}</p>'
                f"{trend_stats_line}"
                f'<p>截稿：{_esc(sub_str)} · 放榜：{_esc(notif_str)} · 会期：{_esc(conf.get("conference_dates", ""))} · '
                f'<a href="{_esc(conf.get("official_source", "#"))}" target="_blank" rel="noopener">官方主页 ↗</a> · '
                '<a href="index.html">返回全部会议日历</a></p>'
                "</div>"
                '<div class="section-jumps">'
                '<button type="button" class="btn" data-conf-filter="all">全部关联条目</button>'
                '<button type="button" class="btn" data-conf-filter="oral">Oral / Spotlight</button>'
                '<button type="button" class="btn" data-conf-filter="ed_findings">E&amp;D / Findings / Workshop</button>'
                '<button type="button" class="btn" data-conf-filter="opensource">已开源代码/权重</button>'
                '<button type="button" class="btn" data-conf-filter="small_gpu">4–8卡可复现</button>'
                "</div>"
                f"{cards_html}"
                "</main>"
            ),
            page_kind="conference-detail",
            all_dates_desc=all_dates_desc,
        )
        (conf_dir / f"{c_slug}.html").write_text(detail_conf_html, encoding="utf-8")

    ccf_note = str(repo_cfg.conferences.get("ccf_edition_note", ""))
    index_body = (
        '<main class="container">'
        '<section class="trend-box">'
        "<h2>📅 2026–2027 国际学术会议与专题 Workshop 追踪日历</h2>"
        f"<p>{_esc(ccf_note)}</p>"
        "</section>"
        '<div style="overflow-x:auto;background:var(--bg-card);border:1px solid var(--border-color);border-radius:12px;padding:12px;">'
        '<table class="metric-table">'
        "<thead><tr><th>会议 / Workshop</th><th>CCF 七版</th><th>投稿截止</th><th>放榜日（特刊触发）</th><th>开会日期</th><th>核心关注方向</th></tr></thead>"
        f'<tbody>{"".join(rows_html)}</tbody>'
        "</table></div></main>"
    )
    (conf_dir / "index.html").write_text(
        _render_shell(
            page_title="顶会与 Workshop 日历",
            site_title=site_title,
            depth=depth,
            assets=assets,
            body_html=index_body,
            page_kind="conferences-index",
            all_dates_desc=all_dates_desc,
        ),
        encoding="utf-8",
    )


def _write_search_shards_and_cold_viewer(
    issues_desc: list[DailyIssue],
    repo_cfg: RepoConfig,
    assets: AssetManifest,
    out_dir: Path,
    *,
    hot_dates: set[str],
) -> None:
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    by_year: dict[str, list[dict[str, str]]] = defaultdict(list)

    for iss in issues_desc:
        yr = iss.date[:4]
        is_hot = iss.date in hot_dates
        for it in iss.featured_items:
            by_year[yr].append(
                {
                    "date": iss.date,
                    "slug": it.slug,
                    "url": _item_rel_path(iss.date, it.slug, is_hot=is_hot),
                    "track_label": it.track_label,
                    "title_zh": it.title_zh,
                    "title_en": it.title_en,
                    "pain": it.one_line_pain,
                    "authors": " ".join(it.authors),
                    "institutions": " ".join(it.institution_tags),
                    "tags": " ".join([it.track] + it.subtopics + it.lab_tags),
                    "metrics": " ".join(b.display_text() for b in it.metric_badges),
                }
            )

    years_sorted = sorted(by_year.keys(), reverse=True)
    (data_dir / "years.json").write_text(
        json.dumps({"years": years_sorted}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for yr, rows in by_year.items():
        (data_dir / f"index-{yr}.json").write_text(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    # Cold-archive fallback viewer (/daily/item.html?date=YYYY-MM-DD&slug=...) per §4.3 Rule 5
    daily_root = out_dir / "daily"
    daily_root.mkdir(parents=True, exist_ok=True)
    cold_html = _render_shell(
        page_title="历史归档条目详情",
        site_title=str(repo_cfg.profile["site"]["title"]),
        depth=1,
        assets=assets,
        body_html='<main id="cold-item-mount" class="container"><div class="empty-state">正在按需加载历史详情数据…</div></main>',
        page_kind="cold-item",
        all_dates_desc=[iss.date for iss in issues_desc],
    )
    (daily_root / "item.html").write_text(cold_html, encoding="utf-8")


def build_static_site(
    *,
    content_dir: Path,
    repo_cfg: RepoConfig,
    out_dir: Path,
    hot_years_override: int | None = None,
) -> dict[str, Any]:
    """Render the complete 3-tier static site from `content/YYYY/MM/YYYY-MM-DD.json` into `out_dir`."""
    issue_files = sorted(content_dir.rglob("*.json"))
    issues: list[DailyIssue] = []
    for jf in issue_files:
        try:
            date.fromisoformat(jf.stem)
        except ValueError:
            continue
        iss = DailyIssue.from_dict(json.loads(jf.read_text(encoding="utf-8")))
        iss.validate(
            featured_max=int(repo_cfg.profile.get("budgets", {}).get("featured_max", 10)),
            folded_max=int(repo_cfg.profile.get("budgets", {}).get("folded_max", 20)),
        )
        issues.append(iss)

    if not issues:
        raise ValueError(f"No valid YYYY-MM-DD.json issue files found under {content_dir}")

    issues_desc = sorted(issues, key=lambda x: x.date, reverse=True)
    all_dates_desc = [x.date for x in issues_desc]
    latest_date = date.fromisoformat(issues_desc[0].date)

    hot_years = (
        hot_years_override
        if hot_years_override is not None
        else int(repo_cfg.profile["site"].get("hot_detail_years", 3))
    )
    hot_cutoff_days = hot_years * 365
    hot_dates = {
        iss.date for iss in issues_desc if (latest_date - date.fromisoformat(iss.date)).days <= hot_cutoff_days
    }

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    assets = _emit_fingerprinted_assets(out_dir)

    # Render Tier 1 & Tier 2 per issue
    detail_pages_count = 0
    weekly_map: dict[str, list[DailyIssue]] = defaultdict(list)

    for idx, iss in enumerate(issues_desc):
        # all_dates_desc is newest -> oldest: older issue is idx + 1, newer issue is idx - 1
        prev_older = issues_desc[idx + 1].date if (idx + 1) < len(issues_desc) else None
        next_newer = issues_desc[idx - 1].date if idx > 0 else None
        is_hot = iss.date in hot_dates

        iso_cal = date.fromisoformat(iss.date).isocalendar()
        weekly_map[f"{iso_cal.year}-W{iso_cal.week:02d}"].append(iss)

        day_dir = out_dir / "daily" / iss.date
        day_dir.mkdir(parents=True, exist_ok=True)

        # Write compact issue.json for dynamic cold-archive fallback & API inspection
        (day_dir / "issue.json").write_text(
            json.dumps(iss.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        day_html = _render_overview_page(
            iss,
            repo_cfg,
            assets,
            depth=2,
            all_dates_desc=all_dates_desc,
            prev_date=prev_older,
            next_date=next_newer,
            is_hot=is_hot,
            is_root_home=False,
        )
        (day_dir / "index.html").write_text(day_html, encoding="utf-8")

        if idx == 0:
            root_html = _render_overview_page(
                iss,
                repo_cfg,
                assets,
                depth=0,
                all_dates_desc=all_dates_desc,
                prev_date=prev_older,
                next_date=None,
                is_hot=is_hot,
                is_root_home=True,
            )
            (out_dir / "index.html").write_text(root_html, encoding="utf-8")

        if is_hot:
            feats = iss.featured_items
            for i_pos, item in enumerate(feats):
                prev_it = feats[i_pos - 1] if i_pos > 0 else None
                next_it = feats[i_pos + 1] if (i_pos + 1) < len(feats) else None
                det_html = _render_detail_page(
                    iss,
                    item,
                    repo_cfg,
                    assets,
                    prev_item=prev_it,
                    next_item=next_it,
                    all_dates_desc=all_dates_desc,
                )
                (day_dir / f"item-{item.slug}.html").write_text(det_html, encoding="utf-8")
                detail_pages_count += 1

    # Render Tier 3: Archive, Weekly, Topics, Conferences, and Sharded Search Index
    archive_dir = out_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "index.html").write_text(
        _render_archive_page(issues_desc, weekly_map, repo_cfg, assets),
        encoding="utf-8",
    )

    weekly_dir = out_dir / "weekly"
    weekly_dir.mkdir(parents=True, exist_ok=True)
    for w_slug, w_issues in weekly_map.items():
        (weekly_dir / f"{w_slug}.html").write_text(
            _render_weekly_page(
                w_slug,
                w_issues,
                repo_cfg,
                assets,
                hot_dates=hot_dates,
                all_dates_desc=all_dates_desc,
            ),
            encoding="utf-8",
        )

    _render_topics_pages(issues_desc, repo_cfg, assets, out_dir, hot_dates=hot_dates)
    _render_conferences_pages(issues_desc, repo_cfg, assets, out_dir, hot_dates=hot_dates)
    _write_search_shards_and_cold_viewer(issues_desc, repo_cfg, assets, out_dir, hot_dates=hot_dates)

    total_bytes = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
    html_count = sum(1 for p in out_dir.rglob("*.html"))
    return {
        "issues_rendered": len(issues_desc),
        "latest_date": issues_desc[0].date,
        "detail_pages_rendered": detail_pages_count,
        "total_html_files": html_count,
        "total_bytes": total_bytes,
        "css_asset": assets.css_rel,
        "js_asset": assets.js_rel,
    }
