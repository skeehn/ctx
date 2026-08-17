-- SQLite schema for .ctx vault
PRAGMA foreign_keys = ON;

-- files: one row per .ctx file
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE,        -- relative to vault root
    title       TEXT,
    updated     INTEGER,
    content_hash TEXT,               -- matches header.id without "sha256:" prefix
    vv          INTEGER              -- version vector for conflict-free sync
);

-- tags: many‑to‑many
CREATE TABLE IF NOT EXISTS tags (
    file_id     INTEGER,
    tag         TEXT,
    PRIMARY KEY (file_id, tag),
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

-- links: typed, directed
CREATE TABLE IF NOT EXISTS links (
    src_id      INTEGER,
    dst_id      INTEGER,
    link_type   TEXT,
    PRIMARY KEY (src_id, dst_id, link_type),
    FOREIGN KEY(src_id) REFERENCES files(id) ON DELETE CASCADE,
    FOREIGN KEY(dst_id) REFERENCES files(id) ON DELETE CASCADE
);

-- chunks: one per named block + a whole‑file fallback chunk
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER,
    chunk_type  TEXT,               -- e.g. "summary", "code/python", "note"
    ordinal     INTEGER,            -- order inside the file
    content_hash TEXT,              -- hash of the block’s raw text
    text        TEXT,               -- the block’s actual content (could be large)
    embedding   BLOB,               -- optional, matches header.embeddings if present
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

-- Optional FTS5 for instant keyword search over chunk.text
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    content='chunks',
    content_rowid='id'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content) VALUES (NEW.id, NEW.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', OLD.id, OLD.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', OLD.id, OLD.text);
    INSERT INTO chunks_fts(rowid, content) VALUES (NEW.id, NEW.text);
END;