"""Export a course's threads and posts as Markdown, CSV or JSON Lines."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from edscrape.core.inbox import post_url
from edscrape.models import Post
from edscrape.store import db

FORMATS = ("md", "csv", "jsonl")
_FLAG_COLS = ("edited", "later_staff_reply", "accepted", "endorsed", "deleted")


class ExportError(Exception):
    pass


def _flags(conn: sqlite3.Connection, course_id: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT f.* FROM post_flags f JOIN posts p ON p.key = f.post_key WHERE p.course_id = ?",
        (course_id,),
    ).fetchall()
    return {r["post_key"]: {c: bool(r[c]) for c in _FLAG_COLS} for r in rows}


def _author(p: Post) -> str:
    return "Anonymous" if p.is_anonymous else (p.author_name or "Anonymous")


def _records(conn: sqlite3.Connection, course_id: int) -> list[tuple[dict, str]]:
    """Flat records (thread order, posts in tree order) plus each record's markdown body."""
    flags = _flags(conn, course_id)
    out: list[tuple[dict, str]] = []
    for t in db.threads_by_course(conn, course_id):
        for p in db.posts_by_thread(conn, t.id):
            rec = {
                "post_key": p.key,
                "thread_id": t.id,
                "thread_title": t.title,
                "category": " / ".join(filter(None, (t.category, t.subcategory, t.subsubcategory))),
                "kind": p.kind,
                "depth": p.depth,
                "author": _author(p),
                "role": p.role_class.value,
                "is_own": p.is_own,
                "created_at": p.created_at,
                "updated_at": p.updated_at,
                **flags.get(p.key, dict.fromkeys(_FLAG_COLS, False)),
                "url": post_url(course_id, t.id, p.key),
                "text": p.text,
            }
            out.append((rec, p.markdown))
    return out


def write(conn: sqlite3.Connection, course_id: int, fmt: str, path: str | Path) -> None:
    if fmt not in FORMATS:
        raise ExportError(f"unknown format {fmt!r}; use one of {', '.join(FORMATS)}")
    recs = _records(conn, course_id)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jsonl":
        with path.open("w", encoding="utf-8") as fh:
            for rec, _ in recs:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    elif fmt == "csv":
        cols = list(recs[0][0]) if recs else ["post_key"]
        with path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for rec, _ in recs:
                w.writerow(rec)
    else:
        path.write_text(_markdown(conn, course_id, recs), encoding="utf-8")


def _markdown(conn: sqlite3.Connection, course_id: int, recs: list[tuple[dict, str]]) -> str:
    course = db.course_by_id(conn, course_id)
    lines = [f"# {(course.code if course else str(course_id)).strip()} export", ""]
    current = None
    for rec, body in recs:
        if rec["thread_id"] != current:
            current = rec["thread_id"]
            lines += [f"## {rec['thread_title']}", ""]
            if rec["category"]:
                lines += [f"Category: {rec['category']}", ""]
        marks = [c.replace("_", " ") for c in _FLAG_COLS if rec[c]]
        meta = f"{rec['author']} ({rec['role']}) - {rec['created_at']}"
        if marks:
            meta += f" - {', '.join(marks)}"
        indent = "#" * min(3 + rec["depth"], 6)
        lines += [f"{indent} {rec['kind']} {rec['post_key']}", "", f"*{meta}*", ""]
        lines += [body.strip() or "_(deleted)_", "", f"[Open in Ed]({rec['url']})", ""]
    return "\n".join(lines)
