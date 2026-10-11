"""Read-side helpers the TUI needs that core/ does not provide. Flags only via `post_flags`."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta

from edscrape.config import Config
from edscrape.core import inbox
from edscrape.models import Course, Flags

ASSIGNMENT_CATEGORY = re.compile(r"assign|assess", re.IGNORECASE)
GLYPH_EDITED, GLYPH_LATER, GLYPH_ENDORSED = "✎", "⚠", "★"
GLYPH_PINNED, GLYPH_ANNOUNCE, GLYPH_ACCEPTED = "📌", "📣", "✔"


def unit_label(code: str) -> str:
    parts = code.split()
    return parts[0] if parts else code


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def age(s: str | None, now: datetime | None = None) -> str:
    dt = parse_ts(s)
    if dt is None or dt.year < 1900:
        return ""
    secs = max(0, int(((now or datetime.now(UTC)) - dt).total_seconds()))
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return "now"


def excerpt(text: str, limit: int = 80) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def flag_glyphs(f: Flags, *, thread_level: bool = True, accepted: bool = False) -> str:
    out = []
    if f.edited:
        out.append(GLYPH_EDITED)
    if f.later_staff_reply:
        out.append(GLYPH_LATER)
    if f.endorsed:
        out.append(GLYPH_ENDORSED)
    if accepted and f.accepted:
        out.append(GLYPH_ACCEPTED)
    if thread_level and f.pinned:
        out.append(GLYPH_PINNED)
    if thread_level and f.announcement:
        out.append(GLYPH_ANNOUNCE)
    return " ".join(out)


def thread_flags(conn: sqlite3.Connection, thread_id: int) -> dict[str, Flags]:
    rows = conn.execute(
        "SELECT f.* FROM post_flags f JOIN posts p ON p.key = f.post_key WHERE p.thread_id = ?",
        (thread_id,),
    ).fetchall()
    return {
        r["post_key"]: Flags(
            edited=bool(r["edited"]),
            later_staff_reply=bool(r["later_staff_reply"]),
            accepted=bool(r["accepted"]),
            endorsed=bool(r["endorsed"]),
            deleted=bool(r["deleted"]),
            pinned=bool(r["pinned"]),
            announcement=bool(r["announcement"]),
            new=bool(r["new"]),
        )
        for r in rows
    }


def assignment_names(course: Course, config: Config | None = None) -> list[str]:
    """Names under an 'Assignments'-like category, plus any names with saved choices."""
    names: list[str] = []

    def walk(nodes: list[dict]) -> None:
        for n in nodes:
            name = str(n.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
            walk(n.get("subcategories") or [])

    for cat in course.categories or []:
        if ASSIGNMENT_CATEGORY.search(str(cat.get("name") or "")):
            walk(cat.get("subcategories") or [])
    if config is not None:
        for saved in config.assignments.get(str(course.id), {}):
            if saved not in names:
                names.append(saved)
    return names


def unit_stats(
    conn: sqlite3.Connection, course: Course, now: datetime | None = None
) -> dict[str, int]:
    """Dashboard numbers for one unit."""
    now = now or datetime.now(UTC)
    unread = len(inbox.list(conn, [course.id], unread_only=True))
    later = conn.execute(
        "SELECT COUNT(DISTINCT p.thread_id) FROM posts p JOIN post_flags f ON f.post_key = p.key"
        " WHERE p.course_id = ? AND f.later_staff_reply = 1 AND f.new = 1",
        (course.id,),
    ).fetchone()[0]
    cutoff = now - timedelta(days=7)
    recent = {
        (t["category"], t["subcategory"], t["subsubcategory"])
        for t in conn.execute(
            "SELECT category, subcategory, subsubcategory, updated_at FROM threads"
            " WHERE course_id = ?",
            (course.id,),
        )
        if (dt := parse_ts(t["updated_at"])) is not None and dt >= cutoff
    }
    active = 0
    for name in assignment_names(course):
        key = name.lower()
        if any(key in {(x or "").strip().lower() for x in trio} for trio in recent):
            active += 1
    return {"unread": unread, "later": later, "assignments": active}
