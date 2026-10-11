"""Pilot tests for the Textual app. Fake Services over `seed_store`; no network, no subprocess."""

from __future__ import annotations

import sqlite3
from importlib import resources

import pytest
from textual.widgets import DataTable, OptionList, SelectionList, Static

from edscrape.auth import AuthError
from edscrape.config import Config
from edscrape.models import AuthStatus, SyncEvent, SyncReport
from edscrape.tui.app import EXPIRED_BANNER, EdScrapeApp
from edscrape.tui.queries import assignment_names
from edscrape.tui.screens.brief import BriefScreen, ed_url
from edscrape.tui.screens.home import HomeScreen
from edscrape.tui.screens.search import SearchScreen
from edscrape.tui.screens.setup import SetupScreen
from edscrape.tui.screens.thread import ThreadScreen
from edscrape.tui.services import Services
from edscrape.tui.widgets.post import PostWidget
from edscrape.tui.widgets.tile import UnitTile
from tests.seed import seed_store

BRIEF_MD = """# A1 brief

## Requirements
- Submit a single PDF. https://edstem.org/au/courses/1001/discussion/102?answer=1021
- Plain bullet without a link
"""


@pytest.fixture
def tconn(tmp_home):
    """Like the `conn` fixture but usable from the brief worker thread."""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(resources.files("edscrape.store").joinpath("schema.sql").read_text("utf-8"))
    yield c
    c.close()


class Fakes:
    def __init__(self, conn, *, auth_ok=True, unit_ids=(1001, 1002)):
        self.opened: list[str] = []
        self.suspended: list[list[str]] = []
        self.claude_calls: list[tuple[list[str], str]] = []
        self.sync_calls: list[list[int]] = []
        self.logins = 0
        self.auth_ok = auth_ok
        self.sync_error: Exception | None = None
        self.services = Services(
            auth_check=self.auth_check,
            login=self.login,
            sync=self.sync,
            open_url=self.opened.append,
            suspend_and_run=self.suspended.append,
            conn=conn,
            config=Config(unit_ids=list(unit_ids)),
            run_claude=self.run_claude,
            list_courses=self.list_courses,
        )

    async def auth_check(self):
        return AuthStatus(ok=self.auth_ok, user_id=1, name="me", reason=None)

    def login(self):
        self.logins += 1
        self.auth_ok = True
        return AuthStatus(ok=True, user_id=1, name="me", reason=None)

    async def sync(self, ids, progress):
        self.sync_calls.append(list(ids))
        if self.sync_error:
            raise self.sync_error
        for cid in ids:
            if progress:
                progress(SyncEvent(course_id=cid, stage="detail", done=1, total=2, message=None))
        return SyncReport(
            runs=[], threads_seen=0, threads_changed=0, posts_new=3, posts_changed=0, status="ok"
        )

    def run_claude(self, argv, prompt):
        self.claude_calls.append((argv, prompt))
        return BRIEF_MD

    async def list_courses(self):
        from edscrape.store import db

        return [(c, True) for c in db.courses(self.services.conn)]


@pytest.fixture
def fakes(tconn):
    seed_store(tconn)
    tconn.execute("DELETE FROM read_state")  # start with everything unread
    tconn.commit()
    return Fakes(tconn)


def home_keys(app) -> list[str]:
    return [str(k.value) for k in app.screen.query_one("#inbox", DataTable).rows]


def made(fakes, **kw):
    return EdScrapeApp(fakes.services, sync_on_start=kw.get("sync_on_start", False))


async def test_home_tiles_and_staff_only_rows(fakes):
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, HomeScreen)
        assert {t.course_id for t in screen.query(UnitTile)} == {1001, 1002}
        keys = home_keys(pilot.app)
        assert keys
        excluded = {"thread:102", "thread:103", "thread:104", "thread:201", "comment:2011"}
        assert not excluded & set(keys)  # students and tombstones never appear
        assert "answer:1021" in keys and "comment:1023" in keys and "answer:1041" in keys
        table = screen.query_one("#inbox", DataTable)
        # excerpt length cap and own badge: thread:104 is student-authored so not listed
        for row in table.rows:
            assert len(table.get_row(row)[4].plain) <= 80
        edited = table.get_row("answer:1041")
        assert "✎" in edited[5].plain
        later = table.get_row("comment:1023")
        assert later[2].plain == "helper"


async def test_dashboard_numbers(fakes):
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        tile = pilot.app.screen.query_one("#tile-1001", UnitTile)
        assert tile.stats["unread"] == 6  # thread:101, answers/comments 1021 1023 1024 1031 1041
        assert tile.stats["later"] == 3  # threads 102, 103, 104: a non-staff post followed by staff


async def test_assignment_names_from_category_tree():
    from edscrape.models import Course

    c = Course(
        id=1, code="X", name="", year="", session="", status="active", role="student",
        categories=[
            {"name": "General", "subcategories": [{"name": "Chat"}]},
            {"name": "Assessments", "subcategories": [{"name": "A1"}, {"name": "A2"}]},
        ],
        selected=True, last_synced_at=None,
    )  # fmt: skip
    cfg = Config(assignments={"1": {"Project": {"keep": [], "exclude": []}}})
    assert assignment_names(c, cfg) == ["A1", "A2", "Project"]


async def test_mark_read_removes_row_and_writes_state(fakes):
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        first = home_keys(pilot.app)[0]
        await pilot.press("m")
        await pilot.pause()
        assert first not in home_keys(pilot.app)
        row = fakes.services.conn.execute(
            "SELECT 1 FROM read_state WHERE post_key = ?", (first,)
        ).fetchone()
        assert row is not None


async def test_mark_unit_read_and_show_all(fakes):
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        await pilot.press("M")
        await pilot.pause()
        assert home_keys(pilot.app) == []
        await pilot.press("a")
        await pilot.pause()
        keys = home_keys(pilot.app)
        assert "answer:1021" in keys and "comment:2012" not in keys  # filter still unit 1001


async def test_tile_selection_filters_and_toggles(fakes):
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        all_keys = set(home_keys(pilot.app))
        await pilot.press("2")
        await pilot.pause()
        only = set(home_keys(pilot.app))
        assert only == {"comment:2012"}  # unit 1002's only unread staff post
        assert pilot.app.screen.query_one("#tile-1002").has_class("selected")
        await pilot.press("2")
        await pilot.pause()
        assert set(home_keys(pilot.app)) == all_keys
        await pilot.press("right")
        await pilot.pause()
        assert pilot.app.screen.unit_filter == 1001
        await pilot.press("left")
        await pilot.pause()
        assert pilot.app.screen.unit_filter is None


async def test_open_in_browser(fakes):
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        await pilot.press("o")
        assert fakes.opened and fakes.opened[0].startswith("https://edstem.org/au/courses/")


async def test_search_then_thread_with_nested_posts(fakes):
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        assert isinstance(pilot.app.screen, SearchScreen)
        for ch in "zip":
            await pilot.press(ch)
        await pilot.pause()
        screen = pilot.app.screen
        assert screen.query_one("#results", DataTable).row_count >= 1
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ThreadScreen)
        posts = list(pilot.app.screen.query(PostWidget))
        depths = {p.post.key: p.post.depth for p in posts}
        assert depths["thread:102"] == 0 and depths["answer:1021"] == 1
        assert depths["comment:1023"] == 3
        assert any("[HELPER]" in p.head_text for p in posts)
        # opening via search focuses the matching post and marks it read
        hit_key = screen.hits[0].post_key
        assert pilot.app.screen.focused.post.key == hit_key
        assert fakes.services.conn.execute(
            "SELECT 1 FROM read_state WHERE post_key=?", (hit_key,)
        ).fetchone()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, SearchScreen)


async def test_enter_opens_thread_from_inbox(fakes):
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ThreadScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, HomeScreen)


async def test_brief_flow_and_followup_has_no_dash_p(fakes):
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        await pilot.press("1", "b")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, BriefScreen)
        names = screen.query_one("#names", OptionList)
        assert names.option_count == 1  # 1001 has category Assignments > A1
        await pilot.press("enter")
        await pilot.pause()
        if screen.stage == "confirm":  # keyword candidates, if any
            await pilot.press("c")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert screen.stage == "result"
        assert fakes.claude_calls and "-p" in fakes.claude_calls[0][0]
        assert any("Submit a single PDF" in ln for ln in screen.lines)
        olist = screen.query_one("#lines", OptionList)
        assert olist.option_count == len(screen.lines)
        olist.highlighted = next(i for i, ln in enumerate(screen.lines) if "edstem.org" in ln)
        await pilot.press("o")
        assert fakes.opened[-1].startswith("https://edstem.org/au/courses/1001/discussion/102")
        olist.highlighted = next(i for i, ln in enumerate(screen.lines) if "Plain bullet" in ln)
        before = len(fakes.opened)
        await pilot.press("o")
        assert len(fakes.opened) == before
        await pilot.press("a")
        assert len(fakes.suspended) == 1
        assert "-p" not in fakes.suspended[0] and fakes.suspended[0][0] == "claude"
        # unchanged input: second build reuses the cache, `f` forces a new claude call
        await pilot.press("f")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert len(fakes.claude_calls) == 2
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, HomeScreen)


async def test_brief_error_is_shown(fakes):
    def boom(argv, prompt):
        raise RuntimeError("claude exploded")

    fakes.services.run_claude = boom
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        await pilot.press("1", "b", "enter")
        await pilot.pause()
        if pilot.app.screen.stage == "confirm":
            await pilot.press("c")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert pilot.app.screen.stage == "error"
        assert "claude exploded" in str(pilot.app.screen.query_one("#error", Static).render())


async def test_expired_banner_and_login(fakes):
    fakes.auth_ok = False
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        banner = pilot.app.screen.query_one("#banner", Static)
        assert banner.display
        assert EXPIRED_BANNER in str(banner.render())
        await pilot.press("l")
        await pilot.pause()
        assert fakes.logins == 1
        assert not pilot.app.screen.query_one("#banner", Static).display


async def test_no_banner_when_logged_in(fakes):
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        assert not pilot.app.screen.query_one("#banner", Static).display
        assert fakes.sync_calls == []


async def test_sync_on_start_and_progress_footer(fakes):
    async with made(fakes, sync_on_start=True).run_test() as pilot:
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert fakes.sync_calls == [[1001, 1002]]
        assert "Synced: 3 new" in str(pilot.app.screen.query_one("#status", Static).render())


async def test_sync_auth_error_shows_banner(fakes):
    fakes.sync_error = AuthError("401")
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert pilot.app.screen.query_one("#banner", Static).display


async def test_setup_wizard_first_run(tconn):
    from edscrape.store import db

    seed_store(tconn)  # supplies courses for the fake list_courses
    f = Fakes(tconn, unit_ids=())
    async with made(f).run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, SetupScreen)  # no units -> wizard
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert screen.stage == "units"  # session already ok -> login step skipped
        sel = screen.query_one("#courses", SelectionList)
        assert sel.option_count == 2
        await pilot.press("c")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert f.sync_calls and sorted(f.sync_calls[0]) == [1001, 1002]
        assert isinstance(pilot.app.screen, HomeScreen)
        assert sorted(f.services.config.unit_ids) == [1001, 1002]
        assert db.course_by_id(tconn, 1001).selected


async def test_setup_wizard_login_step(tconn):
    seed_store(tconn)
    f = Fakes(tconn, auth_ok=False, unit_ids=())
    async with made(f).run_test() as pilot:
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        screen = pilot.app.screen
        assert screen.stage == "login"
        await pilot.press("enter")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert f.logins == 1 and screen.stage == "units"


def test_ed_url_extraction():
    assert ed_url("see [x](https://edstem.org/au/courses/1/discussion/2?answer=3).") == (
        "https://edstem.org/au/courses/1/discussion/2?answer=3"
    )
    assert ed_url("https://example.com only") is None
    assert ed_url("https://notedstem.org/x") is None


async def test_esc_during_build_does_not_crash(fakes):
    import threading

    gate = threading.Event()

    def slow(argv, prompt):
        gate.wait(5)
        return BRIEF_MD

    fakes.services.run_claude = slow
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        await pilot.press("1", "b", "enter")
        await pilot.pause()
        if pilot.app.screen.stage == "confirm":
            await pilot.press("c")
        await pilot.pause()
        assert pilot.app.screen.stage == "loading"
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, HomeScreen)
        gate.set()
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(pilot.app.screen, HomeScreen)


async def test_markup_in_titles_and_errors_is_literal(fakes):
    conn = fakes.services.conn
    conn.execute("UPDATE threads SET title = '[/] oops [bold' WHERE id = 102")
    conn.commit()
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        pilot.app.screen.set_status("Sync failed: [/] bad")
        await pilot.pause()
        await pilot.press("slash")
        for ch in "zip":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ThreadScreen)
        assert "[/] oops" in str(pilot.app.screen.query_one("#thread-head", Static).render())


async def test_open_link_for_answer_and_comment(fakes):
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        pilot.app.push_screen(ThreadScreen(fakes.services, 102))
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ThreadScreen)
        from edscrape import links

        for key in ("answer:1021", "comment:1023"):
            widget = next(w for w in screen.query(PostWidget) if w.post.key == key)
            widget.focus()
            await pilot.pause()
            await pilot.press("o")
            ref = int(key.split(":")[1])
            expected = (
                links.answer_url(1001, 102, ref)
                if key.startswith("answer")
                else links.comment_url(1001, 102, ref)
            )
            assert fakes.opened[-1] == expected


async def test_listing_and_sync_mark_nothing_read(fakes):
    conn = fakes.services.conn
    async with made(fakes, sync_on_start=True).run_test() as pilot:
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("r")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert fakes.sync_calls
        assert conn.execute("SELECT COUNT(*) FROM read_state").fetchone()[0] == 0


async def test_sync_partial_and_failed_footer(fakes):
    from edscrape.tui.app import EdScrapeApp as A

    rep = SyncReport(
        runs=[{"error": "404 on thread 9"}, {"error": None}],
        threads_seen=1, threads_changed=1, posts_new=0, posts_changed=0, status="partial",
    )  # fmt: skip
    assert A._report_text(rep) == "Sync partial: 1 errors — 404 on thread 9"
    rep.status = "error"
    assert A._report_text(rep) == "Sync failed: 404 on thread 9"


async def test_brief_uses_own_connection_when_provided(fakes):
    opened = []

    def open_conn():
        c = fakes.services.conn  # same in-memory db; a real app opens a fresh file connection
        opened.append(c)
        return _NoClose(c)

    fakes.services.open_conn = open_conn
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        await pilot.press("1", "b", "enter")
        await pilot.pause()
        if pilot.app.screen.stage == "confirm":
            await pilot.press("c")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        assert opened and pilot.app.screen.stage == "result"


class _NoClose:
    """Proxy so the worker's close() does not close the shared test connection."""

    def __init__(self, conn):
        self._c = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._c, name)


async def test_thread_navigation_moves_focus_and_marks_read(fakes):
    conn = fakes.services.conn
    async with made(fakes).run_test() as pilot:
        await pilot.pause()
        pilot.app.push_screen(ThreadScreen(fakes.services, 102))
        await pilot.pause()
        screen = pilot.app.screen
        order = [w.post.key for w in screen.query(PostWidget)]
        assert len(order) == 5 and screen.focused.post.key == order[0]
        for expected in order[1:3]:
            await pilot.press("down")
            await pilot.pause()
            assert screen.focused.post.key == expected
            assert conn.execute("SELECT 1 FROM read_state WHERE post_key=?", (expected,)).fetchone()
        await pilot.press("k")
        await pilot.pause()
        assert screen.focused.post.key == order[1]
