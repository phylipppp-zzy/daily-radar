"""Mandatory pre-publish secret, private-network, and research-confidentiality scanner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from daily_radar.config import default_user_config_dir, ensure_mode_0600_read


# Rule 1 of §4.3: Zero binary assets in the repository or rendered site.
FORBIDDEN_BINARY_SUFFIXES = frozenset(
    {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".mp4",
        ".mov",
        ".zip",
        ".tar",
        ".gz",
        ".pt",
        ".pth",
        ".safetensors",
        ".onnx",
        ".bin",
    }
)

SCANNABLE_TEXT_SUFFIXES = frozenset(
    {".json", ".html", ".css", ".js", ".yaml", ".yml", ".md", ".txt", ".xml", ".svg", ".toml", ".py", ".sh"}
)

# Built-in regex patterns for API keys, webhooks, internal IPs, local paths, and private manuscripts.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_or_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github_pat", re.compile(r"\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b")),
    ("pem_private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    (
        "feishu_webhook_url",
        re.compile(r"https?://open\.(?:feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/[A-Za-z0-9-]+"),
    ),
    ("feishu_app_secret", re.compile(r"\bFEISHU_APP_SECRET\s*=\s*['\"]?[A-Za-z0-9]{16,}")),
    ("bearer_secret", re.compile(r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE)),
    (
        "rfc1918_private_ipv4",
        re.compile(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
        ),
    ),
    (
        "internal_machine_or_user_path",
        re.compile(
            r"(?<![A-Za-z0-9_./-])(?:/home/(?!runner\b)[A-Za-z0-9._-]+(?:/|\b)"
            r"|/Users/(?!runner\b)[A-Za-z0-9._-]+(?:/|\b)|/ssd/[A-Za-z0-9._-]+/)"
        ),
    ),
    (
        "unpublished_manuscript_codename",
        re.compile(
            r"\b(?:submission-(?:cvpr|iccv|eccv|icml|neurips|iclr|aaai|acl|emnlp)\d{4}"
            r"|(?:CVPR|ICCV|ECCV|ICML|NeurIPS|ICLR|AAAI|ACL|EMNLP)\d{4}-[A-Za-z0-9_-]+)\b",
            re.IGNORECASE,
        ),
    ),
)

SKIP_SCAN_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", "state"})


@dataclass
class ScanFinding:
    path: str
    line_no: int
    rule: str
    excerpt: str


def load_extra_sensitive_terms(config_home: Path | None = None) -> list[str]:
    cfg_dir = config_home or default_user_config_dir()
    terms_file = cfg_dir / "sensitive_terms.txt"
    if not terms_file.is_file():
        return []
    raw = ensure_mode_0600_read(terms_file)
    terms: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            terms.append(stripped)
    return terms


def scan_text(
    text: str,
    virtual_path: str = "<memory>",
    extra_terms: list[str] | None = None,
) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    terms = extra_terms or []
    for idx, line in enumerate(text.splitlines(), start=1):
        for rule_name, pattern in SECRET_PATTERNS:
            match = pattern.search(line)
            if match:
                token = match.group(0)
                masked = token[:4] + "***" + token[-2:] if len(token) > 8 else "***"
                findings.append(ScanFinding(path=virtual_path, line_no=idx, rule=rule_name, excerpt=masked))
        for term in terms:
            if term in line:
                findings.append(
                    ScanFinding(path=virtual_path, line_no=idx, rule="custom_sensitive_term", excerpt=term[:3] + "***")
                )
    return findings


def scan_directory(
    target_dir: Path,
    *,
    include_tests: bool = False,
    extra_terms: list[str] | None = None,
) -> list[ScanFinding]:
    """Scan a directory tree for forbidden binary files and secret/confidentiality leaks."""
    if not target_dir.exists():
        raise FileNotFoundError(f"Scan target does not exist: {target_dir}")

    terms = extra_terms if extra_terms is not None else load_extra_sensitive_terms()
    findings: list[ScanFinding] = []

    files = [target_dir] if target_dir.is_file() else sorted(target_dir.rglob("*"))
    for path in files:
        rel_parts = path.relative_to(target_dir).parts if path != target_dir else (path.name,)
        if any(part in SKIP_SCAN_DIRS for part in rel_parts):
            continue
        if not include_tests and "tests" in rel_parts:
            continue
        # Scanner definition file itself embeds regex literals for blocked tokens.
        if path.name == "security.py" and "daily_radar" in rel_parts:
            continue
        if not path.is_file():
            continue

        suffix = path.suffix.lower()
        if suffix in FORBIDDEN_BINARY_SUFFIXES:
            findings.append(
                ScanFinding(
                    path=str(path),
                    line_no=0,
                    rule="forbidden_binary_asset",
                    excerpt=f"Binary file '{path.name}' violates zero-binary repository rule",
                )
            )
            continue

        if suffix in SCANNABLE_TEXT_SUFFIXES or path.name in (".gitignore",):
            content = path.read_text(encoding="utf-8", errors="replace")
            findings.extend(scan_text(content, virtual_path=str(path), extra_terms=terms))

    return findings
