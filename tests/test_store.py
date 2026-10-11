from __future__ import annotations

import sqlite3
from dataclasses import replace

from edscrape import paths
from edscrape.models import Course, Post, RoleClass, Thread
from edscrape.store import db
from tests.seed import SEEN, VIEWER_ID, seed_store


def make_thread(tid: int = 1, cid: int = 7, title: str = "Hello world") -> Thread:
    return Thread(
        id=tid,
        course_id=cid,
        number=1,
        type="question",
        title=title,
        category="General",
        subcategory="",
        subsubcategory="",
        accepted_id=None,
        is_pinned=False,
        is_endorsed=False,
        is_answered=False,
        is_staff_answered=False,
        is_private=False,
        reply_count=0,
        created_at="2026-10-01T10:00:00+10:00",
        updated_at="2026-10-01T10:00:00+10:00",
    )


def make_post(key: str = "thread:1", **over) -> Post:
    base = Post(
        key=key,
        ref_id=int(key.split(":")[1]),
        thread_id=1,
        course_id=7,
        kind=key.split(":")[0],
        parent_key=None,
        number=0,
        depth=0,
        user_id=5,
        author_name="A",
        role_class=RoleClass.student,
        is_anonymous=False,
        is_own=False,
        is_endorsed=False,
        is_deleted=False,
        editor_id=None,
        created_at="2026-10-01T10:00:00+10:00",
        updated_at=None,
        content_xml="<document/>",
        text="alpha beta",
        markdown="alpha beta",
        content_hash="h1",
        first_seen_at="x",
        last_seen_at="x",
    )
    return replace(base, **over)


def test_connect_and_migrate(tmp_home):
    conn = db.connect()
    db.migrate(conn)
    db.migrate(conn)  # idempotent
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.get_meta(conn, "schema_version") == db.SCHEMA_VERSION
    assert isinstance(conn.execute("SELECT 1 AS x").fetchone(), sqlite3.Row)
    assert paths.db_path().exists()
    conn.close()


def test_meta_and_course_roundtrip(conn):
    db.set_meta(conn, "k", "v1")
    db.set_meta(conn, "k", "v2")
    assert db.get_meta(conn, "k") == "v2"
    assert db.get_meta(conn, "missing", "d") == "d"
    course = Course(
        7,
        "FIT9999 S1 ",
        "Unit",
        "2026",
        "S1",
        "active",
        "student",
        [{"name": "General"}],
        True,
        None,
    )
    db.upsert_course(conn, course)
    got = db.course_by_id(conn, 7)
    assert got is not None and got.code == "FIT9999 S1" and got.categories == [{"name": "General"}]
    # re-upsert keeps selected and last_synced_at, updates the rest
    db.set_course_selected(conn, 7, False)
    db.upsert_course(conn, replace(course, name="Renamed", selected=True))
    got = db.course_by_id(conn, 7)
    assert got.name == "Renamed" and got.selected is False
    assert [c.id for c in db.courses(conn)] == [7]


def test_user_role_never_overwritten_by_null(conn):
    db.upsert_user(conn, 5, "A")
    db.upsert_user_role(conn, 5, 7, "student")
    db.upsert_user_role(conn, 5, 7, None)
    assert db.role_for(conn, 5, 7) == "student"
    db.upsert_user_role(conn, 5, 7, "tutor")
    assert db.role_for(conn, 5, 7) == "tutor"
    db.upsert_user(conn, 5, None)  # null name keeps the stored one
    assert conn.execute("SELECT name FROM users WHERE id=5").fetchone()["name"] == "A"


def test_upsert_post_versions_and_seen_times(conn):
    db.upsert_thread(conn, make_thread())
    assert db.upsert_post(conn, make_post(), now="t1") == "new"
    assert db.upsert_post(conn, make_post(), now="t2") == "same"
    row = db.post_by_key(conn, "thread:1")
    assert row.first_seen_at == "x" and row.last_seen_at == "t2"
    changed = make_post(content_hash="h2", content_xml="<document>2</document>", text="gamma")
    assert db.upsert_post(conn, changed, now="t3") == "changed"
    versions = conn.execute(
        "SELECT content_hash FROM post_versions ORDER BY content_hash"
    ).fetchall()
    assert [r["content_hash"] for r in versions] == ["h1", "h2"]
    flags = conn.execute("SELECT edited FROM post_flags WHERE post_key='thread:1'").fetchone()
    assert flags["edited"] == 1
    # same hash with different markdown (reparse) updates the row but adds no version
    reparsed = replace(changed, markdown="**gamma**")
    assert db.upsert_post(conn, reparsed, now="t4") == "same"
    assert db.post_by_key(conn, "thread:1").markdown == "**gamma**"
    assert conn.execute("SELECT COUNT(*) FROM post_versions").fetchone()[0] == 2


def test_fts_reads_title_when_thread_upserted_first(conn):
    db.upsert_thread(conn, make_thread(title="Quokka question"))
    db.upsert_post(conn, make_post(text="body words"))
    hit = conn.execute("SELECT rowid FROM posts_fts WHERE posts_fts MATCH 'quokka'").fetchall()
    assert len(hit) == 1
    db.upsert_thread(conn, make_thread(title="Wombat question"))
    assert conn.execute("SELECT 1 FROM posts_fts WHERE posts_fts MATCH 'quokka'").fetchone() is None
    assert conn.execute("SELECT 1 FROM posts_fts WHERE posts_fts MATCH 'wombat'").fetchone()


def test_mark_tombstone_keeps_body(conn):
    db.upsert_thread(conn, make_thread())
    db.upsert_post(conn, make_post())
    db.mark_tombstone(conn, "thread:1", now="t9")
    p = db.post_by_key(conn, "thread:1")
    assert p.is_deleted and p.text == "alpha beta" and p.last_seen_at == "t9"


def test_change_key_not_clobbered_by_none(conn):
    db.upsert_thread(conn, make_thread(), change_key="k1")
    db.upsert_thread(conn, make_thread(title="New title"))
    assert db.stored_change_keys(conn, 7) == {1: "k1"}
    db.set_change_key(conn, 1, "k2")
    assert db.stored_change_keys(conn, 7) == {1: "k2"}


def test_sync_runs_and_prior_sync(conn):
    db.upsert_course(conn, Course(7, "C", "n", "2026", "S1", "active", "student", [], True, None))
    assert not db.has_prior_sync(conn, 7)
    run = db.insert_sync_run(conn, 7, "t0")
    assert not db.has_prior_sync(conn, 7)  # still running
    out = db.finish_sync_run(conn, run, status="error", error="boom")
    assert out["status"] == "error" and not db.has_prior_sync(conn, 7)
    assert db.course_by_id(conn, 7).last_synced_at is None
    run2 = db.insert_sync_run(conn, 7, "t1")
    db.finish_sync_run(conn, run2, status="ok", threads_seen=3, finished_at="t2")
    assert db.has_prior_sync(conn, 7)
    assert db.course_by_id(conn, 7).last_synced_at == "t2"


def test_mark_read_before(conn):
    db.upsert_thread(conn, make_thread())
    db.upsert_post(conn, make_post("thread:1", created_at="2026-09-01T10:00:00+10:00"))
    db.upsert_post(conn, make_post("comment:2", created_at="2026-10-10T10:00:00+10:00"))
    n = db.mark_read_before(conn, 7, "2026-10-01T00:00:00+00:00", read_at="r")
    assert n == 1
    assert [r["post_key"] for r in conn.execute("SELECT post_key FROM read_state")] == ["thread:1"]


# -- seed-based ----------------------------------------------------------------------------------


def test_seed_shape(conn):
    info = seed_store(conn)
    assert info["viewer_id"] == VIEWER_ID == int(db.get_meta(conn, "viewer_user_id"))
    assert [c.id for c in db.courses(conn)] == info["courses"]
    assert {c.id: c.role for c in db.courses(conn)} == {1001: "student", 1002: "staff"}
    assert len(db.threads_by_course(conn, 1001)) == 4 and len(db.threads_by_course(conn, 1002)) == 2
    kinds = {t.id: (t.type, t.is_pinned) for t in db.threads_by_course(conn, 1001)}
    assert kinds[101] == ("announcement", True)
    roles = {p.role_class for tid in info["threads"] for p in db.posts_by_thread(conn, tid)}
    assert roles == set(RoleClass)
    # about half read
    n_read = conn.execute("SELECT COUNT(*) FROM read_state").fetchone()[0]
    assert 0.3 < n_read / len(info["posts"]) < 0.7
    assert sorted(info["read"] + info["unread"]) == info["posts"]


def test_seed_special_posts(conn):
    info = seed_store(conn)
    anon = db.post_by_key(conn, info["anonymous"])
    assert anon.user_id is None and anon.author_name == "Anonymous" and anon.is_anonymous
    assert db.post_by_key(conn, info["own"]).is_own
    tomb = db.post_by_key(conn, info["tombstone"])
    assert tomb.is_deleted
    child = db.post_by_key(conn, "comment:2012")
    assert child.parent_key == tomb.key
    flags = {
        r["post_key"]: r
        for r in conn.execute(
            "SELECT * FROM post_flags WHERE post_key IN (?, ?, ?)",
            (info["edited"], "thread:101", "thread:202"),
        )
    }
    assert flags[info["edited"]]["edited"] == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM post_versions WHERE post_key=?", (info["edited"],)
        ).fetchone()[0]
        == 2
    )
    assert db.post_by_key(conn, info["edited"]).editor_id == 10
    assert flags["thread:101"]["announcement"] == 1 and flags["thread:101"]["pinned"] == 1
    assert flags["thread:202"]["pinned"] == 1 and flags["thread:202"]["announcement"] == 0


def test_seed_is_deterministic():
    def dump():
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        seed_store(c)
        rows = [tuple(r) for r in c.execute("SELECT * FROM posts ORDER BY key")]
        c.close()
        return rows

    assert dump() == dump()


def test_recompute_thread_flags_on_seed(conn):
    info = seed_store(conn)
    assert info["later_staff_reply"] == [
        "comment:1022",
        "comment:1023",
        "comment:2011",
        "thread:102",
        "thread:103",
        "thread:104",
        "thread:201",
    ]
    assert db.post_by_key(conn, "answer:1021") is not None
    accepted = [r["key"] for r in conn.execute("SELECT key FROM posts WHERE accepted = 1")]
    assert accepted == ["answer:1021"]
    # a deleted staff post does not count as a later staff reply
    db.mark_tombstone(conn, "comment:2012")
    db.recompute_thread_flags(conn, 201)
    assert db.post_by_key(conn, "thread:201") is not None
    row = conn.execute("SELECT later_staff_reply FROM posts WHERE key='thread:201'").fetchone()
    assert row["later_staff_reply"] == 0
    # staff posts themselves are never flagged
    assert (
        conn.execute("SELECT later_staff_reply FROM posts WHERE key='answer:1021'").fetchone()[0]
        == 0
    )


def test_posts_by_thread_is_ordered_tree(conn):
    seed_store(conn)
    keys = [p.key for p in db.posts_by_thread(conn, 102)]
    assert keys == ["thread:102", "answer:1021", "comment:1022", "comment:1023", "comment:1024"]
    assert [p.depth for p in db.posts_by_thread(conn, 102)] == [0, 1, 2, 3, 3]
    assert [p.key for p in db.posts_by_thread(conn, 201)] == [
        "thread:201",
        "comment:2011",
        "comment:2012",
    ]
    assert db.post_by_key(conn, "nope") is None and db.thread_by_id(conn, 999) is None
    t = db.thread_by_id(conn, 102)
    assert t.accepted_id == 1021 and t.is_staff_answered
    assert SEEN  # seed constant is importable for downstream lanes
