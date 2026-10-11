from __future__ import annotations

from edscrape import config, paths

POST_COLS = (
    "key, ref_id, thread_id, course_id, kind, parent_key, number, depth, author_name, "
    "role_class, created_at, content_xml, text, markdown, content_hash"
)


def _add_post(conn, key, kind, parent, text, created):
    ref = int(key.split(":")[1])
    conn.execute(
        f"INSERT INTO posts ({POST_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (key, ref, 1, 10, kind, parent, 1, 0, "A", "staff", created, "", text, text, "h" + key),
    )


def _seed(conn):
    conn.execute(
        "INSERT INTO threads (id, course_id, title, type, is_pinned, created_at, updated_at) "
        "VALUES (1, 10, 'Assignment one question', 'announcement', 1, 't0', 't0')"
    )
    _add_post(conn, "thread:1", "thread", None, "How do I submit the zebra file?", "t0")
    _add_post(conn, "answer:5", "answer", "thread:1", "Use the portal.", "t1")


def test_schema_and_flags(conn):
    _seed(conn)
    rows = {r["post_key"]: r for r in conn.execute("SELECT * FROM post_flags")}
    assert set(rows) == {"thread:1", "answer:5"}
    assert rows["answer:5"]["edited"] == 0
    assert rows["thread:1"]["pinned"] == 1 and rows["thread:1"]["announcement"] == 1
    assert rows["answer:5"]["new"] == 1

    conn.execute("INSERT INTO post_versions VALUES ('answer:5','t1','h1','')")
    conn.execute("INSERT INTO post_versions VALUES ('answer:5','t2','h2','')")
    assert conn.execute(
        "SELECT edited FROM post_flags WHERE post_key='answer:5'"
    ).fetchone()["edited"] == 1

    conn.execute("INSERT INTO read_state VALUES ('answer:5','t3')")
    assert conn.execute(
        "SELECT new FROM post_flags WHERE post_key='answer:5'"
    ).fetchone()["new"] == 0


def test_fts(conn):
    _seed(conn)
    hit = conn.execute("SELECT rowid FROM posts_fts WHERE posts_fts MATCH 'zebra'").fetchall()
    assert len(hit) == 1
    # thread title is indexed on the thread body row and follows title updates
    assert conn.execute("SELECT 1 FROM posts_fts WHERE posts_fts MATCH 'Assignment'").fetchone()
    conn.execute("UPDATE threads SET title='Renamed quokka' WHERE id=1")
    assert conn.execute("SELECT 1 FROM posts_fts WHERE posts_fts MATCH 'quokka'").fetchone()
    assert not conn.execute("SELECT 1 FROM posts_fts WHERE posts_fts MATCH 'Assignment'").fetchone()
    conn.execute("UPDATE posts SET text='changed wombat' WHERE key='answer:5'")
    assert conn.execute("SELECT 1 FROM posts_fts WHERE posts_fts MATCH 'wombat'").fetchone()
    conn.execute("DELETE FROM posts WHERE key='answer:5'")
    assert not conn.execute("SELECT 1 FROM posts_fts WHERE posts_fts MATCH 'wombat'").fetchone()


def test_config_roundtrip(tmp_home):
    cfg = config.load()
    assert cfg.full_recheck is True and cfg.recheck_days == 14
    assert cfg.first_sync_read_before_days == 0
    assert cfg.concurrency == 4 and cfg.rate_per_sec == 8
    assert cfg.roles["tutor"] == "staff" and cfg.roles["mentor"] == "helper"
    assert cfg.brief_model == "sonnet" and cfg.assignments == {}
    cfg.unit_ids = [39026]
    cfg.assignments = {"39026": {"A1": {"keep": [1], "exclude": [2]}}}
    config.save(cfg)
    assert (tmp_home / "config.toml").exists()
    assert config.load() == cfg


def test_paths_home_override(tmp_home):
    assert paths.config_dir() == tmp_home
    assert paths.db_path().parent == tmp_home
    assert paths.token_path().name == "token"
