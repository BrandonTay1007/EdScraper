"""EdScrape Textual app: wires screens to an injected `Services`."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from textual.app import App, SuspendNotSupported
from textual.binding import Binding

from edscrape.auth import AuthError
from edscrape.models import AuthStatus, SyncEvent, SyncReport
from edscrape.store import db
from edscrape.tui.queries import unit_label
from edscrape.tui.services import Services, default_services

if TYPE_CHECKING:
    from edscrape.tui.screens.home import HomeScreen

EXPIRED_BANNER = "Session expired — press l to log in"


class EdScrapeApp(App):
    CSS_PATH = "edscrape.tcss"
    TITLE = "EdScrape"
    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, services: Services, *, sync_on_start: bool = True) -> None:
        super().__init__()
        self.services = services
        self.sync_on_start = sync_on_start
        self.home: HomeScreen | None = None
        self.auth_ok: bool | None = None
        self.status_text = ""
        self.syncing = False

    # -- lifecycle -------------------------------------------------------------------------

    def on_mount(self) -> None:
        from edscrape.tui.screens.home import HomeScreen
        from edscrape.tui.screens.setup import SetupScreen

        self.home = HomeScreen(self.services)
        self.push_screen(self.home)
        if not self.services.config.unit_ids:
            self.push_screen(SetupScreen(self.services))
        else:
            self.run_worker(self._startup(), group="startup", exclusive=True)

    async def _startup(self) -> None:
        status = await self.check_auth()
        if status.ok and self.sync_on_start:
            await self.sync_units(list(self.services.config.unit_ids))

    # -- helpers used by screens -----------------------------------------------------------

    def run_suspended(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run blocking `fn` with the terminal handed over (login, interactive claude).

        Where suspending is unsupported (headless test driver) `fn` just runs in place.
        """
        try:
            with self.suspend():
                return fn(*args)
        except SuspendNotSupported:
            return fn(*args)

    def set_status(self, text: str) -> None:
        self.status_text = text
        if self.home is not None:
            self.home.set_status(text)

    def set_banner(self, text: str | None) -> None:
        if self.home is not None:
            self.home.set_banner(text)

    async def check_auth(self) -> AuthStatus:
        try:
            status = await self.services.auth_check()
        except Exception as e:  # network down etc.: treat as not verified
            status = AuthStatus(ok=False, user_id=None, name=None, reason=str(e))
        self.auth_ok = status.ok
        self.set_banner(None if status.ok else EXPIRED_BANNER)
        return status

    async def login_flow(self) -> None:
        try:
            status = self.run_suspended(self.services.login)
        except Exception as e:
            self.notify(f"Login failed: {e}", severity="error")
            return
        if not status.ok:
            self.notify(f"Login failed: {status.reason or 'unknown reason'}", severity="error")
        status = await self.check_auth()
        if status.ok and self.sync_on_start and self.services.config.unit_ids:
            await self.sync_units(list(self.services.config.unit_ids))

    def sync_progress_text(self, ev: SyncEvent) -> str:
        course = db.course_by_id(self.services.conn, ev.course_id)
        label = unit_label(course.code) if course else str(ev.course_id)
        total = f"/{ev.total}" if ev.total is not None else ""
        return f"{label}: {ev.stage} {ev.done}{total}"

    async def sync_units(
        self, ids: list[int], on_event: Callable[[SyncEvent], None] | None = None
    ) -> SyncReport | None:
        """Sync in the app's event loop (sync is async, no thread). Updates the footer text."""
        if self.syncing or not ids:
            return None
        self.syncing = True
        self.set_status("Syncing...")

        def progress(ev: SyncEvent) -> None:
            self.set_status("Syncing " + self.sync_progress_text(ev))
            if on_event:
                on_event(ev)

        report: SyncReport | None = None
        try:
            report = await self.services.sync(ids, progress)
        except AuthError:
            self.auth_ok = False
            self.set_banner(EXPIRED_BANNER)
            self.set_status("Sync stopped: not logged in")
        except Exception as e:
            self.set_status(f"Sync failed: {e}")
        else:
            self.set_status(
                f"Synced ({report.status}): {report.posts_new} new, "
                f"{report.posts_changed} changed posts"
            )
        finally:
            self.syncing = False
        if self.home is not None:
            await self.home.refresh_data()
        return report


def run() -> None:
    """Entry used by the CLI. EDSCRAPE_NO_SYNC=1 skips the background sync on launch."""
    sync = os.environ.get("EDSCRAPE_NO_SYNC", "") in ("", "0")
    EdScrapeApp(default_services(), sync_on_start=sync).run()
