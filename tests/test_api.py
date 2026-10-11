from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import respx

from edscrape import paths
from edscrape.api import ApiError, EdClient
from edscrape.auth import AuthError

API = "https://edstem.org/api"
FIX = Path(__file__).parent / "fixtures" / "api"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text("utf-8"))


class Sleeper:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def client(sleeper: Sleeper | None = None, **kw) -> EdClient:
    kw.setdefault("rate_per_sec", 0)
    kw.setdefault("jitter", lambda: 0.0)
    return EdClient("tok", sleep=sleeper or Sleeper(), **kw)


def page_handler(pages: dict[int, dict], seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        return httpx.Response(200, json=pages[int(params.get("offset", 0))])

    return handler


async def test_paging_short_page_and_dedupe(tmp_home):
    p0, p1 = load("list_0.json"), load("list_1.json")
    assert len(p0["threads"]) == 100 and len(p1["threads"]) == 37
    seen: list[dict] = []
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{API}/courses/39026/threads").mock(
            side_effect=page_handler({0: p0, 100: p1}, seen)
        )
        async with client() as c:
            ids = [t["id"] async for t in c.iter_threads(39026)]
    assert [p["offset"] for p in seen] == ["0", "100"]  # stops at the short page
    assert all(p["limit"] == "100" and p["sort"] == "new" for p in seen)
    assert len(ids) == 100 + 37 - 2 and len(set(ids)) == len(ids)
    raw = paths.raw_dir() / "39026" / "list"
    assert json.loads((raw / "0.json").read_text()) == p0
    assert json.loads((raw / "100.json").read_text()) == p1


async def test_exactly_full_last_page_requests_one_more(tmp_home):
    full = load("list_0.json")
    empty = {"sort_key": "", "threads": [], "users": []}
    seen: list[dict] = []
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{API}/courses/1/threads").mock(
            side_effect=page_handler({0: full, 100: empty}, seen)
        )
        async with client() as c:
            ids = [t["id"] async for t in c.iter_threads(1)]
    assert len(ids) == 100 and len(seen) == 2


async def test_get_thread_has_no_view_param_and_archives(tmp_home):
    detail = load("thread_3626361.json")
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(f"{API}/threads/3626361").mock(
            return_value=httpx.Response(200, json=detail)
        )
        async with client() as c:
            got = await c.get_thread(3626361)
        req = route.calls.last.request
    assert got == detail
    assert "view" not in req.url.params and req.url.query == b""
    assert req.method == "GET" and req.headers["x-token"] == "tok"
    saved = paths.raw_dir() / "39026" / "threads" / "3626361.json"
    assert json.loads(saved.read_text()) == detail


async def test_get_user_archives_user_json(tmp_home):
    user = load("user.json")
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{API}/user").mock(return_value=httpx.Response(200, json=user))
        async with client() as c:
            assert await c.get_user() == user
    assert json.loads((paths.raw_dir() / "user.json").read_text()) == user


async def test_archive_can_be_disabled(tmp_home):
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{API}/user").mock(return_value=httpx.Response(200, json=load("user.json")))
        async with client(archive=False) as c:
            await c.get_user()
    assert not (paths.raw_dir() / "user.json").exists()


async def test_503_then_200_retries_with_backoff(tmp_home):
    sleeper = Sleeper()
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(f"{API}/user").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(503),
                httpx.Response(200, json={"ok": 1}),
            ]
        )
        async with client(sleeper) as c:
            assert await c.get_user() == {"ok": 1}
        assert route.call_count == 3
    assert sleeper.calls == [0.5, 1.0]


async def test_backoff_schedule_exponential_with_jitter(tmp_home):
    sleeper = Sleeper()
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(f"{API}/user").mock(return_value=httpx.Response(500))
        async with client(sleeper, jitter=lambda: 1.0) as c:
            with pytest.raises(ApiError) as exc:
                await c.get_user()
        assert route.call_count == 6  # first try + 5 retries
    assert exc.value.status == 500
    assert sleeper.calls == [1.0, 1.5, 2.5, 4.5, 8.5]  # 0.5*2**n + jitter*0.5


async def test_429_honours_retry_after(tmp_home):
    sleeper = Sleeper()
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{API}/user").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "7"}),
                httpx.Response(200, json={}),
            ]
        )
        async with client(sleeper) as c:
            await c.get_user()
    assert sleeper.calls == [7.0]


async def test_401_raises_auth_error_without_retry(tmp_home):
    sleeper = Sleeper()
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(f"{API}/threads/9").mock(
            return_value=httpx.Response(401, json={"code": "unknown", "message": "Invalid token"})
        )
        async with client(sleeper) as c:
            with pytest.raises(AuthError):
                await c.get_thread(9)
        assert route.call_count == 1
    assert sleeper.calls == []


@pytest.mark.parametrize("status", [403, 404])
async def test_403_404_raise_api_error_without_retry(tmp_home, status):
    sleeper = Sleeper()
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(f"{API}/threads/9").mock(return_value=httpx.Response(status))
        async with client(sleeper) as c:
            with pytest.raises(ApiError) as exc:
                await c.get_thread(9)
        assert route.call_count == 1
    assert exc.value.status == status and sleeper.calls == []


async def test_transport_error_is_retried(tmp_home):
    sleeper = Sleeper()
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{API}/user").mock(
            side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={"a": 1})]
        )
        async with client(sleeper) as c:
            assert await c.get_user() == {"a": 1}
    assert sleeper.calls == [0.5]


async def test_rate_limiter_spaces_requests(tmp_home):
    sleeper = Sleeper()
    now = [100.0]
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{API}/user").mock(return_value=httpx.Response(200, json={}))
        async with client(sleeper, rate_per_sec=2, clock=lambda: now[0]) as c:
            for _ in range(3):
                await c.get_user()
    assert sleeper.calls == [0.5, 1.0]  # slots at +0, +0.5, +1.0 with a frozen clock


async def test_missing_token_refuses_to_start(tmp_home):
    with pytest.raises(AuthError):
        async with EdClient():
            pass


def test_client_exposes_get_methods_only():
    public = {n for n in dir(EdClient) if not n.startswith("_")}
    assert public == {"get_user", "list_threads", "iter_pages", "iter_threads", "get_thread"}
    for forbidden in ("post", "put", "patch", "delete", "request", "send"):
        assert not hasattr(EdClient, forbidden)


async def test_archive_write_is_atomic(tmp_home, monkeypatch):
    target = paths.raw_dir() / "user.json"
    target.parent.mkdir(parents=True)
    target.write_text("old")
    real_replace = os.replace
    seen = []

    def spy(src, dst):
        seen.append((Path(src).name, Path(src).read_text()))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    with respx.mock(assert_all_mocked=True) as router:
        router.get(f"{API}/user").mock(return_value=httpx.Response(200, json={"a": 1}))
        async with client() as c:
            await c.get_user()
    assert seen == [("user.json.tmp", '{"a":1}')]
    assert json.loads(target.read_text()) == {"a": 1}
    assert not list(target.parent.glob("*.tmp"))
