"""Shared dataclasses and Protocols for EdScrape.

FROZEN CONTRACT (plan.md section 3 "Models"). Do not edit from a lane: report the
needed change to the orchestrator, who routes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class RoleClass(StrEnum):
    staff = "staff"
    helper = "helper"
    student = "student"
    unknown = "unknown"


@dataclass(slots=True)
class Course:
    id: int
    code: str
    name: str
    year: str
    session: str
    status: str
    role: str
    categories: list[dict]
    selected: bool
    last_synced_at: str | None


@dataclass(slots=True)
class Thread:
    id: int
    course_id: int
    number: int
    type: str
    title: str
    category: str
    subcategory: str
    subsubcategory: str
    accepted_id: int | None
    is_pinned: bool
    is_endorsed: bool
    is_answered: bool
    is_staff_answered: bool
    is_private: bool
    reply_count: int
    created_at: str
    updated_at: str


@dataclass(slots=True)
class Post:
    key: str
    ref_id: int
    thread_id: int
    course_id: int
    kind: str
    parent_key: str | None
    number: int
    depth: int
    user_id: int | None
    author_name: str
    role_class: RoleClass
    is_anonymous: bool
    is_own: bool
    is_endorsed: bool
    is_deleted: bool
    editor_id: int | None
    created_at: str
    updated_at: str | None
    content_xml: str
    text: str
    markdown: str
    content_hash: str
    first_seen_at: str
    last_seen_at: str


@dataclass(slots=True)
class Flags:
    edited: bool
    later_staff_reply: bool
    accepted: bool
    endorsed: bool
    deleted: bool
    pinned: bool
    announcement: bool
    new: bool


@dataclass(slots=True)
class ParsedContent:
    markdown: str
    text: str
    unknown_tags: list[str]


@dataclass(slots=True)
class PostInfo:
    key: str
    ref_id: int
    kind: str
    parent_key: str | None
    depth: int
    number: int
    user_id: int | None
    author_name: str
    course_role: str | None
    role_class: RoleClass
    is_anonymous: bool
    is_own: bool
    is_endorsed: bool
    is_deleted: bool
    editor_id: int | None
    created_at: str
    updated_at: str | None
    content_xml: str
    text: str
    later_staff_reply: bool
    accepted: bool


@dataclass(slots=True)
class AuthStatus:
    ok: bool
    user_id: int | None
    name: str | None
    reason: str | None


@dataclass(slots=True)
class SyncEvent:
    course_id: int
    stage: str  # "list" | "detail" | "done" | "error"
    done: int
    total: int | None
    message: str | None


@dataclass(slots=True)
class SyncReport:
    runs: list[dict]
    threads_seen: int
    threads_changed: int
    posts_new: int
    posts_changed: int
    status: str  # ok | partial | error


@dataclass(slots=True)
class InboxItem:
    post: Post
    thread: Thread
    flags: Flags
    parent_excerpt: str | None
    url: str
    course_code: str


@dataclass(slots=True)
class SearchHit:
    thread: Thread
    post_key: str
    snippet: str
    url: str
    rank: float


@dataclass(slots=True)
class Candidates:
    auto: list[Thread]
    needs_confirm: list[Thread]
    kept: list[int]
    excluded: list[int]


@dataclass(slots=True)
class BriefResult:
    path: Path
    fresh: bool
    whats_new: list[InboxItem]
    model: str


class Parser(Protocol):
    def __call__(self, content_xml: str, fallback_text: str) -> ParsedContent: ...


class Flagger(Protocol):
    def __call__(
        self,
        thread: dict,
        users: dict[int, dict],
        role_map: dict[str, str],
        viewer_id: int,
    ) -> dict[str, PostInfo]: ...
