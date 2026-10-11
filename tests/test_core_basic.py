"""units, readstate, inbox, search, export, assignments on the seeded store."""

from __future__ import annotations

import csv
import json
import pathlib

import pytest

from edscrape import config as config_mod
from edscrape.core import assignments, export, inbox, readstate, search, units
from edscrape.models import Course, RoleClass
from tests.seed import seed_store
from tests.test_core_support import add_post, add_thread


def course(cid, code, *, status="active", year="2026", session="Semester 2"):
    return Course(cid, code, "n", year, session, status, "student", [], False, None)


# -- units ---------------------------------------------------------------------------------


def test_resolve_trailing_space_and_case():
    cs = [course(1, "FIT2109 S2 2026 Malaysia "), course(2, "FIT1001 S2 2026")]
    assert units.resolve("  fit2109 ", cs).id == 1
    assert units.resolve("FIT2109 s2", cs).id == 1


def test_resolve_prefers_active_then_latest():
    cs = [
        course(1, "FIT2109 S1 2025", status="archived", year="2025"),
        course(2, "FIT2109 S2 2026"),
        course(3, "FIT2109 S1 2026", session="Semester 1"),
        course(4, "FIT2109 S2 2027", status="archived", year="2027"),
    ]
    assert units.resolve("fit2109", cs).id == 2  # active beats archived; latest session wins
    assert units.resolve("fit2109", [cs[0], cs[3]]).id == 4  # none active -> latest year


def test_resolve_ambiguous_and_unknown():
    cs = [course(1, "FIT1001 S2 2026"), course(2, "FIT2002 S2 2026")]
    with pytest.raises(units.AmbiguousUnit) as e:
        units.resolve("FIT", cs)
    assert {c.id for c in e.value.candidates} == {1, 2}
    dup = [course(1, "FIT1001 A"), course(2, "FIT1001 B")]
    with pytest.raises(units.AmbiguousUnit):
        units.resolve("fit1001", dup)
    with pytest.raises(units.UnknownUnit):
        units.resolve("zzz", cs)
    with pytest.raises(units.UnknownUnit):
        units.resolve("   ", cs)


# -- inbox / readstate -----------------------------------------------------------------------


def test_inbox_excludes_students_unknown_and_tombstones(conn):
    seed = seed_store(conn)
    keys = {i.post.key for i in inbox.list(conn, unread_only=False)}
    assert keys == {
        "thread:101",
        "answer:1021",
        "comment:1023",
        "comment:1024",
        "comment:1031",
        "answer:1041",
        "comment:2012",
    }
    assert not keys & {"thread:102", "thread:103", "thread:104", "thread:202"}
    assert seed["tombstone"] not in keys


def test_inbox_unread_filter_order_and_url(conn):
    seed_store(conn)
    items = inbox.list(conn)
    assert [i.post.key for i in items] == [
        "comment:1031",
        "comment:1024",
        "comment:1023",
        "answer:1021",
    ]  # newest first
    by_key = {i.post.key: i for i in items}
    url = "https://edstem.org/au/courses/1001/discussion/102?answer=1021"
    assert by_key["answer:1021"].url == url
    assert by_key["answer:1021"].course_code == "FIT1001 S2 2026"
    assert all(i.flags.new for i in items)
    assert inbox.list(conn, [1002]) == []
    assert {i.post.key for i in inbox.list(conn, [1002], unread_only=False)} == {"comment:2012"}


def test_inbox_parent_excerpt_flags_and_own(conn):
    seed_store(conn)
    add_post(
        conn,
        "answer:5000",
        201,
        1002,
        RoleClass.staff,
        "My own staff reply",
        parent="thread:201",
        own=True,
        created="2026-10-10T10:00:00+10:00",
    )
    items = {i.post.key: i for i in inbox.list(conn, unread_only=False)}
    own = items["answer:5000"]
    assert own.post.is_own and own.flags.new
    assert own.parent_excerpt == "How is part B marked?"
    assert items["thread:101"].parent_excerpt is None
    assert items["answer:1041"].flags.edited and not items["answer:1041"].flags.new
    assert items["answer:1021"].flags.endorsed
    assert items["thread:101"].flags.pinned and items["thread:101"].flags.announcement
    assert items["comment:1023"].parent_excerpt == "Is a zip file fine?"
    add_thread(conn, 300, 1001, "Long", "y" * 500)
    add_post(conn, "answer:5001", 300, 1001, RoleClass.staff, "ok", parent="thread:300")
    got = {i.post.key: i for i in inbox.list(conn, [1001])}
    assert len(got["answer:5001"].parent_excerpt) == 200


def test_readstate_mark_and_mark_course(conn):
    seed_store(conn)
    readstate.mark(conn, ["answer:1021", "comment:1031"])
    assert {i.post.key for i in inbox.list(conn)} == {"comment:1024", "comment:1023"}
    readstate.mark(conn, ["answer:1021"])  # idempotent
    readstate.mark_course(conn, 1001)
    assert inbox.list(conn) == []
    assert inbox.list(conn, [1002], unread_only=False)


# -- search ----------------------------------------------------------------------------------


def test_search_ranked_snippets(conn):
    seed_store(conn)
    hits = search.query(conn, "PDF")
    assert {h.post_key for h in hits} >= {"answer:1021", "comment:1023", "comment:1024"}
    assert all("[" in h.snippet and "]" in h.snippet for h in hits)
    assert [h.rank for h in hits] == sorted(h.rank for h in hits)
    assert hits[0].url.startswith("https://edstem.org/au/courses/1001/discussion/")
    assert search.query(conn, "marked", course_ids=[1001]) == []
    marked = {h.post_key for h in search.query(conn, "marked", course_ids=[1002])}
    assert marked == {"thread:201", "comment:2012"}
    assert len(search.query(conn, "pdf", limit=1)) == 1


def test_search_matches_thread_title(conn):
    seed_store(conn)
    assert "thread:102" in {h.post_key for h in search.query(conn, "submission format")}


@pytest.mark.parametrize(
    "q", ['"', 'pdf"', "*", "pdf*", 'a"b*c', "AND", "OR NOT", "(", "x:y", "", "  "]
)
def test_search_tolerates_fts_syntax(conn, q):
    seed_store(conn)
    search.query(conn, q)  # must not raise


# -- export ----------------------------------------------------------------------------------


def test_export_jsonl_csv_md(conn, tmp_path):
    seed_store(conn)
    export.write(conn, 1001, "jsonl", tmp_path / "a.jsonl")
    rows = [json.loads(line) for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert len(rows) == 10
    anon = next(r for r in rows if r["post_key"] == "thread:103")
    assert anon["author"] == "Anonymous"
    edited = next(r for r in rows if r["post_key"] == "answer:1041")
    assert edited["edited"] is True and edited["url"].endswith("/104?answer=1041")

    export.write(conn, 1001, "csv", tmp_path / "a.csv")
    with (tmp_path / "a.csv").open(newline="") as fh:
        crows = list(csv.DictReader(fh))
    assert [r["post_key"] for r in crows] == [r["post_key"] for r in rows]
    assert next(r for r in crows if r["post_key"] == "thread:103")["author"] == "Anonymous"

    export.write(conn, 1001, "md", tmp_path / "a.md")
    md = (tmp_path / "a.md").read_text()
    assert md.startswith("# FIT1001 S2 2026 export")
    assert md.count("\n## ") == 4  # one section per thread
    assert "[Open in Ed](https://edstem.org/au/courses/1001/discussion/102?answer=1021)" in md
    assert "Anonymous (student)" in md
    assert "Submit a single PDF." in md


def test_export_tombstone_and_bad_format(conn, tmp_path):
    seed_store(conn)
    export.write(conn, 1002, "md", tmp_path / "b.md")
    assert "_(deleted)_" in (tmp_path / "b.md").read_text()
    with pytest.raises(export.ExportError):
        export.write(conn, 1002, "xml", tmp_path / "b.xml")


# -- assignments -----------------------------------------------------------------------------


def test_assignments_auto_keyword_and_saved(conn, tmp_home):
    seed_store(conn)
    add_thread(conn, 110, 1001, "Question about Assignment 1 marking", "when is it due")
    add_thread(conn, 111, 1001, "ass1 clarification", "hmm")
    add_thread(conn, 112, 1001, "Unrelated", "my A1 solution crashes")
    add_thread(conn, 113, 1001, "Unrelated 2", "data1 and a10 and ba1 are not it")
    add_thread(conn, 114, 1001, "Case thing", "x", category="assignments", sub="a1")
    cfg = config_mod.Config()
    c = assignments.candidates(conn, cfg, 1001, "A1")
    assert {t.id for t in c.auto} == {102, 103, 104, 114}
    assert {t.id for t in c.needs_confirm} == {110, 111, 112}
    assert c.kept == [] and c.excluded == []

    assignments.save_choice(cfg, 1001, "A1", keep=[110], exclude=[111])
    reloaded = config_mod.load()
    assert reloaded.assignments["1001"]["A1"] == {"keep": [110], "exclude": [111]}
    c2 = assignments.candidates(conn, reloaded, 1001, "A1")
    assert {t.id for t in c2.needs_confirm} == {112}
    assert c2.kept == [110] and c2.excluded == [111]
    assert {t.id for t in c2.auto} == {102, 103, 104, 114}


def test_assignments_name_variants_and_other_course(conn):
    seed_store(conn)
    add_thread(conn, 120, 1001, "Assignment 2 help", "x")
    add_thread(conn, 121, 1001, "assignment2", "x")
    add_thread(conn, 122, 1001, "Assignment 3", "x")
    cfg = config_mod.Config()
    c = assignments.candidates(conn, cfg, 1001, "Assignment 2")
    assert {t.id for t in c.needs_confirm} == {120, 121}
    c = assignments.candidates(conn, cfg, 1002, "A1")
    assert {t.id for t in c.auto} == {201}


def test_core_has_no_ui_imports():
    root = pathlib.Path(__file__).resolve().parents[1] / "edscrape" / "core"
    for f in root.glob("*.py"):
        src = f.read_text()
        assert "import rich" not in src and "from rich" not in src, f
        assert "import textual" not in src and "from textual" not in src, f
