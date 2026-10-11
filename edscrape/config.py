"""config.toml load/save. Frozen contract: key names and defaults follow plan.md section 3."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

from edscrape import paths


def _default_roles() -> dict[str, str]:
    return {
        "admin": "staff",
        "staff": "staff",
        "tutor": "staff",
        "mentor": "helper",
        "student": "student",
    }


@dataclass(slots=True)
class Config:
    unit_ids: list[int] = field(default_factory=list)
    concurrency: int = 4
    rate_per_sec: int = 8
    full_recheck: bool = True
    recheck_days: int = 14
    first_sync_read_before_days: int = 0
    roles: dict[str, str] = field(default_factory=_default_roles)
    brief_model: str = "sonnet"
    # assignments[str(course_id)][name] = {"keep": [...], "exclude": [...]}
    assignments: dict[str, dict[str, dict[str, list[int]]]] = field(default_factory=dict)


def default_path() -> Path:
    return paths.config_dir() / "config.toml"


def load(path: Path | None = None) -> Config:
    p = Path(path) if path else default_path()
    if not p.exists():
        return Config()
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    cfg = Config()
    cfg.unit_ids = [int(i) for i in data.get("units", {}).get("ids", cfg.unit_ids)]
    sync = data.get("sync", {})
    cfg.concurrency = int(sync.get("concurrency", cfg.concurrency))
    cfg.rate_per_sec = int(sync.get("rate_per_sec", cfg.rate_per_sec))
    cfg.full_recheck = bool(sync.get("full_recheck", cfg.full_recheck))
    cfg.recheck_days = int(sync.get("recheck_days", cfg.recheck_days))
    cfg.first_sync_read_before_days = int(
        sync.get("first_sync_read_before_days", cfg.first_sync_read_before_days)
    )
    if "roles" in data:
        cfg.roles = {str(k): str(v) for k, v in data["roles"].items()}
    cfg.brief_model = str(data.get("brief", {}).get("model", cfg.brief_model))
    cfg.assignments = {
        str(cid): {
            str(name): {
                "keep": [int(i) for i in choice.get("keep", [])],
                "exclude": [int(i) for i in choice.get("exclude", [])],
            }
            for name, choice in names.items()
        }
        for cid, names in data.get("assignments", {}).items()
    }
    return cfg


def save(config: Config, path: Path | None = None) -> None:
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "units": {"ids": list(config.unit_ids)},
        "sync": {
            "concurrency": config.concurrency,
            "rate_per_sec": config.rate_per_sec,
            "full_recheck": config.full_recheck,
            "recheck_days": config.recheck_days,
            "first_sync_read_before_days": config.first_sync_read_before_days,
        },
        "roles": dict(config.roles),
        "brief": {"model": config.brief_model},
        "assignments": config.assignments,
    }
    p.write_text(tomli_w.dumps(data), encoding="utf-8")
