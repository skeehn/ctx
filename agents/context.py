"""
Context Manager for ctx-vault Agent Orchestration
Handles long-context via ctx-vault (knowledge) + cilow vectors (embeddings/cache).
"""
import asyncio
import hashlib
import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import OrderedDict

import httpx
import numpy as np


class ContextSource(Enum):
    """Sources of context for agents."""
    VAULT = "vault"           # ctx-vault structured knowledge
    VECTOR = "vector"         # cilow vector embeddings
    CACHE = "cache"           # In-memory/Redis cache
    AGENT_MEMORY = "agent_memory"  # Agent's own working memory
    EXTERNAL = "external"     # External tools (web search, etc.)


@dataclass
class ContextChunk:
    """A single piece of context with metadata."""
    id: str
    content: str
    source: ContextSource
    relevance_score: float = 1.0
    tokens: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    access_count: int = 0
    last_accessed: float = field(default_factory=time.time)
    
    def __post_init__(self):
        if self.tokens == 0:
            self.tokens = len(self.content) // 4  # Rough estimate


@dataclass
class ContextWindow:
    """A window of context for an agent."""
    chunks: List[ContextChunk] = field(default_factory=list)
    max_tokens: int = 8000
    reserved_tokens: int = 0
    
    @property
    def used_tokens(self) -> int:
        return sum(c.tokens for c in self.chunks)
    
    @property
    def available_tokens(self) -> int:
        return self.max_tokens - self.used_tokens - self.reserved_tokens
    
    def add_chunk(self, chunk: ContextChunk) -> bool:
        """Add a chunk if it fits."""
        if chunk.tokens <= self.available_tokens:
            self.chunks.append(chunk)
            return True
        return False
    
    def remove_chunk(self, chunk_id: str) -> bool:
        """Remove a chunk by ID."""
        for i, c in enumerate(self.chunks):
            if c.id == chunk_id:
                self.chunks.pop(i)
                return True
        return False
    
    def get_context_string(self, separator: str = "\n\n---\n\n") -> str:
        """Get formatted context string for LLM."""
        return separator.join(c.content for c in self.chunks)
    
    def get_metadata(self) -> Dict[str, Any]:
        """Get metadata about the context window."""
        return {
            "chunk_count": len(self.chunks),
            "used_tokens": self.used_tokens,
            "available_tokens": self.available_tokens,
            "max_tokens": self.max_tokens,
            "sources": list(set(c.source.value for c in self.chunks)),
        }


class VectorStore(ABC):
    """Abstract interface for vector storage (cilow, Qdrant, etc.)."""
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
    
    @abstractmethod
    async def add(self, texts: List[str], metadata: List[Dict], embeddings: Optional[List[List[float]]] = None) -> List[str]:
        """Add texts with metadata, return IDs."""
        pass
    
    @abstractmethod
    async def search(self, query: str, top_k: int = 10, filter: Optional[Dict] = None) -> List[Tuple[str, float, Dict]]:
        """Search for similar texts. Returns (id, score, metadata)."""
        pass
    
    @abstractmethod
    async def delete(self, ids: List[str]) -> bool:
        """Delete vectors by IDs."""
        pass
    
    @abstractmethod
    async def get(self, ids: List[str]) -> List[Optional[Dict]]:
        """Get metadata by IDs."""
        pass


class CilowVectorStore(VectorStore):
    """cilow vector store integration."""
    
    def __init__(self, url: str = "http://localhost:8080", collection: str = "ctx_vault"):
        self.url = url.rstrip("/")
        self.collection = collection
        self._client: Optional[httpx.AsyncClient] = None
    
    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
    
    async def add(self, texts: List[str], metadata: List[Dict], embeddings: Optional[List[List[float]]] = None) -> List[str]:
        if not self._client:
            self._client = httpx.AsyncClient(timeout=30.0)
        
        payload = {
            "collection": self.collection,
            "texts": texts,
            "metadata": metadata,
        }
        if embeddings:
            payload["embeddings"] = embeddings
        
        resp = await self._client.post(f"{self.url}/vectors/add", json=payload)
        resp.raise_for_status()
        return resp.json().get("ids", [])
    
    async def search(self, query: str, top_k: int = 10, filter: Optional[Dict] = None) -> List[Tuple[str, float, Dict]]:
        if not self._client:
            self._client = httpx.AsyncClient(timeout=30.0)
        
        payload = {
            "collection": self.collection,
            "query": query,
            "top_k": top_k,
        }
        if filter:
            payload["filter"] = filter
        
        resp = await self._client.post(f"{self.url}/vectors/search", json=payload)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return [(r["id"], r["score"], r["metadata"]) for r in results]
    
    async def delete(self, ids: List[str]) -> bool:
        if not self._client:
            self._client = httpx.AsyncClient(timeout=30.0)
        
        resp = await self._client.post(f"{self.url}/vectors/delete", json={
            "collection": self.collection,
            "ids": ids,
        })
        resp.raise_for_status()
        return resp.json().get("success", False)
    
    async def get(self, ids: List[str]) -> List[Optional[Dict]]:
        if not self._client:
            self._client = httpx.AsyncClient(timeout=30.0)
        
        resp = await self._client.post(f"{self.url}/vectors/get", json={
            "collection": self.collection,
            "ids": ids,
        })
        resp.raise_for_status()
        return resp.json().get("results", [])


class CacheStore(ABC):
    """Abstract interface for cache storage."""
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
    
    @abstractmethod
    async def get(self, key: str) -> Optional[Any]:
        pass
    
    @abstractmethod
    async def set(self, key: str, value: Any, ttl: int = 3600) -> bool:
        pass
    
    @abstractmethod
    async def delete(self, key: str) -> bool:
        pass
    
    @abstractmethod
    async def clear(self) -> bool:
        pass


class InMemoryCache(CacheStore):
    """In-memory LRU cache with TTL support."""
    
    def __init__(self, max_size: int = 1000, default_ttl: int = 3600):
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: OrderedDict[str, Tuple[Any, float]] = OrderedDict()  # key -> (value, expiry)
    
    def _is_expired(self, expiry: float) -> bool:
        return time.time() > expiry
    
    def _evict_expired(self) -> None:
        now = time.time()
        expired_keys = [k for k, (_, exp) in self._cache.items() if now > exp]
        for k in expired_keys:
            self._cache.pop(k, None)
    
    async def get(self, key: str) -> Optional[Any]:
        self._evict_expired()
        if key in self._cache:
            value, expiry = self._cache.pop(key)
            if not self._is_expired(expiry):
                self._cache[key] = (value, expiry)  # Move to end (LRU)
                return value
            return None
        return None
    
    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        self._evict_expired()
        
        if len(self._cache) >= self.max_size and key not in self._cache:
            # Remove oldest (LRU)
            self._cache.popitem(last=False)
        
        expiry = time.time() + (ttl or self.default_ttl)
        self._cache[key] = (value, expiry)
        return True
    
    async def delete(self, key: str) -> bool:
        if key in self._cache:
            self._cache.pop(key)
            return True
        return False
    
    async def clear(self) -> bool:
        self._cache.clear()
        return True


class VaultClient:
    """Client for ctx-vault knowledge operations."""
    
    def __init__(self, base_url: str = "http://localhost:8000", api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None
    
    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
    
    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._client:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            )
        return await self._client.request(method, f"{self.base_url}{path}", **kwargs)
    
    async def search(self, query: str, limit: int = 10, rerank: bool = True) -> List[Dict]:
        resp = await self._request("GET", "/search", params={"q": query, "limit": limit, "rerank": rerank})
        resp.raise_for_status()
        return resp.json()
    
    async def get_file(self, path: str) -> Optional[Dict]:
        resp = await self._request("GET", "/file", params={"path": path})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    
    async def get_graph(self, note: str) -> Dict:
        resp = await self._request("GET", "/graph", params={"note": note})
        resp.raise_for_status()
        return resp.json()
    
    async def list_skills(self, skill_type: Optional[str] = None) -> List[Dict]:
        params = {"type": skill_type} if skill_type else {}
        resp = await self._request("GET", "/skills", params=params)
        resp.raise_for_status()
        return resp.json()


class ContextManager:
    """
    Manages long-context for agents using multiple sources:
    - ctx-vault for structured knowledge (search, graph, skills)
    - Vector store (cilow) for semantic embeddings
    - Cache for hot/frequently accessed data
    - Agent working memory for task-specific context
    
    Features:
    - Token budget management
    - Multi-source retrieval with relevance scoring
    - Automatic context compression
    - Cross-source deduplication
    - Persistence across agent sessions
    """
    
    def __init__(
        self,
        vault_url: str = "http://localhost:8000",
        vector_store: Optional[VectorStore] = None,
        cache_store: Optional[CacheStore] = None,
        api_key: Optional[str] = None,
        default_window_tokens: int = 8000,
        max_cached_contexts: int = 100,
    ):
        self.vault_url = vault_url
        self.vector_store = vector_store
        self.cache_store = cache_store or InMemoryCache(max_size=max_cached_contexts)
        self.api_key = api_key
        self.default_window_tokens = default_window_tokens
        self._vault: Optional[VaultClient] = None
        
        # Context persistence
        self._saved_contexts: Dict[str, ContextWindow] = {}
        self._agent_memories: Dict[str, Dict[str, Any]] = {}
    
    @property
    def vault(self) -> VaultClient:
        if self._vault is None:
            self._vault = VaultClient(self.vault_url, self.api_key)
        return self._vault
    
    async def create_window(
        self,
        max_tokens: Optional[int] = None,
        reserved_tokens: int = 0,
    ) -> ContextWindow:
        """Create a new context window."""
        return ContextWindow(
            max_tokens=max_tokens or self.default_window_tokens,
            reserved_tokens=reserved_tokens,
        )
    
    async def build_context(
        self,
        query: str,
        window: ContextWindow,
        sources: Optional[List[ContextSource]] = None,
        agent_id: Optional[str] = None,
    ) -> ContextWindow:
        """
        Build context for a query from multiple sources.
        
        Retrieves from vault, vector store, cache, and agent memory,
        then deduplicates and ranks by relevance.
        """
        sources = sources or [ContextSource.VAULT, ContextSource.VECTOR, ContextSource.CACHE]
        
        all_chunks: List[ContextChunk] = []
        
        # 1. Check cache first
        if ContextSource.CACHE in sources:
            cached = await self._get_cached_context(query, agent_id)
            all_chunks.extend(cached)
        
        # 2. Search vault for structured knowledge
        if ContextSource.VAULT in sources:
            vault_chunks = await self._search_vault(query, window, agent_id)
            all_chunks.extend(vault_chunks)
        
        # 3. Search vector store for semantic matches
        if ContextSource.VECTOR in sources and self.vector_store:
            vector_chunks = await self._search_vectors(query, window, agent_id)
            all_chunks.extend(vector_chunks)
        
        # 4. Get agent working memory
        if ContextSource.AGENT_MEMORY in sources and agent_id:
            memory_chunks = self._get_agent_memory(agent_id, query)
            all_chunks.extend(memory_chunks)
        
        # Deduplicate and rank
        unique_chunks = self._deduplicate_chunks(all_chunks)
        ranked_chunks = self._rank_chunks(unique_chunks, query)
        
        # Fill window with highest relevance chunks
        for chunk in ranked_chunks:
            if not window.add_chunk(chunk):
                break  # Window full
        
        return window
    
    async def _get_cached_context(self, query: str, agent_id: Optional[str]) -> List[ContextChunk]:
        """Get cached context for query."""
        cache_key = f"ctx:{hashlib.md5(query.encode()).hexdigest()}"
        if agent_id:
            cache_key += f":{agent_id}"
        
        cached = await self.cache_store.get(cache_key)
        if cached:
            chunks = [ContextChunk(**c) for c in cached]
            for c in chunks:
                c.source = ContextSource.CACHE
                c.access_count += 1
            return chunks
        return []
    
    async def _search_vault(self, query: str, window: ContextWindow, agent_id: Optional[str]) -> List[ContextChunk]:
        """Search ctx-vault for relevant chunks."""
        try:
            async with self.vault as v:
                results = await v.search(query, limit=20)
            
            chunks = []
            for r in results:
                content = r.get("text", "") or r.get("content", "")
                if content:
                    chunk = ContextChunk(
                        id=f"vault:{r.get('path', 'unknown')}:{hashlib.md5(content.encode()).hexdigest()[:8]}",
                        content=content,
                        source=ContextSource.VAULT,
                        relevance_score=r.get("score", 0.5),
                        metadata={
                            "path": r.get("path"),
                            "title": r.get("title"),
                            "chunk_type": r.get("chunk_type"),
                        },
                    )
                    chunks.append(chunk)
            
            return chunks
        except Exception:
            return []
    
    async def _search_vectors(self, query: str, window: ContextWindow, agent_id: Optional[str]) -> List[ContextChunk]:
        """Search vector store for semantic matches."""
        if not self.vector_store:
            return []
        
        try:
            async with self.vector_store as vs:
                results = await vs.search(query, top_k=20)
            
            chunks = []
            for vid, score, metadata in results:
                content = metadata.get("text", "") or metadata.get("content", "")
                if content:
                    chunk = ContextChunk(
                        id=f"vector:{vid}",
                        content=content,
                        source=ContextSource.VECTOR,
                        relevance_score=score,
                        metadata=metadata,
                    )
                    chunks.append(chunk)
            
            return chunks
        except Exception:
            return []
    
    def _get_agent_memory(self, agent_id: str, query: str) -> List[ContextChunk]:
        """Get relevant items from agent's working memory."""
        memory = self._agent_memories.get(agent_id, {})
        chunks = []
        
        for key, value in memory.items():
            if isinstance(value, str) and query.lower() in value.lower():
                chunk = ContextChunk(
                    id=f"memory:{agent_id}:{key}",
                    content=value,
                    source=ContextSource.AGENT_MEMORY,
                    relevance_score=0.8,
                    metadata={"key": key},
                )
                chunks.append(chunk)
        
        return chunks
    
    def _deduplicate_chunks(self, chunks: List[ContextChunk]) -> List[ContextChunk]:
        """Remove duplicate chunks based on content hash."""
        seen = set()
        unique = []
        
        for chunk in chunks:
            content_hash = hashlib.md5(chunk.content.encode()).hexdigest()
            if content_hash not in seen:
                seen.add(content_hash)
                unique.append(chunk)
            else:
                # Merge metadata from duplicate
                for u in unique:
                    if hashlib.md5(u.content.encode()).hexdigest() == content_hash:
                        u.metadata.update(chunk.metadata)
                        u.relevance_score = max(u.relevance_score, chunk.relevance_score)
                        break
        
        return unique
    
    def _rank_chunks(self, chunks: List[ContextChunk], query: str) -> List[ContextChunk]:
        """Rank chunks by relevance to query."""
        # Simple ranking: combine relevance_score with recency and access count
        def score(chunk: ContextChunk) -> float:
            recency = 1.0 / (1.0 + (time.time() - chunk.created_at) / 3600)  # Decay over hours
            access_boost = min(chunk.access_count * 0.1, 0.5)
            return chunk.relevance_score * 0.7 + recency * 0.2 + access_boost * 0.1
        
        return sorted(chunks, key=score, reverse=True)
    
    async def cache_context(self, query: str, window: ContextWindow, agent_id: Optional[str] = None, ttl: int = 3600) -> None:
        """Cache a context window for future use."""
        cache_key = f"ctx:{hashlib.md5(query.encode()).hexdigest()}"
        if agent_id:
            cache_key += f":{agent_id}"
        
        # Only cache chunks from vault and vector (not agent memory)
        cacheable = [
            {"id": c.id, "content": c.content, "source": c.source.value, 
             "relevance_score": c.relevance_score, "tokens": c.tokens, "metadata": c.metadata}
            for c in window.chunks
            if c.source in (ContextSource.VAULT, ContextSource.VECTOR)
        ]
        
        await self.cache_store.set(cache_key, cacheable, ttl)
    
    def save_agent_memory(self, agent_id: str, key: str, value: Any) -> None:
        """Save a value to agent's working memory."""
        if agent_id not in self._agent_memories:
            self._agent_memories[agent_id] = {}
        self._agent_memories[agent_id][key] = value
    
    def get_agent_memory(self, agent_id: str, key: str) -> Optional[Any]:
        """Get a value from agent's working memory."""
        return self._agent_memories.get(agent_id, {}).get(key)
    
    def clear_agent_memory(self, agent_id: str) -> None:
        """Clear agent's working memory."""
        if agent_id in self._agent_memories:
            del self._agent_memories[agent_id]
    
    def save_context_window(self, window_id: str, window: ContextWindow) -> None:
        """Persist a context window for later reuse."""
        self._saved_contexts[window_id] = window
    
    def load_context_window(self, window_id: str) -> Optional[ContextWindow]:
        """Load a previously saved context window."""
        return self._saved_contexts.get(window_id)
    
    async def compress_context(
        self,
        window: ContextWindow,
        target_tokens: int,
        strategy: str = "relevance",
    ) -> ContextWindow:
        """
        Compress context window to fit within target tokens.
        
        Strategies:
        - relevance: Keep highest relevance chunks
        - recency: Keep most recent chunks
        - balanced: Mix of relevance and recency
        - summarize: Summarize chunks (requires LLM)
        """
        if window.used_tokens <= target_tokens:
            return window
        
        if strategy == "relevance":
            # Sort by relevance, keep top chunks
            sorted_chunks = sorted(window.chunks, key=lambda c: c.relevance_score, reverse=True)
        elif strategy == "recency":
            sorted_chunks = sorted(window.chunks, key=lambda c: c.last_accessed, reverse=True)
        elif strategy == "balanced":
            def balanced_score(c: ContextChunk) -> float:
                recency = 1.0 / (1.0 + (time.time() - c.last_accessed) / 3600)
                return c.relevance_score * 0.7 + recency * 0.3
            sorted_chunks = sorted(window.chunks, key=balanced_score, reverse=True)
        else:
            sorted_chunks = window.chunks
        
        # Rebuild window
        new_window = ContextWindow(max_tokens=window.max_tokens, reserved_tokens=window.reserved_tokens)
        for chunk in sorted_chunks:
            if not new_window.add_chunk(chunk):
                break
        
        return new_window
    
    async def enrich_with_graph(
        self,
        window: ContextWindow,
        max_hops: int = 2,
    ) -> ContextWindow:
        """Enrich context by following graph links from vault."""
        # Get unique note paths from vault chunks
        note_paths = set()
        for chunk in window.chunks:
            if chunk.source == ContextSource.VAULT and "path" in chunk.metadata:
                note_paths.add(chunk.metadata["path"])
        
        if not note_paths:
            return window
        
        # Follow graph links
        visited = set(note_paths)
        to_visit = list(note_paths)
        
        for hop in range(max_hops):
            next_visit = []
            for note in to_visit:
                if note in visited:
                    continue
                visited.add(note)
                
                try:
                    async with self.vault as v:
                        graph = await v.get_graph(note)
                    
                    for edge in graph.get("edges", []):
                        linked_note = edge.get("dst")
                        if linked_note and linked_note not in visited:
                            next_visit.append(linked_note)
                            
                            # Add linked note content
                            file_data = await v.get_file(linked_note)
                            if file_data and file_data.get("body"):
                                chunk = ContextChunk(
                                    id=f"vault:graph:{linked_note}",
                                    content=file_data["body"][:2000],
                                    source=ContextSource.VAULT,
                                    relevance_score=0.6 * (0.8 ** hop),  # Decay with hops
                                    metadata={"path": linked_note, "via_graph": True, "hop": hop + 1},
                                )
                                window.add_chunk(chunk)
                except Exception:
                    continue
            
            to_visit = next_visit
            if not to_visit:
                break
        
        return window


# Convenience function
async def create_context_manager(
    vault_url: str = "http://localhost:8000",
    vector_url: Optional[str] = None,
    cache: Optional[CacheStore] = None,
    api_key: Optional[str] = None,
) -> ContextManager:
    """Create a context manager with default components."""
    vector = None
    if vector_url:
        vector = CilowVectorStore(vector_url)
    
    return ContextManager(
        vault_url=vault_url,
        vector_store=vector,
        cache_store=cache,
        api_key=api_key,
    )