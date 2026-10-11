from __future__ import annotations

from textual.message import Message
from textual.widgets import Static

from edscrape.models import Course
from edscrape.tui.queries import GLYPH_LATER, unit_label


class UnitTile(Static):
    """One dashboard tile per selected unit; clicking toggles the inbox filter."""

    class Clicked(Message):
        def __init__(self, course_id: int) -> None:
            super().__init__()
            self.course_id = course_id

    def __init__(self, index: int, course: Course, stats: dict[str, int], **kw) -> None:
        super().__init__(**kw)
        self.course_id = course.id
        self.stats = stats
        self.update(
            f"[b]{index}  {unit_label(course.code)}[/b]\n"
            f"{stats['unread']} unread staff/helper\n"
            f"{stats['later']} {GLYPH_LATER} later staff reply\n"
            f"{stats['assignments']} assignments active (7d)"
        )

    def on_click(self) -> None:
        self.post_message(self.Clicked(self.course_id))
