"""Helpers shared by the core/CLI tests (no tests here)."""

from __future__ import annotations

import hashlib
import sqlite3

from edscrape.models import Post, RoleClass, Thread
from edscrape.store import db


def add_post(
    conn: sqlite3.Connection,
    key: str,
    thread_id: int,
    course_id: int,
    role: RoleClass,
    text: str,
    *,
    parent: str | None = None,
    author: str = "Someone",
    created: str = "2026-10-09T10:00:00+10:00",
    own: bool = False,
    seen: str = "2026-10-09T10:00:00+10:00",
) -> None:
    kind = key.split(":")[0]
    xml = f"<document><paragraph>{text}</paragraph></document>"
    db.upsert_post(
        conn,
        Post(
            key=key,
            ref_id=int(key.split(":")[1]),
            thread_id=thread_id,
            course_id=course_id,
            kind=kind,
            parent_key=parent,
            number=0 if kind == "thread" else 50,
            depth=0 if kind == "thread" else 1,
            user_id=None if own else 10,
            author_name=author,
            role_class=role,
            is_anonymous=False,
            is_own=own,
            is_endorsed=False,
            is_deleted=False,
            editor_id=None,
            created_at=created,
            updated_at=None,
            content_xml=xml,
            text=text,
            markdown=text,
            content_hash=hashlib.sha256(xml.encode()).hexdigest(),
            first_seen_at=seen,
            last_seen_at=seen,
        ),
        now=seen,
    )
    conn.commit()


def add_thread(
    conn: sqlite3.Connection,
    tid: int,
    course_id: int,
    title: str,
    body: str,
    *,
    category: str = "General",
    sub: str = "",
    number: int | None = None,
    role: RoleClass = RoleClass.student,
    author: str = "Student Zed",
) -> None:
    db.upsert_thread(
        conn,
        Thread(
            id=tid,
            course_id=course_id,
            number=number or tid,
            type="question",
            title=title,
            category=category,
            subcategory=sub,
            subsubcategory="",
            accepted_id=None,
            is_pinned=False,
            is_endorsed=False,
            is_answered=False,
            is_staff_answered=False,
            is_private=False,
            reply_count=0,
            created_at="2026-10-09T10:00:00+10:00",
            updated_at="2026-10-09T10:00:00+10:00",
        ),
    )
    add_post(conn, f"thread:{tid}", tid, course_id, role, body, author=author)
    db.recompute_thread_flags(conn, tid)
    conn.commit()
