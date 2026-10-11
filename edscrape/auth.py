"""Browser login (Playwright persistent profile), token file, and auth check.

`login()` is synchronous and blocking (it polls a real browser); call it from the main thread or
inside the TUI's suspend hook. `check()` is async because it uses `EdClient`.

Browser factory surface (what tests fake): `browser_factory(user_data_dir: Path) -> session`, where
`session` has exactly
    goto(url: str) -> None
    evaluate(js: str) -> Any       # returns the JS expression value, or None
    close() -> None
`login()` calls `close()` exactly once, even on timeout or error.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from edscrape import paths
from edscrape.models import AuthStatus

if TYPE_CHECKING:
    from edscrape.api import EdClient

LOGIN_URL = "https://edstem.org/au/login"
TOKEN_JS = "localStorage.getItem('authToken')"
POLL_SECONDS = 2.0


class AuthError(Exception):
    """The Ed token is missing, invalid, or expired (HTTP 401)."""


def load_token() -> str | None:
    """Return the stored token, or None when no token file exists / it is empty."""
    p = paths.token_path()
    if not p.exists():
        return None
    token = p.read_text(encoding="utf-8").strip()
    return token or None


def save_token(token: str) -> Path:
    """Write the token with mode 0600 (created with that mode, never world-readable)."""
    p = paths.token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token.strip())
    os.chmod(p, 0o600)
    return p


def jwt_user_id(token: str) -> int | None:
    """Best-effort `user_id` claim from the JWT payload (no verification)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["user_id"])
    except (IndexError, KeyError, ValueError, TypeError):
        return None


class _PlaywrightSession:
    def __init__(self, user_data_dir: Path) -> None:
        from playwright.sync_api import sync_playwright

        user_data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir), headless=False
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def goto(self, url: str) -> None:
        self._page.goto(url)

    def evaluate(self, js: str) -> Any:
        return self._page.evaluate(js)

    def close(self) -> None:
        try:
            self._ctx.close()
        finally:
            self._pw.stop()


def _playwright_factory(user_data_dir: Path) -> _PlaywrightSession:
    return _PlaywrightSession(user_data_dir)


def login(
    browser_factory: Callable[[Path], Any] | None = None,
    timeout_s: float = 300,
    *,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = POLL_SECONDS,
) -> AuthStatus:
    """Open the Ed login page and wait for the user to sign in; store the token on success.

    Time is counted in `poll_s` steps (not wall clock) so tests with an injected `sleep` are fast.
    """
    factory = browser_factory or _playwright_factory
    session = factory(paths.browser_profile_dir())
    try:
        session.goto(LOGIN_URL)
        elapsed = 0.0
        while True:
            try:
                token = session.evaluate(TOKEN_JS)
            except Exception:  # page mid-navigation, context destroyed, etc.
                token = None
            if isinstance(token, str) and token.strip():
                save_token(token)
                return AuthStatus(ok=True, user_id=jwt_user_id(token), name=None, reason=None)
            if elapsed >= timeout_s:
                return AuthStatus(ok=False, user_id=None, name=None, reason="login timed out")
            sleep(poll_s)
            elapsed += poll_s
    finally:
        session.close()


async def check(client: EdClient | None = None) -> AuthStatus:
    """`GET /api/user`; auth problems come back as `AuthStatus(ok=False, reason=...)`."""
    from edscrape.api import ApiError, EdClient

    if client is None:
        token = load_token()
        if token is None:
            return AuthStatus(ok=False, user_id=None, name=None, reason="no token; run login")
        async with EdClient(token) as owned:
            return await check(owned)
    try:
        data = await client.get_user()
    except AuthError:
        return AuthStatus(ok=False, user_id=None, name=None, reason="token rejected (401)")
    except ApiError as e:
        return AuthStatus(ok=False, user_id=None, name=None, reason=str(e))
    user = data.get("user") or {}
    return AuthStatus(ok=True, user_id=user.get("id"), name=user.get("name"), reason=None)
