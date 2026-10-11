"""Read/unread marking. `post_flags.new` is derived from `read_state`."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from edscrape.store.db import utcnow_iso


def mark(conn: sqlite3.Connection, post_keys: Iterable[str]) -> None:
    now = utcnow_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO read_state(post_key, read_at) VALUES (?, ?)",
        [(k, now) for k in post_keys],
    )
    conn.commit()


def mark_course(conn: sqlite3.Connection, course_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO read_state(post_key, read_at) "
        "SELECT key, ? FROM posts WHERE course_id = ?",
        (utcnow_iso(), course_id),
    )
    conn.commit()
