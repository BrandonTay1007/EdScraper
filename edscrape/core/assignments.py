"""Find the threads that belong to a named assignment (category match + keyword candidates)."""

from __future__ import annotations

import re
import sqlite3

from edscrape import config as config_mod
from edscrape.config import Config
from edscrape.models import Candidates, Thread
from edscrape.store import db

_NAME_RE = re.compile(r"^\s*(a|ass|assign|assignment|assessment)\s*0*(\d+)\s*$", re.IGNORECASE)


def keyword_patterns(name: str) -> list[re.Pattern[str]]:
    """Normalised tokens for the assignment name: `a1`, `ass1`, `assignment 1`, `assignment1`
    (and `assessment`) for names like "A1" / "Assignment 1"; otherwise the name itself."""
    m = _NAME_RE.match(name)
    if m:
        n = m.group(2)
        body = rf"(?:a|ass|assignment\s*|assessment\s*){n}"
    else:
        words = [re.escape(w) for w in name.lower().split()]
        if not words:
            return []
        body = r"\s+".join(words)
    return [re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.IGNORECASE)]


def _category_match(t: Thread, name: str) -> bool:
    n = name.strip().lower()
    return any(x.strip().lower() == n for x in (t.category, t.subcategory, t.subsubcategory))


def _saved(config: Config, course_id: int, name: str) -> dict[str, list[int]]:
    return config.assignments.get(str(course_id), {}).get(name, {})


def candidates(conn: sqlite3.Connection, config: Config, course_id: int, name: str) -> Candidates:
    threads = db.threads_by_course(conn, course_id)
    saved = _saved(config, course_id, name)
    kept = [int(i) for i in saved.get("keep", [])]
    excluded = [int(i) for i in saved.get("exclude", [])]
    auto = [t for t in threads if _category_match(t, name)]
    auto_ids = {t.id for t in auto}
    pats = keyword_patterns(name)
    bodies = {
        r["thread_id"]: r["text"] or ""
        for r in conn.execute(
            "SELECT thread_id, text FROM posts WHERE course_id = ? AND kind = 'thread'",
            (course_id,),
        )
    }
    needs = []
    for t in threads:
        if t.id in auto_ids or t.id in kept or t.id in excluded:
            continue
        hay = f"{t.title}\n{bodies.get(t.id, '')}"
        if any(p.search(hay) for p in pats):
            needs.append(t)
    return Candidates(auto=auto, needs_confirm=needs, kept=kept, excluded=excluded)


def save_choice(
    config: Config, course_id: int, name: str, keep: list[int], exclude: list[int]
) -> None:
    config.assignments.setdefault(str(course_id), {})[name] = {
        "keep": sorted({int(i) for i in keep}),
        "exclude": sorted({int(i) for i in exclude}),
    }
    config_mod.save(config)
