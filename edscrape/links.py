"""Ed Discussion deep-link builders (recon section 8). Pure string functions."""

from __future__ import annotations

BASE = "https://edstem.org/au"


def course_url(course_id: int) -> str:
    return f"{BASE}/courses/{course_id}/discussion"


def thread_url(course_id: int, thread_id: int) -> str:
    return f"{BASE}/courses/{course_id}/discussion/{thread_id}"


def answer_url(course_id: int, thread_id: int, answer_id: int) -> str:
    return f"{thread_url(course_id, thread_id)}?answer={answer_id}"


def comment_url(course_id: int, thread_id: int, comment_id: int) -> str:
    return f"{thread_url(course_id, thread_id)}?comment={comment_id}"
