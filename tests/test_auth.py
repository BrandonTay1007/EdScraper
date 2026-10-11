from __future__ import annotations

import base64
import json
import stat

import httpx
import pytest
import respx

from edscrape import paths
from edscrape.api import EdClient
from edscrape.auth import LOGIN_URL, AuthError, check, load_token, login, save_token

API = "https://edstem.org/api"


def make_jwt(user_id: int = 900001) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'HS256'})}.{b64({'user_id': user_id})}.sig"


class FakeSession:
    def __init__(self, script):
        self.script = list(script)  # values or exceptions returned by successive evaluate() calls
        self.visited: list[str] = []
        self.evaluated: list[str] = []
        self.closed = 0

    def goto(self, url):
        self.visited.append(url)

    def evaluate(self, js):
        self.evaluated.append(js)
        item = self.script.pop(0) if self.script else None
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed += 1


def factory_for(session, seen_dirs):
    def factory(user_data_dir):
        seen_dirs.append(user_data_dir)
        return session

    return factory


def test_login_polls_until_token_and_writes_0600(tmp_home):
    token = make_jwt(42)
    session = FakeSession([None, RuntimeError("Execution context was destroyed"), "", token])
    sleeps, dirs = [], []
    status = login(factory_for(session, dirs), timeout_s=60, sleep=sleeps.append)
    assert status.ok and status.user_id == 42 and status.reason is None
    assert session.visited == [LOGIN_URL] and session.closed == 1
    assert "authToken" in session.evaluated[0]
    assert sleeps == [2.0, 2.0, 2.0]
    assert dirs == [paths.browser_profile_dir()]
    assert load_token() == token
    assert stat.S_IMODE(paths.token_path().stat().st_mode) == 0o600


def test_login_timeout_closes_browser_and_writes_nothing(tmp_home):
    session = FakeSession([])
    sleeps = []
    status = login(factory_for(session, []), timeout_s=6, sleep=sleeps.append)
    assert not status.ok and "timed out" in (status.reason or "")
    assert session.closed == 1 and not paths.token_path().exists()
    assert sleeps == [2.0, 2.0, 2.0]


def test_login_closes_browser_when_goto_fails(tmp_home):
    class Boom(FakeSession):
        def goto(self, url):
            raise OSError("no network")

    session = Boom([])
    with pytest.raises(OSError):
        login(factory_for(session, []), sleep=lambda s: None)
    assert session.closed == 1


def test_save_token_tightens_existing_file_mode(tmp_home):
    paths.token_path().parent.mkdir(parents=True, exist_ok=True)
    paths.token_path().write_text("old")
    paths.token_path().chmod(0o644)
    save_token("new-token\n")
    assert load_token() == "new-token"
    assert stat.S_IMODE(paths.token_path().stat().st_mode) == 0o600


def test_load_token_missing_or_empty(tmp_home):
    assert load_token() is None
    paths.token_path().parent.mkdir(parents=True, exist_ok=True)
    paths.token_path().write_text("  \n")
    assert load_token() is None


async def test_check_ok_sends_token_header(tmp_home):
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(f"{API}/user").mock(
            return_value=httpx.Response(200, json={"user": {"id": 5, "name": "Me"}, "courses": []})
        )
        async with EdClient("tok-123", rate_per_sec=0) as client:
            status = await check(client)
        assert route.calls.last.request.headers["x-token"] == "tok-123"
        assert route.calls.last.request.method == "GET"
    assert status.ok and status.user_id == 5 and status.name == "Me"


async def test_check_401_reports_not_ok_and_client_raises(tmp_home):
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{API}/user").mock(
            return_value=httpx.Response(401, json={"code": "unknown", "message": "Invalid token"})
        )
        async with EdClient("bad", rate_per_sec=0) as client:
            status = await check(client)
            with pytest.raises(AuthError):
                await client.get_user()
    assert not status.ok and "401" in status.reason


async def test_check_without_token_does_not_touch_network(tmp_home):
    with respx.mock(assert_all_mocked=True):
        status = await check()
    assert not status.ok and "no token" in status.reason


async def test_check_loads_stored_token(tmp_home):
    save_token("stored")
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(f"{API}/user").mock(
            return_value=httpx.Response(200, json={"user": {"id": 1, "name": "N"}})
        )
        status = await check()
        assert route.calls.last.request.headers["x-token"] == "stored"
    assert status.ok
