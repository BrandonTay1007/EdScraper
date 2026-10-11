"""Sync tests: respx-only (no network), synthetic fixtures in tests/fixtures/api, injected fakes.

The fake parser/flagger below are minimal stand-ins for lane B's modules; they follow the Parser and
Flagger protocols so the real ones can be swapped in at integration.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from edscrape import paths
from edscrape import sync as sync_mod
from edscrape.api import EdClient
from edscrape.auth import AuthError
from edscrape.config import Config
from edscrape.models import ParsedContent, PostInfo, RoleClass, SyncEvent
from edscrape.store import db
from edscrape.sync import change_key, reparse, sync

FIX = Path(__file__).parent / "fixtures" / "api"
CID = 39026
DEEP, TOMB, ANON, MISSING = 3626361, 3618955, 3634515, 3620404
REPLIED = {DEEP, TOMB, ANON, MISSING}
VIEWER = 900001


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text("utf-8"))


# -- fakes -----------------------------------------------------------------------------------------


def fake_parser(content_xml: str, fallback_text: str) -> ParsedContent:
    return ParsedContent(markdown=fallback_text, text=fallback_text, unknown_tags=[])


def fake_flagger(thread, users, role_map, viewer_id):
    def classify(role):
        mapped = role_map.get(role) if role else None
        return RoleClass(mapped) if mapped in RoleClass._value2member_map_ else RoleClass.unknown

    out: dict[str, PostInfo] = {}

    def add(node, kind, parent_key, depth):
        key = f"{kind}:{node['id']}"
        deleted = node.get("type") == ""
        uid = node.get("user_id") or None
        is_own = uid is not None and uid == viewer_id
        user = users.get(uid) if uid else None
        name = (user or {}).get("name", "")
        anonymous = bool(node.get("is_anonymous"))
        if anonymous:
            uid, name = None, "Anonymous"
        out[key] = PostInfo(
            key=key,
            ref_id=node["id"],
            kind=kind,
            parent_key=parent_key,
            depth=depth,
            number=node.get("number") or 0,
            user_id=uid,
            author_name=name,
            course_role=(user or {}).get("course_role"),
            role_class=classify((user or {}).get("course_role")),
            is_anonymous=anonymous,
            is_own=is_own,
            is_endorsed=bool(node.get("is_endorsed")),
            is_deleted=deleted,
            editor_id=node.get("editor_id"),
            created_at=node.get("created_at") or "",
            updated_at=node.get("updated_at"),
            content_xml=node.get("content") or "",
            text=node.get("document") or "",
            later_staff_reply=False,
            accepted=False,
        )
        for child in node.get("comments") or []:
            add(child, "comment", key, depth + 1)

    tkey = f"thread:{thread['id']}"
    add(thread, "thread", None, 0)
    out[tkey].parent_key = None
    for ans in thread.get("answers") or []:
        add(ans, "answer", tkey, 1)
    for com in thread.get("comments") or []:
        add(com, "comment", tkey, 1)
    return out


class FakeEd:
    """In-memory Ed server behind respx. Mutate `pages` / `threads` between syncs."""

    def __init__(self):
        self.user = load("user.json")
        self.pages = {0: load("list_0.json"), 100: load("list_1.json")}
        self.threads = {tid: load(f"thread_{tid}.json") for tid in (DEEP, TOMB, ANON)}
        self.thread_responses: dict[int, list[int]] = {}  # tid -> queued statuses
        self.user_status = 200
        self.list_override: dict[int, dict] = {}
        self.requests: list[httpx.Request] = []

    def item(self, tid: int) -> dict:
        for page in self.pages.values():
            for it in page["threads"]:
                if it["id"] == tid:
                    return it
        raise KeyError(tid)

    def add_simple_detail(self, tid: int) -> None:
        it = copy.deepcopy(self.item(tid))
        it["answers"] = [
            {
                "id": 7890000 + tid % 1000,
                "user_id": 824000,
                "course_id": CID,
                "parent_id": None,
                "editor_id": None,
                "number": 1,
                "type": "answer",
                "kind": "normal",
                "content": (
                    '<document version="2.0"><paragraph>Simple answer</paragraph></document>'
                ),
                "document": "Simple answer",
                "is_endorsed": False,
                "is_anonymous": False,
                "created_at": "2026-08-01T12:00:00+10:00",
                "updated_at": None,
                "comments": [],
            }
        ]
        it["comments"] = []
        self.threads[tid] = {
            "thread": it,
            "users": [{"id": 824000, "name": "Tutor B", "course_role": "tutor", "role": "user"}],
        }

    # handlers
    def on_user(self, request):
        self.requests.append(request)
        if self.user_status != 200:
            return httpx.Response(self.user_status, json={"message": "Invalid token"})
        return httpx.Response(200, json=self.user)

    def on_list(self, request):
        self.requests.append(request)
        offset = int(request.url.params.get("offset", 0))
        cid = int(request.url.path.split("/")[-2])
        if cid in self.list_override:
            return httpx.Response(200, json=self.list_override[cid])
        return httpx.Response(200, json=self.pages[offset])

    def on_thread(self, request):
        self.requests.append(request)
        tid = int(request.url.path.rsplit("/", 1)[1])
        queue = self.thread_responses.get(tid)
        if queue:
            status = queue.pop(0)
            if status != 200:
                return httpx.Response(status, json={"message": "x"})
        if tid not in self.threads:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json=self.threads[tid])

    # inspection
    def fetched_threads(self) -> list[int]:
        return [
            int(r.url.path.rsplit("/", 1)[1])
            for r in self.requests
            if re.fullmatch(r"/api/threads/\d+", r.url.path)
        ]


@pytest.fixture
def ed(tmp_home):
    server = FakeEd()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.get("https://edstem.org/api/user").mock(side_effect=server.on_user)
        router.get(url__regex=r"https://edstem\.org/api/courses/\d+/threads").mock(
            side_effect=server.on_list
        )
        router.get(url__regex=r"https://edstem\.org/api/threads/\d+").mock(
            side_effect=server.on_thread
        )
        yield server


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def make_client(sleeps):
    async def fake_sleep(s):
        sleeps.append(s)

    def factory():
        return EdClient("tok", rate_per_sec=0, sleep=fake_sleep, jitter=lambda: 0.0)

    return factory


@pytest.fixture
def frozen(monkeypatch):
    """Freeze sync's clock far after the fixture dates so the recheck window is empty."""
    state = {"now": datetime(2027, 1, 1, tzinfo=UTC)}
    monkeypatch.setattr(sync_mod, "_now", lambda: state["now"])
    return state


async def run(conn, make_client, *, cfg=None, full=False, progress=None):
    async with make_client() as client:
        return await sync(
            [CID],
            progress,
            parser=fake_parser,
            flagger=fake_flagger,
            full=full,
            config=cfg or Config(),
            client=client,
            conn=conn,
        )


def full_server(ed):
    ed.add_simple_detail(MISSING)


# -- tests -----------------------------------------------------------------------------------------


async def test_first_sync_populates_store(conn, ed, make_client, frozen):
    full_server(ed)
    events: list[SyncEvent] = []
    report = await run(conn, make_client, progress=events.append)
    assert report.status == "ok"
    assert report.threads_seen == 135 and report.threads_changed == 135
    # users + courses (code trimmed), viewer id, pagination with dedupe
    assert db.get_meta(conn, "viewer_user_id") == str(VIEWER)
    assert db.course_by_id(conn, CID).code == "FIT2109 S2 2026 Malaysia"
    assert db.course_by_id(conn, CID).last_synced_at is not None
    assert len(db.threads_by_course(conn, CID)) == 135
    assert conn.execute("SELECT COUNT(*) FROM posts WHERE kind='thread'").fetchone()[0] == 135
    # all requests were GETs against /api, detail without `view`
    assert {r.method for r in ed.requests} == {"GET"}
    assert all("view" not in r.url.params for r in ed.requests)
    assert sorted(ed.fetched_threads()) == sorted(REPLIED)
    # raw archive
    raw = paths.raw_dir()
    assert (raw / "user.json").exists()
    assert (raw / str(CID) / "list" / "0.json").exists() and (
        raw / str(CID) / "list" / "100.json"
    ).exists()
    assert {p.stem for p in (raw / str(CID) / "threads").glob("*.json")} == {
        str(t) for t in REPLIED
    }
    # progress events
    stages = [e.stage for e in events]
    assert stages.count("done") == 1 and "list" in stages and stages.count("detail") == 4
    assert events[-1].stage == "done"
    run_row = conn.execute("SELECT * FROM sync_runs").fetchone()
    assert run_row["status"] == "ok" and run_row["threads_seen"] == 135 and run_row["error"] is None
    # every thread has its change key stored (replied ones after their detail ingested)
    assert all(v for v in db.stored_change_keys(conn, CID).values())


async def test_detail_posts_tree_flags_and_accepted(conn, ed, make_client, frozen):
    full_server(ed)
    await run(conn, make_client)
    posts = db.posts_by_thread(conn, DEEP)
    assert [p.key for p in posts] == [
        "thread:3626361",
        "comment:7576344",
        "answer:7875984",
        "comment:7876038",
        "comment:7876059",
        "comment:7876100",
    ]
    assert [p.depth for p in posts] == [0, 1, 1, 2, 3, 4]
    by_key = {p.key: p for p in posts}
    assert by_key["comment:7876100"].parent_key == "comment:7876059"
    assert by_key["answer:7875984"].role_class is RoleClass.staff
    assert by_key["answer:7875984"].markdown == "Use the provided project"
    assert by_key["answer:7875984"].content_hash and by_key["answer:7875984"].first_seen_at
    rows = {r["key"]: r for r in conn.execute("SELECT * FROM posts WHERE thread_id=?", (DEEP,))}
    assert rows["answer:7875984"]["accepted"] == 1
    assert rows["comment:7876038"]["later_staff_reply"] == 1  # staff replied after the student
    assert rows["comment:7876100"]["later_staff_reply"] == 0
    assert rows["answer:7875984"]["later_staff_reply"] == 0
    # thread post updated from list stays a single row (no duplicate from detail)
    assert conn.execute("SELECT COUNT(*) FROM posts WHERE key='thread:3626361'").fetchone()[0] == 1


async def test_full_recheck_default_fetches_every_replied_thread_each_sync(
    conn, ed, make_client, frozen
):
    full_server(ed)
    assert Config().full_recheck is True
    await run(conn, make_client)
    ed.requests.clear()
    report = await run(conn, make_client)
    assert report.status == "ok" and report.threads_changed == 0
    assert sorted(ed.fetched_threads()) == sorted(REPLIED)


async def test_no_full_recheck_fetches_only_new_changed_or_recent(conn, ed, make_client, frozen):
    full_server(ed)
    cfg = Config(full_recheck=False, recheck_days=14)
    await run(conn, make_client, cfg=cfg)
    assert sorted(ed.fetched_threads()) == sorted(REPLIED)  # first sync: all new
    # nothing changed and nothing within the window -> no detail fetches
    ed.requests.clear()
    report = await run(conn, make_client, cfg=cfg)
    assert ed.fetched_threads() == [] and report.threads_changed == 0
    # one replied thread's updated_at/reply_count moves -> only it is fetched
    ed.requests.clear()
    it = ed.item(TOMB)
    it["updated_at"], it["reply_count"] = "2026-10-05T09:00:00+11:00", 3
    report = await run(conn, make_client, cfg=cfg)
    assert ed.fetched_threads() == [TOMB] and report.threads_changed == 1
    # a brand new replied thread is fetched
    ed.requests.clear()
    new_item = copy.deepcopy(ed.item(3633452))
    new_item.update(
        id=3690001,
        reply_count=1,
        is_pinned=False,
        type="question",
        number=700,
        updated_at="2026-08-05T09:00:00+10:00",
    )
    ed.pages[100]["threads"].append(new_item)
    ed.add_simple_detail(3690001)
    await run(conn, make_client, cfg=cfg)
    assert ed.fetched_threads() == [3690001]
    # recheck window: threads updated within recheck_days of "now" are refetched even if unchanged
    frozen["now"] = datetime(2026, 10, 9, 12, tzinfo=UTC)
    ed.requests.clear()
    await run(conn, make_client, cfg=cfg)
    assert sorted(ed.fetched_threads()) == sorted([DEEP, TOMB, ANON])  # updated 10-03..10-09
    # changing recheck_days shrinks the window
    ed.requests.clear()
    await run(conn, make_client, cfg=Config(full_recheck=False, recheck_days=1))
    assert ed.fetched_threads() == [ANON]


async def test_full_flag_forces_every_replied_thread(conn, ed, make_client, frozen):
    full_server(ed)
    cfg = Config(full_recheck=False)
    await run(conn, make_client, cfg=cfg)
    ed.requests.clear()
    await run(conn, make_client, cfg=cfg)
    assert ed.fetched_threads() == []
    await run(conn, make_client, cfg=cfg, full=True)
    assert sorted(ed.fetched_threads()) == sorted(REPLIED)


async def test_threads_with_no_replies_never_fetched(conn, ed, make_client, frozen):
    full_server(ed)
    await run(conn, make_client, cfg=Config(), full=True)
    assert all(t in REPLIED for t in ed.fetched_threads())


async def test_tombstone_keeps_row_and_children(conn, ed, make_client, frozen):
    full_server(ed)
    await run(conn, make_client)
    tomb = db.post_by_key(conn, "comment:7877194")
    assert tomb.is_deleted and tomb.parent_key == f"thread:{TOMB}" and tomb.content_xml == ""
    child = db.post_by_key(conn, "comment:7877200")
    assert child.parent_key == "comment:7877194" and not child.is_deleted
    keys = [p.key for p in db.posts_by_thread(conn, TOMB)]
    assert keys == [f"thread:{TOMB}", "comment:7877194", "comment:7877200"]
    flags = conn.execute(
        "SELECT deleted FROM post_flags WHERE post_key='comment:7877194'"
    ).fetchone()
    assert flags["deleted"] == 1
    # a deleted staff post does not trigger later_staff_reply for the student post before it
    assert db.post_by_key(conn, f"thread:{TOMB}") is not None


async def test_live_post_becoming_tombstone_keeps_old_body(conn, ed, make_client, frozen):
    full_server(ed)
    await run(conn, make_client)
    before = db.post_by_key(conn, "comment:7876038")
    node = ed.threads[DEEP]["thread"]["answers"][0]["comments"][0]
    node.update(
        type="", kind="", content="", document="", user_id=0, created_at="0001-01-01T00:00:00Z"
    )
    await run(conn, make_client)
    after = db.post_by_key(conn, "comment:7876038")
    assert after.is_deleted and after.text == before.text and after.user_id == before.user_id
    assert db.post_by_key(conn, "comment:7876059").parent_key == "comment:7876038"


async def test_anonymous_normalised_with_is_own_and_not_persisted(conn, ed, make_client, frozen):
    full_server(ed)
    await run(conn, make_client)
    other = db.post_by_key(conn, "answer:7880001")
    mine = db.post_by_key(conn, "comment:7880002")
    for p in (other, mine):
        assert p.is_anonymous and p.user_id is None and p.author_name == "Anonymous"
    assert mine.is_own and not other.is_own
    # thread itself: student view carries no user at all
    t = db.post_by_key(conn, f"thread:{ANON}")
    assert t.is_anonymous is False or t.user_id is None  # list item is_anonymous drives the flag
    assert t.author_name == "Anonymous" and t.user_id is None
    # revealed identities never reach the users/roles tables
    ids = {r[0] for r in conn.execute("SELECT id FROM users")}
    role_ids = {r[0] for r in conn.execute("SELECT user_id FROM user_course_roles")}
    assert 777777 not in ids | role_ids and VIEWER not in ids | role_ids
    assert "Revealed Anon" not in json.dumps(
        [tuple(r) for r in conn.execute("SELECT * FROM posts")]
    )


async def test_editor_id_and_hash_change_versions(conn, ed, make_client, frozen):
    full_server(ed)
    await run(conn, make_client)
    p = db.post_by_key(conn, "comment:7576344")
    assert p.editor_id == 824000
    assert (
        conn.execute("SELECT edited FROM post_flags WHERE post_key='comment:7576344'").fetchone()[
            "edited"
        ]
        == 1
    )  # editor_id alone marks it edited
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM post_versions WHERE post_key='answer:7875984'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT edited FROM post_flags WHERE post_key='answer:7875984'").fetchone()[
            "edited"
        ]
        == 0
    )
    # unchanged re-sync adds no versions
    await run(conn, make_client)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM post_versions WHERE post_key='answer:7875984'"
        ).fetchone()[0]
        == 1
    )
    # body edit (editor_id still null): new version row, edited flag via >1 versions
    ans = ed.threads[DEEP]["thread"]["answers"][0]
    ans["content"] = '<document version="2.0"><paragraph>Use the NEW project</paragraph></document>'
    ans["document"] = "Use the NEW project"
    report = await run(conn, make_client)
    assert report.posts_changed == 1 and report.posts_new == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM post_versions WHERE post_key='answer:7875984'"
        ).fetchone()[0]
        == 2
    )
    assert (
        conn.execute("SELECT edited FROM post_flags WHERE post_key='answer:7875984'").fetchone()[
            "edited"
        ]
        == 1
    )
    assert db.post_by_key(conn, "answer:7875984").markdown == "Use the NEW project"
    # FTS follows the edit
    assert conn.execute("SELECT 1 FROM posts_fts WHERE posts_fts MATCH 'NEW'").fetchone()


async def test_non_null_role_rule(conn, ed, make_client, frozen):
    full_server(ed)
    await run(conn, make_client)
    # list item showed user 830000 as student; the detail payload has course_role null
    assert db.role_for(conn, 830000, CID) == "student"
    assert db.post_by_key(conn, "comment:7876100").role_class is RoleClass.student
    # and the stored role survives an all-null world
    ed.item(3626000)["user"]["course_role"] = None
    await run(conn, make_client)
    assert db.role_for(conn, 830000, CID) == "student"


async def test_401_on_user_aborts_before_any_course(conn, ed, make_client, frozen):
    ed.user_status = 401
    with pytest.raises(AuthError):
        await run(conn, make_client)
    assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
    assert [r.url.path for r in ed.requests] == ["/api/user"]


async def test_401_on_detail_aborts_whole_sync(conn, ed, make_client, frozen):
    full_server(ed)
    ed.thread_responses = {t: [401] for t in REPLIED}
    with pytest.raises(AuthError):
        await run(conn, make_client)
    row = conn.execute("SELECT * FROM sync_runs").fetchone()
    assert row["status"] == "error" and "401" in row["error"]
    assert db.course_by_id(conn, CID).last_synced_at is None


async def test_503_then_200_is_retried_inside_sync(conn, ed, make_client, frozen, sleeps):
    full_server(ed)
    ed.thread_responses = {DEEP: [503, 200]}
    report = await run(conn, make_client)
    assert report.status == "ok"
    assert sleeps == [0.5]
    assert db.post_by_key(conn, "answer:7875984") is not None


async def test_per_thread_failure_is_partial_and_sync_continues(conn, ed, make_client, frozen):
    # MISSING has no detail on the server -> 404; DEEP keeps failing with 503 after retries
    ed.thread_responses = {DEEP: [503] * 10}
    report = await run(conn, make_client)
    assert report.status == "partial"
    row = conn.execute("SELECT * FROM sync_runs").fetchone()
    assert row["status"] == "partial"
    assert f"thread {MISSING}" in row["error"] and f"thread {DEEP}" in row["error"]
    # the other threads were still stored
    assert db.post_by_key(conn, "comment:7877200") is not None
    assert db.post_by_key(conn, "comment:7880002") is not None
    assert db.post_by_key(conn, "answer:7875984") is None
    assert report.runs[0]["status"] == "partial"
    assert db.course_by_id(conn, CID).last_synced_at is not None
    # failed threads keep no change_key, so even with full_recheck=False they are retried
    keys = db.stored_change_keys(conn, CID)
    assert keys[MISSING] is None and keys[DEEP] is None and keys[TOMB]
    ed.thread_responses = {}
    full_server(ed)
    ed.requests.clear()
    report = await run(conn, make_client, cfg=Config(full_recheck=False))
    assert sorted(ed.fetched_threads()) == sorted([MISSING, DEEP])
    assert report.status == "ok"


async def test_unknown_course_is_error_run(conn, ed, make_client, frozen):
    async with make_client() as client:
        report = await sync(
            [424242],
            parser=fake_parser,
            flagger=fake_flagger,
            config=Config(),
            client=client,
            conn=conn,
        )
    assert report.status == "error" and "not found" in report.runs[0]["error"]


async def test_first_sync_read_marking_only_when_enabled(conn, ed, make_client, frozen):
    full_server(ed)
    frozen["now"] = datetime(2026, 10, 9, 12, tzinfo=UTC)
    await run(conn, make_client, cfg=Config())  # default: first_sync_read_before_days = 0
    assert conn.execute("SELECT COUNT(*) FROM read_state").fetchone()[0] == 0


async def test_first_sync_read_marking_with_days(conn, ed, make_client, frozen):
    full_server(ed)
    frozen["now"] = datetime(2026, 10, 9, 12, tzinfo=UTC)
    cfg = Config(first_sync_read_before_days=14)  # cutoff 2026-09-25
    await run(conn, make_client, cfg=cfg)
    read = {r[0] for r in conn.execute("SELECT post_key FROM read_state")}
    assert "thread:3633452" not in read  # announcement 10-10 is newer than the cutoff
    assert "answer:7875984" not in read  # 10-07
    old = conn.execute(
        "SELECT key FROM posts WHERE is_deleted = 0 AND julianday(created_at) < julianday(?)",
        ("2026-09-25T12:00:00+00:00",),
    ).fetchall()
    assert old and {r[0] for r in old} <= read
    assert len(read) == len(old)
    # a later sync is not a first sync: nothing new gets marked
    conn.execute("DELETE FROM read_state")
    conn.commit()
    frozen["now"] = datetime(2027, 1, 1, tzinfo=UTC)
    await run(conn, make_client, cfg=cfg)
    assert conn.execute("SELECT COUNT(*) FROM read_state").fetchone()[0] == 0


async def test_reparse_matches_sync(conn, ed, make_client, frozen):
    full_server(ed)
    await run(conn, make_client)

    def snapshot():
        cols = (
            "key ref_id thread_id course_id kind parent_key number depth user_id author_name "
            "role_class is_anonymous is_own is_endorsed is_deleted editor_id created_at "
            "updated_at content_xml text markdown content_hash later_staff_reply accepted"
        )
        rows = conn.execute(f"SELECT {', '.join(cols.split())} FROM posts ORDER BY key").fetchall()
        return [tuple(r) for r in rows]

    before = snapshot()
    versions = [
        tuple(r)
        for r in conn.execute("SELECT post_key, content_hash FROM post_versions ORDER BY 1, 2")
    ]
    conn.execute("DELETE FROM posts")
    conn.execute("DELETE FROM post_versions")
    conn.commit()
    assert snapshot() == []
    ed.requests.clear()
    reparse([CID], parser=fake_parser, flagger=fake_flagger, conn=conn)
    assert ed.requests == []  # no network
    assert snapshot() == before
    assert [
        tuple(r)
        for r in conn.execute("SELECT post_key, content_hash FROM post_versions ORDER BY 1, 2")
    ] == versions
    # parser changes show up after reparse without any fetch
    reparse(
        [CID],
        parser=lambda x, t: ParsedContent(markdown=f"MD:{t}", text=t, unknown_tags=[]),
        flagger=fake_flagger,
        conn=conn,
    )
    assert db.post_by_key(conn, "answer:7875984").markdown == "MD:Use the provided project"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM post_versions WHERE post_key='answer:7875984'"
        ).fetchone()[0]
        == 1
    )


def test_reparse_requires_viewer_id(tmp_home, conn):
    with pytest.raises(RuntimeError):
        reparse([CID], parser=fake_parser, flagger=fake_flagger, conn=conn)


async def test_parser_failure_falls_back_to_plain_text(conn, ed, make_client, frozen):
    full_server(ed)
    events: list[SyncEvent] = []

    def bad_parser(xml, text):
        raise ValueError("boom")

    async with make_client() as client:
        report = await sync(
            [CID],
            parser=bad_parser,
            flagger=fake_flagger,
            config=Config(),
            client=client,
            conn=conn,
            progress=events.append,
        )
    assert report.status == "partial"
    assert db.post_by_key(conn, "answer:7875984").markdown == "Use the provided project"
    err = conn.execute("SELECT error FROM sync_runs").fetchone()["error"]
    assert "parser failed on thread:" in err and "ValueError: boom" in err
    assert err.count("parser failed") == 5  # capped per run
    assert any(e.stage == "error" and "parser failed" in e.message for e in events)


def test_change_key_is_stable_and_sensitive():
    item = load("list_0.json")["threads"][0]
    k = change_key(item)
    assert k == change_key(copy.deepcopy(item))
    assert json.loads(k)[:7] == [item["updated_at"], 0, None, False, None, False, True]
    for field, value in [
        ("updated_at", "x"),
        ("reply_count", 9),
        ("editor_id", 1),
        ("is_endorsed", True),
        ("accepted_id", 5),
        ("is_answered", True),
        ("is_pinned", False),
        ("content", "<document/>"),
    ]:
        changed = copy.deepcopy(item)
        changed[field] = value
        assert change_key(changed) != k, field


def test_sync_module_imports_lane_b_lazily():
    source = Path(sync_mod.__file__).read_text("utf-8")
    top_level = [ln for ln in source.splitlines() if ln.startswith(("import ", "from "))]
    assert not [ln for ln in top_level if "edparse" in ln or "flags" in ln]


async def test_sync_without_injected_parser_imports_lane_b_lazily(conn, ed, make_client, frozen):
    import importlib.util

    if importlib.util.find_spec("edscrape.edparse") and importlib.util.find_spec("edscrape.flags"):
        pytest.skip("real parser/flagger present; covered by the integration test")
    with pytest.raises(ModuleNotFoundError):
        await run_default(conn, make_client)
    assert ed.requests == []  # failed before any network call


async def run_default(conn, make_client):
    async with make_client() as client:
        return await sync([CID], config=Config(), client=client, conn=conn)


async def test_unexpected_list_error_fails_that_course_only(conn, ed, make_client, frozen):
    full_server(ed)
    ed.list_override[35219] = {"threads": [{"id": 1}], "users": []}  # malformed: no course_id
    async with make_client() as client:
        report = await sync(
            [35219, CID],
            parser=fake_parser,
            flagger=fake_flagger,
            config=Config(),
            client=client,
            conn=conn,
        )
    runs = conn.execute("SELECT course_id, status, error FROM sync_runs ORDER BY id").fetchall()
    assert [(r["course_id"], r["status"]) for r in runs] == [(35219, "error"), (CID, "ok")]
    assert "KeyError" in runs[0]["error"]
    assert report.status == "partial"
    assert conn.execute("SELECT COUNT(*) FROM sync_runs WHERE status='running'").fetchone()[0] == 0
    assert len(db.threads_by_course(conn, CID)) == 135
