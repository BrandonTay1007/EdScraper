"""core.brief with a fake `run_claude`: argv, stdin, caching, what's-new, error paths."""

from __future__ import annotations

import pathlib
import subprocess
from datetime import UTC, datetime

import pytest

from edscrape import config as config_mod
from edscrape import paths
from edscrape.core import brief
from edscrape.models import RoleClass
from edscrape.store import db
from tests.seed import seed_store
from tests.test_core_support import add_post

T0 = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)
STUDENT_NAMES = ("Student Twenty", "Viewer Me")


class FakeClaude:
    def __init__(self, reply="## Requirements and clarifications\n- Submit a PDF ([link](x))"):
        self.calls: list[tuple[list[str], str]] = []
        self.reply = reply

    def __call__(self, argv, prompt):
        self.calls.append((argv, prompt))
        return self.reply


@pytest.fixture
def env(conn, tmp_home):
    seed_store(conn)
    return conn, config_mod.Config()


def cached_files():
    d = paths.briefs_dir()
    files = [p for p in d.rglob("*") if p.is_file()] if d.exists() else []
    return [p for p in files if p.name != "system-prompt.txt"]


def test_build_argv_stdin_and_cache_layout(env):
    conn, cfg = env
    fake = FakeClaude()
    res = brief.build(conn, cfg, 1001, "A1", run_claude=fake, now=T0)
    assert res.fresh and res.model == "sonnet" and res.whats_new == []
    ((argv, prompt),) = fake.calls
    assert argv[:7] == [
        "claude",
        "-p",
        "--model",
        "sonnet",
        "--tools",
        "",
        "--no-session-persistence",
    ]
    sp = pathlib.Path(argv[argv.index("--system-prompt-file") + 1])
    assert sp == paths.briefs_dir() / "fit1001-s2-2026" / "a1" / "system-prompt.txt"
    assert sp.read_text().startswith("You write a short study brief")
    assert "--system-prompt" not in argv

    assert "https://edstem.org/au/courses/1001/discussion/102?answer=1021" in prompt
    assert "https://edstem.org/au/courses/1001/discussion/103?comment=1031" in prompt
    assert "Submit a single PDF." in prompt and "Staff Ten (staff)" in prompt
    assert "Helper Thirty (helper)" in prompt
    assert "✎" in prompt  # edited glyph on answer:1041
    assert "Context (student message, author omitted): Is a zip file fine?" in prompt
    for name in STUDENT_NAMES:
        assert name not in prompt
    assert "Mystery Forty" not in prompt and "Welcome everyone" not in prompt  # other threads

    assert res.path.parent == paths.briefs_dir() / "fit1001-s2-2026" / "a1"
    assert res.path.name == "2026-10-11-0000.md"
    assert res.path.with_suffix(".fingerprint").exists()
    assert res.path.read_text().startswith("## Requirements")


def test_unchanged_fingerprint_skips_claude(env):
    conn, cfg = env
    fake = FakeClaude()
    first = brief.build(conn, cfg, 1001, "A1", run_claude=fake, now=T0)
    second = brief.build(conn, cfg, 1001, "A1", run_claude=fake, now=T1)
    assert len(fake.calls) == 1
    assert not second.fresh and second.path == first.path and second.whats_new == []
    assert len(cached_files()) == 2  # one .md + one .fingerprint
    forced = brief.build(conn, cfg, 1001, "A1", run_claude=fake, force=True, now=T1)
    assert forced.fresh and len(fake.calls) == 2 and forced.path != first.path


def test_changed_input_regenerates_with_whats_new(env):
    conn, cfg = env
    fake = FakeClaude()
    brief.build(conn, cfg, 1001, "A1", run_claude=fake, now=T0)
    seen = "2026-10-11T05:00:00+00:00"
    add_post(
        conn, "comment:1099", 102, 1001, RoleClass.staff, "Extension to Friday.",
        parent="answer:1021", author="Staff Ten", created=seen, seen=seen,
    )  # fmt: skip
    res = brief.build(conn, cfg, 1001, "A1", run_claude=fake, now=T1)
    assert res.fresh and len(fake.calls) == 2
    assert [i.post.key for i in res.whats_new] == ["comment:1099"]
    assert "comment:1099" not in fake.calls[0][1] and "Extension to Friday." in fake.calls[1][1]
    text = res.path.read_text()
    assert text.startswith("## What's new since 2026-10-11 00:00 UTC")
    assert "Extension to Friday." in text and "comment=1099" in text
    assert text.index("What's new") < text.index("## Requirements")


def test_whats_new_counts_edited_versions(env):
    conn, cfg = env
    fake = FakeClaude()
    brief.build(conn, cfg, 1001, "A1", run_claude=fake, now=T0)
    xml = "<document><paragraph>Submit a PDF or DOCX.</paragraph></document>"
    row = db.post_by_key(conn, "answer:1021")
    import dataclasses
    import hashlib

    edited = dataclasses.replace(
        row, content_xml=xml, text="Submit a PDF or DOCX.", markdown="Submit a PDF or DOCX.",
        content_hash=hashlib.sha256(xml.encode()).hexdigest(), editor_id=10,
    )  # fmt: skip
    db.upsert_post(conn, edited, now="2026-10-11T06:00:00+00:00")
    conn.commit()
    res = brief.build(conn, cfg, 1001, "A1", run_claude=fake, now=T1)
    assert [i.post.key for i in res.whats_new] == ["answer:1021"]
    assert res.whats_new[0].flags.edited


def test_kept_threads_included_excluded_dropped(env):
    conn, cfg = env
    cfg.assignments = {"1001": {"A1": {"keep": [101], "exclude": [104]}}}
    fake = FakeClaude()
    brief.build(conn, cfg, 1001, "A1", run_claude=fake, now=T0)
    prompt = fake.calls[0][1]
    assert "Welcome everyone" in prompt  # kept thread 101
    assert "Check the file path" not in prompt  # excluded thread 104


def test_model_override(env):
    conn, cfg = env
    fake = FakeClaude()
    res = brief.build(conn, cfg, 1001, "A1", model="opus", run_claude=fake, now=T0)
    assert res.model == "opus" and fake.calls[0][0][3] == "opus"


def test_no_material_is_error_without_call(env):
    conn, cfg = env
    fake = FakeClaude()
    with pytest.raises(brief.BriefError):
        brief.build(conn, cfg, 1001, "Nonexistent", run_claude=fake)
    assert fake.calls == [] and cached_files() == []


def _raising(exc):
    def run(argv, prompt):
        raise exc

    return run


@pytest.mark.parametrize(
    "runner",
    [
        _raising(FileNotFoundError("claude")),
        _raising(subprocess.TimeoutExpired("claude", 300)),
        _raising(subprocess.CalledProcessError(1, "claude")),
        _raising(brief.BriefError("claude exited 1")),
        lambda argv, prompt: "   ",
    ],
    ids=["missing", "timeout", "nonzero", "brieferror", "empty"],
)
def test_runner_failures_raise_brief_error_and_write_nothing(env, runner):
    conn, cfg = env
    with pytest.raises(brief.BriefError):
        brief.build(conn, cfg, 1001, "A1", run_claude=runner, now=T0)
    assert cached_files() == []


def test_failure_after_success_keeps_previous_brief(env):
    conn, cfg = env
    first = brief.build(conn, cfg, 1001, "A1", run_claude=FakeClaude(), now=T0)
    with pytest.raises(brief.BriefError):
        brief.build(
            conn, cfg, 1001, "A1", run_claude=_raising(FileNotFoundError()), force=True, now=T1
        )
    assert [p.name for p in cached_files() if p.suffix == ".md"] == [first.path.name]


def test_default_runner_maps_subprocess_errors(monkeypatch):
    def fake_run(kind):
        def run(argv, **kw):
            assert kw["input"] == "in" and kw["timeout"] == 300 and kw["capture_output"]
            if kind == "missing":
                raise FileNotFoundError
            if kind == "timeout":
                raise subprocess.TimeoutExpired(argv, 300)
            return subprocess.CompletedProcess(argv, 2 if kind == "nonzero" else 0, "out", "boom")

        return run

    for kind in ("missing", "timeout", "nonzero"):
        monkeypatch.setattr(subprocess, "run", fake_run(kind))
        with pytest.raises(brief.BriefError):
            brief.default_run_claude(["claude"], "in")
    monkeypatch.setattr(subprocess, "run", fake_run("ok"))
    assert brief.default_run_claude(["claude"], "in") == "out"


def test_followup_argv(env):
    conn, cfg = env
    argv, system = brief.followup_argv(conn, cfg, 1001, "A1")
    assert argv[0] == "claude" and "-p" not in argv
    sp = pathlib.Path(argv[argv.index("--system-prompt-file") + 1])
    assert sp.read_text() == system and sp.name == "system-prompt.txt"
    assert "--system-prompt" not in argv
    assert "Answer ONLY from the provided messages" in system
    assert "https://edstem.org/au/courses/1001/discussion/102?answer=1021" in system
    for name in STUDENT_NAMES:
        assert name not in system


def test_slug():
    assert brief.slug("FIT2109 S2 2026 Malaysia ") == "fit2109-s2-2026-malaysia"
    assert brief.slug("A1 / Part 2!") == "a1-part-2"
    assert brief.slug("///") == "x"
