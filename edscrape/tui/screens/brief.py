from __future__ import annotations

import re
from urllib.parse import urlparse

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Input,
    LoadingIndicator,
    OptionList,
    SelectionList,
    Static,
)

from edscrape.core import assignments, brief
from edscrape.models import BriefResult, Candidates
from edscrape.store import db
from edscrape.tui.queries import assignment_names, unit_label
from edscrape.tui.services import Services

URL = re.compile(r"https?://[^\s)\]>]+")
STAGES = ("picker", "confirm", "loading", "result", "error")


def ed_url(line: str) -> str | None:
    """First edstem.org URL in `line`, trailing punctuation stripped."""
    for m in URL.finditer(line):
        url = m.group(0).rstrip(".,;:")
        host = urlparse(url).hostname or ""
        if host == "edstem.org" or host.endswith(".edstem.org"):
            return url
    return None


def _render_line(line: str):
    if line.lstrip().startswith("#"):
        return Text(line.lstrip("# ").strip(), style="bold underline")
    return RichMarkdown(line, hyperlinks=False)


class BriefScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("o", "open_link", "Open link"),
        Binding("a", "followup", "Ask Claude"),
        Binding("f", "force", "Rebuild"),
        Binding("c", "confirm", "Continue", show=False),
    ]

    def __init__(self, services: Services, course_id: int | None = None) -> None:
        super().__init__()
        self.services = services
        self.default_course_id = course_id
        self.entries: list[tuple[int, str]] = []
        self.cand: Candidates | None = None
        self.target: tuple[int, str] | None = None
        self.lines: list[str] = []
        self.result: BriefResult | None = None
        self.stage = "picker"

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static("Pick an assignment (enter), or type a name below", classes="title")
            yield OptionList(id="names")
            yield Input(placeholder="other assignment name, enter to use it", id="custom")
        with Vertical(id="confirm"):
            yield Static(
                "These threads mention the assignment but are not in its category. "
                "Space toggles; c or the button continues.",
                id="confirm-title",
                classes="title",
            )
            yield SelectionList[int](id="keywords")
            yield Button("Build brief", id="go", variant="primary")
        with Vertical(id="loading"):
            yield Static("Asking Claude for a brief...", classes="title")
            yield LoadingIndicator()
        with Vertical(id="result"):
            yield Static("", id="brief-meta")
            yield OptionList(id="lines")
        yield Static("", id="error", classes="title")
        yield Footer()

    def on_mount(self) -> None:
        conn = self.services.conn
        cfg = self.services.config
        ids = [self.default_course_id] if self.default_course_id else list(cfg.unit_ids)
        names = self.query_one("#names", OptionList)
        for cid in ids:
            course = db.course_by_id(conn, cid)
            if course is None:
                continue
            for name in assignment_names(course, cfg):
                self.entries.append((cid, name))
                names.add_option(Text(f"{unit_label(course.code)}  ·  {name}"))
        if self.entries:
            names.highlighted = 0
        self._stage("picker")

    def _stage(self, stage: str) -> None:
        self.stage = stage
        for s in STAGES:
            self.query_one(f"#{s}").display = s == stage
        focus = {"picker": "#names", "confirm": "#keywords", "result": "#lines"}.get(stage)
        if focus:
            self.query_one(focus).focus()

    # -- picker -----------------------------------------------------------------------------

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "names":
            self._choose(*self.entries[event.option_index])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        cid = self.default_course_id or (
            self.entries[0][0] if self.entries else next(iter(self.services.config.unit_ids), None)
        )
        if name and cid is not None:
            self._choose(cid, name)

    def _choose(self, course_id: int, name: str) -> None:
        self.target = (course_id, name)
        cand = assignments.candidates(self.services.conn, self.services.config, course_id, name)
        self.cand = cand
        if cand.needs_confirm:
            sel = self.query_one("#keywords", SelectionList)
            sel.clear_options()
            for t in cand.needs_confirm:
                sel.add_option((Text(f"#{t.number} {t.title}"), t.id, False))
            self._stage("confirm")
        else:
            self._build(force=False)

    # -- confirm ----------------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_confirm()

    def action_confirm(self) -> None:
        if self.stage != "confirm" or not self.cand or not self.target:
            return
        chosen = set(self.query_one("#keywords", SelectionList).selected)
        rest = {t.id for t in self.cand.needs_confirm} - chosen
        cid, name = self.target
        assignments.save_choice(
            self.services.config,
            cid,
            name,
            [*self.cand.kept, *chosen],
            [*self.cand.excluded, *rest],
        )
        self._build(force=False)

    # -- build ------------------------------------------------------------------------------

    def _build(self, *, force: bool) -> None:
        self._stage("loading")
        self._build_worker(force)

    @work(thread=True, exclusive=True, group="brief")
    def _build_worker(self, force: bool) -> None:
        assert self.target is not None
        cid, name = self.target
        svc = self.services
        # own connection: sync may be writing on services.conn from the event loop
        conn = svc.open_conn() if svc.open_conn else svc.conn
        try:
            res = brief.build(
                conn,
                self.services.config,
                cid,
                name,
                force=force,
                run_claude=self.services.run_claude,
            )
            text = res.path.read_text(encoding="utf-8")
        except Exception as e:  # BriefError, OSError, anything from the subprocess
            self.app.call_from_thread(self._fail, str(e) or type(e).__name__)
            return
        finally:
            if svc.open_conn:
                conn.close()
        self.app.call_from_thread(self._show, res, text)

    def _fail(self, message: str) -> None:
        if not self.is_attached:  # user left the screen while the worker ran
            return
        self.query_one("#error", Static).update(Text(f"Brief failed: {message}\n\nesc: back"))
        self._stage("error")

    def _show(self, res: BriefResult, text: str) -> None:
        if not self.is_attached:
            return
        self.result = res
        self.lines = [ln for ln in text.splitlines() if ln.strip()]
        olist = self.query_one("#lines", OptionList)
        olist.clear_options()
        for ln in self.lines:
            olist.add_option(_render_line(ln))
        olist.highlighted = 0 if self.lines else None
        state = "just built" if res.fresh else "cached, unchanged"
        name = self.target[1] if self.target else ""
        self.query_one("#brief-meta", Static).update(
            Text(f"{name} · {state} · model {res.model} · o open link · a ask Claude · f rebuild")
        )
        self._stage("result")

    # -- result actions ---------------------------------------------------------------------

    def action_open_link(self) -> None:
        if self.stage != "result":
            return
        idx = self.query_one("#lines", OptionList).highlighted
        url = ed_url(self.lines[idx]) if idx is not None and idx < len(self.lines) else None
        if url:
            self.services.open_url(url)
        else:
            self.notify("No Ed link on this line", severity="warning")

    def action_force(self) -> None:
        if self.stage == "result" and self.target:
            self._build(force=True)

    def action_followup(self) -> None:
        if self.stage != "result" or not self.target:
            return
        cid, name = self.target
        try:
            argv, _ = brief.followup_argv(self.services.conn, self.services.config, cid, name)
            self.app.run_suspended(self.services.suspend_and_run, argv)  # type: ignore[attr-defined]
        except Exception as e:
            self.notify(f"Follow-up failed: {e}", severity="error", markup=False)

    def action_back(self) -> None:
        if self.stage in ("confirm", "result", "error"):
            self._stage("picker")
        else:
            self.app.pop_screen()
