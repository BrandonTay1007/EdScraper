# EdScrape

Keyboard-driven terminal app that syncs your Ed Discussion units into SQLite and surfaces every teaching-team message (new, edited, superseded) with deep links, local search, export, and cached per-assignment briefs via `claude -p`.

## Install
```sh
uv sync
uv run playwright install chromium
```

## Usage
```sh
uv run edscrape login          # opens a browser; log in to Ed once
uv run edscrape                # launch the TUI
uv run edscrape pull --unit FIT2109
uv run edscrape inbox --unit FIT2109
uv run edscrape search "assignment"
uv run edscrape brief FIT2109 "A1"
```

Data lives in `~/.local/share/edscrape`, config in `~/.config/edscrape/config.toml` (override both with `EDSCRAPE_HOME`). The app only issues GET requests to Ed.

Status: scaffold; commands arrive with later phases.
