"""Verbatim numerical verification against raw source text (anti-hallucination guardrail)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re

from daily_radar.models import MetricBadge, MetricRow, RadarItem


# Strips well-known model identifiers, GPU SKU names, benchmark names, arXiv IDs, and years before extracting metric
# numbers, so "OSWorld" / "Qwen3-VL-8B" / "FPR95" / "H800" / "2607.25904" / "2026" don't create false checks, while
# real experimental numbers (+5.2%, 94.8%, 80.21%, 1,200, 46%, 4×, 318) ARE strictly checked against source text!
KNOWN_NON_METRIC_TOKENS_RE = re.compile(
    r"(?:"
    r"\b20[12]\d(?:-\d{2}-\d{2})?\b"
    r"|\b(?:arXiv:)?\d{4}\.\d{4,5}(?:v\d+)?\b"
    r"|\b(?:FPR95|AUROC|AUPR|Pass@\d+|Top-[15]|R@[15]|L[12]|FP8|BF16|FP16|INT[48])\b"
    r"|\b(?:H100|H200|H800|A100|A800|B200|B300|GB200|GB300|MI300X|MI355X|RTX\s?\d{4}D?|TPU\d+x?)\b"
    r"|\b(?:Qwen\d*(?:\.\d+)?(?:-VL|-Image|-Max|-Omni)?(?:-\d+(?:\.\d+)?[BTM])?)\b"
    r"|\b(?:Llama-?\d+(?:\.\d+)?(?:-\d+[BM])?|DeepSeek-(?:R\d+|V\d+(?:\.\d+)?(?:-Flash)?)|Gemma\d*-\d+B|ERNIE\s*\d+(?:\.\d+)?)\b"
    r"|\b(?:GPT-\d+[A-Za-z0-9.-]*|Claude\s*\d+(?:\.\d+)?|Gemini\s*\d+(?:\.\d+)?|Genie\s*\d+|UI-TARS(?:-\d+(?:\.\d+)?(?:-\d+B)?)?)\b"
    r"|\b(?:ViT-[BLHG]/\d+|Python\s*\d+\.\d+\+?|CUDA\s*\d+(?:\.\d+)?)\b"
    r"|\bv\d+\.\d+(?:\.\d+)?(?:[-a-z0-9.]*)?\b"
    r")",
    re.IGNORECASE,
)

ARXIV_OR_VERSION_IN_SOURCE_RE = re.compile(
    r"(?:\b(?:arXiv:)?\d{4}\.\d{4,5}(?:v\d+)?\b|\bhttps?://[^\s)]+)",
    re.IGNORECASE,
)

NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])\d+(?:,\d{3})*(?:\.\d+)?%?")


def _normalize_num_variants(num_token: str) -> set[str]:
    """Generate exact textual & mathematical equivalents (e.g. '1,200' <-> '1200', '85.2%' <-> '0.852')."""
    raw = num_token.strip()
    no_comma = raw.replace(",", "")
    variants = {raw, no_comma}
    if no_comma.endswith("%"):
        bare = no_comma[:-1]
        variants.add(bare)
        # Allow exact decimal-to-percentage equivalence (e.g., source '0.852' <-> claim '85.2%')
        # only when decimal precision is unambiguous (3+ fractional digits or explicit tenths like 85.2% -> 0.852),
        # preventing hyperparameter values like '0.05' from falsely validating '5%'.
        if "." in bare:
            try:
                dec_ratio = Decimal(bare) / Decimal(100)
                if Decimal(0) < dec_ratio < Decimal(1):
                    formatted = format(dec_ratio.normalize(), "f")
                    variants.add(formatted)
            except InvalidOperation:
                pass
    return variants


def extract_claim_numbers(claim_text: str) -> list[str]:
    scrubbed = KNOWN_NON_METRIC_TOKENS_RE.sub(" ", claim_text)
    scrubbed = scrubbed.replace("⚠️ 原文待核", " ")
    tokens = NUMBER_TOKEN_RE.findall(scrubbed)
    seen: list[str] = []
    for tok in tokens:
        if tok not in seen:
            seen.append(tok)
    return seen


def number_appears_in_source(num_token: str, source_corpus: str) -> bool:
    # Scrub arXiv IDs and URLs from source corpus so digits inside '2607.25904' never validate hallucinated claims.
    cleaned_corpus = ARXIV_OR_VERSION_IN_SOURCE_RE.sub(" ", source_corpus)
    corpus_no_comma = cleaned_corpus.replace(",", "")
    for variant in _normalize_num_variants(num_token):
        escaped = re.escape(variant)
        if variant.endswith("%"):
            pattern = re.compile(rf"(?<![0-9.]){escaped}")
        else:
            # Must NOT be followed by another digit OR a decimal fraction ('.' + digit),
            # otherwise '5' or '48' would falsely match inside '+5.2%' or '48.6%' or '0.05'!
            pattern = re.compile(rf"(?<![0-9.]){escaped}(?![0-9]|\.[0-9])")
        if pattern.search(cleaned_corpus) or pattern.search(corpus_no_comma):
            return True
    return False


def verify_claim_against_source(claim_text: str, source_corpus: str) -> tuple[bool, list[str]]:
    """Return (all_verified, unverified_number_tokens)."""
    nums = extract_claim_numbers(claim_text)
    if not nums:
        return True, []
    missing = [tok for tok in nums if not number_appears_in_source(tok, source_corpus)]
    return len(missing) == 0, missing


def verify_radar_item(item: RadarItem, extra_source_text: str = "") -> RadarItem:
    """Verify every metric badge and experiment row of a RadarItem against its verbatim source text.

    Any claim containing a number absent from the verbatim source text is marked `verified=False`
    so the renderer displays `⚠️ 原文待核` explicitly instead of silently presenting hallucinated digits.
    """
    corpus = "\n".join(
        part
        for part in (
            item.title_en,
            item.abstract_en,
            item.venue_or_source,
            f"github_stars:{item.github_stars}" if item.github_stars is not None else "",
            extra_source_text,
        )
        if part
    )

    verified_badges: list[MetricBadge] = []
    for badge in item.metric_badges:
        clean_text = badge.text.replace(" ⚠️ 原文待核", "").strip()
        ok, missing = verify_claim_against_source(clean_text, corpus)
        verified_badges.append(MetricBadge(text=clean_text, verified=ok, unverified_numbers=missing))
    item.metric_badges = verified_badges

    verified_rows: list[MetricRow] = []
    for row in item.four_elements.experiment_rows:
        clean_val = row.value.replace(" ⚠️ 原文待核", "").strip()
        combined_claim = f"{clean_val} {row.baseline_delta} {row.setup_note}".strip()
        ok, missing = verify_claim_against_source(combined_claim, corpus)
        verified_rows.append(
            MetricRow(
                benchmark=row.benchmark,
                metric=row.metric,
                value=clean_val,
                baseline_delta=row.baseline_delta,
                setup_note=row.setup_note,
                verified=ok,
                unverified_numbers=missing,
            )
        )
    item.four_elements.experiment_rows = verified_rows

    # Also check free-form experiments_summary paragraph and append explicit note if any unverified digit exists.
    clean_summary = re.sub(r"\s*（⚠️ 原文待核数字：[^）]+）$", "", item.four_elements.experiments_summary).strip()
    ok_sum, missing_sum = verify_claim_against_source(clean_summary, corpus)
    if not ok_sum:
        joined = "、".join(missing_sum)
        item.four_elements.experiments_summary = f"{clean_summary}（⚠️ 原文待核数字：{joined}）"
    else:
        item.four_elements.experiments_summary = clean_summary

    return item
