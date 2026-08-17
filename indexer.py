"""
Enhanced indexer with embedding pre‑compute hook, version vector support,
and plugin‑style chunk type handling.
"""
import os
import sqlite3
import time
import json
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from parse_ctx import parse_ctx_file, sha256_of_body

# ----------------------------------------------------------------------
# Optional embedding model (lazy load)
# ----------------------------------------------------------------------
try:
    from sentence_transformers import SentenceTransformer
    _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    _EMBEDDING_AVAILABLE = True
except Exception:  # pragma: no cover
    _embed_model = None
    _EMBEDDING_AVAILABLE = False

def compute_embedding(text: str) -> Optional[bytes]:
    """Return a base64‑encoded embedding vector or None if model not available."""
    if not _EMBEDDING_AVAILABLE or not text.strip():
        return None
    vec = _embed_model.encode([text], normalize_embeddings=True)[0]  # shape (384,)
    # Convert to raw bytes (float32) then base64 for storage in header
    import base64, struct
    raw = struct.pack(f"{len(vec)}f", *vec)
    return base64.b64encode(raw)


# ----------------------------------------------------------------------
# DB helper functions (updated for version vector)
# ----------------------------------------------------------------------
def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    schema_path = Path(__file__).with_name("schema.sql")
    with schema_path.open("r") as f:
        conn.executescript(f.read())
    return conn


def upsert_file(conn: sqlite3.Connection, vault_root: Path, rel_path: Path,
                header: dict, body: str) -> int:
    """
    Insert or update a file record. Returns the file.id.
    Handles version vector and embedding pre‑compute.
    """
    # ---- compute body hash and maybe update header ----
    content_hash = sha256_of_body(body)
    expected_id = f"sha256:{content_hash}"
    # Version vector: a simple integer counter stored in header under "vv"
    # Increment on each upsert; if missing start at 1.
    vv = header.get("vv", 0) + 1
    header["vv"] = vv
    header["id"] = expected_id
    header["updated"] = int(time.time())

    # ---- ensure embeddings exist for configured chunk types ----
    # We'll add embeddings for chunks of type "summary" and "definition" if missing.
    # In a real implementation you could make this configurable.
    chunk_types_to_embed = {"summary", "definition"}
    full_path = vault_root / rel_path
    _, _, chunks = parse_ctx_file(full_path)
    embed_updates = {}
    for ch in chunks:
        if ch["type"] in chunk_types_to_embed and "embedding" not in ch.get("attrs", {}):
            emb = compute_embedding(ch["text"])
            if emb:
                embed_updates[ch["hash"]] = emb  # map chunk hash -> embedding
    # Store embeddings in the chunks table; we could also mirror them in header
    # but keeping them in the table is sufficient for agent access.

    # ---- upsert file row ----
    cur = conn.execute(
        """
        INSERT INTO files(path, title, updated, content_hash, vv)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            title=excluded.title,
            updated=excluded.updated,
            content_hash=excluded.content_hash,
            vv=excluded.vv
        RETURNING id
        """,
        (str(rel_path),
         header.get("title") or rel_path.stem,
         header.get("updated", int(time.time())),
         header["id"].replace("sha256:", ""),
         header["vv"]),
    )
    file_id = cur.fetchone()[0]

    # ---- clear old relations ----
    conn.execute("DELETE FROM tags WHERE file_id=?", (file_id,))
    conn.execute("DELETE FROM links WHERE src_id=?", (file_id,))
    conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))

    # ---- tags ----
    for tag in header.get("tags", []):
        conn.execute(
            "INSERT INTO tags(file_id, tag) VALUES (?, ?)",
            (file_id, tag),
        )

    # ---- links (resolve target now) ----
    for link in header.get("links", []):
        target = link.get("target", "")
        link_type = link.get("type", "related")
        try:
            target_path = (vault_root / target).resolve()
            rel_target = target_path.relative_to(vault_root)
        except Exception:
            continue  # skip broken/external
        dst_cur = conn.execute(
            "SELECT id FROM files WHERE path=?", (str(rel_target),)
        )
        dst_row = dst_cur.fetchone()
        if dst_row:
            dst_id = dst_row[0]
            conn.execute(
                "INSERT INTO links(src_id, dst_id, link_type) VALUES (?, ?, ?)",
                (file_id, dst_id, link_type),
            )

    # ---- chunks ----
    for i, ch in enumerate(chunks):
        emb_blob = None
        if ch["hash"] in embed_updates:
            emb_blob = embed_updates[ch["hash"]]
        conn.execute(
            """
            INSERT INTO chunks(file_id, chunk_type, ordinal, content_hash, text, embedding)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                ch["type"],
                ch["ordinal"],
                ch["hash"],
                ch["text"],
                emb_blob,
            ),
        )
    conn.commit()
    return file_id


def delete_file(conn: sqlite3.Connection, vault_root: Path, rel_path: Path):
    """Remove a file and all its relations."""
    cur = conn.execute("SELECT id FROM files WHERE path=?", (str(rel_path),))
    row = cur.fetchone()
    if row:
        file_id = row[0]
        conn.execute("DELETE FROM files WHERE id=?", (file_id,))
        conn.commit()


# ----------------------------------------------------------------------
# Watchdog event handler
# ----------------------------------------------------------------------
class CtxHandler(FileSystemEventHandler):
    def __init__(self, conn: sqlite3.Connection, vault_root: Path):
        self.conn = conn
        self.vault_root = vault_root

    def _handle(self, event_path: str):
        p = Path(event_path)
        if not p.is_file() or p.suffix != ".ctx":
            return
        try:
            rel = p.relative_to(self.vault_root)
        except ValueError:
            return
        # Read full file to get body and header
        full_text = p.read_text(encoding="utf-8")
        header = {}
        # Parse header if present
        if full_text.startswith("---CTX-HEADER---"):
            try:
                _, json_part, _ = full_text.split("---CTX-HEADER---", 2)
                json_part, _ = json_part.split("---CTX-HEADER---", 1)
                header = json.loads(json_part.strip())
            except Exception:
                header = {}
        # Body is everything outside the header
        if header:
            parts = full_text.split("---CTX-HEADER---")
            if len(parts) >= 3:
                body = parts[0] + parts[2]
            else:
                body = full_text
        else:
            body = full_text
        upsert_file(self.conn, self.vault_root, rel, header, body)

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith(".ctx"):
            try:
                rel = Path(event.src_path).relative_to(self.vault_root)
                delete_file(self.conn, self.vault_root, rel)
            except ValueError:
                pass


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main(vault_root: str, db_path: str):
    vault = Path(vault_root).resolve()
    db = Path(db_path).resolve()
    vault.mkdir(parents=True, exist_ok=True)
    conn = init_db(db)

    # Initial full scan
    print(f"Scanning vault {vault} ...")
    for ctx_file in vault.rglob("*.ctx"):
        try:
            rel = ctx_file.relative_to(vault)
            full_text = ctx_file.read_text(encoding="utf-8")
            header = {}
            if full_text.startswith("---CTX-HEADER---"):
                try:
                    _, json_part, _ = full_text.split("---CTX-HEADER---", 2)
                    json_part, _ = json_part.split("---CTX-HEADER---", 1)
                    header = json.loads(json_part.strip())
                except Exception:
                    header = {}
            if header:
                parts = full_text.split("---CTX-HEADER---")
                if len(parts) >= 3:
                    body = parts[0] + parts[2]
                else:
                    body = full_text
            else:
                body = full_text
            upsert_file(conn, vault, rel, header, body)
        except Exception as e:
            print(f"Error processing {ctx_file}: {e}")

    print("Starting watchdog observer ...")
    observer = Observer()
    observer.schedule(CtxHandler(conn, vault), str(vault), recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping observer...")
        observer.stop()
    observer.join()
    conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Indexer for .ctx vault")
    parser.add_argument(
        "--vault",
        default="~/ai-vault",
        help="Path to the vault directory (default: ~/ai-vault)",
    )
    parser.add_argument(
        "--db",
        default="./vault.db",
        help="Path to SQLite database file (default: ./vault.db)",
    )
    args = parser.parse_args()
    main(os.path.expanduser(args.vault), args.db)