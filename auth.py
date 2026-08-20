"""
Authentication & Rate Limiting for ctx-vault API
Provides JWT-based auth, API keys, and rate limiting for local/production use.
"""
import os
import time
import hashlib
import secrets
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from functools import wraps

import jwt
from fastapi import HTTPException, Depends, Request, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
import sqlite3
from pathlib import Path


# Configuration
JWT_SECRET = os.environ.get("CTX_JWT_SECRET", secrets.token_urlsafe(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24 * 30  # 30 days

# Rate limiting config
DEFAULT_RATE_LIMIT = 100  # requests per minute
DEFAULT_BURST_LIMIT = 20  # burst allowance

# API Key prefix
API_KEY_PREFIX = "ctx_"


class TokenData(BaseModel):
    """Decoded JWT token data."""
    sub: str  # user/agent ID
    tenant: str = "default"
    scopes: List[str] = Field(default_factory=list)
    exp: int
    iat: int


class APIKey(BaseModel):
    """API Key model."""
    key_id: str
    key_hash: str
    name: str
    tenant: str
    scopes: List[str]
    rate_limit: int = DEFAULT_RATE_LIMIT
    burst_limit: int = DEFAULT_BURST_LIMIT
    created_at: int
    expires_at: Optional[int] = None
    last_used_at: Optional[int] = None
    is_active: bool = True


class RateLimitInfo(BaseModel):
    """Rate limit status."""
    limit: int
    remaining: int
    reset_at: int
    retry_after: Optional[int] = None


# In-memory rate limit store (replace with Redis in production)
_rate_limit_store: Dict[str, List[float]] = {}


def get_db_path() -> Path:
    """Get database path for auth tables."""
    db_path_str = os.environ.get("CTX_DB_PATH", "")
    if db_path_str:
        return Path(db_path_str)
    vault_root = Path(os.environ.get("CTX_VAULT_ROOT", Path.home() / "ai-vault"))
    return vault_root / "vault.db"


def init_auth_db():
    """Initialize auth tables in the database."""
    conn = sqlite3.connect(get_db_path())
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS api_keys (
                key_id TEXT PRIMARY KEY,
                key_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                tenant TEXT NOT NULL DEFAULT 'default',
                scopes TEXT NOT NULL DEFAULT '[]',
                rate_limit INTEGER NOT NULL DEFAULT 100,
                burst_limit INTEGER NOT NULL DEFAULT 20,
                created_at INTEGER NOT NULL,
                expires_at INTEGER,
                last_used_at INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            
            CREATE TABLE IF NOT EXISTS api_key_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                FOREIGN KEY (key_id) REFERENCES api_keys(key_id)
            );
            
            CREATE INDEX IF NOT EXISTS idx_api_keys_tenant ON api_keys(tenant);
            CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
            CREATE INDEX IF NOT EXISTS idx_usage_key_time ON api_key_usage(key_id, timestamp);
        """)
        conn.commit()
    finally:
        conn.close()


def hash_api_key(key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key(name: str, tenant: str = "default", scopes: List[str] = None,
                     rate_limit: int = DEFAULT_RATE_LIMIT, burst_limit: int = DEFAULT_BURST_LIMIT,
                     expires_days: Optional[int] = None) -> tuple[str, APIKey]:
    """
    Generate a new API key.
    Returns (plain_key, APIKey_object)
    """
    plain_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    key_hash = hash_api_key(plain_key)
    key_id = secrets.token_urlsafe(16)
    
    now = int(time.time())
    expires_at = None
    if expires_days:
        expires_at = now + (expires_days * 24 * 3600)
    
    api_key = APIKey(
        key_id=key_id,
        key_hash=key_hash,
        name=name,
        tenant=tenant,
        scopes=scopes or ["read", "write", "ingest"],
        rate_limit=rate_limit,
        burst_limit=burst_limit,
        created_at=now,
        expires_at=expires_at,
    )
    
    # Store in database
    conn = sqlite3.connect(get_db_path())
    try:
        conn.execute("""
            INSERT INTO api_keys (key_id, key_hash, name, tenant, scopes, rate_limit, burst_limit, created_at, expires_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            api_key.key_id, api_key.key_hash, api_key.name, api_key.tenant,
            json.dumps(api_key.scopes), api_key.rate_limit, api_key.burst_limit,
            api_key.created_at, api_key.expires_at
        ))
        conn.commit()
    finally:
        conn.close()
    
    return plain_key, api_key


def verify_api_key(key: str) -> Optional[APIKey]:
    """Verify an API key and return its metadata."""
    key_hash = hash_api_key(key)
    
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT * FROM api_keys WHERE key_hash = ? AND is_active = 1
        """, (key_hash,)).fetchone()
        
        if not row:
            return None
        
        # Check expiration
        if row["expires_at"] and row["expires_at"] < int(time.time()):
            return None
        
        return APIKey(
            key_id=row["key_id"],
            key_hash=row["key_hash"],
            name=row["name"],
            tenant=row["tenant"],
            scopes=json.loads(row["scopes"]),
            rate_limit=row["rate_limit"],
            burst_limit=row["burst_limit"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            last_used_at=row["last_used_at"],
            is_active=bool(row["is_active"]),
        )
    finally:
        conn.close()


def record_api_key_usage(key_id: str, endpoint: str):
    """Record API key usage for analytics."""
    conn = sqlite3.connect(get_db_path())
    try:
        now = int(time.time())
        conn.execute("""
            INSERT INTO api_key_usage (key_id, endpoint, timestamp)
            VALUES (?, ?, ?)
        """, (key_id, endpoint, now))
        
        # Update last_used_at
        conn.execute("""
            UPDATE api_keys SET last_used_at = ? WHERE key_id = ?
        """, (now, key_id))
        
        conn.commit()
    finally:
        conn.close()


def check_rate_limit(key_id: str, limit: int, window_seconds: int = 60) -> RateLimitInfo:
    """Check rate limit for an API key using sliding window."""
    now = time.time()
    window_start = now - window_seconds
    
    # Clean old entries and count current window
    if key_id not in _rate_limit_store:
        _rate_limit_store[key_id] = []
    
    timestamps = _rate_limit_store[key_id]
    # Remove expired
    timestamps[:] = [ts for ts in timestamps if ts > window_start]
    
    current_count = len(timestamps)
    remaining = max(0, limit - current_count)
    reset_at = int(now + window_seconds)
    
    if current_count >= limit:
        # Find oldest timestamp to calculate retry_after
        oldest = min(timestamps) if timestamps else now
        retry_after = int(oldest + window_seconds - now) + 1
        return RateLimitInfo(
            limit=limit,
            remaining=0,
            reset_at=reset_at,
            retry_after=retry_after
        )
    
    # Add current request
    timestamps.append(now)
    
    return RateLimitInfo(
        limit=limit,
        remaining=remaining,
        reset_at=reset_at
    )


# FastAPI Dependencies
security = HTTPBearer(auto_error=False)


async def get_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
) -> APIKey:
    """
    Extract and verify API key from Authorization header or X-API-Key header.
    """
    api_key = None
    
    # Check Authorization header (Bearer token)
    if credentials and credentials.scheme.lower() == "bearer":
        api_key = credentials.credentials
    
    # Check X-API-Key header
    if not api_key and x_api_key:
        api_key = x_api_key
    
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="API key required. Provide via Authorization: Bearer <key> or X-API-Key header.",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Verify key
    key_data = verify_api_key(api_key)
    if not key_data:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired API key",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Check rate limit
    rate_info = check_rate_limit(key_data.key_id, key_data.rate_limit)
    
    # Add rate limit headers to response (will be added by middleware)
    request.state.rate_limit = rate_info
    request.state.api_key = key_data
    
    # Record usage
    record_api_key_usage(key_data.key_id, request.url.path)
    
    return key_data


def require_scope(required_scope: str):
    """Dependency factory for requiring a specific scope."""
    async def check_scope(api_key: APIKey = Depends(get_api_key)) -> APIKey:
        if required_scope not in api_key.scopes and "admin" not in api_key.scopes:
            raise HTTPException(
                status_code=403,
                detail=f"Required scope: {required_scope}"
            )
        return api_key
    return check_scope


# Rate limit middleware
async def rate_limit_middleware(request: Request, call_next):
    """Add rate limit headers to all responses."""
    response = await call_next(request)
    
    if hasattr(request.state, "rate_limit"):
        rate_info = request.state.rate_limit
        response.headers["X-RateLimit-Limit"] = str(rate_info.limit)
        response.headers["X-RateLimit-Remaining"] = str(rate_info.remaining)
        response.headers["X-RateLimit-Reset"] = str(rate_info.reset_at)
        if rate_info.retry_after:
            response.headers["Retry-After"] = str(rate_info.retry_after)
    
    return response


# CLI Commands for API Key Management
def cli_create_key(name: str, tenant: str = "default", scopes: str = "read,write,ingest",
                   rate_limit: int = 100, expires_days: int = None) -> tuple[str, APIKey]:
    """CLI helper to create an API key."""
    scope_list = [s.strip() for s in scopes.split(",")]
    return generate_api_key(name, tenant, scope_list, rate_limit, expires_days=expires_days)


def cli_list_keys(tenant: str = None) -> List[APIKey]:
    """List API keys, optionally filtered by tenant."""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        if tenant:
            rows = conn.execute("SELECT * FROM api_keys WHERE tenant = ? ORDER BY created_at DESC", (tenant,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
        
        return [APIKey(
            key_id=row["key_id"],
            key_hash=row["key_hash"],
            name=row["name"],
            tenant=row["tenant"],
            scopes=json.loads(row["scopes"]),
            rate_limit=row["rate_limit"],
            burst_limit=row["burst_limit"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            last_used_at=row["last_used_at"],
            is_active=bool(row["is_active"]),
        ) for row in rows]
    finally:
        conn.close()


def cli_revoke_key(key_id: str) -> bool:
    """Revoke an API key."""
    conn = sqlite3.connect(get_db_path())
    try:
        cursor = conn.execute("UPDATE api_keys SET is_active = 0 WHERE key_id = ?", (key_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


if __name__ == "__main__":
    # Demo: Create a test key
    init_auth_db()
    plain_key, api_key = cli_create_key("test-agent", "local", "read,write,ingest,admin", 1000, 30)
    print(f"Created API Key:")
    print(f"  Key: {plain_key}")
    print(f"  ID: {api_key.key_id}")
    print(f"  Name: {api_key.name}")
    print(f"  Scopes: {api_key.scopes}")
    print(f"  Rate Limit: {api_key.rate_limit}/min")