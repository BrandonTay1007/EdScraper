"""Typer CLI. No subcommand starts the TUI; the maintenance subcommands are hidden."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.prompt import Prompt

from edscrape import auth
from edscrape import config as config_mod
from edscrape import sync as sync_mod
from edscrape.auth import AuthError
from edscrape.core import assignments, brief, export, inbox, search, units
from edscrape.models import Candidates, Course, InboxItem, SyncEvent
from edscrape.store import db

EXIT_USAGE = 2
EXIT_AUTH = 3

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="Surface Ed Discussion teaching-team messages.",
)
out = Console(highlight=False, soft_wrap=True)
err = Console(stderr=True, highlight=False, soft_wrap=True)

UnitOpt = Annotated[list[str] | None, typer.Option("--unit", "-u", help="Unit code prefix.")]


# -- helpers ---------------------------------------------------------------------------------


def _open() -> sqlite3.Connection:
    conn = db.connect()
    db.migrate(conn)
    return conn


def _is_tty() -> bool:
    return sys.stdin.isatty()


def _fail(msg: str, code: int = 1) -> typer.Exit:
    err.print(msg, markup=False)
    return typer.Exit(code)


def _auth_failed(reason: str | None = None) -> typer.Exit:
    detail = f" ({reason})" if reason else ""
    if not _is_tty():
        return _fail(f"Not logged in{detail}. Run `edscrape login` interactively.", EXIT_AUTH)
    return _fail(f"Not logged in or the session expired{detail}. Run `edscrape login`.", 1)


def _resolve_units(conn: sqlite3.Connection, queries: list[str]) -> list[Course]:
    courses = db.courses(conn)
    try:
        return [units.resolve(q, courses) for q in queries]
    except units.AmbiguousUnit as e:
        raise _fail(f"{e}", EXIT_USAGE) from e
    except units.UnknownUnit as e:
        hint = "Run `edscrape units --pick` or `edscrape pull` first."
        raise _fail(f"{e}. {hint}", EXIT_USAGE) from e


def _scope(conn: sqlite3.Connection, queries: list[str] | None, cfg) -> list[int] | None:
    if queries:
        return [c.id for c in _resolve_units(conn, queries)]
    return list(cfg.unit_ids) or None


def _flag_text(it: InboxItem) -> str:
    f = it.flags
    marks = []
    if it.post.is_own:
        marks.append("own")
    for name in ("edited", "later_staff_reply", "accepted", "endorsed", "pinned", "announcement"):
        if getattr(f, name):
            marks.append(name.replace("_", " "))
    if f.new:
        marks.append("new")
    return ", ".join(marks)


async def _fetch_user_courses() -> list[tuple[Course, bool]]:
    """GET /api/user -> (course, has_discussion). GET only."""
    from edscrape.api import EdClient

    async with EdClient(auth.load_token()) as client:
        payload = await client.get_user()
    result = []
    for entry in payload.get("courses") or []:
        course = sync_mod.course_from_entry(entry)
        features = (entry.get("course") or {}).get("features")
        result.append((course, True if features is None else bool(features.get("discussion"))))
    return result


# -- root ------------------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        from edscrape.tui.app import run  # lazy: the TUI is optional at import time

        run()


@app.command(hidden=True)
def login(check: Annotated[bool, typer.Option("--check", help="Only verify the token.")] = False):
    """Log in through a browser, or verify the stored token."""
    if check:
        status = asyncio.run(auth.check())
        if status.ok:
            out.print(f"Logged in as {status.name or status.user_id}", markup=False)
            return
        raise _auth_failed(status.reason)
    status = auth.login()
    if not status.ok:
        raise _fail(f"Login failed: {status.reason}", 1)
    out.print("Login saved.", markup=False)


@app.command("units", hidden=True)
def units_cmd(pick: Annotated[bool, typer.Option("--pick", help="Choose units to sync.")] = False):
    """List known units, or pick the ones to sync."""
    conn = _open()
    cfg = config_mod.load()
    if not pick:
        for c in db.courses(conn):
            mark = "*" if c.id in cfg.unit_ids else " "
            out.print(f"{mark} {c.id:>7}  {c.code}  [{c.status}]  {c.role}", markup=False)
        return
    try:
        fetched = asyncio.run(_fetch_user_courses())
    except AuthError as e:
        raise _auth_failed(str(e)) from e
    for course, _ in fetched:
        db.upsert_course(conn, course)
    conn.commit()
    eligible = [c for c, disc in fetched if c.status == "active" and disc]
    if not eligible:
        raise _fail("No active units with a discussion board were found.")
    chosen = _pick(eligible, set(cfg.unit_ids))
    cfg.unit_ids = chosen
    config_mod.save(cfg)
    for c in db.courses(conn):
        db.set_course_selected(conn, c.id, c.id in chosen)
    conn.commit()
    out.print(f"Saved {len(chosen)} unit(s).", markup=False)


def _pick(courses: list[Course], current: set[int]) -> list[int]:
    for i, c in enumerate(courses, 1):
        mark = "x" if c.id in current else " "
        out.print(f"[{mark}] {i:>2}  {c.code.strip()}  ({c.role})", markup=False)
    default = ",".join(str(i) for i, c in enumerate(courses, 1) if c.id in current) or "all"
    answer = Prompt.ask("Units to sync (numbers, 'all' or 'none')", default=default, console=out)
    answer = answer.strip().lower()
    if answer == "all":
        return [c.id for c in courses]
    if answer in ("none", ""):
        return []
    picked = set()
    for tok in answer.replace(",", " ").split():
        if not tok.isdigit() or not 1 <= int(tok) <= len(courses):
            raise _fail(f"Invalid choice {tok!r}", EXIT_USAGE)
        picked.add(int(tok))
    return [c.id for i, c in enumerate(courses, 1) if i in picked]


@app.command(hidden=True)
def pull(
    unit: UnitOpt = None,
    full: Annotated[bool, typer.Option("--full", help="Re-fetch every thread.")] = False,
):
    """Sync units from Ed (GET only)."""
    conn = _open()
    cfg = config_mod.load()
    if unit:
        if not db.courses(conn):
            try:
                for course, _ in asyncio.run(_fetch_user_courses()):
                    db.upsert_course(conn, course)
                conn.commit()
            except AuthError as e:
                raise _auth_failed(str(e)) from e
        ids = [c.id for c in _resolve_units(conn, unit)]
    else:
        ids = list(cfg.unit_ids)
    if not ids:
        raise _fail("No units selected. Run `edscrape units --pick` or pass --unit.", EXIT_USAGE)

    stages: dict[int, str] = {}

    def progress(ev: SyncEvent) -> None:
        course = db.course_by_id(conn, ev.course_id)
        label = course.code.strip() if course else str(ev.course_id)
        if ev.stage in ("done", "error") or stages.get(ev.course_id) != ev.stage:
            total = f"/{ev.total}" if ev.total is not None else ""
            msg = f" {ev.message}" if ev.message else ""
            out.print(f"{label}: {ev.stage} {ev.done}{total}{msg}", markup=False)
        stages[ev.course_id] = ev.stage

    try:
        report = asyncio.run(sync_mod.sync(ids, progress, full=full, config=cfg, conn=conn))
    except AuthError as e:
        raise _auth_failed(str(e)) from e
    out.print(
        f"Sync {report.status}: {report.threads_seen} threads seen, "
        f"{report.threads_changed} changed, {report.posts_new} new posts, "
        f"{report.posts_changed} changed posts.",
        markup=False,
    )
    if report.status == "error":
        raise typer.Exit(1)


@app.command(hidden=True)
def reparse():
    """Rebuild posts from archived raw files (no network)."""
    conn = _open()
    cfg = config_mod.load()
    ids = list(cfg.unit_ids) or [c.id for c in db.courses(conn) if c.selected]
    if not ids:
        raise _fail("No units selected.", EXIT_USAGE)
    sync_mod.reparse(ids, config=cfg, conn=conn)
    out.print(f"Reparsed {len(ids)} unit(s).", markup=False)


@app.command("inbox", hidden=True)
def inbox_cmd(
    all_: Annotated[bool, typer.Option("--all", help="Include read messages.")] = False,
    unit: UnitOpt = None,
):
    """Print teaching-team messages."""
    conn = _open()
    cfg = config_mod.load()
    items = inbox.list(conn, _scope(conn, unit, cfg), unread_only=not all_)
    if not items:
        out.print("No messages.", markup=False)
    for it in items:
        p = it.post
        flags = _flag_text(it)
        out.print(
            f"{it.course_code} | {p.role_class.value} | {p.author_name} | "
            f"{p.created_at} | {it.thread.title}" + (f" | {flags}" if flags else ""),
            markup=False,
        )
        if it.parent_excerpt:
            out.print(f"    re: {it.parent_excerpt}", markup=False)
        out.print(f"    {' '.join(p.text.split())[:200]}", markup=False)
        out.print(f"    {it.url}", markup=False)


@app.command("search", hidden=True)
def search_cmd(query: str, unit: UnitOpt = None):
    """Full-text search."""
    conn = _open()
    hits = search.query(conn, query, _scope(conn, unit, config_mod.load()))
    if not hits:
        out.print("No matches.", markup=False)
    for h in hits:
        out.print(f"{h.thread.title} | {h.post_key}", markup=False)
        out.print(f"    {' '.join(h.snippet.split())}", markup=False)
        out.print(f"    {h.url}", markup=False)


@app.command("export", hidden=True)
def export_cmd(
    unit: str,
    fmt: Annotated[str, typer.Option("--fmt", help="md, csv or jsonl.")] = "md",
    out_path: Annotated[Path | None, typer.Option("--out", help="Output file.")] = None,
):
    """Export a unit's threads."""
    conn = _open()
    course = _resolve_units(conn, [unit])[0]
    target = out_path or Path(f"{brief.slug(course.code)}.{fmt}")
    try:
        export.write(conn, course.id, fmt, target)
    except export.ExportError as e:
        raise _fail(str(e), EXIT_USAGE) from e
    out.print(f"Wrote {target}", markup=False)


class _WarnCollector(logging.Handler):
    """Collects warnings from core.brief so the CLI can print them."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _confirm_keywords(
    cand: Candidates, conn: sqlite3.Connection, cfg: config_mod.Config, course_id: int, name: str
) -> None:
    """Ask which keyword-only threads belong to the assignment, then save the choice.

    Plain Enter saves nothing (asked again next time); an explicit answer, including "none",
    keeps the chosen threads and excludes the unselected ones.
    """
    if not cand.needs_confirm or not _is_tty():
        return
    out.print("Threads that mention the assignment but are not in its category:", markup=False)
    for i, t in enumerate(cand.needs_confirm, 1):
        out.print(f"  {i:>2}. #{t.number} {t.title}", markup=False)
    answer = Prompt.ask(
        "Include which? (numbers, 'all', 'none', or Enter to decide later)",
        default="",
        show_default=False,
        console=out,
    )
    answer = answer.strip().lower()
    if not answer:
        return
    if answer == "all":
        chosen = {t.id for t in cand.needs_confirm}
    else:
        nums = {int(x) for x in answer.replace(",", " ").split() if x.isdigit()}
        chosen = {t.id for i, t in enumerate(cand.needs_confirm, 1) if i in nums}
    rest = {t.id for t in cand.needs_confirm} - chosen
    assignments.save_choice(cfg, course_id, name, [*cand.kept, *chosen], [*cand.excluded, *rest])


@app.command("brief", hidden=True)
def brief_cmd(
    unit: str,
    name: str,
    model: Annotated[str | None, typer.Option("--model")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
):
    """Generate (or reuse) the cached Claude brief for an assignment."""
    conn = _open()
    cfg = config_mod.load()
    course = _resolve_units(conn, [unit])[0]
    cand = assignments.candidates(conn, cfg, course.id, name)
    out.print(
        f"{len(cand.auto)} thread(s) in category {name!r}, {len(cand.kept)} kept.", markup=False
    )
    _confirm_keywords(cand, conn, cfg, course.id, name)
    notes = _WarnCollector()
    logging.getLogger(brief.__name__).addHandler(notes)
    try:
        result = brief.build(conn, cfg, course.id, name, model=model, force=force)
    except brief.BriefError as e:
        raise _fail(f"Brief failed: {e}") from e
    finally:
        logging.getLogger(brief.__name__).removeHandler(notes)
    for msg in notes.messages:
        err.print(msg, markup=False)
    state = "generated" if result.fresh else "unchanged, reusing cached brief"
    out.print(f"{result.path} ({state}, model {result.model})", markup=False)
    out.print(result.path.read_text(encoding="utf-8"), markup=False)


def main() -> None:
    app()
