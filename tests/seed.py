"""Deterministic store seed shared by test lanes (A, C, D).

`seed_store(conn, scenario="basic")` fills an already-migrated connection (the `conn` fixture).
It returns a dict of the ids/keys created so tests need no magic numbers. No clock is read: every
timestamp below is fixed, so output is identical on every run.

Scenario "basic" (viewer id VIEWER_ID = 9000; all times 2026, +10:00):

Course 1001 "FIT1001 S2 2026" (viewer is a student), roles: 10 staff, 20 student, 30 helper
  thread 101 announcement, pinned, staff 10, body only               posts: thread:101
  thread 102 question by student 20 (A1 clarification)              posts: thread:102,
      answer:1021 (staff 10, accepted),
      comment:1022 (student 20 follow-up on the answer),
      comment:1023 (helper 30 under comment:1022),
      comment:1024 (staff 10 under comment:1022, later than it -> comment:1022 and the thread
      post have later_staff_reply=1)
  thread 103 question by an anonymous author (user_id NULL, "Anonymous")
      posts: thread:103, comment:1031 (staff 10)
  thread 104 question by the viewer (is_own=1)
      posts: thread:104, answer:1041 (staff 10, EDITED: editor_id set + 2 post_versions rows)
Course 1002 "FIT2002 S2 2026" (viewer is staff), roles: 11 staff, 21 student, 40 unknown (role NULL)
  thread 201 question by student 21; posts: thread:201, comment:2011 TOMBSTONE (is_deleted=1)
      with child comment:2012 (staff 11)
  thread 202 pinned post by unknown-role user 40; posts: thread:202

read_state: marked read for thread:101, thread:102, comment:1022, answer:1041, thread:201,
comment:2012 (about half of the 16 posts); the rest are unread.

The returned dict maps names to keys: {"viewer_id", "courses": [1001, 1002], "threads": [...],
"posts": [...all keys...], "read": [...], "unread": [...], "edited": key, "tombstone": key,
"anonymous": key, "own": key, "later_staff_reply": [keys]}.
"""

from __future__ import annotations

import hashlib
import sqlite3

from edscrape.models import Course, Post, RoleClass, Thread
from edscrape.store import db

VIEWER_ID = 9000
SEEN = "2026-10-11T08:00:00+10:00"


def _hash(xml: str) -> str:
    return hashlib.sha256(xml.encode("utf-8")).hexdigest()


def _xml(text: str) -> str:
    return f'<document version="2.0"><paragraph>{text}</paragraph></document>'


def seed_store(conn: sqlite3.Connection, scenario: str = "basic") -> dict:
    if scenario != "basic":
        raise ValueError(f"unknown scenario {scenario!r}")
    db.set_meta(conn, "viewer_user_id", str(VIEWER_ID))

    def course(cid: int, code: str, role: str, selected: bool) -> None:
        db.upsert_course(
            conn,
            Course(
                id=cid,
                code=code,
                name=f"Unit {code}",
                year="2026",
                session="Semester 2",
                status="active",
                role=role,
                categories=[{"name": "Assignments", "subcategories": [{"name": "A1"}]}],
                selected=selected,
                last_synced_at=SEEN,
            ),
        )

    course(1001, "FIT1001 S2 2026", "student", True)
    course(1002, "FIT2002 S2 2026", "staff", True)

    users = {
        10: "Staff Ten",
        20: "Student Twenty",
        30: "Helper Thirty",
        11: "Staff Eleven",
        21: "Student TwentyOne",
        40: "Mystery Forty",
        VIEWER_ID: "Viewer Me",
    }
    for uid, name in users.items():
        db.upsert_user(conn, uid, name)
    for uid, cid, role in [
        (10, 1001, "staff"),
        (20, 1001, "student"),
        (30, 1001, "mentor"),
        (VIEWER_ID, 1001, "student"),
        (11, 1002, "staff"),
        (21, 1002, "student"),
        (VIEWER_ID, 1002, "staff"),
    ]:
        db.upsert_user_role(conn, uid, cid, role)

    def thread(
        tid,
        cid,
        number,
        type_,
        title,
        created,
        updated,
        *,
        pinned=False,
        replies=0,
        accepted=None,
        staff_answered=False,
        category="Assignments",
        sub="A1",
    ) -> None:
        db.upsert_thread(
            conn,
            Thread(
                id=tid,
                course_id=cid,
                number=number,
                type=type_,
                title=title,
                category=category,
                subcategory=sub,
                subsubcategory="",
                accepted_id=accepted,
                is_pinned=pinned,
                is_endorsed=False,
                is_answered=accepted is not None,
                is_staff_answered=staff_answered,
                is_private=False,
                reply_count=replies,
                created_at=created,
                updated_at=updated,
            ),
        )

    def post(
        key,
        tid,
        cid,
        kind,
        parent,
        number,
        depth,
        uid,
        name,
        role,
        created,
        text,
        *,
        anonymous=False,
        own=False,
        deleted=False,
        editor=None,
        endorsed=False,
        updated=None,
        versions=None,
    ) -> None:
        xml = "" if deleted else _xml(text)
        db.upsert_post(
            conn,
            Post(
                key=key,
                ref_id=int(key.split(":")[1]),
                thread_id=tid,
                course_id=cid,
                kind=kind,
                parent_key=parent,
                number=number,
                depth=depth,
                user_id=uid,
                author_name=name,
                role_class=role,
                is_anonymous=anonymous,
                is_own=own,
                is_endorsed=endorsed,
                is_deleted=deleted,
                editor_id=editor,
                created_at=created,
                updated_at=updated,
                content_xml=xml,
                text="" if deleted else text,
                markdown="" if deleted else text,
                content_hash=_hash(xml),
                first_seen_at=SEEN,
                last_seen_at=SEEN,
            ),
            now=SEEN,
        )
        for old in versions or []:  # earlier bodies of an edited post
            old_xml = _xml(old)
            conn.execute(
                "INSERT OR IGNORE INTO post_versions(post_key, seen_at, content_hash, content_xml)"
                " VALUES (?,?,?,?)",
                (key, "2026-10-08T09:00:00+10:00", _hash(old_xml), old_xml),
            )

    S, H, ST, U = RoleClass.staff, RoleClass.helper, RoleClass.student, RoleClass.unknown

    # -- course 1001 ------------------------------------------------------------------------
    thread(
        101,
        1001,
        1,
        "announcement",
        "Welcome to FIT1001",
        "2026-10-01T09:00:00+10:00",
        "2026-10-01T09:00:00+10:00",
        pinned=True,
        category="General",
        sub="",
    )
    post(
        "thread:101",
        101,
        1001,
        "thread",
        None,
        0,
        0,
        10,
        "Staff Ten",
        S,
        "2026-10-01T09:00:00+10:00",
        "Welcome everyone, see the unit guide.",
    )

    thread(
        102,
        1001,
        2,
        "question",
        "A1 submission format?",
        "2026-10-02T10:00:00+10:00",
        "2026-10-04T10:00:00+10:00",
        replies=4,
        accepted=1021,
        staff_answered=True,
    )
    post(
        "thread:102",
        102,
        1001,
        "thread",
        None,
        0,
        0,
        20,
        "Student Twenty",
        ST,
        "2026-10-02T10:00:00+10:00",
        "What format should A1 be submitted in?",
    )
    post(
        "answer:1021",
        102,
        1001,
        "answer",
        "thread:102",
        1,
        1,
        10,
        "Staff Ten",
        S,
        "2026-10-02T11:00:00+10:00",
        "Submit a single PDF.",
        endorsed=True,
    )
    post(
        "comment:1022",
        102,
        1001,
        "comment",
        "answer:1021",
        2,
        2,
        20,
        "Student Twenty",
        ST,
        "2026-10-03T09:00:00+10:00",
        "Is a zip file fine?",
    )
    post(
        "comment:1023",
        102,
        1001,
        "comment",
        "comment:1022",
        3,
        3,
        30,
        "Helper Thirty",
        H,
        "2026-10-03T12:00:00+10:00",
        "No zip, PDF only.",
    )
    post(
        "comment:1024",
        102,
        1001,
        "comment",
        "comment:1022",
        4,
        3,
        10,
        "Staff Ten",
        S,
        "2026-10-04T10:00:00+10:00",
        "Confirming: PDF only.",
    )

    thread(
        103,
        1001,
        3,
        "question",
        "Anonymous question",
        "2026-10-05T11:00:00+10:00",
        "2026-10-05T13:00:00+10:00",
        replies=1,
    )
    post(
        "thread:103",
        103,
        1001,
        "thread",
        None,
        0,
        0,
        None,
        "Anonymous",
        ST,
        "2026-10-05T11:00:00+10:00",
        "Can someone explain Q3?",
        anonymous=True,
    )
    post(
        "comment:1031",
        103,
        1001,
        "comment",
        "thread:103",
        1,
        1,
        10,
        "Staff Ten",
        S,
        "2026-10-05T13:00:00+10:00",
        "See lecture 4, slide 12.",
    )

    thread(
        104,
        1001,
        4,
        "question",
        "My own question",
        "2026-10-06T08:00:00+10:00",
        "2026-10-09T08:00:00+10:00",
        replies=1,
        staff_answered=True,
    )
    post(
        "thread:104",
        104,
        1001,
        "thread",
        None,
        0,
        0,
        VIEWER_ID,
        "Viewer Me",
        ST,
        "2026-10-06T08:00:00+10:00",
        "Why does my test fail?",
        own=True,
    )
    post(
        "answer:1041",
        104,
        1001,
        "answer",
        "thread:104",
        1,
        1,
        10,
        "Staff Ten",
        S,
        "2026-10-06T09:00:00+10:00",
        "Check the file path (corrected).",
        editor=10,
        updated="2026-10-08T09:00:00+10:00",
        versions=["Check the file name."],
    )

    # -- course 1002 (viewer is staff) --------------------------------------------------------
    thread(
        201,
        1002,
        1,
        "question",
        "Marking query",
        "2026-10-07T10:00:00+10:00",
        "2026-10-07T15:00:00+10:00",
        replies=2,
    )
    post(
        "thread:201",
        201,
        1002,
        "thread",
        None,
        0,
        0,
        21,
        "Student TwentyOne",
        ST,
        "2026-10-07T10:00:00+10:00",
        "How is part B marked?",
    )
    post(
        "comment:2011",
        201,
        1002,
        "comment",
        "thread:201",
        1,
        1,
        None,
        "",
        ST,
        "0001-01-01T00:00:00Z",
        "",
        deleted=True,
    )
    post(
        "comment:2012",
        201,
        1002,
        "comment",
        "comment:2011",
        2,
        2,
        11,
        "Staff Eleven",
        S,
        "2026-10-07T15:00:00+10:00",
        "Part B is marked on correctness.",
    )

    thread(
        202,
        1002,
        2,
        "post",
        "Notes from an ex-member",
        "2026-10-08T10:00:00+10:00",
        "2026-10-08T10:00:00+10:00",
        pinned=True,
        category="General",
        sub="",
    )
    post(
        "thread:202",
        202,
        1002,
        "thread",
        None,
        0,
        0,
        40,
        "Mystery Forty",
        U,
        "2026-10-08T10:00:00+10:00",
        "Some notes from a user with no role.",
    )

    for tid in (101, 102, 103, 104, 201, 202):
        db.recompute_thread_flags(conn, tid)

    read = ["thread:101", "thread:102", "comment:1022", "answer:1041", "thread:201", "comment:2012"]
    for key in read:
        conn.execute("INSERT INTO read_state(post_key, read_at) VALUES (?, ?)", (key, SEEN))
    conn.commit()

    keys = [r["key"] for r in conn.execute("SELECT key FROM posts ORDER BY key")]
    return {
        "viewer_id": VIEWER_ID,
        "courses": [1001, 1002],
        "threads": [101, 102, 103, 104, 201, 202],
        "posts": keys,
        "read": read,
        "unread": [k for k in keys if k not in read],
        "edited": "answer:1041",
        "tombstone": "comment:2011",
        "anonymous": "thread:103",
        "own": "thread:104",
        "later_staff_reply": [
            r["key"]
            for r in conn.execute("SELECT key FROM posts WHERE later_staff_reply = 1 ORDER BY key")
        ],
    }
