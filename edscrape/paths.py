"""Filesystem locations. `$EDSCRAPE_HOME` overrides both config and data dirs."""

from __future__ import annotations

import os
from pathlib import Path


def _home_override() -> Path | None:
    value = os.environ.get("EDSCRAPE_HOME")
    return Path(value).expanduser() if value else None


def config_dir() -> Path:
    return _home_override() or Path.home() / ".config" / "edscrape"


def data_dir() -> Path:
    return _home_override() or Path.home() / ".local" / "share" / "edscrape"


def db_path() -> Path:
    return data_dir() / "edscrape.db"


def token_path() -> Path:
    return data_dir() / "token"


def raw_dir() -> Path:
    return data_dir() / "raw"


def briefs_dir() -> Path:
    return data_dir() / "briefs"


def browser_profile_dir() -> Path:
    return data_dir() / "browser-profile"
