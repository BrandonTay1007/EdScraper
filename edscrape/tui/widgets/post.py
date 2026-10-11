from __future__ import annotations

from rich.console import Group
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual.message import Message
from textual.widgets import Static

from edscrape.models import Flags, Post
from edscrape.tui.queries import age, flag_glyphs

ROLE_STYLE = {"staff": "bold magenta", "helper": "bold cyan", "student": "dim", "unknown": "dim"}
MAX_INDENT = 8


class PostWidget(Static, can_focus=True):
    """One post in a thread. Focusing it posts `Focused` (the screen marks it read)."""

    class Focused(Message):
        def __init__(self, post: Post) -> None:
            super().__init__()
            self.post = post

    def __init__(self, post: Post, flags: Flags, *, is_thread_post: bool = False) -> None:
        super().__init__()
        self.post = post
        self.flags = flags
        self.styles.margin = (0, 0, 1, min(post.depth, MAX_INDENT) * 2)
        head = Text()
        head.append(f"[{post.role_class.value.upper()}]", style=ROLE_STYLE[post.role_class.value])
        if post.is_own:
            head.append(" [own]", style="bold green")
        head.append(f" {post.author_name or 'Anonymous'}", style="bold")
        glyphs = flag_glyphs(flags, thread_level=is_thread_post, accepted=True)
        if glyphs:
            head.append(f"  {glyphs}")
        head.append(f"  {age(post.created_at)}", style="dim")
        if post.is_deleted:
            body: Text | RichMarkdown = Text("[deleted]", style="dim italic")
        else:
            body = RichMarkdown(post.markdown or post.text)
        self.head_text = head.plain
        self.update(Group(head, body))

    def on_focus(self) -> None:
        self.post_message(self.Focused(self.post))
