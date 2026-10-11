"""The seam between the TUI and everything with side effects.

The app only ever reaches Ed, the browser, `claude` and the database through a `Services`
instance, so pilot tests inject fakes. `default_services()` wires the real modules.
"""

from __future__ import annotations

import sqlite3
import subprocess
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from edscrape import auth
from edscrape import config as config_mod
from edscrape import sync as sync_mod
from edscrape.config import Config
from edscrape.models import AuthStatus, Course, SyncEvent, SyncReport
from edscrape.store import db

Progress = Callable[[SyncEvent], None]


@dataclass
class Services:
    # async: GET /api/user; never raises for auth problems (returns ok=False)
    auth_check: Callable[[], Awaitable[AuthStatus]]
    # blocking (browser login); the app calls it inside `App.suspend()`
    login: Callable[[], AuthStatus]
    # async: sync the given course ids, reporting through the progress callback
    sync: Callable[[list[int], Progress | None], Awaitable[SyncReport]]
    open_url: Callable[[str], object]
    # blocking: run an interactive command (the `claude` follow-up) in the real terminal
    suspend_and_run: Callable[[list[str]], object]
    conn: sqlite3.Connection
    config: Config
    # (argv, prompt) -> stdout; None means brief.build's default (`claude -p`)
    run_claude: Callable[[list[str], str], str] | None = None
    # async: (course, has_discussion) for every course in the user's Ed account
    list_courses: Callable[[], Awaitable[list[tuple[Course, bool]]]] | None = None
    # fresh connection for the brief worker thread (closed by the caller); None = share `conn`
    open_conn: Callable[[], sqlite3.Connection] | None = None


async def _list_courses() -> list[tuple[Course, bool]]:
    """GET /api/user -> (course, has_discussion)."""
    from edscrape.api import EdClient

    async with EdClient(auth.load_token()) as client:
        payload = await client.get_user()
    result = []
    for entry in payload.get("courses") or []:
        features = (entry.get("course") or {}).get("features")
        has_discussion = True if features is None else bool(features.get("discussion"))
        result.append((sync_mod.course_from_entry(entry), has_discussion))
    return result


def _run_interactive(argv: list[str]) -> int:
    try:
        return subprocess.run(argv, check=False).returncode
    except FileNotFoundError:
        print(f"command not found: {argv[0]}")
        input("Press enter to return...")
        return 127


def _open_conn() -> sqlite3.Connection:
    conn = db.connect()
    db.migrate(conn)
    return conn


def default_services(config: Config | None = None) -> Services:
    cfg = config or config_mod.load()
    conn = db.connect()
    db.migrate(conn)

    async def _sync(ids: list[int], progress: Progress | None) -> SyncReport:
        return await sync_mod.sync(ids, progress, config=cfg, conn=conn)

    return Services(
        auth_check=lambda: auth.check(),
        login=lambda: auth.login(),
        sync=_sync,
        open_url=webbrowser.open,
        suspend_and_run=_run_interactive,
        conn=conn,
        config=cfg,
        run_claude=None,
        list_courses=_list_courses,
        open_conn=_open_conn,
    )
