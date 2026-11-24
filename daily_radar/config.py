"""Configuration loading and strict 0600 credential management."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import Any

import yaml


WEEKDAYS_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def default_user_config_dir() -> Path:
    override = os.environ.get("DAILY_RADAR_CONFIG_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".config")
    return base / "daily-radar"


def ensure_mode_0600_read(path: Path) -> str:
    """Read a credential file and fail fast if group/other bits are set."""
    if not path.is_file():
        raise FileNotFoundError(f"Credential file not found: {path}")
    st = path.stat()
    mode = stat.S_IMODE(st.st_mode)
    if (mode & 0o077) != 0:
        raise PermissionError(
            f"Credential file {path} has unsafe permissions {oct(mode)}; expected 0o600 (chmod 600 {path})."
        )
    return path.read_text(encoding="utf-8")


def write_mode_0600_atomic(path: Path, content: str) -> None:
    """Create parent dir with 0700 and write file atomically with 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def init_user_config_stubs(config_home: Path | None = None) -> dict[str, Path]:
    """Initialize 0600 credential stub files without overwriting existing user secrets."""
    root = config_home or default_user_config_dir()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass

    webhook_path = root / "webhook.json"
    if not webhook_path.exists():
        write_mode_0600_atomic(
            webhook_path,
            json.dumps(
                {
                    "webhook_url": "",
                    "secret": "",
                    "note": "Paste your dedicated Feishu group custom bot webhook_url and HMAC secret here (mode 0600).",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    llm_path = root / "llm.json"
    if not llm_path.exists():
        write_mode_0600_atomic(
            llm_path,
            json.dumps(
                {
                    "primary": {
                        "name": "primary-openai-compatible",
                        "base_url": "",
                        "api_key": "",
                        "screen_model": "gpt-4o-mini",
                        "distill_model": "gpt-4.1",
                        "timeout_sec": 60,
                    },
                    "backup": {
                        "name": "backup-openai-compatible",
                        "base_url": "",
                        "api_key": "",
                        "screen_model": "deepseek-chat",
                        "distill_model": "deepseek-chat",
                        "timeout_sec": 60,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    terms_path = root / "sensitive_terms.txt"
    if not terms_path.exists():
        write_mode_0600_atomic(
            terms_path,
            "# Extra local-only sensitive strings (one per line) blocked from public HTML/JSON\n",
        )

    return {
        "config_home": root,
        "webhook": webhook_path,
        "llm": llm_path,
        "sensitive_terms": terms_path,
    }


@dataclass
class RepoConfig:
    repo_root: Path
    profile: dict[str, Any]
    sources: dict[str, Any]
    conferences: dict[str, Any]

    @classmethod
    def load(cls, repo_root: Path, config_dir: Path | None = None) -> RepoConfig:
        cfg = config_dir or (repo_root / "config")
        profile = yaml.safe_load((cfg / "profile.yaml").read_text(encoding="utf-8"))
        sources = yaml.safe_load((cfg / "sources.yaml").read_text(encoding="utf-8"))
        conferences = json.loads((cfg / "conferences.json").read_text(encoding="utf-8"))
        assert isinstance(profile, dict) and "tracks" in profile, "Invalid profile.yaml"
        assert isinstance(sources, dict) and "sources" in sources, "Invalid sources.yaml"
        assert isinstance(conferences, dict) and "conferences" in conferences, "Invalid conferences.json"
        return cls(repo_root=repo_root, profile=profile, sources=sources, conferences=conferences)
