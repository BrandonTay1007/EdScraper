-- FROZEN CONTRACT (plan.md section 3 "Store"). Changes go through the orchestrator.
-- Flags are read only through the post_flags view.

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    code TEXT,
    name TEXT,
    year TEXT,
    session TEXT,
    status TEXT,
    role TEXT,
    categories_json TEXT,
    selected INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT
);

CREATE TABLE IF NOT EXISTS user_course_roles (
    user_id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    course_role TEXT,
    PRIMARY KEY (user_id, course_id)
);

CREATE TABLE IF NOT EXISTS threads (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL,
    number INTEGER,
    type TEXT,
    title TEXT,
    category TEXT,
    subcategory TEXT,
    subsubcategory TEXT,
    accepted_id INTEGER,
    editor_id INTEGER,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    is_endorsed INTEGER NOT NULL DEFAULT 0,
    is_answered INTEGER NOT NULL DEFAULT 0,
    is_staff_answered INTEGER NOT NULL DEFAULT 0,
    is_private INTEGER NOT NULL DEFAULT 0,
    reply_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    list_body_hash TEXT,
    change_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_threads_course ON threads(course_id);

CREATE TABLE IF NOT EXISTS posts (
    key TEXT PRIMARY KEY,
    ref_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    parent_key TEXT,
    number INTEGER,
    depth INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER,
    author_name TEXT,
    role_class TEXT,
    is_anonymous INTEGER NOT NULL DEFAULT 0,
    is_own INTEGER NOT NULL DEFAULT 0,
    is_endorsed INTEGER NOT NULL DEFAULT 0,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    editor_id INTEGER,
    created_at TEXT,
    updated_at TEXT,
    content_xml TEXT,
    text TEXT,
    markdown TEXT,
    content_hash TEXT,
    later_staff_reply INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT,
    last_seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_thread ON posts(thread_id);
CREATE INDEX IF NOT EXISTS idx_posts_course ON posts(course_id);
CREATE INDEX IF NOT EXISTS idx_posts_role ON posts(role_class);

CREATE TABLE IF NOT EXISTS post_versions (
    post_key TEXT NOT NULL,
    seen_at TEXT,
    content_hash TEXT NOT NULL,
    content_xml TEXT,
    PRIMARY KEY (post_key, content_hash)
);

CREATE TABLE IF NOT EXISTS read_state (
    post_key TEXT PRIMARY KEY,
    read_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    course_id INTEGER,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    threads_seen INTEGER,
    threads_changed INTEGER,
    posts_new INTEGER,
    posts_changed INTEGER,
    error TEXT
);

-- Standalone FTS5 table; rowid = posts.rowid. title is the thread title for kind='thread'.
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(title, text);

CREATE TRIGGER IF NOT EXISTS posts_fts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, title, text) VALUES (
        new.rowid,
        CASE WHEN new.kind = 'thread'
             THEN COALESCE((SELECT title FROM threads WHERE id = new.thread_id), '')
             ELSE '' END,
        COALESCE(new.text, ''));
END;

CREATE TRIGGER IF NOT EXISTS posts_fts_au AFTER UPDATE ON posts BEGIN
    DELETE FROM posts_fts WHERE rowid = old.rowid;
    INSERT INTO posts_fts(rowid, title, text) VALUES (
        new.rowid,
        CASE WHEN new.kind = 'thread'
             THEN COALESCE((SELECT title FROM threads WHERE id = new.thread_id), '')
             ELSE '' END,
        COALESCE(new.text, ''));
END;

CREATE TRIGGER IF NOT EXISTS posts_fts_ad AFTER DELETE ON posts BEGIN
    DELETE FROM posts_fts WHERE rowid = old.rowid;
END;

-- Re-index the thread body row when the thread title changes.
CREATE TRIGGER IF NOT EXISTS threads_title_fts_au AFTER UPDATE OF title ON threads BEGIN
    DELETE FROM posts_fts WHERE rowid IN (
        SELECT rowid FROM posts WHERE key = 'thread:' || new.id);
    INSERT INTO posts_fts(rowid, title, text)
        SELECT rowid, COALESCE(new.title, ''), COALESCE(text, '')
        FROM posts WHERE key = 'thread:' || new.id;
END;

CREATE VIEW IF NOT EXISTS post_flags AS
SELECT
    p.key AS post_key,
    (p.editor_id IS NOT NULL
        OR (SELECT COUNT(*) FROM post_versions v WHERE v.post_key = p.key) > 1) AS edited,
    p.later_staff_reply AS later_staff_reply,
    p.accepted AS accepted,
    p.is_endorsed AS endorsed,
    p.is_deleted AS deleted,
    COALESCE(t.is_pinned, 0) AS pinned,
    COALESCE(t.type = 'announcement', 0) AS announcement,
    NOT EXISTS (SELECT 1 FROM read_state r WHERE r.post_key = p.key) AS new
FROM posts p
LEFT JOIN threads t ON t.id = p.thread_id;
