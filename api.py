"""
FastAPI query layer for the .ctx vault.
Provides /search, /graph, /chunk, /stats, /file, /skills, /agents, /insights, /canvas, /tags, /daily endpoints.
"""
import sqlite3
from pathlib import Path
from typing import List, Optional, Dict, Any
import os
import uuid
import time
from datetime import date

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

app = FastAPI(title="CTX Vault API", version="0.3.0")

# ----------------------------------------------------------------------
# Configuration (could be moved to env vars or config file)
# ----------------------------------------------------------------------
# Explicit DB path takes precedence over vault root
DB_PATH_STR = os.environ.get("CTX_DB_PATH", "")
if DB_PATH_STR:
    DB_PATH = Path(DB_PATH_STR)
else:
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

class FileResponse(BaseModel):
    path: str
    title: str
    updated: int
    content_hash: str
    header: dict
    body: str
    chunks: List[ChunkResponse]
    links: List[dict]
    tags: List[str]

class FileRawResponse(BaseModel):
    path: str
    content: str
    content_type: str = "text/plain"

class ChunkListResponse(BaseModel):
    path: str
    title: str
    chunks: List[ChunkResponse]

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
                c.content,
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
            text=row["content"],
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
            "SELECT AVG(LENGTH(content)) FROM chunks"
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
# Full file retrieval endpoints
# ----------------------------------------------------------------------
@app.get("/file", response_model=FileResponse)
def get_file(
    path: str = Query(..., description="Relative path to .ctx file"),
):
    """Get complete file with header, body, chunks, links, and tags."""
    conn = get_conn()
    try:
        # Get file record
        file_row = conn.execute(
            "SELECT id, path, title, updated, content_hash FROM files WHERE path=?",
            (path,)
        ).fetchone()
        if not file_row:
            raise HTTPException(status_code=404, detail="File not found")
        
        file_id, file_path, title, updated, content_hash = file_row
        
        # Get header from tags/links (reconstruct)
        tags = [row[0] for row in conn.execute(
            "SELECT tag FROM tags WHERE file_id = ?", (file_id,)
        ).fetchall()]
        
        link_rows = conn.execute(
            """SELECT f.path AS dst, l.link_type
               FROM links l
               JOIN files f ON f.id = l.dst_id
               WHERE l.src_id = ?""",
            (file_id,)
        ).fetchall()
        links = [{"target": r["dst"], "type": r["link_type"]} for r in link_rows]
        
        # Read raw file content from disk for body only
        vault_root = DB_PATH.parent if os.environ.get("CTX_DB_PATH") else Path(os.environ.get("CTX_VAULT_ROOT", Path.home() / "ai-vault"))
        full_path = vault_root / path
        
        body = ""
        header = {
            "v": 1,
            "id": f"sha256:{content_hash}",
            "updated": updated,
            "author": "",
            "tags": tags,
            "links": links,
            "embeddings": {},
        }
        if full_path.exists():
            raw_content = full_path.read_text(encoding="utf-8")
            from parse_ctx import parse_ctx_file
            _, body, _ = parse_ctx_file(full_path)
        
        # Get chunks
        chunk_rows = conn.execute(
            """SELECT chunk_type, ordinal, content_hash, content, embedding
               FROM chunks WHERE file_id = ? ORDER BY ordinal""",
            (file_id,)
        ).fetchall()
        chunks = []
        for cr in chunk_rows:
            chunks.append(ChunkResponse(
                path=file_path,
                title=title,
                chunk_type=cr["chunk_type"],
                ordinal=cr["ordinal"],
                content_hash=cr["content_hash"],
                text=cr["content"],
                embedding=cr["embedding"].decode("utf-8") if cr["embedding"] else None,
            ))
        
        return FileResponse(
            path=file_path,
            title=title,
            updated=updated,
            content_hash=content_hash,
            header=header,
            body=body,
            chunks=chunks,
            links=links,
            tags=tags,
        )
    finally:
        conn.close()


@app.get("/file/raw", response_model=FileRawResponse)
def get_file_raw(
    path: str = Query(..., description="Relative path to .ctx file"),
):
    """Get raw file content as plain text."""
    # Use vault root from env, fallback to DB parent
    vault_root = Path(os.environ.get("CTX_VAULT_ROOT", DB_PATH.parent))
    full_path = vault_root / path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    content = full_path.read_text(encoding="utf-8")
    return FileRawResponse(path=path, content=content)


@app.get("/file/chunks", response_model=ChunkListResponse)
def get_file_chunks(
    path: str = Query(..., description="Relative path to .ctx file"),
):
    """Get all chunks for a file."""
    conn = get_conn()
    try:
        file_row = conn.execute(
            "SELECT id, path, title FROM files WHERE path=?",
            (path,)
        ).fetchone()
        if not file_row:
            raise HTTPException(status_code=404, detail="File not found")
        
        file_id, file_path, title = file_row
        
        chunk_rows = conn.execute(
            """SELECT chunk_type, ordinal, content_hash, content, embedding
               FROM chunks WHERE file_id = ? ORDER BY ordinal""",
            (file_id,)
        ).fetchall()
        chunks = []
        for cr in chunk_rows:
            chunks.append(ChunkResponse(
                path=file_path,
                title=title,
                chunk_type=cr["chunk_type"],
                ordinal=cr["ordinal"],
                content_hash=cr["content_hash"],
                text=cr["content"],
                embedding=cr["embedding"].decode("utf-8") if cr["embedding"] else None,
            ))
        
        return ChunkListResponse(path=file_path, title=title, chunks=chunks)
    finally:
        conn.close()

# ----------------------------------------------------------------------
# Skill System endpoints
# ----------------------------------------------------------------------
from skill_system import SkillRegistry, Skill, SkillType, AgentContext, AgentRole, initialize_default_skills

# Initialize skill registry
_skill_registry = None

def get_skill_registry() -> SkillRegistry:
    global _skill_registry
    if _skill_registry is None:
        _skill_registry = SkillRegistry(str(DB_PATH))
        initialize_default_skills(_skill_registry)
    return _skill_registry


class SkillCreateRequest(BaseModel):
    name: str
    type: str
    description: str
    required_tags: List[str] = []
    required_chunk_types: List[str] = []
    max_tokens: int = 4000
    config: Dict[str, Any] = {}
    author: str = "user"
    tags: List[str] = []


class SkillResponse(BaseModel):
    id: str
    name: str
    type: str
    description: str
    required_tags: List[str]
    required_chunk_types: List[str]
    max_tokens: int
    config: Dict[str, Any]
    version: int
    created_at: int
    updated_at: int
    author: str
    tags: List[str]


class AgentContextCreateRequest(BaseModel):
    role: str = "subagent"
    parent_id: Optional[str] = None
    session_id: str = ""
    token_budget: int = 8000
    skill_ids: List[str] = []


class AgentContextResponse(BaseModel):
    agent_id: str
    role: str
    parent_id: Optional[str]
    session_id: str
    skill_ids: List[str]
    token_budget: int
    tokens_used: int
    active_files: List[str]
    active_chunks: List[str]
    insights: List[str]
    created_at: int
    updated_at: int


class InsightShareRequest(BaseModel):
    agent_id: str
    skill_id: str
    insight: str
    context_tags: List[str] = []
    related_files: List[str] = []


@app.get("/skills", response_model=List[SkillResponse])
def list_skills(type: Optional[str] = Query(None, description="Filter by skill type")):
    """List all available skills."""
    registry = get_skill_registry()
    skill_type = SkillType(type) if type else None
    skills = registry.list_skills(skill_type)
    return [SkillResponse(
        id=s.id, name=s.name, type=s.type.value, description=s.description,
        required_tags=s.required_tags, required_chunk_types=s.required_chunk_types,
        max_tokens=s.max_tokens, config=s.config, version=s.version,
        created_at=s.created_at, updated_at=s.updated_at,
        author=s.author, tags=s.tags
    ) for s in skills]


@app.post("/skills", response_model=SkillResponse)
def create_skill(request: SkillCreateRequest):
    """Create a new skill."""
    registry = get_skill_registry()
    skill = Skill(
        id=f"skill_{uuid.uuid4().hex[:12]}",
        name=request.name,
        type=SkillType(request.type),
        description=request.description,
        required_tags=request.required_tags,
        required_chunk_types=request.required_chunk_types,
        max_tokens=request.max_tokens,
        config=request.config,
        author=request.author,
        tags=request.tags,
    )
    registry.register_skill(skill)
    return SkillResponse(
        id=skill.id, name=skill.name, type=skill.type.value, description=skill.description,
        required_tags=skill.required_tags, required_chunk_types=skill.required_chunk_types,
        max_tokens=skill.max_tokens, config=skill.config, version=skill.version,
        created_at=skill.created_at, updated_at=skill.updated_at,
        author=skill.author, tags=skill.tags
    )


@app.get("/skills/{skill_id}", response_model=SkillResponse)
def get_skill(skill_id: str):
    """Get a skill by ID."""
    registry = get_skill_registry()
    skill = registry.get_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return SkillResponse(
        id=skill.id, name=skill.name, type=skill.type.value, description=skill.description,
        required_tags=skill.required_tags, required_chunk_types=skill.required_chunk_types,
        max_tokens=skill.max_tokens, config=skill.config, version=skill.version,
        created_at=skill.created_at, updated_at=skill.updated_at,
        author=skill.author, tags=skill.tags
    )


@app.post("/agents", response_model=AgentContextResponse)
def create_agent_context(request: AgentContextCreateRequest):
    """Create a new agent context (for subagents)."""
    registry = get_skill_registry()
    role = AgentRole(request.role)
    ctx = registry.create_agent_context(
        role=role,
        parent_id=request.parent_id,
        session_id=request.session_id,
        token_budget=request.token_budget
    )
    ctx.skill_ids = request.skill_ids
    registry.save_agent_context(ctx)
    return AgentContextResponse(
        agent_id=ctx.agent_id, role=ctx.role.value, parent_id=ctx.parent_id,
        session_id=ctx.session_id, skill_ids=ctx.skill_ids,
        token_budget=ctx.token_budget, tokens_used=ctx.tokens_used,
        active_files=ctx.active_files, active_chunks=ctx.active_chunks,
        insights=ctx.insights, created_at=ctx.created_at, updated_at=ctx.updated_at
    )


@app.get("/agents/{agent_id}", response_model=AgentContextResponse)
def get_agent_context(agent_id: str):
    """Get agent context by ID."""
    registry = get_skill_registry()
    ctx = registry.get_agent_context(agent_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Agent context not found")
    return AgentContextResponse(
        agent_id=ctx.agent_id, role=ctx.role.value, parent_id=ctx.parent_id,
        session_id=ctx.session_id, skill_ids=ctx.skill_ids,
        token_budget=ctx.token_budget, tokens_used=ctx.tokens_used,
        active_files=ctx.active_files, active_chunks=ctx.active_chunks,
        insights=ctx.insights, created_at=ctx.created_at, updated_at=ctx.updated_at
    )


@app.get("/agents/{agent_id}/subagents", response_model=List[AgentContextResponse])
def get_subagents(agent_id: str):
    """Get all subagents for an agent."""
    registry = get_skill_registry()
    subagents = registry.get_subagent_contexts(agent_id)
    return [AgentContextResponse(
        agent_id=s.agent_id, role=s.role.value, parent_id=s.parent_id,
        session_id=s.session_id, skill_ids=s.skill_ids,
        token_budget=s.token_budget, tokens_used=s.tokens_used,
        active_files=s.active_files, active_chunks=s.active_chunks,
        insights=s.insights, created_at=s.created_at, updated_at=s.updated_at
    ) for s in subagents]


@app.post("/insights/share")
def share_insight(request: InsightShareRequest):
    """Share an insight from an agent."""
    registry = get_skill_registry()
    registry.share_insight(
        agent_id=request.agent_id,
        skill_id=request.skill_id,
        insight=request.insight,
        context_tags=request.context_tags,
        related_files=request.related_files,
    )
    return {"status": "shared"}


@app.get("/insights")
def get_insights(
    skill_id: Optional[str] = Query(None),
    context_tags: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=100),
):
    """Get shared insights."""
    registry = get_skill_registry()
    tags = context_tags.split(",") if context_tags else None
    insights = registry.get_shared_insights(skill_id=skill_id, context_tags=tags, limit=limit)
    return {"insights": insights}


# ----------------------------------------------------------------------
# Graph/Canvas/Tags/Daily endpoints (Obsidian-like features)
# ----------------------------------------------------------------------


class CanvasNodeModel(BaseModel):
    id: str
    type: str
    x: float
    y: float
    width: float = 300
    height: float = 200
    note_path: Optional[str] = None
    text: Optional[str] = None
    image_url: Optional[str] = None
    url: Optional[str] = None
    label: Optional[str] = None
    color: Optional[str] = None
    background_color: Optional[str] = None
    border_color: Optional[str] = None


class CanvasEdgeModel(BaseModel):
    id: str
    from_node: str
    to_node: str
    from_side: str = "right"
    to_side: str = "left"
    label: Optional[str] = None
    color: Optional[str] = None


class CanvasModel(BaseModel):
    id: str
    name: str
    nodes: List[CanvasNodeModel] = []
    edges: List[CanvasEdgeModel] = []
    created_at: int
    updated_at: int
    viewport_x: float = 0
    viewport_y: float = 0
    viewport_zoom: float = 1.0


class CanvasCreateRequest(BaseModel):
    name: str


class CanvasUpdateRequest(BaseModel):
    nodes: Optional[List[CanvasNodeModel]] = None
    edges: Optional[List[CanvasEdgeModel]] = None
    viewport_x: Optional[float] = None
    viewport_y: Optional[float] = None
    viewport_zoom: Optional[float] = None


class TagModel(BaseModel):
    name: str
    parent: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None
    created_at: int


class TagCreateRequest(BaseModel):
    name: str
    parent: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None


# Graph endpoints
@app.get("/graph/backlinks")
def get_backlinks(path: str = Query(..., description="Note path")):
    """Get all notes linking TO this note (backlinks)."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT f.path, f.title, f.updated, l.link_type
            FROM links l
            JOIN files f ON f.id = l.src_id
            WHERE l.dst_id = (SELECT id FROM files WHERE path = ?)
        """, (path,)).fetchall()
        return {"backlinks": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/graph/forward-links")
def get_forward_links(path: str = Query(..., description="Note path")):
    """Get all notes this note links TO (forward links)."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT f.path, f.title, f.updated, l.link_type
            FROM links l
            JOIN files f ON f.id = l.dst_id
            WHERE l.src_id = (SELECT id FROM files WHERE path = ?)
        """, (path,)).fetchall()
        return {"forward_links": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/graph/local")
def get_local_graph(
    path: str = Query(..., description="Center note path"),
    depth: int = Query(2, ge=1, le=5, description="Graph depth")
):
    """Get local graph around a note (like Obsidian's local graph)."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        center = conn.execute(
            "SELECT id, path, title, updated FROM files WHERE path = ?",
            (path,)
        ).fetchone()
        if not center:
            raise HTTPException(status_code=404, detail="Note not found")
        
        center_id = center['id']
        visited = {center_id}
        nodes = [dict(center)]
        edges = []
        
        current_level = [center_id]
        for d in range(depth):
            next_level = []
            for node_id in current_level:
                # Outgoing
                out_rows = conn.execute("""
                    SELECT f.id, f.path, f.title, f.updated, l.link_type
                    FROM links l
                    JOIN files f ON f.id = l.dst_id
                    WHERE l.src_id = ?
                """, (node_id,)).fetchall()
                for r in out_rows:
                    if r['id'] not in visited:
                        visited.add(r['id'])
                        nodes.append(dict(r))
                        next_level.append(r['id'])
                    edges.append({
                        "source": node_id,
                        "target": r['id'],
                        "type": r['link_type']
                    })
                
                # Incoming
                in_rows = conn.execute("""
                    SELECT f.id, f.path, f.title, f.updated, l.link_type
                    FROM links l
                    JOIN files f ON f.id = l.src_id
                    WHERE l.dst_id = ?
                """, (node_id,)).fetchall()
                for r in in_rows:
                    if r['id'] not in visited:
                        visited.add(r['id'])
                        nodes.append(dict(r))
                        next_level.append(r['id'])
                    edges.append({
                        "source": r['id'],
                        "target": node_id,
                        "type": r['link_type']
                    })
            current_level = next_level
            if not current_level:
                break
        
        return {"nodes": nodes, "edges": edges}
    finally:
        conn.close()


@app.get("/graph/global")
def get_global_graph(limit: int = Query(500, ge=10, le=2000)):
    """Get global graph of all notes."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        nodes = conn.execute(
            "SELECT id, path, title, updated FROM files LIMIT ?",
            (limit,)
        ).fetchall()
        edges = conn.execute("""
            SELECT l.src_id AS source, l.dst_id AS target, l.link_type AS type
            FROM links l
            JOIN files f1 ON f1.id = l.src_id
            JOIN files f2 ON f2.id = l.dst_id
            WHERE f1.id IS NOT NULL AND f2.id IS NOT NULL
            LIMIT ?
        """, (limit * 2,)).fetchall()
        return {
            "nodes": [dict(r) for r in nodes],
            "edges": [dict(r) for r in edges]
        }
    finally:
        conn.close()


@app.get("/graph/path")
def find_path(
    from_path: str = Query(..., description="Source note path"),
    to_path: str = Query(..., description="Target note path")
):
    """Find shortest path between two notes."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        from_row = conn.execute("SELECT id FROM files WHERE path = ?", (from_path,)).fetchone()
        to_row = conn.execute("SELECT id FROM files WHERE path = ?", (to_path,)).fetchone()
        if not from_row or not to_row:
            raise HTTPException(status_code=404, detail="One or both notes not found")
        
        from_id = from_row['id']
        to_id = to_row['id']
        
        # BFS
        from collections import deque
        queue = deque([(from_id, [])])
        visited = {from_id}
        
        while queue:
            current_id, path = queue.popleft()
            if current_id == to_id:
                # Reconstruct full path with node info
                full_path = []
                for node_id in path + [to_id]:
                    n = conn.execute("SELECT id, path, title FROM files WHERE id = ?", (node_id,)).fetchone()
                    full_path.append(dict(n))
                return {"path": full_path}
            
            # Get neighbors
            neighbors = conn.execute("""
                SELECT dst_id FROM links WHERE src_id = ?
                UNION
                SELECT src_id FROM links WHERE dst_id = ?
            """, (current_id, current_id)).fetchall()
            
            for n in neighbors:
                nid = n[0]
                if nid not in visited:
                    visited.add(nid)
                    queue.append((nid, path + [current_id]))
        
        raise HTTPException(status_code=404, detail="No path found between notes")
    finally:
        conn.close()


@app.get("/graph/orphans")
def get_orphans():
    """Get notes with no connections (in or out)."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT f.path, f.title, f.updated
            FROM files f
            WHERE f.id NOT IN (SELECT src_id FROM links)
            AND f.id NOT IN (SELECT dst_id FROM links)
        """).fetchall()
        return {"orphans": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/graph/hubs")
def get_hubs(limit: int = Query(20, ge=1, le=100)):
    """Get most connected notes (hubs)."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT f.path, f.title, f.updated,
                   (SELECT COUNT(*) FROM links WHERE src_id = f.id) AS out_count,
                   (SELECT COUNT(*) FROM links WHERE dst_id = f.id) AS in_count
            FROM files f
            ORDER BY (out_count + in_count) DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return {"hubs": [dict(r) for r in rows]}
    finally:
        conn.close()


# Canvas endpoints
@app.post("/canvas", response_model=CanvasModel)
def create_canvas(request: CanvasCreateRequest):
    """Create a new canvas/workspace."""
    canvas_id = f"canvas_{uuid.uuid4().hex[:12]}"
    now = int(time.time())
    canvas = CanvasModel(
        id=canvas_id,
        name=request.name,
        nodes=[],
        edges=[],
        created_at=now,
        updated_at=now,
    )
    # Save to DB
    conn = get_conn()
    try:
        data = canvas.model_dump()
        conn.execute("""
            INSERT INTO canvases (id, name, data, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (canvas.id, canvas.name, json.dumps(data), canvas.created_at, canvas.updated_at))
        conn.commit()
    finally:
        conn.close()
    return canvas


@app.get("/canvas/{canvas_id}", response_model=CanvasModel)
def get_canvas(canvas_id: str):
    """Get canvas by ID."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM canvases WHERE id = ?", (canvas_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Canvas not found")
        data = json.loads(row['data'])
        return CanvasModel(
            id=row['id'],
            name=row['name'],
            nodes=[CanvasNodeModel(**n) for n in data.get('nodes', [])],
            edges=[CanvasEdgeModel(**e) for e in data.get('edges', [])],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            viewport_x=data.get('viewport_x', 0),
            viewport_y=data.get('viewport_y', 0),
            viewport_zoom=data.get('viewport_zoom', 1.0),
        )
    finally:
        conn.close()


@app.put("/canvas/{canvas_id}", response_model=CanvasModel)
def update_canvas(canvas_id: str, request: CanvasUpdateRequest):
    """Update canvas nodes, edges, or viewport."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM canvases WHERE id = ?", (canvas_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Canvas not found")
        
        data = json.loads(row['data'])
        if request.nodes is not None:
            data['nodes'] = [n.model_dump() for n in request.nodes]
        if request.edges is not None:
            data['edges'] = [e.model_dump() for e in request.edges]
        if request.viewport_x is not None:
            data['viewport_x'] = request.viewport_x
        if request.viewport_y is not None:
            data['viewport_y'] = request.viewport_y
        if request.viewport_zoom is not None:
            data['viewport_zoom'] = request.viewport_zoom
        
        updated_at = int(time.time())
        conn.execute("""
            UPDATE canvases SET data = ?, updated_at = ? WHERE id = ?
        """, (json.dumps(data), updated_at, canvas_id))
        conn.commit()
        
        return CanvasModel(
            id=row['id'],
            name=row['name'],
            nodes=[CanvasNodeModel(**n) for n in data.get('nodes', [])],
            edges=[CanvasEdgeModel(**e) for e in data.get('edges', [])],
            created_at=row['created_at'],
            updated_at=updated_at,
            viewport_x=data.get('viewport_x', 0),
            viewport_y=data.get('viewport_y', 0),
            viewport_zoom=data.get('viewport_zoom', 1.0),
        )
    finally:
        conn.close()


@app.get("/canvases")
def list_canvases():
    """List all canvases."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, name, created_at, updated_at FROM canvases ORDER BY updated_at DESC").fetchall()
        return {"canvases": [dict(r) for r in rows]}
    finally:
        conn.close()


# Ensure canvases table exists
def _ensure_canvases_table():
    conn = get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS canvases (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                data TEXT,
                created_at INTEGER,
                updated_at INTEGER
            )
        """)
        conn.commit()
    finally:
        conn.close()

_ensure_canvases_table()


# Tag endpoints
@app.post("/tags", response_model=TagModel)
def create_tag(request: TagCreateRequest):
    """Create a hierarchical tag."""
    tag = TagModel(
        name=request.name,
        parent=request.parent,
        color=request.color,
        description=request.description,
        created_at=int(time.time()),
    )
    conn = get_conn()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO tags (name, parent, color, description, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (tag.name, tag.parent, tag.color, tag.description, tag.created_at))
        conn.commit()
    finally:
        conn.close()
    return tag


@app.get("/tags")
def get_tags():
    """Get full tag hierarchy as nested structure."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
        tags = {row['name']: TagModel(
            name=row['name'], parent=row['parent'], color=row['color'],
            description=row['description'], created_at=row['created_at']
        ) for row in rows}
        
        def build_tree(name: str):
            tag = tags[name]
            children = {}
            for t in tags.values():
                if t.parent == name:
                    children[t.name] = build_tree(t.name)
            return {"tag": tag, "children": children}
        
        root = {}
        for tag in tags.values():
            if tag.parent is None:
                root[tag.name] = build_tree(tag.name)
        return {"hierarchy": root}
    finally:
        conn.close()


@app.get("/tags/{tag_name}/notes")
def get_notes_by_tag(tag_name: str, include_children: bool = Query(True)):
    """Get all notes with a tag (and optionally child tags)."""
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    try:
        tags_to_search = [tag_name]
        if include_children:
            # Get all descendant tags
            all_tag_rows = conn.execute("SELECT name, parent FROM tags").fetchall()
            all_tags = {r['name']: r['parent'] for r in all_tag_rows}
            
            def is_descendant(tname: str, ancestor: str) -> bool:
                parent = all_tags.get(tname)
                while parent:
                    if parent == ancestor:
                        return True
                    parent = all_tags.get(parent)
                return False
            
            for tname in all_tags:
                if is_descendant(tname, tag_name):
                    tags_to_search.append(tname)
        
        placeholders = ','.join(['?'] * len(tags_to_search))
        rows = conn.execute(f"""
            SELECT DISTINCT f.path, f.title, f.updated
            FROM files f
            JOIN tags t ON t.file_id = f.id
            WHERE t.tag IN ({placeholders})
        """, tags_to_search).fetchall()
        return {"notes": [dict(r) for r in rows]}
    finally:
        conn.close()


# Ensure tags tables exist
def _ensure_tags_tables():
    conn = get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                name TEXT PRIMARY KEY,
                parent TEXT,
                color TEXT,
                description TEXT,
                created_at INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tag_aliases (
                alias TEXT PRIMARY KEY,
                tag_name TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()

_ensure_tags_tables()


# Daily notes endpoints
@app.get("/daily")
def get_daily_note(date_str: Optional[str] = Query(None, description="Date in YYYY-MM-DD format")):
    """Get or create daily note path."""
    if date_str is None:
        date_str = date.today().isoformat()
    
    path = f"daily/{date_str}.ctx"
    
    # Check if exists
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, path, title, updated FROM files WHERE path = ?", (path,)).fetchone()
        if row:
            return {"path": path, "date": date_str, "exists": True, "note": dict(row)}
        else:
            return {"path": path, "date": date_str, "exists": False}
    finally:
        conn.close()


@app.post("/daily")
def create_daily_note(date_str: Optional[str] = Query(None, description="Date in YYYY-MM-DD format")):
    """Create a daily note if it doesn't exist."""
    if date_str is None:
        date_str = date.today().isoformat()
    
    path = f"daily/{date_str}.ctx"
    vault_root = Path(os.environ.get("CTX_VAULT_ROOT", DB_PATH.parent))
    full_path = vault_root / path
    
    # Create directory
    full_path.parent.mkdir(parents=True, exist_ok=True)
    
    if not full_path.exists():
        today = date.fromisoformat(date_str)
        content = f"""---CTX-HEADER---
{{
  "v": 1,
  "id": "sha256:placeholder",
  "updated": {int(time.time())},
  "author": "system",
  "tags": ["daily", "journal"],
  "links": [],
  "embeddings": {{}}
}}
---CTX-HEADER---

# Daily Note: {today.strftime('%A, %B %d, %Y')}

## Tasks
- [ ] 

## Notes

## Reflections
"""
        full_path.write_text(content, encoding="utf-8")
        
        # Trigger reindex
        conn = get_conn()
        try:
            from parse_ctx import parse_ctx_file
            header, body, _ = parse_ctx_file(full_path)
            # The indexer watchdog will pick it up, or we can call upsert_file directly
        finally:
            conn.close()
    
    return {"path": path, "date": date_str, "created": True}


# ----------------------------------------------------------------------
# Token-Aware Context Injection endpoints
# ----------------------------------------------------------------------

from context_injection import ContextInjector, ContextStrategy, TokenCounter

# Initialize context injector
_context_injector = None

def get_context_injector() -> ContextInjector:
    global _context_injector
    if _context_injector is None:
        vault_root = Path(os.environ.get("CTX_VAULT_ROOT", DB_PATH.parent))
        _context_injector = ContextInjector(str(DB_PATH), vault_root)
    return _context_injector


class ContextBuildRequest(BaseModel):
    query: str
    budget: int = 4000
    strategy: str = "relevance_first"
    filters: Dict[str, Any] = {}
    system_prompt: Optional[str] = None


class ContextBuildResponse(BaseModel):
    messages: List[Dict]
    package: Dict


@app.post("/context/build", response_model=ContextBuildResponse)
def build_context(request: ContextBuildRequest):
    """Build token-aware context for model injection."""
    injector = get_context_injector()
    try:
        strategy = ContextStrategy(request.strategy)
    except ValueError:
        strategy = ContextStrategy.RELEVANCE_FIRST
    
    messages, package = injector.inject_context(
        query=request.query,
        budget=request.budget,
        strategy=strategy,
        filters=request.filters if request.filters else None,
        system_prompt=request.system_prompt,
    )
    
    return ContextBuildResponse(
        messages=messages,
        package={
            "total_tokens": package.total_tokens,
            "budget": package.budget,
            "strategy": package.strategy.value,
            "query": package.query,
            "chunk_count": len(package.chunks),
            "metadata": package.metadata,
        }
    )


@app.post("/context/for-skill")
def context_for_skill(
    skill_name: str = Query(..., description="Skill name"),
    query: str = Query(..., description="Query for context"),
):
    """Get context optimized for a specific skill."""
    registry = get_skill_registry()
    skill = registry.get_skill(skill_name)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    
    injector = get_context_injector()
    messages, package = injector.inject_for_skill(
        skill_name=skill_name,
        query=query,
        skill_config=skill.config,
    )
    
    return {
        "messages": messages,
        "package": {
            "total_tokens": package.total_tokens,
            "budget": package.budget,
            "strategy": package.strategy.value,
            "chunk_count": len(package.chunks),
        }
    }


@app.get("/context/estimate-tokens")
def estimate_tokens(text: str = Query(..., description="Text to count tokens for")):
    """Estimate token count for text."""
    injector = get_context_injector()
    count = injector.estimate_tokens(text)
    return {"tokens": count, "text_length": len(text)}


# ----------------------------------------------------------------------

@app.get("/search/minimal")
def search_minimal(
    q: str = Query(..., description="Search query"),
    limit: int = Query(10, ge=1, le=100),
):
    """Search with minimal snippet extraction for maximum token efficiency."""
    conn = get_conn()
    try:
        # Use even shorter snippets for minimal token usage
        rows = conn.execute(
            """
            SELECT
                f.path,
                f.title,
                c.chunk_type,
                c.ordinal,
                snippet(chunks_fts, 0, '<b>', '</b>', '...', 4) AS snippet,  -- Even shorter: 4 fragments
                bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN files f ON f.id = c.file_id
            WHERE chunks_fts MATCH ?
            ORDER BY bm25(chunks_fts) DESC
            LIMIT ?
            """,
            (q, limit)
        ).fetchall()
        
        results = []
        for row in rows:
            # Clean up HTML tags from snippet for cleaner output
            snippet = row['snippet'].replace('<b>', '**').replace('</b>', '**')
            results.append(SearchResult(
                path=row['path'],
                title=row['title'],
                chunk_type=row['chunk_type'],
                snippet=snippet,
                score=float(row['score']) if row['score'] else 0.0,
                links=[],  # Simplified for minimal version
                tags=[]
            ))
        return results
    finally:
        conn.close()

# Run with: uvicorn api:app --reload
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=True)