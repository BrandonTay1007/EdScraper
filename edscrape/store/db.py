"""SQLite connection, migration and repositories.

Rules callers must respect:
- Upsert the `threads` row BEFORE its `thread:<id>` post (the FTS trigger reads the title).
- Flags are read through the `post_flags` view; `later_staff_reply` / `accepted` are stored columns
  recomputed by `recompute_thread_flags`.
- Functions never commit; the caller decides transaction boundaries (sync commits per thread).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from edscrape import paths
from edscrape.models import Course, Post, RoleClass, Thread

SCHEMA_VERSION = "1"


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# -- connection / migration ----------------------------------------------------------------------


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the store (default `paths.db_path()`), WAL, Row factory, foreign keys on.

    `check_same_thread=False` so TUI workers can share one connection (calls are not concurrent).
    """
    target = str(path) if path is not None else str(paths.db_path())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply `schema.sql` (idempotent) and record `meta.schema_version`."""
    schema = resources.files("edscrape.store").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    set_meta(conn, "schema_version", SCHEMA_VERSION)
    conn.commit()


# -- meta ----------------------------------------------------------------------------------------


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# -- courses -------------------------------------------------------------------------------------


def upsert_course(conn: sqlite3.Connection, course: Course) -> None:
    """Insert/update a course. `selected` and `last_synced_at` of an existing row are kept."""
    conn.execute(
        "INSERT INTO courses(id, code, name, year, session, status, role, categories_json, "
        "selected, last_synced_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET code=excluded.code, name=excluded.name, "
        "year=excluded.year, session=excluded.session, status=excluded.status, "
        "role=excluded.role, categories_json=excluded.categories_json",
        (
            course.id,
            course.code.strip(),
            course.name,
            course.year,
            course.session,
            course.status,
            course.role,
            json.dumps(course.categories),
            int(course.selected),
            course.last_synced_at,
        ),
    )


def set_course_selected(conn: sqlite3.Connection, course_id: int, selected: bool) -> None:
    conn.execute("UPDATE courses SET selected = ? WHERE id = ?", (int(selected), course_id))


def _course(row: sqlite3.Row) -> Course:
    return Course(
        id=row["id"],
        code=row["code"] or "",
        name=row["name"] or "",
        year=row["year"] or "",
        session=row["session"] or "",
        status=row["status"] or "",
        role=row["role"] or "",
        categories=json.loads(row["categories_json"] or "[]"),
        selected=bool(row["selected"]),
        last_synced_at=row["last_synced_at"],
    )


def course_by_id(conn: sqlite3.Connection, course_id: int) -> Course | None:
    row = conn.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    return _course(row) if row else None


def courses(conn: sqlite3.Connection) -> list[Course]:
    rows = conn.execute("SELECT * FROM courses ORDER BY code, id").fetchall()
    return [_course(r) for r in rows]


# -- users / roles -------------------------------------------------------------------------------


def upsert_user(conn: sqlite3.Connection, user_id: int, name: str | None) -> None:
    conn.execute(
        "INSERT INTO users(id, name) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = COALESCE(excluded.name, users.name)",
        (user_id, name),
    )


def upsert_user_role(
    conn: sqlite3.Connection, user_id: int, course_id: int, course_role: str | None
) -> None:
    """Store a course role. A null role never overwrites a stored one (user left the course)."""
    if course_role is None:
        return
    conn.execute(
        "INSERT INTO user_course_roles(user_id, course_id, course_role) VALUES (?,?,?) "
        "ON CONFLICT(user_id, course_id) DO UPDATE SET course_role = excluded.course_role",
        (user_id, course_id, course_role),
    )


def role_for(conn: sqlite3.Connection, user_id: int, course_id: int) -> str | None:
    row = conn.execute(
        "SELECT course_role FROM user_course_roles WHERE user_id = ? AND course_id = ?",
        (user_id, course_id),
    ).fetchone()
    return row["course_role"] if row else None


# -- threads -------------------------------------------------------------------------------------


def upsert_thread(
    conn: sqlite3.Connection,
    thread: Thread,
    *,
    editor_id: int | None = None,
    list_body_hash: str | None = None,
    change_key: str | None = None,
) -> None:
    """Insert/update a thread row. `change_key=None` leaves a stored change_key untouched (use
    `set_change_key` once the detail fetch succeeded)."""
    conn.execute(
        "INSERT INTO threads(id, course_id, number, type, title, category, subcategory, "
        "subsubcategory, accepted_id, editor_id, is_pinned, is_endorsed, is_answered, "
        "is_staff_answered, is_private, reply_count, created_at, updated_at, list_body_hash, "
        "change_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET course_id=excluded.course_id, number=excluded.number, "
        "type=excluded.type, title=excluded.title, category=excluded.category, "
        "subcategory=excluded.subcategory, subsubcategory=excluded.subsubcategory, "
        "accepted_id=excluded.accepted_id, editor_id=excluded.editor_id, "
        "is_pinned=excluded.is_pinned, is_endorsed=excluded.is_endorsed, "
        "is_answered=excluded.is_answered, is_staff_answered=excluded.is_staff_answered, "
        "is_private=excluded.is_private, reply_count=excluded.reply_count, "
        "created_at=excluded.created_at, updated_at=excluded.updated_at, "
        "list_body_hash=COALESCE(excluded.list_body_hash, threads.list_body_hash), "
        "change_key=COALESCE(excluded.change_key, threads.change_key)",
        (
            thread.id,
            thread.course_id,
            thread.number,
            thread.type,
            thread.title,
            thread.category,
            thread.subcategory,
            thread.subsubcategory,
            thread.accepted_id,
            editor_id,
            int(thread.is_pinned),
            int(thread.is_endorsed),
            int(thread.is_answered),
            int(thread.is_staff_answered),
            int(thread.is_private),
            thread.reply_count,
            thread.created_at,
            thread.updated_at,
            list_body_hash,
            change_key,
        ),
    )


def set_change_key(conn: sqlite3.Connection, thread_id: int, change_key: str) -> None:
    conn.execute("UPDATE threads SET change_key = ? WHERE id = ?", (change_key, thread_id))


def stored_change_keys(conn: sqlite3.Connection, course_id: int) -> dict[int, str | None]:
    rows = conn.execute("SELECT id, change_key FROM threads WHERE course_id = ?", (course_id,))
    return {r["id"]: r["change_key"] for r in rows}


def _thread(row: sqlite3.Row) -> Thread:
    return Thread(
        id=row["id"],
        course_id=row["course_id"],
        number=row["number"] or 0,
        type=row["type"] or "",
        title=row["title"] or "",
        category=row["category"] or "",
        subcategory=row["subcategory"] or "",
        subsubcategory=row["subsubcategory"] or "",
        accepted_id=row["accepted_id"],
        is_pinned=bool(row["is_pinned"]),
        is_endorsed=bool(row["is_endorsed"]),
        is_answered=bool(row["is_answered"]),
        is_staff_answered=bool(row["is_staff_answered"]),
        is_private=bool(row["is_private"]),
        reply_count=row["reply_count"],
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
    )


def thread_by_id(conn: sqlite3.Connection, thread_id: int) -> Thread | None:
    row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    return _thread(row) if row else None


def threads_by_course(conn: sqlite3.Connection, course_id: int) -> list[Thread]:
    """Pinned first, then newest created first (the order Ed shows)."""
    rows = conn.execute(
        "SELECT * FROM threads WHERE course_id = ? "
        "ORDER BY is_pinned DESC, julianday(created_at) DESC, id DESC",
        (course_id,),
    ).fetchall()
    return [_thread(r) for r in rows]


# -- posts ---------------------------------------------------------------------------------------


def post_from_row(row: sqlite3.Row) -> Post:
    return Post(
        key=row["key"],
        ref_id=row["ref_id"],
        thread_id=row["thread_id"],
        course_id=row["course_id"],
        kind=row["kind"],
        parent_key=row["parent_key"],
        number=row["number"] or 0,
        depth=row["depth"],
        user_id=row["user_id"],
        author_name=row["author_name"] or "",
        role_class=RoleClass(row["role_class"] or "unknown"),
        is_anonymous=bool(row["is_anonymous"]),
        is_own=bool(row["is_own"]),
        is_endorsed=bool(row["is_endorsed"]),
        is_deleted=bool(row["is_deleted"]),
        editor_id=row["editor_id"],
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"],
        content_xml=row["content_xml"] or "",
        text=row["text"] or "",
        markdown=row["markdown"] or "",
        content_hash=row["content_hash"] or "",
        first_seen_at=row["first_seen_at"] or "",
        last_seen_at=row["last_seen_at"] or "",
    )


def upsert_post(conn: sqlite3.Connection, post: Post, *, now: str | None = None) -> str:
    """Insert/update a post. Returns "new", "changed" (content_hash differs) or "same".

    `first_seen_at` is set once; `last_seen_at` is always `now`. A `post_versions` row is written
    for the first body and for every changed body. `later_staff_reply` / `accepted` are not
    touched (see `recompute_thread_flags`).
    """
    now = now or utcnow_iso()
    existing = conn.execute(
        "SELECT content_hash, first_seen_at FROM posts WHERE key = ?", (post.key,)
    ).fetchone()
    values = (
        post.ref_id,
        post.thread_id,
        post.course_id,
        post.kind,
        post.parent_key,
        post.number,
        post.depth,
        post.user_id,
        post.author_name,
        post.role_class.value,
        int(post.is_anonymous),
        int(post.is_own),
        int(post.is_endorsed),
        int(post.is_deleted),
        post.editor_id,
        post.created_at,
        post.updated_at,
        post.content_xml,
        post.text,
        post.markdown,
        post.content_hash,
    )
    if existing is None:
        conn.execute(
            "INSERT INTO posts(ref_id, thread_id, course_id, kind, parent_key, number, depth, "
            "user_id, author_name, role_class, is_anonymous, is_own, is_endorsed, is_deleted, "
            "editor_id, created_at, updated_at, content_xml, text, markdown, content_hash, "
            "key, first_seen_at, last_seen_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*values, post.key, post.first_seen_at or now, now),
        )
        status = "new"
    else:
        conn.execute(
            "UPDATE posts SET ref_id=?, thread_id=?, course_id=?, kind=?, parent_key=?, "
            "number=?, depth=?, user_id=?, author_name=?, role_class=?, is_anonymous=?, "
            "is_own=?, is_endorsed=?, is_deleted=?, editor_id=?, created_at=?, updated_at=?, "
            "content_xml=?, text=?, markdown=?, content_hash=?, last_seen_at=? WHERE key=?",
            (*values, now, post.key),
        )
        status = "changed" if existing["content_hash"] != post.content_hash else "same"
    if status != "same":
        conn.execute(
            "INSERT OR IGNORE INTO post_versions(post_key, seen_at, content_hash, content_xml) "
            "VALUES (?,?,?,?)",
            (post.key, now, post.content_hash, post.content_xml),
        )
    return status


def mark_tombstone(conn: sqlite3.Connection, post_key: str, *, now: str | None = None) -> None:
    """Flag an existing post deleted, keeping its previous body, author and timestamps."""
    conn.execute(
        "UPDATE posts SET is_deleted = 1, last_seen_at = ? WHERE key = ?",
        (now or utcnow_iso(), post_key),
    )


def post_by_key(conn: sqlite3.Connection, key: str) -> Post | None:
    row = conn.execute("SELECT * FROM posts WHERE key = ?", (key,)).fetchone()
    return post_from_row(row) if row else None


def posts_by_thread(conn: sqlite3.Connection, thread_id: int) -> list[Post]:
    """Ordered tree: the thread post, then children depth-first, siblings by `number`."""
    rows = conn.execute("SELECT * FROM posts WHERE thread_id = ?", (thread_id,)).fetchall()
    posts = [post_from_row(r) for r in rows]
    children: dict[str | None, list[Post]] = {}
    keys = {p.key for p in posts}
    for p in posts:
        parent = p.parent_key if p.parent_key in keys else None
        children.setdefault(parent, []).append(p)
    for group in children.values():
        group.sort(key=lambda p: (p.kind != "thread", p.number, p.ref_id))
    ordered: list[Post] = []

    def walk(parent: str | None) -> None:
        for p in children.get(parent, []):
            ordered.append(p)
            walk(p.key)

    walk(None)
    return ordered


def recompute_thread_flags(conn: sqlite3.Connection, thread_id: int) -> None:
    """Recompute stored `later_staff_reply` and `accepted` for every post of a thread.

    later_staff_reply(P): P is not staff-class AND a non-deleted staff-class post in the same
    thread has created_at later than P's. accepted(P): P is the thread's `accepted_id` answer.
    """
    conn.execute(
        "UPDATE posts SET later_staff_reply = CASE WHEN role_class <> 'staff' AND EXISTS ("
        "  SELECT 1 FROM posts s WHERE s.thread_id = posts.thread_id AND s.role_class = 'staff' "
        "  AND s.is_deleted = 0 AND julianday(s.created_at) > julianday(posts.created_at)"
        ") THEN 1 ELSE 0 END WHERE thread_id = ?",
        (thread_id,),
    )
    conn.execute(
        "UPDATE posts SET accepted = CASE WHEN kind = 'answer' AND ref_id = ("
        "  SELECT accepted_id FROM threads WHERE id = posts.thread_id) THEN 1 ELSE 0 END "
        "WHERE thread_id = ?",
        (thread_id,),
    )


# -- sync runs / read state ----------------------------------------------------------------------


def insert_sync_run(conn: sqlite3.Connection, course_id: int, started_at: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO sync_runs(course_id, started_at, status) VALUES (?, ?, 'running')",
        (course_id, started_at or utcnow_iso()),
    )
    return int(cur.lastrowid)


def finish_sync_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    threads_seen: int = 0,
    threads_changed: int = 0,
    posts_new: int = 0,
    posts_changed: int = 0,
    error: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    finished = finished_at or utcnow_iso()
    conn.execute(
        "UPDATE sync_runs SET finished_at=?, status=?, threads_seen=?, threads_changed=?, "
        "posts_new=?, posts_changed=?, error=? WHERE id=?",
        (finished, status, threads_seen, threads_changed, posts_new, posts_changed, error, run_id),
    )
    row = conn.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
    if status in ("ok", "partial"):
        conn.execute(
            "UPDATE courses SET last_synced_at = ? WHERE id = ?", (finished, row["course_id"])
        )
    return dict(row)


def has_prior_sync(conn: sqlite3.Connection, course_id: int) -> bool:
    """True when a previous sync of this course finished with status ok or partial."""
    row = conn.execute(
        "SELECT 1 FROM sync_runs WHERE course_id = ? AND status IN ('ok','partial') LIMIT 1",
        (course_id,),
    ).fetchone()
    return row is not None


def mark_read_before(
    conn: sqlite3.Connection, course_id: int, before_iso: str, *, read_at: str | None = None
) -> int:
    """Insert read_state for every post of the course created before `before_iso`."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO read_state(post_key, read_at) "
        "SELECT key, ? FROM posts WHERE course_id = ? AND is_deleted = 0 "
        "AND julianday(created_at) < julianday(?)",
        (read_at or utcnow_iso(), course_id, before_iso),
    )
    return cur.rowcount
