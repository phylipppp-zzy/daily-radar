"""Strict data schemas and invariants for Daily Research Radar."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any


BANNED_PR_CLICHES: tuple[str, ...] = (
    "提出了创新框架",
    "取得了优异性能",
    "取得了最先进的性能",
    "填补了空白",
    "引领了行业",
    "颠覆性突破",
    "赋能千行百业",
)

VALID_SECTIONS = frozenset({"papers", "opensource", "industry"})
VALID_OPENNESS = frozenset({"code_and_weights", "code_only", "paper_only", "blog_or_release"})
VALID_RUN_MODES = frozenset({"live", "trial"})

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,79}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def slugify(text: str, max_len: int = 56) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not cleaned:
        cleaned = "item"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("-")
    if len(cleaned) < 2:
        cleaned = f"{cleaned}-x"
    return cleaned


def assert_no_pr_cliche(text: str, field_name: str) -> None:
    for phrase in BANNED_PR_CLICHES:
        if phrase in text:
            raise ValueError(f"Field '{field_name}' contains banned PR cliché '{phrase}': {text!r}")


def visible_char_len(text: str) -> int:
    return len(re.sub(r"\s+", " ", text.strip()))


@dataclass
class RawCandidate:
    source_id: str
    source_name: str
    section: str
    title: str
    url: str
    published_at: str
    summary_raw: str
    authors: list[str] = field(default_factory=list)
    institutions: list[str] = field(default_factory=list)
    arxiv_id: str = ""
    doi: str = ""
    github_repo: str = ""
    github_stars: int | None = None
    hf_url: str = ""
    venue: str = ""
    license_name: str = ""
    corroborating_sources: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RawCandidate:
        return cls(**data)


@dataclass
class MetricRow:
    benchmark: str
    metric: str
    value: str
    baseline_delta: str = ""
    setup_note: str = ""
    verified: bool = True
    unverified_numbers: list[str] = field(default_factory=list)

    def display_value(self) -> str:
        if self.verified:
            return self.value
        return f"{self.value} ⚠️ 原文待核"


@dataclass
class MetricBadge:
    text: str
    verified: bool = True
    unverified_numbers: list[str] = field(default_factory=list)

    def display_text(self) -> str:
        if self.verified or "⚠️ 原文待核" in self.text:
            return self.text
        return f"{self.text} ⚠️ 原文待核"


@dataclass
class FourElements:
    pain_point: str
    mechanism: str
    experiments_summary: str
    repro_and_reference: str
    experiment_rows: list[MetricRow] = field(default_factory=list)
    backbone_and_params: str = "原文未单列"
    gpu_and_duration: str = "原文未单列"
    seeds_note: str = "原文未单列"
    verdict_label: str = ""
    verdict_reason: str = ""
    critique_points: list[str] = field(default_factory=list)
    evidence_boundary: str = ""


@dataclass
class RadarItem:
    slug: str
    section: str
    track: str
    track_label: str
    subtopics: list[str]
    lab_tags: list[str]
    institution_tags: list[str]
    title_zh: str
    title_en: str
    one_line_pain: str
    metric_badges: list[MetricBadge]
    openness: str
    openness_label: str
    small_compute_friendly: bool
    small_compute_note: str
    authors: list[str]
    published_date: str
    venue_or_source: str
    url_primary: str
    url_pdf: str = ""
    url_arxiv_html: str = ""
    url_github: str = ""
    url_hf: str = ""
    arxiv_id: str = ""
    doi: str = ""
    github_stars: int | None = None
    license_name: str = ""
    corroborating_sources: list[str] = field(default_factory=list)
    abstract_en: str = ""
    four_elements: FourElements = field(
        default_factory=lambda: FourElements(
            pain_point="",
            mechanism="",
            experiments_summary="",
            repro_and_reference="",
        )
    )
    conference_slug: str = ""
    conference_track_tier: str = ""
    relative_score: float = 0.0

    def validate(self, min_pain_chars: int = 45, max_pain_chars: int = 60) -> None:
        assert SLUG_RE.match(self.slug), f"Invalid item slug: {self.slug!r}"
        assert self.section in VALID_SECTIONS, f"Invalid section: {self.section!r}"
        assert self.openness in VALID_OPENNESS, f"Invalid openness: {self.openness!r}"
        assert self.title_zh.strip(), f"Empty title_zh in {self.slug}"
        assert self.title_en.strip(), f"Empty title_en in {self.slug}"
        assert DATE_RE.match(self.published_date), f"Invalid published_date: {self.published_date!r}"
        assert self.url_primary.startswith(("https://", "http://")), f"Invalid url_primary: {self.url_primary!r}"
        assert 1 <= len(self.metric_badges) <= 2, (
            f"Summary card '{self.slug}' must expose 1–2 metric badges, got {len(self.metric_badges)}"
        )
        pain_len = visible_char_len(self.one_line_pain)
        if not (min_pain_chars <= pain_len <= max_pain_chars):
            raise ValueError(
                f"Item '{self.slug}' one_line_pain must be {min_pain_chars}–{max_pain_chars} chars, "
                f"got {pain_len}: {self.one_line_pain!r}"
            )
        assert_no_pr_cliche(self.one_line_pain, f"{self.slug}.one_line_pain")
        assert_no_pr_cliche(self.four_elements.pain_point, f"{self.slug}.four_elements.pain_point")
        assert self.four_elements.pain_point.strip(), f"Missing pain_point in {self.slug}"
        assert self.four_elements.mechanism.strip(), f"Missing mechanism in {self.slug}"
        assert self.four_elements.experiments_summary.strip(), f"Missing experiments_summary in {self.slug}"
        assert self.four_elements.repro_and_reference.strip(), f"Missing repro_and_reference in {self.slug}"
        fe = self.four_elements
        for name in ("verdict_label", "verdict_reason", "evidence_boundary"):
            assert isinstance(getattr(fe, name), str), f"Invalid {name} in {self.slug}"
        assert isinstance(fe.critique_points, list) and all(
            isinstance(point, str) and point.strip() for point in fe.critique_points
        ), f"Invalid critique_points in {self.slug}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RadarItem:
        badges = [MetricBadge(**b) for b in data.get("metric_badges", [])]
        fe_raw = dict(data.get("four_elements", {}))
        rows = [MetricRow(**r) for r in fe_raw.pop("experiment_rows", [])]
        fe = FourElements(**fe_raw, experiment_rows=rows)
        copied = {k: v for k, v in data.items() if k not in ("metric_badges", "four_elements")}
        return cls(**copied, metric_badges=badges, four_elements=fe)


@dataclass
class FoldedItem:
    slug: str
    section: str
    track: str
    track_label: str
    title_zh: str
    title_en: str
    one_line_takeaway: str
    url: str
    published_date: str
    venue_or_source: str
    arxiv_id: str = ""

    def validate(self) -> None:
        assert self.section in VALID_SECTIONS, f"Invalid folded section: {self.section!r}"
        assert self.title_zh.strip(), f"Empty folded title_zh in {self.slug}"
        assert self.one_line_takeaway.strip(), f"Empty one_line_takeaway in {self.slug}"
        assert_no_pr_cliche(self.one_line_takeaway, f"{self.slug}.one_line_takeaway")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FoldedItem:
        return cls(**data)


@dataclass
class DailyIssue:
    date: str
    weekday_zh: str
    generated_at_cst: str
    global_trend_summary: list[str]
    highlights_top3: list[str]
    featured_items: list[RadarItem]
    folded_items: list[FoldedItem]
    special_conference_banner: dict[str, str] | None = None
    source_health_alerts: list[str] = field(default_factory=list)
    llm_channel_used: str = "deterministic-verified"
    run_mode: str = "live"
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provenance:
            self.provenance = {
                "run_mode": self.run_mode,
                "distiller": self.llm_channel_used,
                "source_origin": "local_fixtures" if self.run_mode == "trial" else "live_web",
            }
        else:
            self.provenance.setdefault("run_mode", self.run_mode)
            self.provenance.setdefault("distiller", self.llm_channel_used)

    def validate(self, featured_max: int = 10, folded_max: int = 20) -> None:
        assert DATE_RE.match(self.date), f"Invalid issue date: {self.date!r}"
        assert self.run_mode in VALID_RUN_MODES, f"Invalid run_mode: {self.run_mode!r}"
        assert 1 <= len(self.global_trend_summary) <= 4, "global_trend_summary must have 1–4 sentences"
        assert len(self.highlights_top3) <= 3, "highlights_top3 must have at most 3 bullets"
        assert len(self.featured_items) <= featured_max, (
            f"Issue {self.date} exceeds featured_max={featured_max}: {len(self.featured_items)}"
        )
        assert len(self.folded_items) <= folded_max, (
            f"Issue {self.date} exceeds folded_max={folded_max}: {len(self.folded_items)}"
        )
        seen_slugs: set[str] = set()
        for item in self.featured_items:
            item.validate()
            if item.slug in seen_slugs:
                raise ValueError(f"Duplicate item slug '{item.slug}' in issue {self.date}")
            seen_slugs.add(item.slug)
        for fitem in self.folded_items:
            fitem.validate()

    def stats_by_track(self) -> dict[str, int]:
        counts = {"gui-rl": 0, "ossl-ood": 0, "vlm-infra": 0, "opensource_and_industry": 0}
        for item in self.featured_items:
            if item.section in ("opensource", "industry"):
                counts["opensource_and_industry"] += 1
            elif item.track in counts:
                counts[item.track] += 1
            else:
                counts["vlm-infra"] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "weekday_zh": self.weekday_zh,
            "generated_at_cst": self.generated_at_cst,
            "global_trend_summary": self.global_trend_summary,
            "highlights_top3": self.highlights_top3,
            "special_conference_banner": self.special_conference_banner,
            "source_health_alerts": self.source_health_alerts,
            "llm_channel_used": self.llm_channel_used,
            "run_mode": self.run_mode,
            "provenance": self.provenance,
            "featured_items": [it.to_dict() for it in self.featured_items],
            "folded_items": [fi.to_dict() for fi in self.folded_items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DailyIssue:
        llm_channel = str(data.get("llm_channel_used", "unknown"))
        inferred_mode = "trial" if llm_channel == "trial-verified-distiller" else "live"
        run_mode = str(data.get("run_mode") or inferred_mode)
        raw_prov = data.get("provenance")
        provenance = dict(raw_prov) if isinstance(raw_prov, dict) else {}
        return cls(
            date=data["date"],
            weekday_zh=data["weekday_zh"],
            generated_at_cst=data["generated_at_cst"],
            global_trend_summary=list(data.get("global_trend_summary", [])),
            highlights_top3=list(data.get("highlights_top3", [])),
            featured_items=[RadarItem.from_dict(x) for x in data.get("featured_items", [])],
            folded_items=[FoldedItem.from_dict(x) for x in data.get("folded_items", [])],
            special_conference_banner=data.get("special_conference_banner"),
            source_health_alerts=list(data.get("source_health_alerts", [])),
            llm_channel_used=llm_channel,
            run_mode=run_mode,
            provenance=provenance,
        )
