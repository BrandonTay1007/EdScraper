"""Role classification and per-post flags from an Ed thread dict.

Pure functions over dicts and ``models`` types; no I/O.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from edscrape.models import PostInfo, RoleClass

log = logging.getLogger(__name__)

_warned: set[str] = set()


def classify_role(course_role: str | None, role_map: dict[str, str]) -> RoleClass:
    """Map an Ed ``course_role`` to a RoleClass; unmapped values warn once."""
    if course_role is None:
        return RoleClass.unknown
    mapped = role_map.get(course_role)
    if mapped is not None:
        try:
            return RoleClass(mapped)
        except ValueError:
            pass
    if course_role not in _warned:
        _warned.add(course_role)
        log.warning("unmapped course_role %r; treating as unknown", course_role)
    return RoleClass.unknown


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _lookup_user(users: dict, user_id) -> dict | None:
    if not user_id:
        return None
    return users.get(user_id) or users.get(str(user_id))


def compute(
    thread: dict,
    users: dict[int, dict],
    role_map: dict[str, str],
    viewer_id: int,
) -> dict[str, PostInfo]:
    """Return ``{post_key: PostInfo}`` for the thread body, answers and comments.

    Accepts a list item that has no ``answers``/``comments`` keys.
    """
    accepted_id = thread.get("accepted_id")
    posts: dict[str, PostInfo] = {}

    def add(item: dict, kind: str, parent_key: str | None, depth: int, user_obj=None) -> str:
        ref_id = item["id"]
        key = f"{kind}:{ref_id}"
        if item.get("type") == "":
            posts[key] = PostInfo(
                key=key,
                ref_id=ref_id,
                kind=kind,
                parent_key=parent_key,
                depth=depth,
                number=item.get("number") or 0,
                user_id=None,
                author_name="",
                course_role=None,
                role_class=RoleClass.unknown,
                is_anonymous=False,
                is_own=False,
                is_endorsed=False,
                is_deleted=True,
                editor_id=None,
                created_at=item.get("created_at") or "",
                updated_at=None,
                content_xml="",
                text="",
                later_staff_reply=False,
                accepted=False,
            )
            return key

        raw_uid = item.get("user_id") or None
        is_own = raw_uid is not None and raw_uid == viewer_id
        is_anon = bool(item.get("is_anonymous"))
        user = user_obj or _lookup_user(users, raw_uid)
        if is_anon:
            uid, name, course_role = None, "Anonymous", None
        else:
            uid = raw_uid
            name = (user or {}).get("name") or ""
            course_role = (user or {}).get("course_role")
        posts[key] = PostInfo(
            key=key,
            ref_id=ref_id,
            kind=kind,
            parent_key=parent_key,
            depth=depth,
            number=item.get("number") or 0,
            user_id=uid,
            author_name=name,
            course_role=course_role,
            role_class=classify_role(course_role, role_map),
            is_anonymous=is_anon,
            is_own=is_own,
            is_endorsed=bool(item.get("is_endorsed")),
            is_deleted=False,
            # anonymous: never store the real editor id (it can identify the author)
            editor_id=(-1 if is_anon else item["editor_id"]) if item.get("editor_id") else None,
            created_at=item.get("created_at") or "",
            updated_at=item.get("updated_at"),
            content_xml=item.get("content") or "",
            text=item.get("document") or "",
            later_staff_reply=False,
            accepted=kind == "answer" and accepted_id is not None and ref_id == accepted_id,
        )
        return key

    def walk_comments(comments: list[dict], parent_key: str, depth: int) -> None:
        for c in comments or []:
            key = add(c, "comment", parent_key, depth)
            walk_comments(c.get("comments") or [], key, depth + 1)

    root_key = add(thread, "thread", None, 0, user_obj=thread.get("user"))
    for a in thread.get("answers") or []:
        akey = add(a, "answer", root_key, 1)
        walk_comments(a.get("comments") or [], akey, 2)
    walk_comments(thread.get("comments") or [], root_key, 1)

    staff_times = [
        t
        for p in posts.values()
        if p.role_class is RoleClass.staff
        and not p.is_deleted
        and (t := _parse_dt(p.created_at)) is not None
    ]
    for p in posts.values():
        if p.is_deleted or p.role_class is RoleClass.staff:
            continue
        t = _parse_dt(p.created_at)
        if t is not None:
            p.later_staff_reply = any(s > t for s in staff_times)
    return posts
