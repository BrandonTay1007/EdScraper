from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Input, Static

from edscrape.core import search
from edscrape.models import SearchHit
from edscrape.tui.queries import age, excerpt, unit_label
from edscrape.tui.services import Services


class SearchScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("down", "to_results", "Results", show=False),
    ]

    def __init__(self, services: Services, course_id: int | None = None) -> None:
        super().__init__()
        self.services = services
        self.course_id = course_id
        self.hits: list[SearchHit] = []

    def compose(self) -> ComposeResult:
        yield Static("Search (all selected units)" if not self.course_id else "Search (one unit)",
                     classes="title")
        yield Input(placeholder="type to search, enter opens the first result", id="query")
        yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#results", DataTable).add_columns("Unit", "Thread", "Match", "Age")
        self.query_one("#query", Input).focus()

    def _scope(self) -> list[int] | None:
        if self.course_id:
            return [self.course_id]
        return list(self.services.config.unit_ids) or None

    def on_input_changed(self, event: Input.Changed) -> None:
        self.hits = search.query(self.services.conn, event.value, self._scope())
        table = self.query_one("#results", DataTable)
        table.clear()
        for i, h in enumerate(self.hits):
            course = self.services.conn.execute(
                "SELECT code FROM courses WHERE id = ?", (h.thread.course_id,)
            ).fetchone()
            table.add_row(
                unit_label(course["code"]) if course else "",
                Text(excerpt(h.thread.title, 50)),
                Text(excerpt(h.snippet, 70)),
                age(h.thread.updated_at),
                key=str(i),
            )

    def _open(self, index: int) -> None:
        if 0 <= index < len(self.hits):
            from edscrape.tui.screens.thread import ThreadScreen

            hit = self.hits[index]
            self.app.push_screen(ThreadScreen(self.services, hit.thread.id, hit.post_key))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._open(self.query_one("#results", DataTable).cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._open(int(str(event.row_key.value)))

    def action_to_results(self) -> None:
        if self.hits:
            self.query_one("#results", DataTable).focus()

    def action_back(self) -> None:
        self.app.pop_screen()
