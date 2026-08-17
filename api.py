"""
FastAPI query layer for the .ctx vault.
Provides /search, /graph, /chunk, /stats endpoints.
"""
import sqlite3
from pathlib import Path
from typing import List, Optional
import os

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

app = FastAPI(title="CTX Vault API", version="0.1.0")

# ----------------------------------------------------------------------
# Configuration (could be moved to env vars or config file)
# ----------------------------------------------------------------------
# Explicit DB path takes precedence over vault root
DB_PATH = Path(os.environ.get("CTX_DB_PATH", ""))
if not DB_PATH:
    VAULT_ROOT = Path(os.environ.get("CTX_VAULT_ROOT", Path.home() / "ai-vault"))
    DB_PATH = VAULT_ROOT / "vault.db"

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

# ----------------------------------------------------------------------
# Pydantic models
# ----------------------------------------------------------------------
class SearchResult(BaseModel):
    path: str
    title: str
    chunk_type: str
    snippet: str
    score: float
    links: List[dict]
    tags: List[str]

class GraphNode(BaseModel):
    path: str
    title: str
    updated: int

class GraphEdge(BaseModel):
    dst: str
    type: str

class GraphResponse(BaseModel):
    note: GraphNode
    out: List[GraphEdge]
    incoming: List[GraphEdge]

class ChunkResponse(BaseModel):
    path: str
    title: str
    chunk_type: str
    ordinal: int
    content_hash: str
    text: str
    embedding: Optional[str] = None  # base64 if present

class StatsResponse(BaseModel):
    notes: int
    links: int
    chunks: int
    avg_chunk_len: float
    update_latency_ms: float

# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------
@app.get("/search", response_model=List[SearchResult])
def search(
    q: str = Query(..., description="Search query"),
    limit: int = Query(10, ge=1, le=100),
):
    conn = get_conn()
    try:
        # Use FTS5 to rank matches
        rows = conn.execute(
            """
            SELECT
                f.path,
                f.title,
                c.chunk_type,
                snippet(chunks_fts, 0, '<b>', '</b>', '...', 10) AS snippet,
                bm25(chunks_fts) AS score,
                c.ordinal
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN files f ON f.id = c.file_id
            WHERE chunks_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (q, limit),
        ).fetchall()
        results = []
        for r in rows:
            # fetch links and tags for the parent file
            links = conn.execute(
                "SELECT dst_id, link_type FROM links WHERE src_id = (SELECT id FROM files WHERE path=?)",
                (r["path"],),
            ).fetchall()
            link_list = [
                {"target": conn.execute("SELECT path FROM files WHERE id=?", (lid,)).fetchone()[0],
                 "type": ltype}
                for lid, ltype in links
            ]
            tags = [row[0] for row in conn.execute(
                "SELECT tag FROM tags WHERE file_id = (SELECT id FROM files WHERE path=?)", (r["path"],)
            ).fetchall()]
            results.append(
                SearchResult(
                    path=r["path"],
                    title=r["title"],
                    chunk_type=r["chunk_type"],
                    snippet=r["snippet"].replace("<b>", "**").replace("</b>", "**"),  # simple markdown emphasis
                    score=float(r["score"]),
                    links=link_list,
                    tags=tags,
                )
            )
        return results
    finally:
        conn.close()

@app.get("/graph", response_model=GraphResponse)
def graph(note: str = Query(..., description="Relative path to .ctx file")):
    conn = get_conn()
    try:
        # Find file id
        row = conn.execute(
            "SELECT id, title, updated FROM files WHERE path=?", (note,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Note not found")
        note_id, title, updated = row
        # Outgoing links
        out_rows = conn.execute(
            """
            SELECT f.path AS dst, l.link_type
            FROM links l
            JOIN files f ON f.id = l.dst_id
            WHERE l.src_id = ?
            """,
            (note_id,),
        ).fetchall()
        out_edges = [GraphEdge(dst=r["dst"], type=r["link_type"]) for r in out_rows]
        # Incoming links
        in_rows = conn.execute(
            """
            SELECT f.path AS src, l.link_type
            FROM links l
            JOIN files f ON f.id = l.src_id
            WHERE l.dst_id = ?
            """,
            (note_id,),
        ).fetchall()
        in_edges = [GraphEdge(dst=r["src"], type=r["link_type"]) for r in in_rows]
        return GraphResponse(
            note=GraphNode(path=note, title=title, updated=updated),
            out=out_edges,
            incoming=in_edges,
        )
    finally:
        conn.close()

@app.get("/chunk", response_model=ChunkResponse)
def chunk(
    id: str = Query(..., description="Chunk content hash (hex)"),
):
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT
                f.path,
                f.title,
                c.chunk_type,
                c.ordinal,
                c.content_hash,
                c.text,
                c.embedding
            FROM chunks c
            JOIN files f ON f.id = c.file_id
            WHERE c.content_hash = ?
            """,
            (id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Chunk not found")
        return ChunkResponse(
            path=row["path"],
            title=row["title"],
            chunk_type=row["chunk_type"],
            ordinal=row["ordinal"],
            content_hash=row["content_hash"],
            text=row["text"],
            embedding=row["embedding"].decode("utf-8") if row["embedding"] else None,
        )
    finally:
        conn.close()

@app.get("/stats", response_model=StatsResponse)
def stats():
    conn = get_conn()
    try:
        notes = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        avg_len = conn.execute(
            "SELECT AVG(LENGTH(text)) FROM chunks"
        ).fetchone()[0] or 0.0
        # crude latency: average time to update a file (we could store metrics)
        update_latency_ms = 2.0  # placeholder
        return StatsResponse(
            notes=notes,
            links=links,
            chunks=chunks,
            avg_chunk_len=float(avg_len),
            update_latency_ms=update_latency_ms,
        )
    finally:
        conn.close()

# ----------------------------------------------------------------------
# Run with: uvicorn api:app --reload
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=True)