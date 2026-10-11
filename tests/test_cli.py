"""CLI via typer's CliRunner against a seeded store under a temporary EDSCRAPE_HOME."""

from __future__ import annotations

import re
import sys
import types

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from edscrape import auth, cli, paths
from edscrape import config as config_mod
from edscrape import sync as sync_mod
from edscrape.core import brief
from edscrape.models import AuthStatus, RoleClass, SyncReport
from edscrape.store import db
from tests.seed import seed_store
from tests.test_core_support import add_thread

runner = CliRunner()
HIDDEN = {"login", "units", "pull", "reparse", "inbox", "search", "export", "brief"}


@pytest.fixture
def home(tmp_home):
    c = db.connect()
    db.migrate(c)
    seed_store(c)
    c.close()
    return tmp_home


def invoke(*args, input=None):
    return runner.invoke(cli.app, list(args), input=input)


def test_hidden_subcommands_exist_but_not_listed():
    cmd = get_command(cli.app)
    assert HIDDEN <= set(cmd.commands)
    assert all(cmd.commands[n].hidden for n in HIDDEN)
    help_text = invoke("--help").output
    assert not any(f" {n} " in help_text for n in HIDDEN)
    assert invoke("brief", "--help").exit_code == 0


def test_no_subcommand_runs_tui_lazily(monkeypatch):
    calls = []
    pkg = types.ModuleType("edscrape.tui")
    pkg.__path__ = []
    mod = types.ModuleType("edscrape.tui.app")
    mod.run = lambda: calls.append("run")
    monkeypatch.setitem(sys.modules, "edscrape.tui", pkg)
    monkeypatch.setitem(sys.modules, "edscrape.tui.app", mod)
    assert invoke().exit_code == 0
    assert calls == ["run"]


def test_cli_module_does_not_import_tui_at_top_level():
    src = open(cli.__file__).read()
    assert "\nfrom edscrape.tui" not in src and "\nimport edscrape.tui" not in src


def test_inbox_default_and_all(home):
    res = invoke("inbox")
    assert res.exit_code == 0
    assert "answer:1021" not in res.output
    assert "Submit a single PDF." in res.output
    assert "https://edstem.org/au/courses/1001/discussion/102?answer=1021" in res.output
    assert "Welcome everyone" not in res.output  # read already
    res = invoke("inbox", "--all", "--unit", "fit2002")
    assert "Part B is marked on correctness." in res.output
    assert "FIT1001" not in res.output
    assert "re: Is a zip file fine?" in invoke("inbox", "--all", "-u", "fit1001").output


def test_inbox_unknown_unit_exit_2(home):
    res = invoke("inbox", "--unit", "nope")
    assert res.exit_code == 2


def test_inbox_uses_configured_units(home):
    cfg = config_mod.load()
    cfg.unit_ids = [1002]
    config_mod.save(cfg)
    assert "FIT1001" not in invoke("inbox", "--all").output


def test_search(home):
    res = invoke("search", "PDF")
    assert res.exit_code == 0 and "discussion/102?answer=1021" in res.output
    assert invoke("search", 'x"*').exit_code == 0
    assert "No matches." in invoke("search", "zzzzqq").output


def test_units_list(home):
    cfg = config_mod.load()
    cfg.unit_ids = [1002]
    config_mod.save(cfg)
    out = invoke("units").output
    assert "FIT1001 S2 2026" in out
    assert re.search(r"^\* +1002 ", out, re.M) and re.search(r"^ +1001 ", out, re.M)


def test_export(home, tmp_path):
    target = tmp_path / "x.jsonl"
    res = invoke("export", "FIT1001", "--fmt", "jsonl", "--out", str(target))
    assert res.exit_code == 0 and target.read_text().count("\n") == 10
    assert invoke("export", "FIT1001", "--fmt", "pdf", "--out", str(target)).exit_code == 2
    assert invoke("export", "FIT", "--out", str(target)).exit_code == 2  # ambiguous


class _Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, prompt):
        self.calls.append((argv, prompt))
        return "## Requirements and clarifications\n- PDF only ([link](https://edstem.org/x))"


def test_brief_command_and_cache(home, monkeypatch):
    fake = _Fake()
    monkeypatch.setattr(brief, "default_run_claude", fake)
    res = invoke("brief", "fit1001", "A1")
    assert res.exit_code == 0, res.output
    assert "generated" in res.output and "PDF only" in res.output
    assert len(fake.calls) == 1 and fake.calls[0][0][:3] == ["claude", "-p", "--model"]
    assert (paths.briefs_dir() / "fit1001-s2-2026" / "a1").is_dir()
    res = invoke("brief", "fit1001", "A1", "--model", "opus")
    assert "unchanged" in res.output and len(fake.calls) == 1
    res = invoke("brief", "fit1001", "A1", "--force", "--model", "opus")
    assert len(fake.calls) == 2 and fake.calls[1][0][3] == "opus"


def test_brief_error_exit_1_and_no_cache(home, monkeypatch):
    def missing(argv, prompt):
        raise FileNotFoundError

    monkeypatch.setattr(brief, "default_run_claude", missing)
    res = invoke("brief", "fit1001", "A1")
    assert res.exit_code == 1 and "Brief failed" in res.output
    assert not paths.briefs_dir().exists() or not list(paths.briefs_dir().rglob("*.md"))


def _keyword_thread():
    c = db.connect()
    add_thread(c, 110, 1001, "Question about A1 marking", "when due?", number=9)
    c.close()


def test_brief_non_tty_skips_keyword_prompt(home, monkeypatch):
    _keyword_thread()
    fake = _Fake()
    monkeypatch.setattr(brief, "default_run_claude", fake)
    monkeypatch.setattr(cli, "_is_tty", lambda: False)
    res = invoke("brief", "fit1001", "A1")
    assert res.exit_code == 0
    assert "Question about A1 marking" not in fake.calls[0][1]  # included none
    assert config_mod.load().assignments == {}  # nothing saved


def test_brief_tty_confirms_and_saves_choice(home, monkeypatch):
    c = db.connect()
    add_thread(c, 110, 1001, "Question about A1 marking", "when due?", number=9)
    # a staff reply so the kept thread has teaching-team content
    from tests.test_core_support import add_post

    add_post(c, "answer:7000", 110, 1001, RoleClass.staff, "Marking is on Friday.",
             parent="thread:110", author="Staff Ten")  # fmt: skip
    add_thread(c, 111, 1001, "ass1 other", "x", number=10)
    c.close()
    fake = _Fake()
    monkeypatch.setattr(brief, "default_run_claude", fake)
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    res = invoke("brief", "fit1001", "A1", input="2\n")  # newest-first: 111 is #1, 110 is #2
    assert res.exit_code == 0, res.output
    assert "Marking is on Friday." in fake.calls[0][1]
    saved = config_mod.load().assignments["1001"]["A1"]
    assert saved == {"keep": [110], "exclude": [111]}
    # saved choices mean no further prompt
    res = invoke("brief", "fit1001", "A1", input="")
    assert res.exit_code == 0


def test_brief_unknown_unit(home):
    assert invoke("brief", "zzz", "A1").exit_code == 2


def test_pull_auth_failure_exit_3_non_tty(home, monkeypatch):
    async def boom(*a, **k):
        raise auth.AuthError("401")

    monkeypatch.setattr(sync_mod, "sync", boom)
    monkeypatch.setattr(cli, "_is_tty", lambda: False)
    res = invoke("pull", "--unit", "FIT1001")
    assert res.exit_code == 3
    assert "edscrape login" in res.output


def test_pull_auth_failure_tty_prints_how_to_log_in(home, monkeypatch):
    async def boom(*a, **k):
        raise auth.AuthError("401")

    monkeypatch.setattr(sync_mod, "sync", boom)
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    res = invoke("pull", "--unit", "FIT1001")
    assert res.exit_code == 1 and "Run `edscrape login`" in res.output


def test_login_check(home, monkeypatch):
    async def bad():
        return AuthStatus(ok=False, user_id=None, name=None, reason="token rejected (401)")

    async def good():
        return AuthStatus(ok=True, user_id=1, name="Me", reason=None)

    monkeypatch.setattr(cli, "_is_tty", lambda: False)
    monkeypatch.setattr(auth, "check", bad)
    assert invoke("login", "--check").exit_code == 3
    monkeypatch.setattr(auth, "check", good)
    res = invoke("login", "--check")
    assert res.exit_code == 0 and "Me" in res.output


def test_pull_reports_progress_and_passes_options(home, monkeypatch):
    seen = {}

    async def fake_sync(ids, progress=None, **kw):
        seen["ids"], seen["kw"] = list(ids), kw
        from edscrape.models import SyncEvent

        progress(SyncEvent(1001, "list", 100, None, None))
        progress(SyncEvent(1001, "detail", 1, 4, None))
        progress(SyncEvent(1001, "detail", 2, 4, None))
        progress(SyncEvent(1001, "done", 4, 4, None))
        return SyncReport(runs=[], threads_seen=4, threads_changed=2, posts_new=3,
                          posts_changed=1, status="ok")  # fmt: skip

    monkeypatch.setattr(sync_mod, "sync", fake_sync)
    res = invoke("pull", "--unit", "FIT1001", "--full")
    assert res.exit_code == 0, res.output
    assert seen["ids"] == [1001] and seen["kw"]["full"] is True
    assert "FIT1001 S2 2026: list 100" in res.output
    assert "FIT1001 S2 2026: done 4/4" in res.output
    assert res.output.count("detail") == 1  # only stage transitions are printed
    assert "Sync ok: 4 threads seen" in res.output


def test_pull_without_units_is_usage_error(home):
    assert invoke("pull").exit_code == 2


def test_reparse(home, monkeypatch):
    got = {}
    monkeypatch.setattr(sync_mod, "reparse", lambda ids, **kw: got.update(ids=list(ids)))
    cfg = config_mod.load()
    cfg.unit_ids = [1001]
    config_mod.save(cfg)
    assert invoke("reparse").exit_code == 0 and got["ids"] == [1001]


def test_units_pick_saves_selection(home, monkeypatch):
    from edscrape.models import Course

    def c(cid, code, status="active"):
        return Course(cid, code, "n", "2026", "S2", status, "student", [], False, None)

    async def fetch():
        return [
            (c(1001, "FIT1001"), True),
            (c(1002, "FIT2002"), False),
            (c(1003, "OLD1", "archived"), True),
            (c(1004, "FIT4004"), True),
        ]

    monkeypatch.setattr(cli, "_fetch_user_courses", fetch)
    res = invoke("units", "--pick", input="1 2\n")
    assert res.exit_code == 0, res.output
    assert "FIT2002" not in res.output and "OLD1" not in res.output  # hidden choices
    assert config_mod.load().unit_ids == [1001, 1004]
    conn = db.connect()
    assert {r["id"] for r in conn.execute("SELECT id FROM courses WHERE selected = 1")} == {
        1001,
        1004,
    }
    conn.close()
    assert invoke("units", "--pick", input="9\n").exit_code == 2


def _two_keyword_threads():
    c = db.connect()
    add_thread(c, 110, 1001, "Question about A1 marking", "x", number=9)
    add_thread(c, 111, 1001, "ass1 other", "x", number=10)
    c.close()


def test_confirm_enter_saves_nothing_and_asks_again(home, monkeypatch):
    _two_keyword_threads()
    monkeypatch.setattr(brief, "default_run_claude", lambda argv, prompt: "## x\n- y")
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    res = invoke("brief", "fit1001", "A1", input="\n")
    assert res.exit_code == 0, res.output
    assert config_mod.load().assignments == {}
    assert "Include which?" in invoke("brief", "fit1001", "A1", input="\n").output


def test_confirm_explicit_none_excludes_all(home, monkeypatch):
    _two_keyword_threads()
    monkeypatch.setattr(brief, "default_run_claude", lambda argv, prompt: "## x\n- y")
    monkeypatch.setattr(cli, "_is_tty", lambda: True)
    assert invoke("brief", "fit1001", "A1", input="none\n").exit_code == 0
    assert config_mod.load().assignments["1001"]["A1"] == {"keep": [], "exclude": [110, 111]}


def test_brief_cli_prints_model_mismatch_note(home, monkeypatch):
    monkeypatch.setattr(brief, "default_run_claude", lambda argv, prompt: "## x\n- y")
    assert invoke("brief", "fit1001", "A1").exit_code == 0
    res = invoke("brief", "fit1001", "A1", "--model", "opus")
    assert "cached brief was built with sonnet; use --force to rebuild with opus" in res.output
