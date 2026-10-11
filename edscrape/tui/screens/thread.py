from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static

from edscrape.core import inbox, readstate
from edscrape.store import db
from edscrape.tui.queries import age, flag_glyphs, thread_flags, unit_label
from edscrape.tui.services import Services
from edscrape.tui.widgets.post import PostWidget


class PostList(VerticalScroll, can_focus=False):
    pass


class ThreadScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("o", "open", "Open in Ed"),
        Binding("down,j", "focus_next", "Next post", show=False),
        Binding("up,k", "focus_previous", "Prev post", show=False),
    ]

    def __init__(self, services: Services, thread_id: int, focus_key: str | None = None) -> None:
        super().__init__()
        self.services = services
        self.thread_id = thread_id
        self.focus_key = focus_key

    def compose(self) -> ComposeResult:
        yield Static("", id="thread-head")
        yield PostList(id="posts")
        yield Footer()

    async def on_mount(self) -> None:
        conn = self.services.conn
        thread = db.thread_by_id(conn, self.thread_id)
        if thread is None:
            self.query_one("#thread-head", Static).update("Thread not found")
            return
        course = db.course_by_id(conn, thread.course_id)
        flags = thread_flags(conn, thread.id)
        where = " / ".join(x for x in (thread.category, thread.subcategory) if x)
        top = flags.get(f"thread:{thread.id}")
        glyphs = flag_glyphs(top) if top else ""
        self.query_one("#thread-head", Static).update(
            f"[b]{thread.title}[/b] {glyphs}\n"
            f"{unit_label(course.code) if course else thread.course_id} · {where or 'no category'}"
            f" · {thread.type} #{thread.number} · {thread.reply_count} replies"
            f" · started {age(thread.created_at)} ago · updated {age(thread.updated_at)} ago"
        )
        posts = db.posts_by_thread(conn, thread.id)
        widgets = [
            PostWidget(p, flags[p.key], is_thread_post=p.kind == "thread")
            for p in posts
            if p.key in flags
        ]
        await self.query_one("#posts", PostList).mount_all(widgets)
        target = next((w for w in widgets if w.post.key == self.focus_key), None)
        if target is None and widgets:
            target = widgets[0]
        if target is not None:
            target.focus()

    def on_post_widget_focused(self, event: PostWidget.Focused) -> None:
        readstate.mark(self.services.conn, [event.post.key])

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_open(self) -> None:
        w = self.focused
        if isinstance(w, PostWidget):
            self.services.open_url(inbox.post_url(w.post.course_id, w.post.thread_id, w.post.key))
