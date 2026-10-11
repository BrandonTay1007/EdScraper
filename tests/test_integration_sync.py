"""Integration: lane A's sync with lane B's real parser and flagger (nothing injected).

Serves lane A's API fixtures through respx. The only fixture edit is one bold/link thread body
(list item and detail), since the shipped API fixtures contain plain paragraphs only.
"""

from __future__ import annotations

import pytest
import respx

from edscrape import sync as sync_mod
from edscrape.api import EdClient
from edscrape.config import Config
from edscrape.sync import reparse, sync
from tests.test_sync import ANON, CID, DEEP, MISSING, FakeEd

MARKUP_XML = (
    '<document version="2.0"><paragraph>Read <bold>this</bold> and '
    '<link href="https://example.com/x">the page</link>.</paragraph></document>'
)


@pytest.fixture
def server(tmp_home):
    ed = FakeEd()
    ed.add_simple_detail(MISSING)
    ed.item(DEEP)["content"] = MARKUP_XML
    ed.threads[DEEP]["thread"]["content"] = MARKUP_XML
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get("https://edstem.org/api/user").mock(side_effect=ed.on_user)
        router.get(url__regex=r"https://edstem\.org/api/courses/\d+/threads").mock(
            side_effect=ed.on_list
        )
        router.get(url__regex=r"https://edstem\.org/api/threads/\d+").mock(side_effect=ed.on_thread)
        yield ed


@pytest.fixture
async def synced(conn, server, monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setattr(sync_mod, "_now", lambda: datetime(2027, 1, 1, tzinfo=UTC))

    async def no_sleep(_s):
        return None

    async with EdClient("tok", rate_per_sec=0, sleep=no_sleep, jitter=lambda: 0.0) as client:
        report = await sync([CID], config=Config(), client=client, conn=conn)
    return report


def _post(conn, key):
    return conn.execute("SELECT * FROM posts WHERE key=?", (key,)).fetchone()


async def test_real_parser_and_flagger_run_end_to_end(conn, synced):
    assert synced.status == "ok" and synced.posts_new == 145
    assert conn.execute("SELECT COUNT(*) FROM post_flags").fetchone()[0] == 145

    # real parser: markup becomes markdown that differs from plain text
    deep = _post(conn, f"thread:{DEEP}")
    assert deep["markdown"] and deep["markdown"] != deep["text"]
    assert "**this**" in deep["markdown"] and "https://example.com/x" in deep["markdown"]

    # real role map
    classes = {r[0] for r in conn.execute("SELECT DISTINCT role_class FROM posts")}
    assert {"staff", "student"} <= classes

    # tombstone
    assert conn.execute("SELECT COUNT(*) FROM posts WHERE is_deleted=1").fetchone()[0] >= 1
    assert (
        conn.execute(
            "SELECT deleted FROM post_flags f JOIN posts p ON p.key=f.post_key WHERE p.is_deleted=1"
        ).fetchone()[0]
        == 1
    )

    # anonymous: fixture editor_id is null, so the store keeps None
    anon = _post(conn, f"thread:{ANON}")
    assert anon["is_anonymous"] == 1 and anon["author_name"] == "Anonymous"
    assert anon["editor_id"] is None and anon["user_id"] is None

    # later_staff_reply on a student post, consistent with the view
    rows = conn.execute(
        "SELECT p.key, p.role_class, f.later_staff_reply FROM posts p "
        "JOIN post_flags f ON f.post_key=p.key WHERE p.later_staff_reply=1"
    ).fetchall()
    assert any(r["role_class"] == "student" for r in rows)
    assert all(r["later_staff_reply"] == 1 for r in rows)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM posts p JOIN post_flags f ON f.post_key=p.key "
            "WHERE p.later_staff_reply != f.later_staff_reply OR p.accepted != f.accepted"
        ).fetchone()[0]
        == 0
    )


async def test_reparse_reproduces_synced_rows(conn, synced):
    cols = "key, content_hash, markdown, role_class"
    before = conn.execute(f"SELECT {cols} FROM posts ORDER BY key").fetchall()
    reparse([CID], config=Config(), conn=conn)
    after = conn.execute(f"SELECT {cols} FROM posts ORDER BY key").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    assert len(before) == 145
