"""Multi-source HTTP & fixture collectors, parsers, rate limiting, and 3-day source health tracker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
import html
import html.entities
from html.parser import HTMLParser
from http.client import HTTPException
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


OPENALEX_ARXIV_SOURCE_ID = "S4306400194"
XML_CDATA_SPLIT_RE = re.compile(r"(<!\[CDATA\[.*?\]\]>)", re.DOTALL)
XML_NAMED_OR_BARE_AMP_RE = re.compile(
    r"&(?!amp;|lt;|gt;|quot;|apos;|#[0-9]+;|#x[0-9a-fA-F]+;)([A-Za-z][A-Za-z0-9]+;)?"
)


def _escape_non_cdata_xml(chunk: str) -> str:
    def _replace_amp(match: re.Match[str]) -> str:
        named = match.group(1)
        if named:
            # Convert HTML named entities (e.g. &nbsp;, &mdash;, &rsquo;) to numeric XML char refs.
            name = named[:-1]
            cp = html.entities.name2codepoint.get(name)
            if cp is not None:
                return f"&#{cp};"
            return f"&amp;{named}"
        return "&amp;"

    return XML_NAMED_OR_BARE_AMP_RE.sub(_replace_amp, chunk)


def sanitize_xml_bytes(xml_bytes: bytes) -> bytes:
    """Strip UTF-8 BOM, illegal XML 1.0 control chars, invalid char refs, and unescaped `&` / HTML entities."""
    text = xml_bytes.decode("utf-8-sig", errors="replace").lstrip()
    text = ILLEGAL_XML_CTRL_RE.sub("", text)
    text = ILLEGAL_XML_ENTITY_RE.sub("", text)
    parts = XML_CDATA_SPLIT_RE.split(text)
    for idx in range(0, len(parts), 2):
        parts[idx] = _escape_non_cdata_xml(parts[idx])
    return "".join(parts).encode("utf-8")


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


class HealthCheckSchemaError(ValueError):
    """Raised immediately (fail-fast) when source_health.json violates the required schema."""


@dataclass
class SourceHealthFailure:
    source_id: str
    source_name: str
    last_ok_date: str
    consecutive_failed_dates: list[str]
    last_error: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "last_ok_date": self.last_ok_date,
            "consecutive_failed_dates": self.consecutive_failed_dates,
            "consecutive_failed_days": len(self.consecutive_failed_dates),
            "last_error": self.last_error,
        }


@dataclass
class HealthCheckReport:
    state_file: str
    total_sources: int
    ok_sources: list[str]
    failing_sources: list[SourceHealthFailure]
    missing_sources: list[str]

    @property
    def is_healthy(self) -> bool:
        return len(self.failing_sources) == 0 and len(self.missing_sources) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_file": self.state_file,
            "total_sources": self.total_sources,
            "ok_count": len(self.ok_sources),
            "failed_count": len(self.failing_sources),
            "missing_count": len(self.missing_sources),
            "healthy": self.is_healthy,
            "failing_sources": [f.to_dict() for f in self.failing_sources],
            "missing_sources": self.missing_sources,
        }


REQUIRED_HEALTH_FIELDS: tuple[str, ...] = ("last_ok_date", "consecutive_failed_dates", "last_error")


def check_source_health(
    state_file: Path,
    *,
    expected_source_ids: list[str] | None = None,
) -> HealthCheckReport:
    """Strictly validate source_health.json schema (fail-fast) and report all failing sources."""
    if not state_file.is_file():
        raise HealthCheckSchemaError(f"Health state file does not exist: {state_file}")
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HealthCheckSchemaError(f"Invalid UTF-8 or JSON in {state_file}: {exc}") from exc

    if not isinstance(data, dict) or "sources" not in data or not isinstance(data["sources"], dict):
        raise HealthCheckSchemaError(f"Health state file {state_file} must be a JSON object with a 'sources' dict.")

    sources_map: dict[str, Any] = data["sources"]
    ok_sources: list[str] = []
    failing_sources: list[SourceHealthFailure] = []

    for src_id, entry in sources_map.items():
        if not isinstance(entry, dict):
            raise HealthCheckSchemaError(f"Source '{src_id}' entry must be a JSON object, got {type(entry).__name__}")
        for req_key in REQUIRED_HEALTH_FIELDS:
            if req_key not in entry:
                raise HealthCheckSchemaError(
                    f"Source '{src_id}' missing required field '{req_key}' (present keys: {sorted(entry.keys())})"
                )
        last_ok = entry["last_ok_date"]
        failed_dates = entry["consecutive_failed_dates"]
        last_error = entry["last_error"]
        if not isinstance(last_ok, str):
            raise HealthCheckSchemaError(f"Source '{src_id}' field 'last_ok_date' must be str, got {type(last_ok).__name__}")
        if last_ok.strip():
            try:
                date.fromisoformat(last_ok.strip())
            except ValueError as exc:
                raise HealthCheckSchemaError(
                    f"Source '{src_id}' field 'last_ok_date' is not a valid ISO date: {last_ok!r}"
                ) from exc
        if not isinstance(failed_dates, list) or not all(isinstance(d, str) and d.strip() for d in failed_dates):
            raise HealthCheckSchemaError(
                f"Source '{src_id}' field 'consecutive_failed_dates' must be a list of non-empty date strings."
            )
        for fd in failed_dates:
            try:
                date.fromisoformat(fd.strip())
            except ValueError as exc:
                raise HealthCheckSchemaError(
                    f"Source '{src_id}' field 'consecutive_failed_dates' contains invalid ISO date: {fd!r}"
                ) from exc
        if not isinstance(last_error, str):
            raise HealthCheckSchemaError(f"Source '{src_id}' field 'last_error' must be str, got {type(last_error).__name__}")

        if failed_dates or last_error.strip():
            failing_sources.append(
                SourceHealthFailure(
                    source_id=str(src_id),
                    source_name=str(entry.get("source_name") or src_id),
                    last_ok_date=last_ok,
                    consecutive_failed_dates=list(failed_dates),
                    last_error=last_error,
                )
            )
        else:
            if not last_ok.strip():
                raise HealthCheckSchemaError(
                    f"Source '{src_id}' has empty 'last_ok_date' with no failures recorded; cannot be marked healthy."
                )
            ok_sources.append(str(src_id))

    missing_sources: list[str] = []
    if expected_source_ids:
        for exp_id in expected_source_ids:
            if exp_id not in sources_map:
                missing_sources.append(exp_id)

    return HealthCheckReport(
        state_file=str(state_file),
        total_sources=len(sources_map),
        ok_sources=ok_sources,
        failing_sources=failing_sources,
        missing_sources=missing_sources,
    )


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
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = exc
            if attempt < max_retries:
                sleep_fn(1.0 * (2**attempt))
                continue
            raise
    assert last_err is not None
    raise last_err


class _ArxivExcerptParser(HTMLParser):
    _ignored_tags = {"head", "script", "style", "nav", "header", "footer", "figure", "svg", "math", "mjx-container"}
    _ignored_classes = {
        "ltx_page_header", "ltx_page_footer", "ltx_page_navbar", "ltx_note",
        "ltx_math", "ltx_equation", "ltx_equationgroup", "ltx_figure",
    }
    _void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    _block_tags = {"article", "br", "div", "li", "p", "section", "td", "th"}
    _heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    _preferred_heading = re.compile(
        r"\b(?:introductions?|methods?|methodology|approach|experiments?|experimental|evaluations?|results|"
        r"limitations?|conclusions?)\b",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        super().__init__()
        self._ignored_tag = ""
        self._ignored_depth = 0
        self._heading = ""
        self._preferred_level = 0
        self._parts: list[str] = []
        self._sections: list[str] = []
        self._preferred_sections: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._ignored_tag:
            if tag == self._ignored_tag:
                self._ignored_depth += 1
            return
        classes = (dict(attrs).get("class") or "").lower().split()
        if tag in self._ignored_tags or self._ignored_classes.intersection(classes):
            if tag not in self._void_tags:
                self._ignored_tag = tag
                self._ignored_depth = 1
        elif tag in self._heading_tags:
            self._finish_section()
            self._heading = tag
            if self._preferred_level >= int(tag[1]):
                self._preferred_level = 0
        elif tag in self._block_tags:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._ignored_tag:
            if tag == self._ignored_tag:
                self._ignored_depth -= 1
                if self._ignored_depth == 0:
                    self._ignored_tag = ""
            return
        if tag == self._heading:
            if not self._preferred_level and self._preferred_heading.search("".join(self._parts)):
                self._preferred_level = int(tag[1])
            self._heading = ""
            self._parts.append("\n")
        elif tag in self._block_tags:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._ignored_tag:
            self._parts.append(re.sub(r"\s+", " ", data))

    def _finish_section(self) -> None:
        section = "\n".join(part for line in "".join(self._parts).splitlines() if (part := " ".join(line.split())))
        self._parts.clear()
        if section:
            self._sections.append(section)
            if self._preferred_level:
                self._preferred_sections.append(section)

    def excerpt(self, max_chars: int) -> str:
        self._finish_section()
        text = "\n\n".join(self._preferred_sections or self._sections)
        prefix = text[:max_chars]
        if len(text) > max_chars and not text[max_chars].isspace() and not prefix[-1].isspace():
            prefix = re.sub(r"\s+\S+$", "", prefix)
        return prefix.rstrip()


def fetch_arxiv_html_excerpt(
    arxiv_id: str,
    *,
    base_url: str = "https://arxiv.org/html",
    timeout_sec: float = 10.0,
    max_chars: int = 7000,
    max_bytes: int = 524_288,
) -> str:
    """Fetch an arXiv HTML paper (bounded bytes & timeout) and return a clean text excerpt."""
    arxiv_id = arxiv_id.strip()
    if not arxiv_id or max_chars <= 0 or max_bytes <= 0:
        return ""

    path = urllib.parse.quote(arxiv_id, safe="/")
    url = f"{base_url.rstrip('/')}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            if getattr(resp, "status", 200) != 200:
                return ""
            raw = resp.read(max_bytes)
    except (OSError, HTTPException, urllib.error.URLError):
        return ""

    html_text = raw.decode("utf-8-sig", errors="replace")
    lower_head = html_text[:8192].lower()
    if not any(marker in lower_head for marker in ("<article", "ltx_document", "ltx_page", "<h1", "<h2")):
        return ""

    parser = _ArxivExcerptParser()
    parser.feed(html_text)
    parser.close()
    return parser.excerpt(max_chars)


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


def reconstruct_openalex_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    """Reconstruct plaintext abstract from OpenAlex `abstract_inverted_index`."""
    if not isinstance(inverted_index, dict) or not inverted_index:
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int):
                word_positions.append((pos, str(word)))
    if not word_positions:
        return ""
    word_positions.sort(key=lambda pair: pair[0])
    return " ".join(word for _, word in word_positions)


def _derive_openalex_search_query(source_cfg: dict[str, Any]) -> str:
    explicit = str(source_cfg.get("openalex_query") or "").strip()
    if explicit:
        return explicit
    parsed = urllib.parse.urlparse(str(source_cfg.get("url") or ""))
    qs = urllib.parse.parse_qs(parsed.query)
    raw_sq = (qs.get("search_query") or [""])[0]
    quoted_terms = re.findall(r'"([^"]+)"', raw_sq)
    if quoted_terms:
        return " OR ".join(f'"{t}"' for t in quoted_terms[:8])
    return str(source_cfg.get("name") or "arXiv")


def build_openalex_arxiv_url(
    source_cfg: dict[str, Any],
    *,
    run_date: str,
    max_age_days: int = 5,
) -> str:
    """Build an OpenAlex works query scoped to the arXiv index (`S4306400194`)."""
    explicit_url = str(source_cfg.get("openalex_fallback_url") or "").strip()
    if explicit_url:
        return explicit_url
    ref = date.fromisoformat(run_date)
    lookback = max(max_age_days + 2, 7)
    from_date = (ref - timedelta(days=lookback)).isoformat()
    to_date = (ref + timedelta(days=2)).isoformat()
    search_q = _derive_openalex_search_query(source_cfg)
    params = urllib.parse.urlencode(
        {
            "filter": (
                f"primary_location.source.id:{OPENALEX_ARXIV_SOURCE_ID},"
                f"from_publication_date:{from_date},to_publication_date:{to_date}"
            ),
            "search": search_q,
            "sort": "publication_date:desc",
            "per-page": "35",
            "select": "id,doi,title,publication_date,primary_location,locations,authorships,abstract_inverted_index,ids",
        }
    )
    return f"https://api.openalex.org/works?{params}"


def parse_openalex_works(json_bytes: bytes, source_cfg: dict[str, Any], fallback_date: str) -> list[RawCandidate]:
    """Parse OpenAlex `/works` JSON response (arXiv index `S4306400194`) into RawCandidates."""
    payload = json.loads(json_bytes.decode("utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise ValueError(
                f"OpenAlex response missing 'results' list (keys: {sorted(payload.keys())})"
            )
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError(f"Unexpected OpenAlex JSON root type: {type(payload).__name__}")
    results: list[RawCandidate] = []
    for work in rows:
        if not isinstance(work, dict):
            continue
        title = " ".join(str(work.get("title") or "").split())
        if not title:
            continue
        summary = reconstruct_openalex_abstract(work.get("abstract_inverted_index"))
        if not summary:
            summary = title

        pub_date = normalize_iso_date(str(work.get("publication_date") or ""), fallback_date)
        ids_obj = work.get("ids") if isinstance(work.get("ids"), dict) else {}
        primary_loc = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
        locations = work.get("locations") if isinstance(work.get("locations"), list) else []

        # Prioritize explicit arXiv identifiers/URLs BEFORE DOIs, and only accept strings explicitly referencing arXiv
        # so publisher DOIs (e.g. `10.1145/3290605.3300766`) never corrupt `arxiv_id`!
        arxiv_candidates = [
            str(ids_obj.get("arxiv") or ""),
            str(primary_loc.get("landing_page_url") or ""),
            str(primary_loc.get("pdf_url") or ""),
        ]
        for loc in locations:
            if isinstance(loc, dict):
                arxiv_candidates.append(str(loc.get("landing_page_url") or ""))
                arxiv_candidates.append(str(loc.get("pdf_url") or ""))
        arxiv_candidates.extend(
            [
                str(work.get("doi") or ""),
                str(ids_obj.get("doi") or ""),
            ]
        )

        arxiv_id = ""
        for cand_str in arxiv_candidates:
            if "arxiv" not in cand_str.lower():
                continue
            ax = normalize_arxiv_id(cand_str)
            if ax:
                arxiv_id = ax
                break

        doi = str(work.get("doi") or ids_obj.get("doi") or "").strip()
        authors: list[str] = []
        institutions: list[str] = []
        for auth in work.get("authorships") or []:
            if not isinstance(auth, dict):
                continue
            author_obj = auth.get("author")
            if isinstance(author_obj, dict) and author_obj.get("display_name"):
                authors.append(str(author_obj["display_name"]).strip())
            for inst in auth.get("institutions") or []:
                if isinstance(inst, dict) and inst.get("display_name"):
                    inst_name = str(inst["display_name"]).strip()
                    if inst_name and inst_name not in institutions:
                        institutions.append(inst_name)

        gh_match = GITHUB_URL_IN_TEXT_RE.search(summary)
        hf_match = HF_URL_IN_TEXT_RE.search(summary)
        gh_repo = normalize_github_repo("", gh_match.group(0)) if gh_match else ""
        canonical_url = (
            f"https://arxiv.org/abs/{arxiv_id}"
            if arxiv_id
            else str(primary_loc.get("landing_page_url") or work.get("id") or "")
        )
        if not canonical_url:
            continue

        results.append(
            RawCandidate(
                source_id=source_cfg["id"],
                source_name=source_cfg["name"],
                section=source_cfg.get("section", "papers"),
                title=title,
                url=canonical_url,
                published_at=pub_date,
                summary_raw=summary,
                authors=authors,
                institutions=institutions,
                arxiv_id=arxiv_id,
                doi=doi,
                github_repo=gh_repo,
                hf_url=hf_match.group(0) if hf_match else "",
                venue="arXiv (OpenAlex S4306400194)",
                extra={
                    "source_kind": "arxiv_api",
                    "fallback_provider": f"openalex:{OPENALEX_ARXIV_SOURCE_ID}",
                    "openalex_id": str(work.get("id") or ""),
                },
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


EXPLICIT_ARXIV_PROSE_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf|html)/|arxiv[:\s]+)(\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)


def _extract_explicit_arxiv_id(text: str) -> str:
    if not text:
        return ""
    match = EXPLICIT_ARXIV_PROSE_RE.search(text)
    return match.group(1) if match else ""


def _split_ainews_items(
    entry_title: str,
    entry_html: str,
    entry_url: str,
    pub_date: str,
    source_cfg: dict[str, Any],
) -> list[RawCandidate]:
    """Split Latent Space `[AINews]` digest bullet items into standalone high-signal candidates."""
    sub_items: list[RawCandidate] = []
    for idx, match in enumerate(AINEWS_BULLET_RE.finditer(entry_html), start=1):
        lead = strip_html(match.group(1) or "")
        rest_html = match.group(2) or ""
        full_plain = strip_html(f"{lead} {rest_html}").strip(" -—:：")
        if len(full_plain) < 40:
            continue
        hrefs = HREF_RE.findall(match.group(0))
        external_hrefs = [
            url for url in hrefs if "latent.space" not in url.lower() and "substack.com" not in url.lower()
        ]
        item_url = external_hrefs[0] if external_hrefs else entry_url
        sub_title = lead if len(lead) >= 12 else full_plain.split(".")[0][:96]

        ax_id = ""
        for u in external_hrefs:
            if "arxiv.org" in u.lower():
                ax_id = normalize_arxiv_id(u)
                if ax_id:
                    break
        if not ax_id:
            ax_id = _extract_explicit_arxiv_id(full_plain)

        primary_gh_repo = normalize_github_repo("", item_url)
        gh_repo = primary_gh_repo
        if not gh_repo:
            secondary_gh = [normalize_github_repo("", u) for u in external_hrefs[1:]]
            secondary_gh = [r for r in secondary_gh if r]
            if len(set(secondary_gh)) == 1:
                gh_repo = secondary_gh[0]
            elif not secondary_gh:
                gh_repo = normalize_github_repo("", full_plain)

        if item_url == entry_url:
            if ax_id:
                item_url = f"https://arxiv.org/abs/{ax_id}"
            else:
                sep = "&" if "?" in entry_url else "?"
                item_url = f"{entry_url}{sep}ainews_item={idx}"

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
                section="opensource" if primary_gh_repo else source_cfg.get("section", "industry"),
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


def _rss_title_from_summary(summary: str) -> str:
    first_line = summary.splitlines()[0] if summary else ""
    first_sentence = re.split(r"(?<=[.!?])\s+|(?<=[。！？])", first_line, maxsplit=1)[0]
    title = " ".join(first_sentence.split())
    if len(title) <= 96:
        return title
    prefix = title[:95]
    if not title[95].isspace():
        prefix = prefix.rpartition(" ")[0] or prefix
    return prefix.rstrip() + "…"


def _compile_keyword_matchers(raw_keywords: list[str] | tuple[str, ...]) -> tuple[re.Pattern[str] | str, ...]:
    matchers: list[re.Pattern[str] | str] = []
    for kw in raw_keywords:
        cleaned = str(kw).strip().lower()
        if not cleaned:
            continue
        if cleaned.isascii():
            matchers.append(re.compile(rf"(?<![a-z0-9]){re.escape(cleaned)}(?![a-z0-9])"))
        else:
            matchers.append(cleaned)
    return tuple(matchers)


def parse_rss_or_atom(xml_bytes: bytes, source_cfg: dict[str, Any], fallback_date: str) -> list[RawCandidate]:
    root = ET.fromstring(sanitize_xml_bytes(xml_bytes))
    org = str(source_cfg.get("org") or source_cfg["name"])
    split_ainews = bool(source_cfg.get("split_ainews", False))
    social_feed = bool(source_cfg.get("social_feed"))
    exclude_title_regex = source_cfg.get("exclude_title_regex")
    keyword_matchers = _compile_keyword_matchers(source_cfg.get("require_keywords") or [])
    results: list[RawCandidate] = []

    def matches_keywords(title: str, summary: str) -> bool:
        if not keyword_matchers:
            return True
        text = (title + " " + summary).lower()
        return any(
            (m.search(text) is not None) if isinstance(m, re.Pattern) else (m in text)
            for m in keyword_matchers
        )

    rss_items = root.findall(".//item")
    atom_items = root.findall("{http://www.w3.org/2005/Atom}entry") or root.findall("entry")
    for entry in rss_items or atom_items:
        ns = "{http://www.w3.org/2005/Atom}" if entry.tag.startswith("{") else ""
        title = " ".join((entry.findtext(f"{ns}title") or "").split())
        if rss_items:
            link = (entry.findtext("link") or "").strip()
            pub_raw = entry.findtext("pubDate") or entry.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
            content_html = (
                entry.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
                or entry.findtext("description")
                or ""
            )
        else:
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
            content_html = entry.findtext(f"{ns}content") or entry.findtext(f"{ns}summary") or ""

        summary = strip_html(content_html)
        if social_feed and len(summary) < 28:
            continue
        title = title or _rss_title_from_summary(summary)
        if not title or not link:
            continue
        if exclude_title_regex and re.search(exclude_title_regex, title):
            continue
        pub_date = normalize_iso_date(pub_raw, fallback_date)
        if split_ainews and ("[ainews]" in title.lower() or "smol.ai" in link.lower()):
            split_rows = _split_ainews_items(title, content_html, link, pub_date, source_cfg)
            if split_rows:
                results.extend(row for row in split_rows if matches_keywords(row.title, row.summary_raw))
                continue
        if not matches_keywords(title, summary or title):
            continue
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
                arxiv_id=_extract_explicit_arxiv_id(summary),
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


def _is_arxiv_fallback_trigger(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            return True
        if "timed out" in str(reason).lower():
            return True
    return False


def _collect_live_source(
    src: dict[str, Any],
    *,
    run_date: str,
    max_age_days: int,
    rate_limiter: HostRateLimiter,
) -> list[RawCandidate]:
    src_kind = str(src["kind"])
    extra_urls = [str(u) for u in (src.get("extra_urls") or []) if str(u).strip()]
    fallback_urls = [str(u) for u in (src.get("fallback_urls") or []) if str(u).strip()]
    urls_to_try = [str(src["url"])] + fallback_urls
    any_ok = False
    last_exc: Exception | None = None

    # Multi-feed union mode (e.g. Anthropic research+engineering+news or active Qwen release feeds)
    if extra_urls:
        union_urls = [str(src["url"])] + extra_urls
        merged: list[RawCandidate] = []
        seen_urls: set[str] = set()
        for url in union_urls:
            try:
                raw_bytes = fetch_url_bytes(url, rate_limiter=rate_limiter)
                fresh = _filter_by_freshness(
                    parse_source_payload(raw_bytes, src, run_date),
                    run_date=run_date,
                    max_age_days=max_age_days,
                    source_kind=src_kind,
                )
                any_ok = True
                for cand in fresh:
                    if cand.url not in seen_urls:
                        seen_urls.add(cand.url)
                        merged.append(cand)
            except Exception as exc:
                last_exc = exc
                continue
        if any_ok:
            return merged
        urls_to_try = fallback_urls

    for url in urls_to_try:
        try:
            raw_bytes = fetch_url_bytes(url, rate_limiter=rate_limiter)
            fresh = _filter_by_freshness(
                parse_source_payload(raw_bytes, src, run_date),
                run_date=run_date,
                max_age_days=max_age_days,
                source_kind=src_kind,
            )
            any_ok = True
            if fresh:
                return fresh
        except Exception as exc:
            last_exc = exc
            continue

    if any_ok:
        return []

    assert last_exc is not None
    if src_kind == "arxiv_api" and _is_arxiv_fallback_trigger(last_exc):
        openalex_url = build_openalex_arxiv_url(src, run_date=run_date, max_age_days=max_age_days)
        try:
            oa_bytes = fetch_url_bytes(openalex_url, rate_limiter=rate_limiter)
            return _filter_by_freshness(
                parse_openalex_works(oa_bytes, src, run_date),
                run_date=run_date,
                max_age_days=max_age_days,
                source_kind=src_kind,
            )
        except Exception as oa_exc:
            raise RuntimeError(
                f"Primary arXiv failed ({type(last_exc).__name__}: {last_exc}); "
                f"OpenAlex {OPENALEX_ARXIV_SOURCE_ID} fallback failed ({type(oa_exc).__name__}: {oa_exc})"
            ) from oa_exc

    raise last_exc


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
            parsed = _collect_live_source(
                src,
                run_date=run_date,
                max_age_days=src_max_age,
                rate_limiter=limiter,
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
