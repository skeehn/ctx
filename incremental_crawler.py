"""
Incremental Crawl/Update System for ctx-vault
Supports RSS feeds, sitemaps, and webhook-based updates for keeping knowledge fresh.
"""
import os
import json
import hashlib
import time
import feedparser
import requests
from pathlib import Path
from typing import Dict, List, Optional, Any, Set
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from urllib.parse import urljoin, urlparse
import sqlite3
import logging
from threading import Thread
import hashlib

logger = logging.getLogger(__name__)


@dataclass
class CrawlSource:
    """Configuration for a crawl source."""
    source_id: str
    name: str
    source_type: str  # "rss", "sitemap", "webhook"
    url: str
    schedule: str  # cron expression or interval in minutes
    last_crawl: Optional[str] = None
    last_etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_hash: Optional[str] = None
    is_active: bool = True
    config: Dict = None
    
    def __post_init__(self):
        if self.config is None:
            self.config = {}


@dataclass
class CrawlResult:
    """Result of a crawl operation."""
    source_id: str
    timestamp: str
    urls_found: int
    urls_new: int
    urls_updated: int
    urls_unchanged: int
    errors: List[str]
    duration_seconds: float


class IncrementalCrawler:
    """Manages incremental crawling of web sources."""
    
    def __init__(self, vault_root: Path, db_path: Optional[Path] = None):
        self.vault_root = Path(vault_root)
        self.db_path = db_path or (self.vault_root / "crawl.db")
        self._init_db()
        self.sources: Dict[str, CrawlSource] = {}
        self._load_sources()
    
    def _init_db(self):
        """Initialize crawl database."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS crawl_sources (
                    source_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    url TEXT NOT NULL,
                    schedule TEXT NOT NULL,
                    last_crawl TEXT,
                    last_etag TEXT,
                    last_modified TEXT,
                    content_hash TEXT,
                    is_active INTEGER DEFAULT 1,
                    config TEXT
                );
                
                CREATE TABLE IF NOT EXISTS crawled_urls (
                    url TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    title TEXT,
                    crawled_at TEXT NOT NULL,
                    etag TEXT,
                    last_modified TEXT,
                    FOREIGN KEY (source_id) REFERENCES crawl_sources(source_id)
                );
                
                CREATE TABLE IF NOT EXISTS crawl_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    urls_found INTEGER,
                    urls_new INTEGER,
                    urls_updated INTEGER,
                    urls_unchanged INTEGER,
                    errors TEXT,
                    duration_seconds REAL,
                    FOREIGN KEY (source_id) REFERENCES crawl_sources(source_id)
                );
                
                CREATE INDEX IF NOT EXISTS idx_crawled_source ON crawled_urls(source_id);
                CREATE INDEX IF NOT EXISTS idx_crawl_history_source ON crawl_history(source_id);
            """)
            conn.commit()
        finally:
            conn.close()
    
    def _load_sources(self):
        """Load crawl sources from database."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM crawl_sources WHERE is_active = 1").fetchall()
            for row in rows:
                source = CrawlSource(
                    source_id=row["source_id"],
                    name=row["name"],
                    source_type=row["source_type"],
                    url=row["url"],
                    schedule=row["schedule"],
                    last_crawl=row["last_crawl"],
                    last_etag=row["last_etag"],
                    last_modified=row["last_modified"],
                    content_hash=row["content_hash"],
                    is_active=bool(row["is_active"]),
                    config=json.loads(row["config"]) if row["config"] else {},
                )
                self.sources[source.source_id] = source
        finally:
            conn.close()
    
    def add_source(self, source: CrawlSource) -> CrawlSource:
        """Add a new crawl source."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO crawl_sources (source_id, name, source_type, url, schedule, last_crawl, last_etag, last_modified, content_hash, is_active, config)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                source.source_id, source.name, source.source_type, source.url,
                source.schedule, source.last_crawl, source.last_etag, source.last_modified,
                source.content_hash, int(source.is_active), json.dumps(source.config)
            ))
            conn.commit()
        finally:
            conn.close()
        
        self.sources[source.source_id] = source
        return source
    
    def remove_source(self, source_id: str) -> bool:
        """Remove a crawl source."""
        if source_id not in self.sources:
            return False
        
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM crawl_sources WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM crawled_urls WHERE source_id = ?", (source_id,))
            conn.execute("DELETE FROM crawl_history WHERE source_id = ?", (source_id,))
            conn.commit()
        finally:
            conn.close()
        
        del self.sources[source_id]
        return True
    
    def list_sources(self) -> List[CrawlSource]:
        """List all crawl sources."""
        return list(self.sources.values())
    
    def get_source(self, source_id: str) -> Optional[CrawlSource]:
        """Get a crawl source by ID."""
        return self.sources.get(source_id)
    
    def _compute_content_hash(self, content: bytes) -> str:
        """Compute SHA256 hash of content."""
        return hashlib.sha256(content).hexdigest()
    
    def _fetch_with_conditionals(self, url: str, etag: str = None, last_modified: str = None) -> tuple:
        """
        Fetch URL with conditional headers (ETag, Last-Modified).
        Returns (content, etag, last_modified, status_code, is_new)
        """
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        
        try:
            response = requests.get(url, headers=headers, timeout=30)
            
            if response.status_code == 304:
                # Not modified
                return None, etag, last_modified, 304, False
            
            content = response.content
            new_etag = response.headers.get("ETag")
            new_last_modified = response.headers.get("Last-Modified")
            
            return content, new_etag, new_last_modified, response.status_code, True
            
        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
            return None, etag, last_modified, 0, False
    
    def crawl_rss(self, source: CrawlSource) -> CrawlResult:
        """Crawl an RSS/Atom feed."""
        start_time = time.time()
        errors = []
        urls_found = 0
        urls_new = 0
        urls_updated = 0
        urls_unchanged = 0
        
        try:
            # Parse feed
            feed = feedparser.parse(source.url)
            
            if feed.bozo and feed.bozo_exception:
                errors.append(f"Feed parse error: {feed.bozo_exception}")
            
            conn = sqlite3.connect(self.db_path)
            
            for entry in feed.entries:
                urls_found += 1
                url = entry.get("link", "")
                if not url:
                    continue
                
                # Get entry content
                content = entry.get("summary", entry.get("description", ""))
                title = entry.get("title", "")
                
                # Compute content hash
                entry_content = f"{title}\n{content}".encode()
                content_hash = self._compute_content_hash(entry_content)
                
                # Check if already crawled
                cursor = conn.execute(
                    "SELECT content_hash FROM crawled_urls WHERE url = ? AND source_id = ?",
                    (url, source.source_id)
                )
                row = cursor.fetchone()
                
                if row:
                    if row["content_hash"] == content_hash:
                        urls_unchanged += 1
                        continue
                    else:
                        urls_updated += 1
                else:
                    urls_new += 1
                
                # Store/update
                conn.execute("""
                    INSERT OR REPLACE INTO crawled_urls (url, source_id, content_hash, title, crawled_at, etag, last_modified)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (url, source.source_id, content_hash, title, datetime.now().isoformat(), None, None))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            errors.append(f"RSS crawl failed: {e}")
        
        duration = time.time() - start_time
        return CrawlResult(
            source_id=source.source_id,
            timestamp=datetime.now().isoformat(),
            urls_found=urls_found,
            urls_new=urls_new,
            urls_updated=urls_updated,
            urls_unchanged=urls_unchanged,
            errors=errors,
            duration_seconds=duration
        )
    
    def crawl_sitemap(self, source: CrawlSource) -> CrawlResult:
        """Crawl a sitemap.xml."""
        start_time = time.time()
        errors = []
        urls_found = 0
        urls_new = 0
        urls_updated = 0
        urls_unchanged = 0
        
        try:
            # Fetch sitemap
            response = requests.get(source.url, timeout=30)
            if response.status_code != 200:
                errors.append(f"Failed to fetch sitemap: HTTP {response.status_code}")
                return CrawlResult(
                    source_id=source.source_id,
                    timestamp=datetime.now().isoformat(),
                    urls_found=0, urls_new=0, urls_updated=0, urls_unchanged=0,
                    errors=errors, duration_seconds=time.time() - start_time
                )
            
            # Parse XML
            import xml.etree.ElementTree as ET
            root = ET.fromstring(response.content)
            
            # Handle namespace
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            urls = root.findall(".//sm:url/sm:loc", ns) or root.findall(".//url/loc")
            
            conn = sqlite3.connect(self.db_path)
            
            for url_elem in urls:
                url = url_elem.text.strip()
                if not url:
                    continue
                
                urls_found += 1
                
                # Check if already crawled
                cursor = conn.execute(
                    "SELECT content_hash FROM crawled_urls WHERE url = ? AND source_id = ?",
                    (url, source.source_id)
                )
                row = cursor.fetchone()
                
                if row:
                    urls_unchanged += 1
                else:
                    urls_new += 1
                    conn.execute("""
                        INSERT OR REPLACE INTO crawled_urls (url, source_id, content_hash, title, crawled_at, etag, last_modified)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (url, source.source_id, "", "", datetime.now().isoformat(), None, None))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            errors.append(f"Sitemap crawl failed: {e}")
        
        duration = time.time() - start_time
        return CrawlResult(
            source_id=source.source_id,
            timestamp=datetime.now().isoformat(),
            urls_found=urls_found,
            urls_new=urls_new,
            urls_updated=urls_updated,
            urls_unchanged=urls_unchanged,
            errors=errors,
            duration_seconds=duration
        )
    
    def crawl_source(self, source_id: str) -> CrawlResult:
        """Crawl a single source by ID."""
        source = self.sources.get(source_id)
        if not source:
            raise ValueError(f"Source {source_id} not found")
        
        if source.source_type == "rss":
            result = self.crawl_rss(source)
        elif source.source_type == "sitemap":
            result = self.crawl_sitemap(source)
        else:
            raise ValueError(f"Unknown source type: {source.source_type}")
        
        # Update source last_crawl
        source.last_crawl = datetime.now().isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE crawl_sources SET last_crawl = ? WHERE source_id = ?", 
                        (source.last_crawl, source_id))
            conn.commit()
        finally:
            conn.close()
        
        # Record history
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO crawl_history (source_id, timestamp, urls_found, urls_new, urls_updated, urls_unchanged, errors, duration_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                source_id, result.timestamp, result.urls_found, result.urls_new,
                result.urls_updated, result.urls_unchanged, json.dumps(result.errors), result.duration_seconds
            ))
            conn.commit()
        finally:
            conn.close()
        
        return result
    
    def crawl_all(self) -> List[CrawlResult]:
        """Crawl all active sources."""
        results = []
        for source_id in self.sources:
            if self.sources[source_id].is_active:
                try:
                    result = self.crawl_source(source_id)
                    results.append(result)
                except Exception as e:
                    logger.error(f"Failed to crawl {source_id}: {e}")
        return results
    
    def get_crawl_history(self, source_id: str, limit: int = 10) -> List[CrawlResult]:
        """Get crawl history for a source."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM crawl_history WHERE source_id = ? ORDER BY timestamp DESC LIMIT ?",
                (source_id, limit)
            ).fetchall()
            
            return [CrawlResult(
                source_id=row["source_id"],
                timestamp=row["timestamp"],
                urls_found=row["urls_found"],
                urls_new=row["urls_new"],
                urls_updated=row["urls_updated"],
                urls_unchanged=row["urls_unchanged"],
                errors=json.loads(row["errors"]) if row["errors"] else [],
                duration_seconds=row["duration_seconds"]
            ) for row in rows]
        finally:
            conn.close()
    
    def get_crawled_urls(self, source_id: str, limit: int = 100) -> List[Dict]:
        """Get URLs crawled for a source."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM crawled_urls WHERE source_id = ? ORDER BY crawled_at DESC LIMIT ?",
                (source_id, limit)
            ).fetchall()
            
            return [dict(row) for row in rows]
        finally:
            conn.close()


class WebhookHandler:
    """Handles incoming webhook notifications for real-time updates."""
    
    def __init__(self, crawler: IncrementalCrawler, secret: str = None):
        self.crawler = crawler
        self.secret = secret or os.environ.get("WEBHOOK_SECRET", "")
    
    def verify_signature(self, payload: bytes, signature: str) -> bool:
        """Verify webhook signature."""
        if not self.secret:
            return True  # No secret configured, skip verification
        
        expected = hmac.new(
            self.secret.encode(),
            payload,
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(expected, signature)
    
    def handle_github_webhook(self, payload: Dict, event: str) -> Dict:
        """Handle GitHub webhook events."""
        # For repo pushes, trigger crawl of linked sitemaps/RSS
        if event == "push":
            repo_url = payload.get("repository", {}).get("html_url", "")
            # Could trigger specific source crawls based on repo
            return {"status": "received", "event": event}
        
        return {"status": "ignored", "event": event}
    
    def handle_generic_webhook(self, payload: Dict, source_id: str) -> Dict:
        """Handle generic webhook for a specific source."""
        source = self.crawler.get_source(source_id)
        if not source:
            return {"status": "error", "message": "Source not found"}
        
        # Trigger immediate crawl
        try:
            result = self.crawler.crawl_source(source_id)
            return {"status": "success", "result": asdict(result)}
        except Exception as e:
            return {"status": "error", "message": str(e)}


def create_crawler(vault_root: str) -> IncrementalCrawler:
    """Create an incremental crawler."""
    return IncrementalCrawler(Path(vault_root))


def cli_add_rss_source(vault_root: str, name: str, url: str, schedule: str = "0 */6 * * *") -> CrawlSource:
    """CLI helper to add RSS source."""
    crawler = IncrementalCrawler(Path(vault_root))
    source = CrawlSource(
        source_id=f"rss_{hashlib.md5(url.encode()).hexdigest()[:8]}",
        name=name,
        source_type="rss",
        url=url,
        schedule=schedule,
    )
    crawler.add_source(source)
    return source


def cli_add_sitemap_source(vault_root: str, name: str, url: str, schedule: str = "0 */12 * * *") -> CrawlSource:
    """CLI helper to add sitemap source."""
    crawler = IncrementalCrawler(Path(vault_root))
    source = CrawlSource(
        source_id=f"sitemap_{hashlib.md5(url.encode()).hexdigest()[:8]}",
        name=name,
        source_type="sitemap",
        url=url,
        schedule=schedule,
    )
    crawler.add_source(source)
    return source


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 3:
        print("Usage: python incremental_crawler.py <vault_root> <command> [args]")
        print("Commands:")
        print("  add-rss <name> <url> [schedule]")
        print("  add-sitemap <name> <url> [schedule]")
        print("  list")
        print("  crawl <source_id>")
        print("  crawl-all")
        print("  history <source_id>")
        sys.exit(1)
    
    vault_root = sys.argv[1]
    command = sys.argv[2]
    
    crawler = IncrementalCrawler(Path(vault_root))
    
    if command == "add-rss":
        name = sys.argv[3]
        url = sys.argv[4]
        schedule = sys.argv[5] if len(sys.argv) > 5 else "0 */6 * * *"
        source = cli_add_rss_source(vault_root, name, url, schedule)
        print(f"Added RSS source: {source.name} ({source.source_id})")
    
    elif command == "add-sitemap":
        name = sys.argv[3]
        url = sys.argv[4]
        schedule = sys.argv[5] if len(sys.argv) > 5 else "0 */12 * * *"
        source = cli_add_sitemap_source(vault_root, name, url, schedule)
        print(f"Added sitemap source: {source.name} ({source.source_id})")
    
    elif command == "list":
        for source in crawler.list_sources():
            print(f"{source.source_id} | {source.name} | {source.source_type} | {source.url} | {source.schedule}")
    
    elif command == "crawl":
        source_id = sys.argv[3]
        result = crawler.crawl_source(source_id)
        print(f"Crawled {result.source_id}: {result.urls_found} found, {result.urls_new} new, {result.urls_updated} updated")
    
    elif command == "crawl-all":
        results = crawler.crawl_all()
        for r in results:
            print(f"{r.source_id}: {r.urls_found} found, {r.urls_new} new, {r.urls_updated} updated")
    
    elif command == "history":
        source_id = sys.argv[3]
        history = crawler.get_crawl_history(source_id)
        for h in history:
            print(f"{h.timestamp}: {h.urls_found} found, {h.urls_new} new, {h.urls_updated} updated")
    
    else:
        print(f"Unknown command: {command}")