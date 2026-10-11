"""Per-assignment Claude brief: deterministic input assembly, fingerprint cache, `claude -p` call.

`claude` is only ever run through an injectable `run_claude(argv, prompt) -> str`. System prompt
flag used: `--system-prompt-file <path>`; the text is written to `system-prompt.txt` in the
assignment's cache dir (overwritten each run).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from edscrape import paths
from edscrape.config import Config
from edscrape.core import assignments
from edscrape.core.inbox import items_for_keys, post_url
from edscrape.models import BriefResult, InboxItem, Post, RoleClass, Thread
from edscrape.store import db

SYSTEM_PROMPT_FILE = "system-prompt.txt"
TIMEOUT_S = 300
STUDENT_EXCERPT_LEN = 300
TEAM = (RoleClass.staff, RoleClass.helper)

GLYPHS = {
    "edited": "✎",
    "later_staff_reply": "↩",
    "accepted": "✓",
    "endorsed": "★",
    "pinned": "⚑",
    "announcement": "!",
}
GLYPH_LEGEND = (
    "Flag glyphs: ✎ edited, ✓ accepted answer, ★ endorsed, ⚑ pinned thread, ! announcement."
)

RunClaude = Callable[[list[str], str], str]


class BriefError(Exception):
    pass


@dataclass
class _Input:
    text: str
    fingerprint: str
    team_keys: list[str]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def default_run_claude(argv: list[str], prompt: str) -> str:
    try:
        proc = subprocess.run(
            argv, input=prompt, text=True, capture_output=True, timeout=TIMEOUT_S, check=False
        )
    except FileNotFoundError as e:
        raise BriefError("the `claude` CLI was not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise BriefError(f"claude timed out after {TIMEOUT_S}s") from e
    if proc.returncode != 0:
        raise BriefError(f"claude exited {proc.returncode}: {(proc.stderr or '').strip()[-500:]}")
    return proc.stdout


def _parse_ts(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _excerpt(text: str, n: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[: n - 3] + "..."


def _thread_ids(
    conn: sqlite3.Connection, config: Config, course_id: int, name: str
) -> list[Thread]:
    cand = assignments.candidates(conn, config, course_id, name)
    ids = {t.id: t for t in cand.auto if t.id not in cand.excluded}
    for tid in cand.kept:
        t = db.thread_by_id(conn, tid)
        if t is not None and t.course_id == course_id:
            ids[t.id] = t
    return sorted(ids.values(), key=lambda t: (t.number, t.id))


def _flag_rows(conn: sqlite3.Connection, thread_id: int) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT f.* FROM post_flags f JOIN posts p ON p.key = f.post_key WHERE p.thread_id = ?",
        (thread_id,),
    ).fetchall()
    return {r["post_key"]: r for r in rows}


def _fp_row(p: Post, f: sqlite3.Row) -> tuple:
    return (
        p.key,
        p.content_hash,
        int(f["edited"]),
        int(f["later_staff_reply"]),
        int(f["endorsed"]),
    )


def _glyphs(row: sqlite3.Row) -> str:
    return "".join(g for k, g in GLYPHS.items() if row[k])


def assemble_input(conn: sqlite3.Connection, config: Config, course_id: int, name: str) -> _Input:
    course = db.course_by_id(conn, course_id)
    code = course.code.strip() if course else str(course_id)
    parts = [f"# {code} - {name}", "", GLYPH_LEGEND, ""]
    fp_rows: list[tuple] = []
    team_keys: list[str] = []
    for t in _thread_ids(conn, config, course_id, name):
        flags = _flag_rows(conn, t.id)
        posts = db.posts_by_thread(conn, t.id)
        by_key = {p.key: p for p in posts}
        block: list[str] = []
        context_done: set[str] = set()

        for p in posts:
            if p.is_deleted or p.role_class not in TEAM or p.key not in flags:
                continue
            parent: Post | None = by_key.get(p.parent_key) if p.parent_key else None
            if (
                parent is not None
                and parent.role_class not in TEAM
                and not parent.is_deleted
                and parent.key not in context_done
                and parent.key in flags
            ):
                context_done.add(parent.key)
                fp_rows.append(_fp_row(parent, flags[parent.key]))
                block.append(
                    f"> Context (student message, author omitted): "
                    f"{_excerpt(parent.text, STUDENT_EXCERPT_LEN)}"
                )
                block.append("")
            fp_rows.append(_fp_row(p, flags[p.key]))
            team_keys.append(p.key)
            glyphs = _glyphs(flags[p.key])
            head = f"### {glyphs + ' ' if glyphs else ''}{p.author_name} ({p.role_class.value})"
            block += [
                f"{head}, {p.created_at}",
                f"Link: {post_url(course_id, t.id, p.key)}",
                "",
                (p.markdown or p.text).strip(),
                "",
            ]
        if block:
            tglyphs = _glyphs_thread(flags.get(f"thread:{t.id}"))
            cat = " / ".join(x for x in (t.category, t.subcategory, t.subsubcategory) if x)
            parts += [
                f"## Thread: {t.title}{' ' + tglyphs if tglyphs else ''}",
                f"Category: {cat}  |  Link: {post_url(course_id, t.id, f'thread:{t.id}')}",
                "",
                *block,
            ]
    fingerprint = hashlib.sha256(repr(sorted(set(fp_rows))).encode()).hexdigest()
    return _Input("\n".join(parts).rstrip() + "\n", fingerprint, team_keys)


def _glyphs_thread(row: sqlite3.Row | None) -> str:
    if row is None:
        return ""
    return "".join(GLYPHS[k] for k in ("pinned", "announcement") if row[k])


def _cache_dir(conn: sqlite3.Connection, course_id: int, name: str) -> Path:
    course = db.course_by_id(conn, course_id)
    unit = slug(course.code if course else str(course_id))
    return paths.briefs_dir() / unit / slug(name)


def _latest(d: Path) -> Path | None:
    files = sorted(d.glob("*.md")) if d.exists() else []
    return files[-1] if files else None


def _read_meta(md: Path) -> dict[str, str]:
    """`.fingerprint` file: line 1 fingerprint, line 2 built_at (ISO), line 3 model."""
    fp = md.with_suffix(".fingerprint")
    try:
        lines = fp.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    keys = ("fingerprint", "built_at", "model")
    return dict(zip(keys, lines, strict=False))


def _previous_ts(md: Path, meta: dict[str, str]) -> datetime | None:
    if "built_at" in meta and (ts := _parse_ts(meta["built_at"])):
        return ts
    try:
        return datetime.strptime(md.stem, "%Y-%m-%d-%H%M").replace(tzinfo=UTC)
    except ValueError:
        return None


def _whats_new(conn: sqlite3.Connection, keys: list[str], since: datetime) -> list[InboxItem]:
    new: list[str] = []
    for key in keys:
        # Compare as datetimes in Python: stored stamps can carry different UTC offsets.
        stamps = [
            r[0]
            for r in conn.execute(
                "SELECT first_seen_at FROM posts WHERE key = ? "
                "UNION ALL SELECT seen_at FROM post_versions WHERE post_key = ?",
                (key, key),
            )
        ]
        if any((ts := _parse_ts(x)) is not None and ts > since for x in stamps if x):
            new.append(key)
    return items_for_keys(conn, new)


def _render_whats_new(items: list[InboxItem], since: datetime) -> str:
    lines = [f"## What's new since {since.strftime('%Y-%m-%d %H:%M')} UTC", ""]
    if not items:
        lines.append("No new or edited teaching-team messages (only flags changed).")
    for it in items:
        tags = [it.post.role_class.value]
        if it.flags.edited:
            tags.append("edited")
        lines.append(
            f'- {it.post.author_name} ({", ".join(tags)}) in "{it.thread.title}": '
            f"{_excerpt(it.post.text, 160)} [link]({it.url})"
        )
    return "\n".join(lines)


def _prompt(name: str) -> str:
    return resources.files("edscrape.prompts").joinpath(name).read_text(encoding="utf-8")


def _write_system_prompt(cache: Path, text: str) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / SYSTEM_PROMPT_FILE
    _atomic_write(path, text)
    return path


def build(
    conn: sqlite3.Connection,
    config: Config,
    course_id: int,
    name: str,
    *,
    model: str | None = None,
    force: bool = False,
    run_claude: RunClaude | None = None,
    now: datetime | None = None,
) -> BriefResult:
    model = model or config.brief_model
    inp = assemble_input(conn, config, course_id, name)
    if not inp.team_keys:
        raise BriefError(f"no teaching-team messages found for {name!r}")
    cache = _cache_dir(conn, course_id, name)
    latest = _latest(cache)
    meta = _read_meta(latest) if latest else {}
    if latest and not force and meta.get("fingerprint") == inp.fingerprint:
        return BriefResult(path=latest, fresh=False, whats_new=[], model=meta.get("model") or model)

    argv = [
        "claude",
        "-p",
        "--model",
        model,
        "--tools",
        "",
        "--no-session-persistence",
        "--system-prompt-file",
        str(_write_system_prompt(cache, _prompt("brief.md"))),
    ]
    runner = run_claude or default_run_claude
    try:
        out = runner(argv, inp.text)
    except BriefError:
        raise
    except FileNotFoundError as e:
        raise BriefError("the `claude` CLI was not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise BriefError(f"claude timed out after {TIMEOUT_S}s") from e
    except subprocess.CalledProcessError as e:
        raise BriefError(f"claude exited {e.returncode}") from e
    except OSError as e:
        raise BriefError(f"could not run claude: {e}") from e
    if not out or not out.strip():
        raise BriefError("claude returned an empty brief")

    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    whats_new: list[InboxItem] = []
    body = out.strip()
    if latest:
        since = _previous_ts(latest, meta)
        if since is not None:
            whats_new = _whats_new(conn, inp.team_keys, since)
            body = _render_whats_new(whats_new, since) + "\n\n" + body
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{stamp.strftime('%Y-%m-%d-%H%M')}.md"
    _atomic_write(path, body + "\n")
    _atomic_write(
        path.with_suffix(".fingerprint"),
        f"{inp.fingerprint}\n{stamp.isoformat(timespec='seconds')}\n{model}\n",
    )
    return BriefResult(path=path, fresh=True, whats_new=whats_new, model=model)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def followup_argv(
    conn: sqlite3.Connection, config: Config, course_id: int, name: str
) -> tuple[list[str], str]:
    """Interactive `claude` (no `-p`) seeded with the follow-up prompt plus the brief input."""
    inp = assemble_input(conn, config, course_id, name)
    system_prompt = _prompt("followup.md").rstrip() + "\n\n---\n\n" + inp.text
    argv = [
        "claude",
        "--model",
        config.brief_model,
        "--tools",
        "",
        "--no-session-persistence",
        "--system-prompt-file",
        str(_write_system_prompt(_cache_dir(conn, course_id, name), system_prompt)),
    ]
    return argv, system_prompt
