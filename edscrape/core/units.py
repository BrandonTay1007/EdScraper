"""Resolve a user-typed unit query to a single Course."""

from __future__ import annotations

from edscrape.models import Course


class UnitError(Exception):
    pass


class UnknownUnit(UnitError):
    def __init__(self, query: str):
        super().__init__(f"no unit matches {query!r}")
        self.query = query


class AmbiguousUnit(UnitError):
    def __init__(self, candidates: list[Course]):
        names = ", ".join(c.code.strip() for c in candidates)
        super().__init__(f"ambiguous unit; candidates: {names}")
        self.candidates = candidates


def _unit_token(course: Course) -> str:
    parts = course.code.split()
    return parts[0].lower() if parts else ""


def _recency(course: Course) -> tuple[str, str]:
    return (course.year.strip(), course.session.strip())


def resolve(query: str, courses: list[Course]) -> Course:
    """Case-insensitive code prefix match on the trimmed code.

    If the query is exactly one unit code (first word of the code) only that unit's offerings
    compete; otherwise several distinct unit codes make the query ambiguous. Within a unit, active
    offerings beat others, then the latest (year, session) wins; a remaining tie is ambiguous.
    """
    q = " ".join(query.split()).lower()
    if not q:
        raise UnknownUnit(query)
    matches = [c for c in courses if " ".join(c.code.split()).lower().startswith(q)]
    if not matches:
        raise UnknownUnit(query)
    exact = [c for c in matches if _unit_token(c) == q]
    if exact:
        matches = exact
    if len({_unit_token(c) for c in matches}) > 1:
        raise AmbiguousUnit(sorted(matches, key=lambda c: c.code))
    active = [c for c in matches if c.status == "active"]
    pool = active or matches
    best = max(_recency(c) for c in pool)
    top = [c for c in pool if _recency(c) == best]
    if len(top) > 1:
        raise AmbiguousUnit(sorted(top, key=lambda c: c.code))
    return top[0]
