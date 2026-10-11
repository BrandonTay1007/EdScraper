from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

import pytest


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EDSCRAPE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    schema = resources.files("edscrape.store").joinpath("schema.sql").read_text(encoding="utf-8")
    c.executescript(schema)
    yield c
    c.close()
