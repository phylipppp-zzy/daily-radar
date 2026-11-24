"""Multi-source HTTP & fixture collectors, parsers, rate limiting, and 3-day source health tracker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
import html
import json
from pathlib import Path
import re
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from daily_radar.dedup import normalize_arxiv_id, normalize_github_repo
from daily_radar.models import RawCandidate


USER_AGENT = "DailyResearchRadar/0.1 (+https://github.com/phylipppp-zzy/daily-radar)"

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
GITHUB_URL_IN_TEXT_RE = re.compile(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
HF_URL_IN_TEXT_RE = re.compile(r"https?://huggingface\.co/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?")
HTML_TAG_RE = re.compile(r"<[^>]+>")
ILLEGAL_XML_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
ILLEGAL_XML_ENTITY_RE = re.compile(
    r"&#(?:0*[0-8]|0*1[12]|0*1[4-9]|0*2\d|0*3[01]|x0*[0-8]|x0*[bBcC]|x0*1[0-9a-fA-F]|x0*[eEfF]);"
)
AINEWS_BULLET_RE = re.compile(
    r"<li[^>]*>\s*(?:<p[^>]*>)?\s*(?:<(?:strong|b)>([^<]{6,160})</(?:strong|b)>)?(.*?)</li>",
    re.IGNORECASE | re.DOTALL,
)
HREF_RE = re.compile(r'href=["\'](https?://[^"\'>\s]+)["\']', re.IGNORECASE)
GH_TRENDING_ARTICLE_RE = re.compile(r"<article[^>]*class=[\"'][^\"']*Box-row[^\"']*[\"'][^>]*>(.*?)</article>", re.DOTALL)
GH_TRENDING_REPO_HREF_RE = re.compile(
    r"<h2[^>]*>.*?<a[^>]*href=[\"']/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)[\"'][^>]*>",
    re.DOTALL,
)
GH_TRENDING_DESC_RE = re.compile(r"<p[^>]*class=[\"'][^\"']*col-9[^\"']*[\"'][^>]*>(.*?)</p>", re.DOTALL)
GH_TRENDING_STARS_RE = re.compile(r"([\d,]+)\s+stars\s+today", re.IGNORECASE)
GH_TRENDING_TOTAL_STARS_RE = re.compile(r"href=[\"']/[^\"']+/stargazers[\"'][^>]*>\s*(?:<svg.*?</svg>)?\s*([\d,]+)", re.DOTALL)

DOMAIN_ORG_HINTS: tuple[tuple[str, str], ...] = (
    ("qwen.ai", "Alibaba Qwen"),
    ("qwenlm.github.io", "Alibaba Qwen"),
    ("allenai.org", "AllenAI"),
    ("anthropic.com", "Anthropic"),
    ("openai.com", "OpenAI"),
    ("deepmind.google", "Google DeepMind"),
    ("deepseek.com", "DeepSeek"),
    ("seed.bytedance.com", "ByteDance Seed"),
    ("microsoft.com", "Microsoft Research"),
)


def sanitize_xml_bytes(xml_bytes: bytes) -> bytes:
    """Strip UTF-8 BOM, illegal XML 1.0 control chars, and invalid numeric char refs before ET.fromstring."""
    text = xml_bytes.decode("utf-8-sig", errors="replace")
    text = ILLEGAL_XML_CTRL_RE.sub("", text)
    text = ILLEGAL_XML_ENTITY_RE.sub("", text)
    return text.encode("utf-8")


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    unescaped = html.unescape(raw)
    cleaned = re.sub(r"<(?:br|p|/p|/li|/div|/h[1-6])[^>]*>", "\n", unescaped, flags=re.IGNORECASE)
    cleaned = HTML_TAG_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    return cleaned.strip()


def normalize_iso_date(raw: str, fallback_date: str) -> str:
    if not raw:
        return fallback_date
    text = raw.strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    try:
        dt = parsedate_to_datetime(text)
        return dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return fallback_date


class HostRateLimiter:
    """Enforces serial per-host minimum intervals to avoid 429 bans."""

    def __init__(
        self,
        *,
        default_interval: float = 1.5,
        arxiv_interval: float = 3.1,
        github_interval: float = 1.2,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.default_interval = default_interval
        self.arxiv_interval = arxiv_interval
        self.github_interval = github_interval
        self._sleep = sleep_fn
        self._clock = clock_fn
        self._last_hit: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urllib.parse.urlparse(url).netloc.lower()
        if not host or host.startswith(("127.0.0.1", "localhost")):
            return
        if "arxiv.org" in host:
            interval = self.arxiv_interval
        elif "github.com" in host:
            interval = self.github_interval
        else:
            interval = self.default_interval

        now = self._clock()
        prev = self._last_hit.get(host)
        if prev is not None:
            remain = interval - (now - prev)
            if remain > 0:
                self._sleep(remain)
        self._last_hit[host] = self._clock()


@dataclass
class SourceHealthTracker:
    """Tracks per-source consecutive failure days and triggers alerts on >= N consecutive days."""

    state_file: Path
    alert_threshold_days: int = 3

    def _load(self) -> dict[str, Any]:
        if not self.state_file.is_file():
            return {"sources": {}}
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def _save(self, data: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def record_success(self, source_id: str, run_date: str) -> None:
        data = self._load()
        sources = data.setdefault("sources", {})
        sources[source_id] = {
            "last_ok_date": run_date,
            "consecutive_failed_dates": [],
            "last_error": "",
        }
        self._save(data)

    def record_failure(self, source_id: str, source_name: str, run_date: str, error_msg: str) -> str | None:
        data = self._load()
        sources = data.setdefault("sources", {})
        entry = sources.get(source_id, {"last_ok_date": "", "consecutive_failed_dates": [], "last_error": ""})
        failed_dates = list(entry.get("consecutive_failed_dates", []))
        if run_date not in failed_dates:
            failed_dates.append(run_date)
        failed_dates = sorted(failed_dates)[-14:]
        entry["consecutive_failed_dates"] = failed_dates
        entry["last_error"] = error_msg[:240]
        entry["source_name"] = source_name
        sources[source_id] = entry
        self._save(data)

        if len(failed_dates) >= self.alert_threshold_days:
            return (
                f"信源连续 {len(failed_dates)} 天拉取失败：{source_name} ({source_id}) — {entry['last_error']}"
            )
        return None


def fetch_url_bytes(
    url: str,
    *,
    rate_limiter: HostRateLimiter | None = None,
    timeout: float = 25.0,
    max_retries: int = 2,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bytes:
    if rate_limiter is not None:
        rate_limiter.wait(url)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, text/xml, */*",
    }
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            last_err = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                sleep_fn(1.5 * (2**attempt))
                continue
            raise
        except urllib.error.URLError as exc:
            last_err = exc
            if attempt < max_retries:
                sleep_fn(1.0 * (2**attempt))
                continue
            raise
    assert last_err is not None
    raise last_err


def parse_arxiv_atom(xml_bytes: bytes, source_cfg: dict[str, Any], fallback_date: str) -> list[RawCandidate]:
    root = ET.fromstring(sanitize_xml_bytes(xml_bytes))
    results: list[RawCandidate] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        id_text = (entry.findtext("atom:id", default="", namespaces=ATOM_NS) or "").strip()
        arxiv_id = normalize_arxiv_id(id_text)
        title = " ".join((entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").split())
        summary = " ".join((entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or "").split())
        if not title or not summary:
            continue
        published_raw = entry.findtext("atom:published", default="", namespaces=ATOM_NS) or ""
        updated_raw = entry.findtext("atom:updated", default="", namespaces=ATOM_NS) or ""
        pub_date = normalize_iso_date(published_raw, fallback_date)
        upd_date = normalize_iso_date(updated_raw, pub_date)
        if upd_date > pub_date:
            pub_date = upd_date

        authors: list[str] = []
        institutions: list[str] = []
        for author_el in entry.findall("atom:author", ATOM_NS):
            name = (author_el.findtext("atom:name", default="", namespaces=ATOM_NS) or "").strip()
            if name:
                authors.append(name)
            for aff_el in author_el.findall("arxiv:affiliation", ATOM_NS):
                aff = (aff_el.text or "").strip()
                if aff and aff not in institutions:
                    institutions.append(aff)

        comment = " ".join((entry.findtext("arxiv:comment", default="", namespaces=ATOM_NS) or "").split())
        doi = (entry.findtext("arxiv:doi", default="", namespaces=ATOM_NS) or "").strip()
        primary_cat_el = entry.find("arxiv:primary_category", ATOM_NS)
        primary_cat = primary_cat_el.attrib.get("term", "") if primary_cat_el is not None else ""

        combined_text = f"{summary} {comment}"
        gh_match = GITHUB_URL_IN_TEXT_RE.search(combined_text)
        hf_match = HF_URL_IN_TEXT_RE.search(combined_text)
        gh_repo = normalize_github_repo("", gh_match.group(0)) if gh_match else ""

        venue_str = f"arXiv {primary_cat}".strip()
        if comment:
            for conf_kw in ("NeurIPS", "ICLR", "ICML", "CVPR", "ICCV", "ECCV", "ACL", "EMNLP", "AAAI", "COLM", "CHI"):
                if conf_kw.lower() in comment.lower():
                    venue_str = f"{comment[:64]} ({venue_str})"
                    break

        canonical_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else id_text
        results.append(
            RawCandidate(
                source_id=source_cfg["id"],
                source_name=source_cfg["name"],
                section=source_cfg.get("section", "papers"),
                title=title,
                url=canonical_url,
                published_at=pub_date,
                summary_raw=summary if not comment else f"{summary}\n[Comment: {comment}]",
                authors=authors,
                institutions=institutions,
                arxiv_id=arxiv_id,
                doi=doi,
                github_repo=gh_repo,
                hf_url=hf_match.group(0) if hf_match else "",
                venue=venue_str,
                extra={"source_kind": "arxiv_api", "comment": comment, "primary_category": primary_cat},
            )
        )
    return results


def parse_hf_daily_papers(json_bytes: bytes, source_cfg: dict[str, Any], fallback_date: str) -> list[RawCandidate]:
    payload = json.loads(json_bytes.decode("utf-8"))
    rows = payload if isinstance(payload, list) else payload.get("papers", [])
    results: list[RawCandidate] = []
    for row in rows:
        paper = row.get("paper", row) if isinstance(row, dict) else {}
        arxiv_id = normalize_arxiv_id(str(paper.get("id", "")))
        title = " ".join(str(paper.get("title") or row.get("title") or "").split())
        summary = " ".join(str(paper.get("summary") or row.get("summary") or "").split())
        if not title:
            continue
        pub_raw = str(paper.get("publishedAt") or row.get("publishedAt") or "")
        pub_date = normalize_iso_date(pub_raw, fallback_date)
        authors = []
        for a in paper.get("authors", []):
            if isinstance(a, dict) and a.get("name"):
                authors.append(str(a["name"]))
            elif isinstance(a, str):
                authors.append(a)
        gh_url = str(paper.get("githubRepo") or "")
        gh_repo = normalize_github_repo("", gh_url)
        stars = paper.get("githubStars")
        upvotes = paper.get("upvotes", row.get("upvotes"))
        hf_url = f"https://huggingface.co/papers/{arxiv_id}" if arxiv_id else ""
        url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else (hf_url or gh_url)
        org_obj = paper.get("organization") or row.get("organization") or {}
        institutions = [str(org_obj["fullname"])] if isinstance(org_obj, dict) and org_obj.get("fullname") else []

        results.append(
            RawCandidate(
                source_id=source_cfg["id"],
                source_name=source_cfg["name"],
                section=source_cfg.get("section", "papers"),
                title=title,
                url=url,
                published_at=pub_date,
                summary_raw=summary,
                authors=authors,
                institutions=institutions,
                arxiv_id=arxiv_id,
                github_repo=gh_repo,
                github_stars=int(stars) if isinstance(stars, int) else None,
                hf_url=hf_url,
                venue="HF Daily Papers",
                extra={"source_kind": "hf_daily_papers", "hf_upvotes": upvotes},
            )
        )
    return results


def parse_github_releases(json_bytes: bytes, source_cfg: dict[str, Any], fallback_date: str) -> list[RawCandidate]:
    payload = json.loads(json_bytes.decode("utf-8"))
    if not isinstance(payload, list):
        return []
    repo = str(source_cfg.get("repo", ""))
    results: list[RawCandidate] = []
    for rel in payload:
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            continue
        tag = str(rel.get("tag_name") or "").strip()
        rel_name = str(rel.get("name") or tag).strip()
        body = strip_html(str(rel.get("body") or ""))
        html_url = str(rel.get("html_url") or f"https://github.com/{repo}/releases")
        pub_date = normalize_iso_date(str(rel.get("published_at") or ""), fallback_date)
        title = f"{repo} {rel_name}" if repo.lower() not in rel_name.lower() else rel_name
        results.append(
            RawCandidate(
                source_id=source_cfg["id"],
                source_name=source_cfg["name"],
                section=source_cfg.get("section", "opensource"),
                title=title,
                url=html_url,
                published_at=pub_date,
                summary_raw=body or f"Release {tag} of {repo}",
                authors=[repo.split("/")[0]] if "/" in repo else [],
                institutions=[repo.split("/")[0]] if "/" in repo else [],
                github_repo=repo.lower(),
                venue=f"GitHub Release ({tag})",
                license_name=str(source_cfg.get("license", "Apache-2.0 / MIT")),
                extra={"source_kind": "github_releases", "release_tag": tag},
            )
        )
    return results


def _split_ainews_items(
    entry_title: str,
    entry_html: str,
    entry_url: str,
    pub_date: str,
    source_cfg: dict[str, Any],
) -> list[RawCandidate]:
    """Split Latent Space `[AINews]` digest bullet items into standalone high-signal candidates."""
    sub_items: list[RawCandidate] = []
    for match in AINEWS_BULLET_RE.finditer(entry_html):
        lead = strip_html(match.group(1) or "")
        rest_html = match.group(2) or ""
        full_plain = strip_html(f"{lead} {rest_html}").strip(" -—:：")
        if len(full_plain) < 40:
            continue
        hrefs = [
            u
            for u in HREF_RE.findall(match.group(0))
            if "latent.space" not in u.lower() and "substack.com" not in u.lower()
        ]
        item_url = hrefs[0] if hrefs else entry_url
        sub_title = lead if len(lead) >= 12 else full_plain.split(".")[0][:96]
        gh_repo = normalize_github_repo("", item_url)
        ax_id = normalize_arxiv_id(item_url)
        inferred_insts = [str(source_cfg.get("org", "Latent Space"))]
        parsed_host = urllib.parse.urlparse(item_url).netloc.lower()
        for dom, org_label in DOMAIN_ORG_HINTS:
            if dom in parsed_host and org_label not in inferred_insts:
                inferred_insts.insert(0, org_label)
        if gh_repo and "/" in gh_repo:
            owner = gh_repo.split("/")[0]
            if owner not in inferred_insts:
                inferred_insts.insert(0, owner)
        sub_items.append(
            RawCandidate(
                source_id=source_cfg["id"],
                source_name=f"{source_cfg['name']} 拆条",
                section="opensource" if gh_repo else source_cfg.get("section", "industry"),
                title=sub_title,
                url=item_url,
                published_at=pub_date,
                summary_raw=full_plain,
                authors=["Latent Space [AINews]"],
                institutions=inferred_insts,
                arxiv_id=ax_id,
                github_repo=gh_repo,
                venue="Latent Space [AINews]",
                extra={"source_kind": "rss", "split_from": entry_title},
            )
        )
    return sub_items


def parse_rss_or_atom(xml_bytes: bytes, source_cfg: dict[str, Any], fallback_date: str) -> list[RawCandidate]:
    root = ET.fromstring(sanitize_xml_bytes(xml_bytes))
    org = str(source_cfg.get("org") or source_cfg["name"])
    split_ainews = bool(source_cfg.get("split_ainews", False))
    results: list[RawCandidate] = []

    # RSS 2.0 items
    rss_items = root.findall(".//item")
    if rss_items:
        for item in rss_items:
            title = " ".join((item.findtext("title") or "").split())
            link = (item.findtext("link") or "").strip()
            pub_raw = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
            pub_date = normalize_iso_date(pub_raw, fallback_date)
            content_html = (
                item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                or item.findtext("description")
                or ""
            )
            if not title or not link:
                continue
            if split_ainews and "[ainews]" in title.lower():
                split_rows = _split_ainews_items(title, content_html, link, pub_date, source_cfg)
                if split_rows:
                    results.extend(split_rows)
                    continue
            summary = strip_html(content_html)
            results.append(
                RawCandidate(
                    source_id=source_cfg["id"],
                    source_name=source_cfg["name"],
                    section=source_cfg.get("section", "industry"),
                    title=title,
                    url=link,
                    published_at=pub_date,
                    summary_raw=summary or title,
                    authors=[org],
                    institutions=[org],
                    arxiv_id=normalize_arxiv_id(summary),
                    github_repo=normalize_github_repo("", summary),
                    venue=org,
                    extra={"source_kind": "rss"},
                )
            )
        return results

    # Atom 1.0 entries
    for entry in root.findall("{http://www.w3.org/2005/Atom}entry") or root.findall("entry"):
        ns = "{http://www.w3.org/2005/Atom}" if entry.tag.startswith("{") else ""
        title = " ".join((entry.findtext(f"{ns}title") or "").split())
        link = ""
        for lk in entry.findall(f"{ns}link"):
            rel = lk.attrib.get("rel", "alternate")
            href = lk.attrib.get("href", "").strip()
            if href and rel == "alternate":
                link = href
                break
            if href and not link:
                link = href
        pub_raw = entry.findtext(f"{ns}published") or entry.findtext(f"{ns}updated") or ""
        pub_date = normalize_iso_date(pub_raw, fallback_date)
        content_html = entry.findtext(f"{ns}content") or entry.findtext(f"{ns}summary") or ""
        if not title or not link:
            continue
        if split_ainews and "[ainews]" in title.lower():
            split_rows = _split_ainews_items(title, content_html, link, pub_date, source_cfg)
            if split_rows:
                results.extend(split_rows)
                continue
        summary = strip_html(content_html)
        results.append(
            RawCandidate(
                source_id=source_cfg["id"],
                source_name=source_cfg["name"],
                section=source_cfg.get("section", "industry"),
                title=title,
                url=link,
                published_at=pub_date,
                summary_raw=summary or title,
                authors=[org],
                institutions=[org],
                arxiv_id=normalize_arxiv_id(summary),
                github_repo=normalize_github_repo("", summary),
                venue=org,
                extra={"source_kind": "rss"},
            )
        )
    return results


def parse_conference_accepted_json(
    json_bytes: bytes,
    source_cfg: dict[str, Any],
    fallback_date: str,
) -> list[RawCandidate]:
    """Parse official `<conf>.cc/static/virtual/data/*-orals-posters.json` or OpenReview accepted JSON."""
    payload = json.loads(json_bytes.decode("utf-8"))
    rows = payload.get("results", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    conf_name = str(source_cfg.get("conference_name") or source_cfg["name"])
    conf_slug = str(source_cfg.get("conference_slug") or "")
    results: list[RawCandidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = " ".join(str(row.get("name") or row.get("title") or "").split())
        abstract = " ".join(str(row.get("abstract") or "").split())
        if not title:
            continue
        decision = str(row.get("decision") or row.get("tier") or "Poster").strip()
        track = str(row.get("track") or "Main").strip()
        url = str(row.get("virtualsite_url") or row.get("url") or source_cfg.get("url") or "")
        authors: list[str] = []
        insts: list[str] = []
        for a in row.get("authors", []):
            if isinstance(a, dict):
                if a.get("fullname"):
                    authors.append(str(a["fullname"]))
                if a.get("institution") and str(a["institution"]) not in insts:
                    insts.append(str(a["institution"]))
            elif isinstance(a, str):
                authors.append(a)
        results.append(
            RawCandidate(
                source_id=source_cfg["id"],
                source_name=source_cfg["name"],
                section="papers",
                title=title,
                url=url,
                published_at=fallback_date,
                summary_raw=abstract or title,
                authors=authors,
                institutions=insts,
                arxiv_id=normalize_arxiv_id(str(row.get("arxiv_id") or url)),
                github_repo=normalize_github_repo("", str(row.get("code_url") or abstract)),
                venue=f"{conf_name} ({track} · {decision})",
                extra={
                    "source_kind": "conference_json",
                    "conference_slug": conf_slug,
                    "conference_track_tier": f"{track} · {decision}",
                },
            )
        )
    return results


def parse_github_trending(raw_bytes: bytes, source_cfg: dict[str, Any], fallback_date: str) -> list[RawCandidate]:
    """Parse GitHub Trending HTML (`<article class="Box-row">`) or GitHub Search Repositories JSON (`items`)."""
    text = raw_bytes.decode("utf-8-sig", errors="replace").lstrip()
    results: list[RawCandidate] = []
    if text.startswith(("{", "[")):
        payload = json.loads(text)
        items = payload.get("items", payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return []
        for row in items:
            if not isinstance(row, dict):
                continue
            full_name = str(row.get("full_name") or "").strip()
            if "/" not in full_name:
                continue
            desc = " ".join(str(row.get("description") or "").split())
            html_url = str(row.get("html_url") or f"https://github.com/{full_name}")
            stars = row.get("stargazers_count")
            lic = (row.get("license") or {}).get("spdx_id") if isinstance(row.get("license"), dict) else ""
            owner = full_name.split("/")[0]
            pub_date = normalize_iso_date(str(row.get("pushed_at") or row.get("created_at") or ""), fallback_date)
            results.append(
                RawCandidate(
                    source_id=source_cfg["id"],
                    source_name=source_cfg["name"],
                    section=source_cfg.get("section", "opensource"),
                    title=f"{full_name}: {desc[:88]}" if desc else full_name,
                    url=html_url,
                    published_at=pub_date,
                    summary_raw=desc or f"Trending GitHub repository {full_name}",
                    authors=[owner],
                    institutions=[owner],
                    github_repo=full_name.lower(),
                    github_stars=int(stars) if isinstance(stars, int) else None,
                    venue="GitHub Trending",
                    license_name=str(lic or ""),
                    extra={"source_kind": "github_trending"},
                )
            )
        return results

    for art_match in GH_TRENDING_ARTICLE_RE.finditer(text):
        block = art_match.group(1)
        repo_m = GH_TRENDING_REPO_HREF_RE.search(block)
        if not repo_m:
            continue
        full_name = repo_m.group(1).strip("/")
        desc_m = GH_TRENDING_DESC_RE.search(block)
        desc = strip_html(desc_m.group(1)) if desc_m else ""
        today_m = GH_TRENDING_STARS_RE.search(block)
        total_m = GH_TRENDING_TOTAL_STARS_RE.search(block)
        total_stars = int(total_m.group(1).replace(",", "")) if total_m else None
        today_note = f" ({today_m.group(1)} stars today)" if today_m else ""
        owner = full_name.split("/")[0]
        results.append(
            RawCandidate(
                source_id=source_cfg["id"],
                source_name=source_cfg["name"],
                section=source_cfg.get("section", "opensource"),
                title=f"{full_name}: {desc[:88]}" if desc else full_name,
                url=f"https://github.com/{full_name}",
                published_at=fallback_date,
                summary_raw=f"{desc}{today_note}".strip() or f"GitHub Trending repository {full_name}",
                authors=[owner],
                institutions=[owner],
                github_repo=full_name.lower(),
                github_stars=total_stars,
                venue="GitHub Trending",
                extra={"source_kind": "github_trending"},
            )
        )
    return results


def parse_source_payload(raw_bytes: bytes, source_cfg: dict[str, Any], fallback_date: str) -> list[RawCandidate]:
    kind = source_cfg["kind"]
    if kind == "arxiv_api":
        return parse_arxiv_atom(raw_bytes, source_cfg, fallback_date)
    if kind == "hf_daily_papers":
        return parse_hf_daily_papers(raw_bytes, source_cfg, fallback_date)
    if kind == "github_releases":
        return parse_github_releases(raw_bytes, source_cfg, fallback_date)
    if kind == "github_trending":
        return parse_github_trending(raw_bytes, source_cfg, fallback_date)
    if kind == "rss":
        return parse_rss_or_atom(raw_bytes, source_cfg, fallback_date)
    if kind == "conference_json":
        return parse_conference_accepted_json(raw_bytes, source_cfg, fallback_date)
    raise ValueError(f"Unsupported source kind: {kind!r}")


def _filter_by_freshness(
    candidates: list[RawCandidate],
    *,
    run_date: str,
    max_age_days: int,
    source_kind: str,
) -> list[RawCandidate]:
    """Drop stale items older than `max_age_days` so historical RSS archives & old releases never loop."""
    if source_kind == "conference_json" or max_age_days <= 0:
        return candidates
    ref = date.fromisoformat(run_date)
    fresh: list[RawCandidate] = []
    for c in candidates:
        try:
            pub = date.fromisoformat(c.published_at)
        except ValueError:
            fresh.append(c)
            continue
        age_days = (ref - pub).days
        if -2 <= age_days <= max_age_days:
            fresh.append(c)
    return fresh


def collect_all_sources(
    sources_cfg: dict[str, Any],
    *,
    run_date: str,
    health_tracker: SourceHealthTracker | None = None,
    fixtures_dir: Path | None = None,
) -> tuple[list[RawCandidate], list[str]]:
    """Collect candidates serially across all enabled sources (or fixture snapshots) and track source health."""
    rl_cfg = sources_cfg.get("rate_limits", {})
    default_max_age = int(rl_cfg.get("max_age_days", 5))
    limiter = HostRateLimiter(
        default_interval=float(rl_cfg.get("default_per_host_interval_sec", 1.5)),
        arxiv_interval=float(rl_cfg.get("arxiv_interval_sec", 3.1)),
        github_interval=float(rl_cfg.get("github_interval_sec", 1.2)),
    )

    all_candidates: list[RawCandidate] = []
    alerts: list[str] = []

    for src in sources_cfg.get("sources", []):
        if not src.get("enabled", True):
            continue
        src_id = str(src["id"])
        src_name = str(src["name"])
        src_kind = str(src["kind"])
        src_max_age = int(src.get("max_age_days", default_max_age))

        if fixtures_dir is not None:
            # Look for `<src_id>.xml`, `<src_id>.json`, `<src_id>.atom`, or `<src_id>.html` in fixtures_dir.
            candidates_files = [
                fixtures_dir / f"{src_id}.xml",
                fixtures_dir / f"{src_id}.json",
                fixtures_dir / f"{src_id}.atom",
                fixtures_dir / f"{src_id}.html",
            ]
            existing = next((p for p in candidates_files if p.is_file()), None)
            if existing is None:
                continue
            raw_bytes = existing.read_bytes()
            parsed = _filter_by_freshness(
                parse_source_payload(raw_bytes, src, run_date),
                run_date=run_date,
                max_age_days=src_max_age,
                source_kind=src_kind,
            )
            all_candidates.extend(parsed)
            if health_tracker is not None:
                health_tracker.record_success(src_id, run_date)
            continue

        try:
            raw_bytes = fetch_url_bytes(str(src["url"]), rate_limiter=limiter)
            parsed = _filter_by_freshness(
                parse_source_payload(raw_bytes, src, run_date),
                run_date=run_date,
                max_age_days=src_max_age,
                source_kind=src_kind,
            )
            all_candidates.extend(parsed)
            if health_tracker is not None:
                health_tracker.record_success(src_id, run_date)
        except Exception as exc:
            if health_tracker is not None:
                alert = health_tracker.record_failure(src_id, src_name, run_date, f"{type(exc).__name__}: {exc}")
                if alert:
                    alerts.append(alert)
            else:
                alerts.append(f"信源拉取异常：{src_name} ({src_id}) — {type(exc).__name__}: {exc}")

    return all_candidates, alerts
