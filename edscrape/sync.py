"""Sync Ed units into the store (plan.md section 3 "Sync rule") and rebuild it offline.

`sync()` is async (shares an `EdClient`); `reparse()` is plain sync, no network.
Parser and flagger are injected; when None they are imported lazily from `edscrape.edparse.parse`
and `edscrape.flags.compute` (never at module import time).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from edscrape import config as config_mod
from edscrape import paths
from edscrape.api import ApiError, EdClient
from edscrape.auth import AuthError, load_token
from edscrape.models import (
    Course,
    Flagger,
    ParsedContent,
    Parser,
    Post,
    PostInfo,
    SyncEvent,
    SyncReport,
    Thread,
)
from edscrape.store import db

Progress = Callable[[SyncEvent], None]
MAX_PARSE_ERRORS = 5  # parser failures recorded per course run (the rest are not listed)


def _now() -> datetime:
    """Current UTC time. Module-level so tests can freeze it."""
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _resolve(parser: Parser | None, flagger: Flagger | None) -> tuple[Parser, Flagger]:
    if parser is None:
        from edscrape.edparse import parse as parser  # lane B module, lazy on purpose
    if flagger is None:
        from edscrape.flags import compute as flagger  # lane B module, lazy on purpose
    return parser, flagger


# -- change detection ------------------------------------------------------------------------------


def content_hash(content_xml: str) -> str:
    return hashlib.sha256((content_xml or "").encode("utf-8")).hexdigest()


def change_key(item: dict[str, Any]) -> str:
    """Serialised list-item change tuple:
    JSON array [updated_at, reply_count, editor_id, is_endorsed, accepted_id, is_answered,
    is_pinned, sha256(content)] with booleans coerced to bool, compact separators."""
    return json.dumps(
        [
            item.get("updated_at"),
            item.get("reply_count", 0),
            item.get("editor_id"),
            bool(item.get("is_endorsed")),
            item.get("accepted_id"),
            bool(item.get("is_answered")),
            bool(item.get("is_pinned")),
            content_hash(item.get("content") or ""),
        ],
        separators=(",", ":"),
    )


def _within_days(updated_at: str | None, days: int, now: datetime) -> bool:
    if not updated_at:
        return True
    try:
        dt = datetime.fromisoformat(updated_at)
    except ValueError:
        return True  # unparseable: be safe and refetch
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return now - dt <= timedelta(days=days)


# -- payload helpers -------------------------------------------------------------------------------


def _course_from_entry(entry: dict[str, Any], selected: bool = False) -> Course:
    c = entry["course"]
    cats = ((c.get("settings") or {}).get("discussion") or {}).get("categories") or []
    return Course(
        id=c["id"],
        code=(c.get("code") or "").strip(),
        name=c.get("name") or "",
        year=str(c.get("year") or ""),
        session=c.get("session") or "",
        status=c.get("status") or "",
        role=((entry.get("role") or {}).get("role")) or "",
        categories=cats,
        selected=selected,
        last_synced_at=None,
    )


def _thread_from_item(item: dict[str, Any]) -> Thread:
    return Thread(
        id=item["id"],
        course_id=item["course_id"],
        number=item.get("number") or 0,
        type=item.get("type") or "",
        title=item.get("title") or "",
        category=item.get("category") or "",
        subcategory=item.get("subcategory") or "",
        subsubcategory=item.get("subsubcategory") or "",
        accepted_id=item.get("accepted_id"),
        is_pinned=bool(item.get("is_pinned")),
        is_endorsed=bool(item.get("is_endorsed")),
        is_answered=bool(item.get("is_answered")),
        is_staff_answered=bool(item.get("is_staff_answered")),
        is_private=bool(item.get("is_private")),
        reply_count=item.get("reply_count") or 0,
        created_at=item.get("created_at") or "",
        updated_at=item.get("updated_at") or "",
    )


def _walk(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield the thread/answer/comment dicts of a detail payload (depth-first)."""
    yield node
    for key in ("answers", "comments"):
        for child in node.get(key) or []:
            yield from _walk(child)


def _visible_user_ids(thread: dict[str, Any]) -> set[int]:
    """Authors of non-anonymous, non-deleted items. Anonymous authors are never persisted, even
    when a staff view reveals them in the payload."""
    out: set[int] = set()
    for node in _walk(thread):
        uid = node.get("user_id")
        if uid and not node.get("is_anonymous") and node.get("type") != "":
            out.add(uid)
    return out


def _users_for_flagger(
    conn: sqlite3.Connection,
    course_id: int,
    users: dict[int, dict[str, Any]],
    visible: set[int],
) -> dict[int, dict[str, Any]]:
    """Persist visible users/roles (non-null rule) and fill a null payload role from the store,
    so a user who left the course keeps the role their old posts had."""
    out: dict[int, dict[str, Any]] = {}
    for uid, user in users.items():
        user = dict(user)
        if uid in visible:
            db.upsert_user(conn, uid, user.get("name"))
            db.upsert_user_role(conn, uid, course_id, user.get("course_role"))
        if user.get("course_role") is None:
            user["course_role"] = db.role_for(conn, uid, course_id)
        out[uid] = user
    return out


def _store_infos(
    conn: sqlite3.Connection,
    infos: Iterable[PostInfo],
    parser: Parser,
    *,
    thread_id: int,
    course_id: int,
    now: str,
    only_kind: str | None = None,
    on_parse_error: Callable[[str, Exception], None] | None = None,
) -> tuple[int, int]:
    """Parse and upsert posts; returns (posts_new, posts_changed). Thread row must already exist."""
    new = changed = 0
    ordered = sorted(infos, key=lambda i: (i.kind != "thread", i.depth, i.number, i.ref_id))
    for info in ordered:
        if only_kind is not None and info.kind != only_kind:
            continue
        if info.is_deleted and db.post_by_key(conn, info.key) is not None:
            db.mark_tombstone(conn, info.key, now=now)
            continue
        text, markdown = info.text, info.text
        if info.content_xml and not info.is_deleted:
            try:
                parsed: ParsedContent = parser(info.content_xml, info.text)
                text, markdown = parsed.text, parsed.markdown
            except Exception as e:  # a parser bug must not lose the post; use plain text
                if on_parse_error is not None:
                    on_parse_error(info.key, e)
        post = Post(
            key=info.key,
            ref_id=info.ref_id,
            thread_id=thread_id,
            course_id=course_id,
            kind=info.kind,
            parent_key=info.parent_key,
            number=info.number,
            depth=info.depth,
            user_id=info.user_id,
            author_name=info.author_name,
            role_class=info.role_class,
            is_anonymous=info.is_anonymous,
            is_own=info.is_own,
            is_endorsed=info.is_endorsed,
            is_deleted=info.is_deleted,
            editor_id=info.editor_id,
            created_at=info.created_at,
            updated_at=info.updated_at,
            content_xml=info.content_xml,
            text=text,
            markdown=markdown,
            content_hash=content_hash(info.content_xml),
            first_seen_at=now,
            last_seen_at=now,
        )
        status = db.upsert_post(conn, post, now=now)
        if status == "new":
            new += 1
        elif status == "changed":
            changed += 1
    return new, changed


def _ingest_list_item(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    *,
    parser: Parser,
    flagger: Flagger,
    role_map: dict[str, str],
    viewer_id: int,
    now: str,
    key: str | None,
    on_parse_error: Callable[[str, Exception], None] | None = None,
) -> tuple[int, int]:
    """Upsert the thread row, then its `thread:<id>` post from the list item (body is in the list).
    `key` is stored as the thread's change_key (None leaves the stored one untouched)."""
    course_id = item["course_id"]
    db.upsert_thread(
        conn,
        _thread_from_item(item),
        editor_id=item.get("editor_id"),
        list_body_hash=content_hash(item.get("content") or ""),
        change_key=key,
    )
    users: dict[int, dict[str, Any]] = {}
    if item.get("user") and item["user"].get("id"):
        users[item["user"]["id"]] = item["user"]
    visible = _visible_user_ids(item)
    users = _users_for_flagger(conn, course_id, users, visible)
    infos = flagger(item, users, role_map, viewer_id)
    return _store_infos(
        conn,
        infos.values(),
        parser,
        thread_id=item["id"],
        course_id=course_id,
        now=now,
        only_kind="thread",
        on_parse_error=on_parse_error,
    )


def _ingest_detail(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    *,
    parser: Parser,
    flagger: Flagger,
    role_map: dict[str, str],
    viewer_id: int,
    now: str,
    on_parse_error: Callable[[str, Exception], None] | None = None,
) -> tuple[int, int]:
    """Upsert every post of a thread-detail payload and recompute the stored thread flags."""
    thread = data["thread"]
    course_id = thread["course_id"]
    users = {u["id"]: u for u in data.get("users") or [] if u.get("id")}
    users = _users_for_flagger(conn, course_id, users, _visible_user_ids(thread))
    infos = flagger(thread, users, role_map, viewer_id)
    result = _store_infos(
        conn,
        infos.values(),
        parser,
        thread_id=thread["id"],
        course_id=course_id,
        now=now,
        on_parse_error=on_parse_error,
    )
    db.recompute_thread_flags(conn, thread["id"])
    return result


# -- sync ------------------------------------------------------------------------------------------


def _emit(progress: Progress | None, event: SyncEvent) -> None:
    if progress is not None:
        progress(event)


async def sync(
    course_ids: Iterable[int],
    progress: Progress | None = None,
    *,
    parser: Parser | None = None,
    flagger: Flagger | None = None,
    full: bool = False,
    config: config_mod.Config | None = None,
    client: EdClient | None = None,
    conn: sqlite3.Connection | None = None,
) -> SyncReport:
    """Run the Sync rule for each course id. Raises `AuthError` on any 401 (after recording the
    failed run). `conn` is optional (default: open and migrate `paths.db_path()`)."""
    cfg = config or config_mod.load()
    parser, flagger = _resolve(parser, flagger)
    async with AsyncExitStack() as stack:
        if conn is None:
            conn = db.connect()
            stack.callback(conn.close)
            db.migrate(conn)
        if client is None:
            client = await stack.enter_async_context(
                EdClient(load_token(), concurrency=cfg.concurrency, rate_per_sec=cfg.rate_per_sec)
            )
        # 1. user + courses
        user_payload = await client.get_user()
        viewer_id = int(user_payload["user"]["id"])
        db.set_meta(conn, "viewer_user_id", str(viewer_id))
        for entry in user_payload.get("courses") or []:
            db.upsert_course(conn, _course_from_entry(entry))
        conn.commit()

        runs: list[dict] = []
        for cid in course_ids:
            runs.append(
                await _sync_course(
                    conn, client, int(cid), cfg, parser, flagger, viewer_id, full, progress
                )
            )
    totals = {k: sum(r[k] or 0 for r in runs) for k in ("threads_seen", "threads_changed")}
    statuses = {r["status"] for r in runs}
    if runs and statuses == {"error"}:
        status = "error"
    elif statuses <= {"ok"}:
        status = "ok"
    else:
        status = "partial"
    return SyncReport(
        runs=runs,
        threads_seen=totals["threads_seen"],
        threads_changed=totals["threads_changed"],
        posts_new=sum(r["posts_new"] or 0 for r in runs),
        posts_changed=sum(r["posts_changed"] or 0 for r in runs),
        status=status,
    )


async def _sync_course(
    conn: sqlite3.Connection,
    client: EdClient,
    cid: int,
    cfg: config_mod.Config,
    parser: Parser,
    flagger: Flagger,
    viewer_id: int,
    full: bool,
    progress: Progress | None,
) -> dict:
    prior = db.has_prior_sync(conn, cid)
    run_id = db.insert_sync_run(conn, cid, _iso(_now()))
    conn.commit()
    counts = {"threads_seen": 0, "threads_changed": 0, "posts_new": 0, "posts_changed": 0}
    errors: list[str] = []

    parse_failures = 0

    def on_parse_error(key: str, exc: Exception) -> None:
        nonlocal parse_failures
        parse_failures += 1
        if parse_failures > MAX_PARSE_ERRORS:
            return
        msg = f"parser failed on {key}: {type(exc).__name__}: {exc}"[:200]
        errors.append(msg)
        _emit(progress, SyncEvent(cid, "error", 0, None, msg))

    def finish(status: str) -> dict:
        run = db.finish_sync_run(
            conn, run_id, status=status, error="; ".join(errors) or None, **counts
        )
        conn.commit()
        return run

    if db.course_by_id(conn, cid) is None:
        errors.append(f"course {cid} not found in /api/user")
        _emit(progress, SyncEvent(cid, "error", 0, None, errors[-1]))
        return finish("error")

    try:
        # 2. list pages -> 3. upsert threads + thread posts
        known = db.stored_change_keys(conn, cid)
        seen: set[int] = set()
        need_detail: list[int] = []
        list_keys: dict[int, str] = {}  # change_key stored once a detail ingest succeeds
        now = _now()
        async for _offset, page in client.iter_pages(cid):
            for item in page.get("threads", []):
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                key = change_key(item)
                is_new = item["id"] not in known
                changed = is_new or known[item["id"]] != key
                replied = (item.get("reply_count") or 0) > 0
                new, upd = _ingest_list_item(
                    conn,
                    item,
                    parser=parser,
                    flagger=flagger,
                    role_map=cfg.roles,
                    viewer_id=viewer_id,
                    now=_iso(now),
                    key=None if replied else key,
                    on_parse_error=on_parse_error,
                )
                conn.commit()
                counts["threads_seen"] += 1
                counts["threads_changed"] += int(changed)
                counts["posts_new"] += new
                counts["posts_changed"] += upd
                if replied:
                    list_keys[item["id"]] = key
                if replied and (
                    cfg.full_recheck
                    or full
                    or changed
                    or _within_days(item.get("updated_at"), cfg.recheck_days, now)
                ):
                    need_detail.append(item["id"])
            _emit(progress, SyncEvent(cid, "list", len(seen), None, None))

        # 4. detail fetches (concurrent fetch, sequential store, commit per thread)
        total = len(need_detail)
        done = 0

        async def fetch(tid: int) -> tuple[int, dict[str, Any] | Exception]:
            try:
                return tid, await client.get_thread(tid, course_id=cid)
            except AuthError:
                raise
            except Exception as e:
                return tid, e

        tasks = [asyncio.ensure_future(fetch(tid)) for tid in need_detail]
        try:
            for fut in asyncio.as_completed(tasks):
                tid, result = await fut
                done += 1
                if isinstance(result, Exception):
                    msg = f"thread {tid}: {result}"
                    errors.append(msg)
                    _emit(progress, SyncEvent(cid, "error", done, total, msg))
                    continue
                try:
                    new, upd = _ingest_detail(
                        conn,
                        result,
                        parser=parser,
                        flagger=flagger,
                        role_map=cfg.roles,
                        viewer_id=viewer_id,
                        now=_iso(_now()),
                        on_parse_error=on_parse_error,
                    )
                    if tid in list_keys:
                        db.set_change_key(conn, tid, list_keys[tid])
                    conn.commit()
                    counts["posts_new"] += new
                    counts["posts_changed"] += upd
                except Exception as e:
                    conn.rollback()
                    msg = f"thread {tid}: {type(e).__name__}: {e}"
                    errors.append(msg)
                    _emit(progress, SyncEvent(cid, "error", done, total, msg))
                    continue
                _emit(progress, SyncEvent(cid, "detail", done, total, None))
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        # 5. first-sync read marking (off unless first_sync_read_before_days > 0)
        if not prior and cfg.first_sync_read_before_days > 0:
            cutoff = _now() - timedelta(days=cfg.first_sync_read_before_days)
            db.mark_read_before(conn, cid, _iso(cutoff))
    except AuthError:
        conn.rollback()
        errors.append("401 unauthorized")
        _emit(progress, SyncEvent(cid, "error", 0, None, "401 unauthorized"))
        finish("error")
        raise
    except ApiError as e:
        conn.rollback()
        errors.append(str(e))
        _emit(progress, SyncEvent(cid, "error", 0, None, str(e)))
        return finish("error")
    except Exception as e:  # malformed item, archive write failure, ...: fail this course only
        conn.rollback()
        msg = f"{type(e).__name__}: {e}"[:200]
        errors.append(msg)
        _emit(progress, SyncEvent(cid, "error", 0, None, msg))
        return finish("error")
    run = finish("partial" if errors else "ok")
    _emit(progress, SyncEvent(cid, "done", counts["threads_seen"], counts["threads_seen"], None))
    return run


# -- reparse ---------------------------------------------------------------------------------------


def _archived_list_pages(cid: int) -> list[tuple[int, list[dict[str, Any]]]]:
    folder = paths.raw_dir() / str(cid) / "list"
    if not folder.is_dir():
        return []
    files = sorted(folder.glob("*.json"), key=lambda p: int(p.stem))
    return [(int(p.stem), json.loads(p.read_text("utf-8")).get("threads", [])) for p in files]


def reparse(
    course_ids: Iterable[int],
    *,
    parser: Parser | None = None,
    flagger: Flagger | None = None,
    config: config_mod.Config | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Rebuild posts from archived raw JSON through the same ingest code as `sync`, no network.
    Thread bodies come from list pages, answers/comments from detail files."""
    cfg = config or config_mod.load()
    parser, flagger = _resolve(parser, flagger)
    own = conn is None
    if conn is None:
        conn = db.connect()
        db.migrate(conn)
    try:
        viewer = db.get_meta(conn, "viewer_user_id")
        if viewer is None:
            user_file = paths.raw_dir() / "user.json"
            if not user_file.exists():
                raise RuntimeError("viewer id unknown: run sync once before reparse")
            viewer = str(json.loads(user_file.read_text("utf-8"))["user"]["id"])
            db.set_meta(conn, "viewer_user_id", viewer)
        viewer_id = int(viewer)
        now = _iso(_now())
        for cid in course_ids:
            seen: set[int] = set()
            for _offset, items in _archived_list_pages(int(cid)):
                for item in items:
                    if item["id"] in seen:
                        continue
                    seen.add(item["id"])
                    _ingest_list_item(
                        conn,
                        item,
                        parser=parser,
                        flagger=flagger,
                        role_map=cfg.roles,
                        viewer_id=viewer_id,
                        now=now,
                        key=None,
                    )
                    conn.commit()
            folder = paths.raw_dir() / str(cid) / "threads"
            files = (
                sorted(folder.glob("*.json"), key=lambda p: int(p.stem)) if folder.is_dir() else []
            )
            for path in files:
                data = json.loads(Path(path).read_text("utf-8"))
                _ingest_detail(
                    conn,
                    data,
                    parser=parser,
                    flagger=flagger,
                    role_map=cfg.roles,
                    viewer_id=viewer_id,
                    now=now,
                )
                conn.commit()
    finally:
        if own:
            conn.close()
