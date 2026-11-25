"""7-day sliding window cross-source deduplication and canonical event merging."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from daily_radar.models import DailyIssue, RawCandidate


ARXIV_ID_RE = re.compile(r"(?:arxiv[:/\s]+)?(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
GITHUB_REPO_URL_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
WORD_TOKEN_RE = re.compile(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}")

STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "via",
        "towards",
        "toward",
        "using",
        "into",
        "over",
        "under",
        "through",
        "paper",
        "daily",
    }
)

SOURCE_PRIORITY = {
    "arxiv_api": 0,
    "conference_json": 1,
    "github_releases": 2,
    "rss": 3,
    "github_trending": 4,
    "hf_daily_papers": 5,
}


def normalize_arxiv_id(raw: str) -> str:
    if not raw:
        return ""
    match = ARXIV_ID_RE.search(raw.strip())
    return match.group(1) if match else ""


def normalize_doi(raw: str) -> str:
    if not raw:
        return ""
    cleaned = raw.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    return cleaned.strip("/")


def normalize_github_repo(raw_repo: str, fallback_url: str = "") -> str:
    target = raw_repo.strip()
    if not target and fallback_url:
        m = GITHUB_REPO_URL_RE.search(fallback_url)
        if m:
            target = m.group(1)
    target = re.sub(r"\.git$", "", target).strip("/").lower()
    if "/" in target:
        parts = target.split("/")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return f"{parts[0]}/{parts[1]}"
    return ""


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    filtered_q = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if not k.lower().startswith(("utm_", "ref", "source"))
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (parsed.scheme.lower() or "https", parsed.netloc.lower(), path, "", urlencode(filtered_q), "")
    )


def title_tokens(title: str) -> set[str]:
    lowered = re.sub(r"^\[ainews\]\s*", "", title.lower().strip())
    return {tok for tok in WORD_TOKEN_RE.findall(lowered) if tok not in STOPWORDS}


def title_fingerprint(title: str) -> str:
    tokens = sorted(title_tokens(title))
    if not tokens:
        return re.sub(r"\s+", "", title.lower().strip())[:48]
    return " ".join(tokens[:14])


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def candidate_keys(cand: RawCandidate) -> set[str]:
    keys: set[str] = set()
    ax = normalize_arxiv_id(cand.arxiv_id) or normalize_arxiv_id(cand.url)
    if ax:
        keys.add(f"arxiv:{ax}")
    doi = normalize_doi(cand.doi)
    if doi:
        keys.add(f"doi:{doi}")
    gh = normalize_github_repo(cand.github_repo, cand.url)
    if gh:
        tag = str(cand.extra.get("release_tag", "")).strip().lower()
        if tag:
            keys.add(f"gh_release:{gh}@{tag}")
        elif cand.section == "opensource":
            keys.add(f"gh_repo:{gh}")
    nurl = normalize_url(cand.url)
    if nurl:
        keys.add(f"url:{nurl}")
    fp = title_fingerprint(cand.title)
    if fp and len(fp) >= 12:
        keys.add(f"title_fp:{fp}")
    return keys


def load_recent_seen_keys(content_dir: Path, target_date: str, window_days: int = 7) -> tuple[set[str], list[set[str]]]:
    """Load canonical keys and title token sets from issues in [target_date - window_days, target_date - 1]."""
    seen_keys: set[str] = set()
    seen_title_sets: list[set[str]] = []
    if not content_dir.is_dir():
        return seen_keys, seen_title_sets

    ref = date.fromisoformat(target_date)
    earliest = ref - timedelta(days=window_days)

    for json_file in sorted(content_dir.rglob("*.json")):
        stem = json_file.stem
        try:
            d = date.fromisoformat(stem)
        except ValueError:
            continue
        if not (earliest <= d < ref):
            continue
        issue = DailyIssue.from_dict(json.loads(json_file.read_text(encoding="utf-8")))
        for item in issue.featured_items:
            ax = normalize_arxiv_id(item.arxiv_id) or normalize_arxiv_id(item.url_primary)
            if ax:
                seen_keys.add(f"arxiv:{ax}")
            doi = normalize_doi(item.doi)
            if doi:
                seen_keys.add(f"doi:{doi}")
            if item.url_primary:
                seen_keys.add(f"url:{normalize_url(item.url_primary)}")
            if item.url_github and item.section == "opensource":
                gh = normalize_github_repo("", item.url_github)
                if gh:
                    rel_match = re.search(r"/releases/tag/([^/?#]+)", item.url_primary) or re.search(
                        r"GitHub Release \(([^)]+)\)", item.venue_or_source
                    )
                    if rel_match:
                        seen_keys.add(f"gh_release:{gh}@{rel_match.group(1).strip().lower()}")
                    else:
                        seen_keys.add(f"gh_repo:{gh}")
                    seen_keys.add(f"gh_url:{normalize_url(item.url_github)}")
            fp = title_fingerprint(item.title_en)
            if fp:
                seen_keys.add(f"title_fp:{fp}")
            toks = title_tokens(item.title_en)
            if len(toks) >= 4:
                seen_title_sets.append(toks)
        for fitem in issue.folded_items:
            ax = normalize_arxiv_id(fitem.arxiv_id) or normalize_arxiv_id(fitem.url)
            if ax:
                seen_keys.add(f"arxiv:{ax}")
            if fitem.url:
                seen_keys.add(f"url:{normalize_url(fitem.url)}")
            fp = title_fingerprint(fitem.title_en)
            if fp:
                seen_keys.add(f"title_fp:{fp}")
            toks = title_tokens(fitem.title_en)
            if len(toks) >= 4:
                seen_title_sets.append(toks)

    return seen_keys, seen_title_sets


def _merge_two(primary: RawCandidate, secondary: RawCandidate) -> RawCandidate:
    """Merge metadata from secondary into primary while recording corroborating provenance."""
    if secondary.source_name and secondary.source_name != primary.source_name:
        if secondary.source_name not in primary.corroborating_sources:
            primary.corroborating_sources.append(secondary.source_name)
    for src in secondary.corroborating_sources:
        if src != primary.source_name and src not in primary.corroborating_sources:
            primary.corroborating_sources.append(src)
    if not primary.arxiv_id and secondary.arxiv_id:
        primary.arxiv_id = normalize_arxiv_id(secondary.arxiv_id)
    if not primary.doi and secondary.doi:
        primary.doi = secondary.doi
    if not primary.github_repo and secondary.github_repo:
        primary.github_repo = secondary.github_repo
    if primary.github_stars is None and secondary.github_stars is not None:
        primary.github_stars = secondary.github_stars
    if not primary.hf_url and secondary.hf_url:
        primary.hf_url = secondary.hf_url
    if not primary.authors and secondary.authors:
        primary.authors = list(secondary.authors)
    if not primary.institutions and secondary.institutions:
        primary.institutions = list(secondary.institutions)
    if len(secondary.summary_raw) > len(primary.summary_raw) and not primary.arxiv_id:
        primary.summary_raw = secondary.summary_raw
    # Preserve HF upvotes or extra signals
    for k, v in secondary.extra.items():
        if k not in primary.extra and v is not None:
            primary.extra[k] = v
    return primary


def deduplicate_candidates(
    candidates: list[RawCandidate],
    *,
    content_dir: Path | None = None,
    target_date: str,
    window_days: int = 7,
) -> tuple[list[RawCandidate], int]:
    """Deduplicate candidates within batch (canonical primary wins) and against 7-day history.

    Returns (deduplicated_candidates, dropped_or_merged_count).
    """
    hist_keys: set[str] = set()
    hist_titles: list[set[str]] = []
    if content_dir is not None:
        hist_keys, hist_titles = load_recent_seen_keys(content_dir, target_date, window_days=window_days)

    # Sort so primary canonical sources (arXiv / official release / official blog) come before aggregators (HF).
    ordered = sorted(
        candidates,
        key=lambda c: SOURCE_PRIORITY.get(str(c.extra.get("source_kind", "")), 5),
    )

    kept: list[RawCandidate] = []
    kept_key_groups: list[set[str]] = []
    kept_title_sets: list[set[str]] = []
    dropped_or_merged = 0

    for cand in ordered:
        cand.arxiv_id = normalize_arxiv_id(cand.arxiv_id) or normalize_arxiv_id(cand.url)
        ckeys = candidate_keys(cand)
        ctoks = title_tokens(cand.title)

        # 1. Check 7-day history
        if ckeys & hist_keys:
            dropped_or_merged += 1
            continue
        if len(ctoks) >= 5 and any(jaccard_similarity(ctoks, ht) >= 0.86 for ht in hist_titles):
            dropped_or_merged += 1
            continue

        # 2. Check current batch for cross-source merge
        matched_idx: int | None = None
        for idx, existing_keys in enumerate(kept_key_groups):
            if ckeys & existing_keys:
                matched_idx = idx
                break
            if len(ctoks) >= 5 and jaccard_similarity(ctoks, kept_title_sets[idx]) >= 0.86:
                matched_idx = idx
                break

        if matched_idx is not None:
            kept[matched_idx] = _merge_two(kept[matched_idx], cand)
            kept_key_groups[matched_idx] |= ckeys
            kept_title_sets[matched_idx] |= ctoks
            dropped_or_merged += 1
        else:
            kept.append(cand)
            kept_key_groups.append(set(ckeys))
            kept_title_sets.append(ctoks)

    return kept, dropped_or_merged
