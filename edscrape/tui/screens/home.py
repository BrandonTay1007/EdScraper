from __future__ import annotations

import asyncio

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from edscrape.core import inbox, readstate
from edscrape.models import Course, InboxItem
from edscrape.tui.queries import age, excerpt, flag_glyphs, unit_label, unit_stats
from edscrape.tui.services import Services
from edscrape.tui.widgets.tile import UnitTile

COLUMNS = ("Unit", "Where", "Role", "Author", "Message", "Flags", "Age")


class HomeScreen(Screen):
    BINDINGS = [
        Binding("o", "open", "Open in Ed"),
        Binding("m", "mark", "Mark read"),
        Binding("M", "mark_unit", "Unit read"),
        Binding("b", "brief", "Brief"),
        Binding("slash", "search", "Search"),
        Binding("u", "units", "Units"),
        Binding("r", "sync", "Sync"),
        Binding("a", "toggle_all", "All/unread"),
        Binding("l", "login", "Log in"),
        Binding("left", "tile_prev", "Prev unit", show=False, priority=True),
        Binding("right", "tile_next", "Next unit", show=False, priority=True),
        *[Binding(str(n), f"tile({n})", show=False) for n in range(1, 10)],
        Binding("0", "tile(0)", show=False),
    ]

    def __init__(self, services: Services) -> None:
        super().__init__()
        self.services = services
        self.items: list[InboxItem] = []
        self.courses: list[Course] = []
        self.unit_filter: int | None = None
        self.show_all = False
        self._refresh_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        banner = Static("", id="banner")
        banner.display = False
        yield banner
        yield Horizontal(id="tiles")
        table = DataTable(id="inbox", cursor_type="row", zebra_stripes=True)
        table.add_columns(*COLUMNS)
        yield table
        yield Static("Nothing to show.", id="empty")
        yield Static("", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#inbox", DataTable).focus()
        await self.refresh_data()

    async def on_screen_resume(self) -> None:
        await self.refresh_data()

    # -- state ------------------------------------------------------------------------------

    def set_banner(self, text: str | None) -> None:
        banner = self.query_one("#banner", Static)
        banner.display = bool(text)
        banner.update(Text(text or ""))

    def set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(Text(text))

    def current_item(self) -> InboxItem | None:
        table = self.query_one("#inbox", DataTable)
        if table.row_count == 0 or not 0 <= table.cursor_row < len(self.items):
            return None
        return self.items[table.cursor_row]

    def _load_courses(self) -> list[Course]:
        found = (db_course(self.services, cid) for cid in self.services.config.unit_ids)
        return [c for c in found if c is not None]

    async def refresh_data(self) -> None:
        async with self._refresh_lock:
            await self._refresh()

    async def _refresh(self) -> None:
        table = self.query_one("#inbox", DataTable)
        keep_key = None
        keep_row = table.cursor_row
        if self.items and 0 <= keep_row < len(self.items):
            keep_key = self.items[keep_row].post.key

        self.courses = self._load_courses()
        if self.unit_filter not in {c.id for c in self.courses}:
            self.unit_filter = None
        conn = self.services.conn
        tiles = self.query_one("#tiles", Horizontal)
        await tiles.remove_children()
        await tiles.mount(
            *[
                UnitTile(
                    i,
                    c,
                    unit_stats(conn, c),
                    id=f"tile-{c.id}",
                    classes="selected" if c.id == self.unit_filter else "",
                )
                for i, c in enumerate(self.courses, 1)
            ]
        )

        ids = [self.unit_filter] if self.unit_filter else [c.id for c in self.courses]
        self.items = inbox.list(conn, ids, unread_only=not self.show_all) if ids else []
        table.clear()
        for it in self.items:
            table.add_row(*self._row(it), key=it.post.key)
        self.query_one("#empty").display = not self.items
        if self.items:
            row = next(
                (i for i, it in enumerate(self.items) if it.post.key == keep_key),
                min(keep_row, len(self.items) - 1),
            )
            table.move_cursor(row=max(row, 0))

    def _row(self, it: InboxItem) -> list[Text]:
        p, t = it.post, it.thread
        bold = "bold" if it.flags.new else ""
        role = p.role_class.value + (" · own" if p.is_own else "")
        where = " / ".join(x for x in (t.category, t.subcategory, t.subsubcategory) if x)
        cells = [
            unit_label(it.course_code),
            where,
            role,
            p.author_name or "Anonymous",
            excerpt(p.text),
            flag_glyphs(it.flags),
            age(p.created_at),
        ]
        return [Text(c, style=bold) for c in cells]

    # -- actions ----------------------------------------------------------------------------

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        item = self.current_item()
        if item is not None:
            from edscrape.tui.screens.thread import ThreadScreen

            self.app.push_screen(ThreadScreen(self.services, item.thread.id, item.post.key))

    def action_open(self) -> None:
        item = self.current_item()
        if item is not None:
            self.services.open_url(item.url)

    async def action_mark(self) -> None:
        item = self.current_item()
        if item is not None:
            readstate.mark(self.services.conn, [item.post.key])
            await self.refresh_data()

    async def action_mark_unit(self) -> None:
        item = self.current_item()
        cid = self.unit_filter or (item.post.course_id if item else None)
        if cid is not None:
            readstate.mark_course(self.services.conn, cid)
            await self.refresh_data()

    async def action_toggle_all(self) -> None:
        self.show_all = not self.show_all
        self.notify("Showing all messages" if self.show_all else "Showing unread only")
        await self.refresh_data()

    async def _select_unit(self, course_id: int | None) -> None:
        self.unit_filter = course_id
        await self.refresh_data()

    async def action_tile(self, n: int) -> None:
        if n == 0 or n > len(self.courses):
            await self._select_unit(None)
            return
        cid = self.courses[n - 1].id
        await self._select_unit(None if self.unit_filter == cid else cid)

    async def _step_tile(self, step: int) -> None:
        order: list[int | None] = [None, *[c.id for c in self.courses]]
        i = order.index(self.unit_filter) if self.unit_filter in order else 0
        await self._select_unit(order[(i + step) % len(order)])

    async def action_tile_prev(self) -> None:
        await self._step_tile(-1)

    async def action_tile_next(self) -> None:
        await self._step_tile(1)

    async def on_unit_tile_clicked(self, event: UnitTile.Clicked) -> None:
        await self._select_unit(None if self.unit_filter == event.course_id else event.course_id)

    def action_search(self) -> None:
        from edscrape.tui.screens.search import SearchScreen

        self.app.push_screen(SearchScreen(self.services, self.unit_filter))

    def action_units(self) -> None:
        from edscrape.tui.screens.setup import SetupScreen

        self.app.push_screen(SetupScreen(self.services, mode="units"))

    def action_brief(self) -> None:
        from edscrape.tui.screens.brief import BriefScreen

        item = self.current_item()
        cid = self.unit_filter or (item.post.course_id if item else None)
        self.app.push_screen(BriefScreen(self.services, cid))

    def action_sync(self) -> None:
        ids = list(self.services.config.unit_ids)
        if not ids:
            self.notify("No units selected; press u", severity="warning")
        elif self.app.syncing:  # type: ignore[attr-defined]
            self.notify("Sync already running")
        else:
            self.app.run_worker(self.app.sync_units(ids), group="sync")  # type: ignore[attr-defined]

    async def action_login(self) -> None:
        await self.app.login_flow()  # type: ignore[attr-defined]


def db_course(services: Services, course_id: int) -> Course | None:
    from edscrape.store import db

    return db.course_by_id(services.conn, course_id)
