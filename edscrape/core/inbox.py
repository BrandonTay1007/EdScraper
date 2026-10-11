"""Teaching-team inbox: staff/helper posts (own posts included), newest first."""

from __future__ import annotations

import builtins
import sqlite3
from collections.abc import Iterable

from edscrape import links
from edscrape.models import Flags, InboxItem, Thread
from edscrape.store import db

EXCERPT_LEN = 200

# Flags come only from the post_flags view (project convention).
_SELECT = """
SELECT p.*, f.edited AS f_edited, f.later_staff_reply AS f_lsr, f.accepted AS f_accepted,
       f.endorsed AS f_endorsed, f.deleted AS f_deleted, f.pinned AS f_pinned,
       f.announcement AS f_announcement, f.new AS f_new,
       parent.text AS parent_text, c.code AS course_code
FROM posts p
JOIN post_flags f ON f.post_key = p.key
JOIN courses c ON c.id = p.course_id
LEFT JOIN posts parent ON parent.key = p.parent_key
"""


def post_url(course_id: int, thread_id: int, key: str) -> str:
    kind, _, ref = key.partition(":")
    if kind == "answer":
        return links.answer_url(course_id, thread_id, int(ref))
    if kind == "comment":
        return links.comment_url(course_id, thread_id, int(ref))
    return links.thread_url(course_id, thread_id)


def _excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    return " ".join(text.split())[:EXCERPT_LEN]


def _fetch(conn: sqlite3.Connection, where: str, params: builtins.list) -> builtins.list[InboxItem]:
    rows = conn.execute(
        f"{_SELECT} WHERE {where} ORDER BY julianday(p.created_at) DESC, p.key", params
    ).fetchall()
    threads: dict[int, Thread] = {}
    items: builtins.list[InboxItem] = []
    for r in rows:
        post = db._post(r)
        if post.thread_id not in threads:
            t = db.thread_by_id(conn, post.thread_id)
            if t is None:
                continue
            threads[post.thread_id] = t
        flags = Flags(
            edited=bool(r["f_edited"]),
            later_staff_reply=bool(r["f_lsr"]),
            accepted=bool(r["f_accepted"]),
            endorsed=bool(r["f_endorsed"]),
            deleted=bool(r["f_deleted"]),
            pinned=bool(r["f_pinned"]),
            announcement=bool(r["f_announcement"]),
            new=bool(r["f_new"]),
        )
        items.append(
            InboxItem(
                post=post,
                thread=threads[post.thread_id],
                flags=flags,
                parent_excerpt=_excerpt(r["parent_text"]) if post.parent_key else None,
                url=post_url(post.course_id, post.thread_id, post.key),
                course_code=(r["course_code"] or "").strip(),
            )
        )
    return items


def list(  # noqa: A001 - contract name
    conn: sqlite3.Connection,
    course_ids: Iterable[int] | None = None,
    unread_only: bool = True,
) -> builtins.list[InboxItem]:
    where = ["p.role_class IN ('staff', 'helper')", "p.is_deleted = 0"]
    params: builtins.list = []
    if course_ids is not None:
        ids = [*course_ids]
        if not ids:
            return []
        where.append(f"p.course_id IN ({','.join('?' * len(ids))})")
        params += ids
    if unread_only:
        where.append("f.new = 1")
    return _fetch(conn, " AND ".join(where), params)


def items_for_keys(conn: sqlite3.Connection, keys: Iterable[str]) -> builtins.list[InboxItem]:
    ks = [*keys]
    if not ks:
        return []
    return _fetch(conn, f"p.key IN ({','.join('?' * len(ks))})", ks)
