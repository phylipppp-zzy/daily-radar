"""BibTeX and `zoteroctl add` command generators matching Zotero Web API item schemas."""

from __future__ import annotations

import json
import re
from typing import Any

from daily_radar.models import RadarItem


TRACK_TO_ZOTERO_TAG = {
    "gui-rl": "topic:gui-rl",
    "ossl-ood": "topic:ossl-ood",
    "vlm-infra": "topic:vlm-infra",
}


def _split_author_name(full_name: str, creator_type: str = "author") -> dict[str, str]:
    cleaned = " ".join(full_name.strip().split())
    if not cleaned:
        return {"creatorType": creator_type, "name": "Unknown"}
    if "," in cleaned:
        last, first = [x.strip() for x in cleaned.split(",", 1)]
        return {"creatorType": creator_type, "firstName": first, "lastName": last}
    parts = cleaned.split(" ")
    if len(parts) == 1:
        return {"creatorType": creator_type, "name": parts[0]}
    return {"creatorType": creator_type, "firstName": " ".join(parts[:-1]), "lastName": parts[-1]}


def _escape_bibtex_text(raw: str) -> str:
    """Escape LaTeX/BibTeX special characters (%, &, #, _) so generated BibTeX compiles cleanly."""
    out = raw.replace("\\", "\\textbackslash{}")
    for ch in ("%", "&", "#", "_"):
        out = out.replace(ch, f"\\{ch}")
    return out


def build_bibtex(item: RadarItem) -> str:
    year = item.published_date[:4]
    first_author_token = "radar"
    if item.authors:
        creator = _split_author_name(item.authors[0])
        raw_last = creator.get("lastName") or creator.get("name") or "radar"
        first_author_token = re.sub(r"[^a-z0-9]", "", raw_last.lower()) or "radar"
    first_word = re.sub(r"[^a-z0-9]", "", item.title_en.split()[0].lower()) if item.title_en.split() else "work"
    cite_key = f"{first_author_token}{year}{first_word}"
    authors_bib = _escape_bibtex_text(" and ".join(item.authors) if item.authors else item.venue_or_source)
    title_bib = _escape_bibtex_text(item.title_en)
    venue_bib = _escape_bibtex_text(item.venue_or_source)

    if item.arxiv_id:
        clean_arxiv = re.sub(r"v\d+$", "", item.arxiv_id)
        lines = [
            f"@misc{{{cite_key},",
            f"  title={{{title_bib}}},",
            f"  author={{{authors_bib}}},",
            f"  year={{{year}}},",
            f"  eprint={{{clean_arxiv}}},",
            "  archivePrefix={arXiv},",
            f"  url={{{item.url_primary}}}",
            "}",
        ]
        return "\n".join(lines)

    entry_type = "inproceedings" if item.section == "papers" else "misc"
    venue_field = (
        f"  booktitle={{{venue_bib}}},"
        if entry_type == "inproceedings"
        else f"  howpublished={{{venue_bib}}},"
    )
    lines = [
        f"@{entry_type}{{{cite_key},",
        f"  title={{{title_bib}}},",
        f"  author={{{authors_bib}}},",
        f"  year={{{year}}},",
        venue_field,
        f"  url={{{item.url_primary}}}",
        "}",
    ]
    return "\n".join(lines)


def build_zotero_api_item(item: RadarItem) -> dict[str, Any]:
    """Build a valid Zotero Web API parent item JSON accepted by `zoteroctl add FILE.json`."""
    topic_tag = TRACK_TO_ZOTERO_TAG.get(item.track, "topic:vlm-infra")
    tags = [{"tag": topic_tag}]
    for sub in item.subtopics[:2]:
        tags.append({"tag": f"subtopic:{sub}"})

    # Zotero Web API v3 requires creatorType='programmer' for computerProgram items ('author' returns HTTP 400).
    is_program = (not item.arxiv_id) and (item.section == "opensource")
    creator_role = "programmer" if is_program else "author"
    creators = (
        [_split_author_name(a, creator_type=creator_role) for a in item.authors]
        if item.authors
        else [{"creatorType": creator_role, "name": item.venue_or_source}]
    )

    if item.arxiv_id:
        clean_arxiv = re.sub(r"v\d+$", "", item.arxiv_id)
        payload: dict[str, Any] = {
            "itemType": "preprint",
            "title": item.title_en,
            "creators": creators,
            "abstractNote": item.abstract_en,
            "repository": "arXiv",
            "archiveID": f"arXiv:{clean_arxiv}",
            "date": item.published_date,
            "url": f"https://arxiv.org/abs/{clean_arxiv}",
            "tags": tags,
        }
        if item.doi:
            payload["DOI"] = item.doi
        return payload

    if item.section == "papers":
        payload = {
            "itemType": "conferencePaper",
            "title": item.title_en,
            "creators": creators,
            "abstractNote": item.abstract_en,
            "proceedingsTitle": item.venue_or_source,
            "date": item.published_date,
            "url": item.url_primary,
            "tags": tags,
        }
        if item.doi:
            payload["DOI"] = item.doi
        return payload

    if item.section == "opensource":
        return {
            "itemType": "computerProgram",
            "title": item.title_en,
            "creators": creators,
            "abstractNote": item.abstract_en or item.one_line_pain,
            "date": item.published_date,
            "url": item.url_github or item.url_primary,
            "rights": item.license_name,
            "tags": tags,
        }

    return {
        "itemType": "blogPost",
        "title": item.title_en,
        "creators": creators,
        "abstractNote": item.abstract_en or item.one_line_pain,
        "blogTitle": item.venue_or_source,
        "date": item.published_date,
        "url": item.url_primary,
        "tags": tags,
    }


def build_zoteroctl_command(item: RadarItem, apply: bool = False) -> str:
    """Generate a subshell-isolated POSIX one-liner writing a regular temp file for `zoteroctl add`.

    Note:
    1. `zoteroctl`'s `read_json_file` checks `Path(path).is_file()`, so `mktemp` is used instead of `<(...)` FIFOs.
    2. Wrapped in `( ... )` so `tmp_zot` and `trap ... EXIT` never pollute or overwrite the user's interactive shell traps.
    3. Resolves `zoteroctl` from `$PATH` or falls back to `$HOME/workspace/zoteroctl/zoteroctl`.
    """
    payload_obj = build_zotero_api_item(item)
    compact_json = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":"))
    shell_safe = compact_json.replace("'", "'\\''")
    apply_flag = " --apply" if apply else ""
    return (
        f"(tmp_zot=$(mktemp /tmp/zoteroctl-XXXXXX.json) && "
        f"trap 'rm -f \"$tmp_zot\"' EXIT && "
        f"printf '%s\\n' '{shell_safe}' > \"$tmp_zot\" && "
        f"zot_bin=$(command -v zoteroctl || printf '%s' \"$HOME/workspace/zoteroctl/zoteroctl\") && "
        f"\"$zot_bin\" add \"$tmp_zot\"{apply_flag})"
    )
