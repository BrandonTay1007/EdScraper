"""First-run wizard: log in, pick units, first sync. `mode="units"` skips the login step."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, SelectionList, Static

from edscrape import config as config_mod
from edscrape.auth import AuthError
from edscrape.models import Course, SyncEvent
from edscrape.store import db
from edscrape.tui.queries import unit_label
from edscrape.tui.services import Services

STAGES = ("login", "units", "sync")


class SetupScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Cancel"),
        Binding("enter", "primary", "Continue", show=False),
        Binding("c", "primary", "Continue", show=False),
    ]

    def __init__(self, services: Services, mode: str = "first") -> None:
        super().__init__()
        self.services = services
        self.mode = mode
        self.stage = "login"
        self.courses: dict[int, Course] = {}
        self.progress: dict[int, str] = {}
        self.failed = False
        self._all_fetched: list[Course] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="step", classes="title")
        with Vertical(id="login"):
            yield Static("", id="login-msg")
            yield Button("Open browser to log in", id="login-btn", variant="primary")
        with Vertical(id="units"):
            yield Static("", id="units-msg", classes="hint")
            yield SelectionList[int](id="courses")
            yield Button("Use these units", id="units-btn", variant="primary")
        with Vertical(id="sync"):
            yield Static("", id="progress")
        yield Footer()

    async def on_mount(self) -> None:
        if self.mode == "units":
            await self._to_units()
            return
        self._stage("login", "Step 1 of 3: log in to Ed")
        self.query_one("#login-msg", Static).update("Checking for an existing session...")
        self.run_worker(self._check_login(), group="setup", exclusive=True)

    def _stage(self, stage: str, title: str) -> None:
        self.stage = stage
        self.query_one("#step", Static).update(title)
        for s in STAGES:
            self.query_one(f"#{s}").display = s == stage

    # -- step 1 -----------------------------------------------------------------------------

    async def _check_login(self) -> None:
        try:
            status = await self.services.auth_check()
        except Exception:
            status = None
        if status is not None and status.ok:
            await self._to_units()
        else:
            self.query_one("#login-msg", Static).update(
                "A browser window will open. Log in to Ed, then return here.\n"
                "Press enter to start."
            )

    async def _do_login(self) -> None:
        try:
            status = self.app.run_suspended(self.services.login)  # type: ignore[attr-defined]
        except Exception as e:
            self.query_one("#login-msg", Static).update(f"Login failed: {e}. Enter to retry.")
            return
        if status.ok:
            await self._to_units()
        else:
            reason = status.reason or "unknown reason"
            self.query_one("#login-msg", Static).update(f"Login failed: {reason}. Enter to retry.")

    # -- step 2 -----------------------------------------------------------------------------

    async def _to_units(self) -> None:
        title = "Step 2 of 3: choose units" if self.mode == "first" else "Choose units"
        self._stage("units", title)
        msg = self.query_one("#units-msg", Static)
        if self.services.list_courses is None:
            msg.update("This build cannot list courses.")
            return
        msg.update("Loading your courses...")
        try:
            fetched = await self.services.list_courses()
        except AuthError:
            self._stage("login", "Step 1 of 3: log in to Ed")
            self.query_one("#login-msg", Static).update("Session expired. Press enter to log in.")
            return
        except Exception as e:
            msg.update(f"Could not load courses: {e}")
            return
        eligible = [c for c, discussion in fetched if c.status == "active" and discussion]
        self.courses = {c.id: c for c in eligible}
        self._all_fetched = [c for c, _ in fetched]
        if not eligible:
            msg.update("No active units with a discussion board were found.")
            return
        current = set(self.services.config.unit_ids) or set(self.courses)
        sel = self.query_one("#courses", SelectionList)
        sel.clear_options()
        for c in eligible:
            sel.add_option((f"{unit_label(c.code)}  {c.name}  ({c.role})", c.id, c.id in current))
        msg.update("space toggles a unit; c (or the button) continues")
        sel.focus()

    async def _save_units(self) -> None:
        chosen = list(self.query_one("#courses", SelectionList).selected)
        if not chosen:
            self.notify("Select at least one unit", severity="warning")
            return
        conn, cfg = self.services.conn, self.services.config
        cfg.unit_ids = chosen
        config_mod.save(cfg)
        for c in self._all_fetched:
            db.upsert_course(conn, c)
        for c in db.courses(conn):
            db.set_course_selected(conn, c.id, c.id in chosen)
        conn.commit()
        await self._to_sync(chosen)

    # -- step 3 -----------------------------------------------------------------------------

    async def _to_sync(self, ids: list[int]) -> None:
        self._stage("sync", "Step 3 of 3: first sync" if self.mode == "first" else "Syncing")
        self.progress = {cid: "waiting" for cid in ids}
        self._render_progress()
        self.run_worker(self._run_sync(ids), group="setup", exclusive=True)

    def _render_progress(self) -> None:
        lines = []
        for cid, text in self.progress.items():
            c = db.course_by_id(self.services.conn, cid)
            lines.append(f"{unit_label(c.code) if c else cid}: {text}")
        self.query_one("#progress", Static).update("\n".join(lines))

    def _on_event(self, ev: SyncEvent) -> None:
        total = f"/{ev.total}" if ev.total is not None else ""
        self.progress[ev.course_id] = f"{ev.stage} {ev.done}{total}"
        self._render_progress()

    async def _run_sync(self, ids: list[int]) -> None:
        app = self.app
        report = await app.sync_units(ids, self._on_event)  # type: ignore[attr-defined]
        if report is None:
            self.failed = True
            self.query_one("#progress", Static).update(
                app.status_text + "\n\nPress enter to continue."  # type: ignore[attr-defined]
            )
        else:
            app.pop_screen()

    # -- keys -------------------------------------------------------------------------------

    async def action_primary(self) -> None:
        if self.stage == "login":
            await self._do_login()
        elif self.stage == "units":
            await self._save_units()
        elif self.failed:
            self.app.pop_screen()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        await self.action_primary()

    def action_back(self) -> None:
        if self.mode == "units" or self.services.config.unit_ids:
            self.app.pop_screen()
