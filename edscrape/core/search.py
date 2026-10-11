"""Full-text search over posts_fts."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable

from edscrape.core.inbox import post_url
from edscrape.models import SearchHit
from edscrape.store import db

_TOKEN = re.compile(r"\w+", re.UNICODE)


def fts_query(q: str) -> str:
    """Turn free text into a safe FTS5 expression: every word quoted, implicit AND.
    Operators, quotes, `*`, `:` and other syntax in the input are dropped, never interpreted."""
    return " ".join(f'"{t}"' for t in _TOKEN.findall(q))


def query(
    conn: sqlite3.Connection,
    q: str,
    course_ids: Iterable[int] | None = None,
    limit: int = 50,
) -> list[SearchHit]:
    expr = fts_query(q)
    if not expr:
        return []
    sql = (
        "SELECT p.key, p.thread_id, p.course_id, "
        "snippet(posts_fts, -1, '[', ']', '...', 16) AS snip, bm25(posts_fts) AS rank "
        "FROM posts_fts JOIN posts p ON p.rowid = posts_fts.rowid "
        "WHERE posts_fts MATCH ?"
    )
    params: list = [expr]
    if course_ids is not None:
        ids = [*course_ids]
        if not ids:
            return []
        sql += f" AND p.course_id IN ({','.join('?' * len(ids))})"
        params += ids
    sql += " ORDER BY rank, p.key LIMIT ?"
    params.append(limit)
    hits: list[SearchHit] = []
    for r in conn.execute(sql, params).fetchall():
        thread = db.thread_by_id(conn, r["thread_id"])
        if thread is None:
            continue
        hits.append(
            SearchHit(
                thread=thread,
                post_key=r["key"],
                snippet=r["snip"],
                url=post_url(r["course_id"], r["thread_id"], r["key"]),
                rank=float(r["rank"]),
            )
        )
    return hits
