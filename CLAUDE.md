# EdScrape

Terminal app that logs into Ed Discussion (edstem.org/au) and surfaces teaching-team messages. Design and task list: `docs/scratchpad/edscrape-v1/plan.md` (local only).

## Layout
- `edscrape/models.py`, `edscrape/store/schema.sql`, `edscrape/config.py`: frozen shared contracts. Do not change without the orchestrator.
- `edscrape/paths.py`: XDG dirs; `EDSCRAPE_HOME` overrides both config and data.
- Planned: `auth.py`, `api.py`, `sync.py`, `edparse.py`, `flags.py`, `links.py`, `store/db.py`, `core/`, `cli.py`, `tui/`.
- `tests/`: `conftest.py` provides `tmp_home` and `conn` (in-memory schema) fixtures.

## Commands
- `uv sync`
- `uv run pytest`
- `uv run ruff check .`

## Conventions
- GET only against Ed. Never call `/renew_token`, `/read`, `/glance` or any non-GET.
- No network in tests (respx with `assert_all_mocked`). Fixtures are anonymised.
- Read flags only through the `post_flags` view.
- Post keys: `thread:<id>`, `answer:<id>`, `comment:<id>` (id sequences collide, so never use bare ids).
- Commits: Conventional Commits, no agent attribution.
