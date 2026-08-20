#!/usr/bin/env python3
"""
Comprehensive Test Suite for ctx-vault
Includes unit tests, integration tests, and e2e tests.
"""
import os
import sys
import json
import tempfile
import shutil
import unittest
import io
import sqlite3
from pathlib import Path
from typing import Dict, List, Any
from unittest.mock import patch, MagicMock, AsyncMock
import asyncio

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

# Set test environment BEFORE importing modules that use database
os.environ["CTX_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ["CTX_VAULT_ROOT"] = str(Path(tempfile.mkdtemp()) / "ai-vault")

from parse_ctx import parse_ctx_file, parse_ctx_string, extract_chunks, sha256_of_body
from ingest.converter import build_ctx_file
from indexer import init_db, upsert_file
from api import get_conn, internal_search as api_search
from api import get_conn as api_get_conn
from ingest.fetcher import fetch_source_sync, FetchError
from ingest.parsers.web import parse_web_html
from ingest.parsers.pdf import parse_pdf
from ingest.parsers.image import parse_image
from ingest.parsers.media import parse_media
from ingest.converter import build_ctx_file as converter_build_ctx_file
from auth import init_auth_db, generate_api_key, verify_api_key, check_rate_limit
from backup import BackupManager, create_backup_cli, list_backups_cli, restore_backup_cli
from incremental_crawler import IncrementalCrawler, CrawlSource
from reranker import rerank_results, search_with_rerank
from skill_system import SkillRegistry, Skill, SkillType, AgentRole, initialize_default_skills


class TestParseCtx(unittest.TestCase):
    """Test .ctx file parsing and building."""
    
    def setUp(self):
        self.sample_ctx = """---CTX-HEADER---
{
  "v": 2,
  "id": "sha256:abc123",
  "updated": 1700000000,
  "title": "Test Note",
  "authors": ["Author One", "Author Two"],
  "date": "2024-01-15T10:00:00",
  "tags": ["test", "python"],
  "links": [{"target": "other.ctx", "type": "references"}],
  "embeddings": {}
}
---CTX-HEADER---
::::<text> {ordinal=0}:::
This is the main content of the note.

It has multiple paragraphs.
:::

::::<code> {ordinal=1,language=python}:::
def hello():
    print("Hello, World!")
:::

::::<table> {ordinal=2}:::
| Header 1 | Header 2 |
|----------|----------|
| Cell 1   | Cell 2   |
| Cell 3   | Cell 4   |
:::"""
    
    def test_parse_valid_ctx(self):
        """Test parsing a valid .ctx file."""
        header, body, chunks = parse_ctx_string(self.sample_ctx)
        
        self.assertEqual(header["v"], 2)
        self.assertEqual(header["title"], "Test Note")
        self.assertEqual(header["authors"], ["Author One", "Author Two"])
        self.assertEqual(header["tags"], ["test", "python"])
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0]["type"], "text")
        self.assertEqual(chunks[1]["type"], "code")
        self.assertEqual(chunks[1].get("language"), "python")
        self.assertEqual(chunks[2]["type"], "table")
    
    def test_extract_chunks(self):
        """Test chunk extraction from body."""
        _, body, chunks = parse_ctx_string(self.sample_ctx)
        extracted = extract_chunks(body)
        
        self.assertEqual(len(extracted), 3)
        self.assertIn("text", [c["type"] for c in extracted])
        self.assertIn("code", [c["type"] for c in extracted])
        self.assertIn("table", [c["type"] for c in extracted])
    
    def test_sha256_of_body(self):
        """Test SHA256 computation."""
        hash1 = sha256_of_body("test content")
        hash2 = sha256_of_body("test content")
        hash3 = sha256_of_body("different content")
        
        self.assertEqual(hash1, hash2)
        self.assertNotEqual(hash1, hash3)
        self.assertEqual(len(hash1), 64)  # SHA256 hex length
    
    def test_build_ctx_file(self):
        """Test building a .ctx file from parsed data."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ctx", delete=False) as f:
            tmp_path = Path(f.name)
        
        try:
            parsed = {
                "title": "Test",
                "authors": ["Me"],
                "date": "2024-01-01",
                "tags": ["test"],
                "chunks": [
                    {"type": "text", "content": "Hello world"},
                    {"type": "code", "content": "print(1)", "language": "python"},
                ]
            }
            build_ctx_file(parsed, tmp_path)
            
            # Verify it can be parsed back
            header, body, chunks = parse_ctx_file(tmp_path)
            self.assertEqual(header["title"], "Test")
            self.assertEqual(len(chunks), 2)
        finally:
            tmp_path.unlink(missing_ok=True)
    
    def test_invalid_ctx(self):
        """Test handling of invalid .ctx files."""
        invalid = "not a valid ctx file"
        header, body, chunks = parse_ctx_string(invalid)
        # Parser generates synthetic header for invalid input
        self.assertIn("v", header)
        self.assertEqual(chunks, [])


class TestIndexer(unittest.TestCase):
    """Test indexer functions."""
    
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.db_path = self.temp_dir / "test.db"
        self.vault_root = self.temp_dir / "vault"
        self.vault_root.mkdir()
        
        # Initialize DB
        self.conn = init_db(self.db_path)
    
    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    async def test_upsert_and_search(self):
        """Test upserting a file and searching."""
        header = {
            "v": 2,
            "id": "sha256:test123",
            "updated": 1700000000,
            "title": "Test Document",
            "authors": ["Tester"],
            "date": "2024-01-01",
            "tags": ["test", "unit"],
            "links": [],
            "embeddings": {}
        }
        body = "This is a test document about Python async programming."
        
        rel_path = Path("test_note.ctx")
        file_id = upsert_file(self.conn, self.vault_root, rel_path, header, body)
        
        self.assertIsInstance(file_id, int)
        self.assertGreater(file_id, 0)
        
        # Search
        results = await api_search(self.conn, "async", limit=10)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["title"], "Test Document")
    
    async def test_multiple_files(self):
        """Test indexing multiple files."""
        for i in range(5):
            header = {"title": f"Doc {i}", "tags": ["test"]}
            body = f"Content of document {i} about topic {i % 2}."
            upsert_file(self.conn, self.vault_root, Path(f"doc{i}.ctx"), header, body)
        
        # Search for topic 0
        results = await api_search(self.conn, "topic 0", limit=10)
        self.assertGreaterEqual(len(results), 2)


class TestAuth(unittest.TestCase):
    """Test authentication system."""
    
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        os.environ["CTX_DB_PATH"] = str(self.temp_dir / "test_auth.db")
    
    def tearDown(self):
        if "CTX_DB_PATH" in os.environ:
            del os.environ["CTX_DB_PATH"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_generate_and_verify_api_key(self):
        """Test API key generation and verification."""
        init_auth_db()
        
        plain_key, api_key = generate_api_key(
            "test-agent", "local", ["read", "write", "ingest"], 1000, 30
        )
        
        self.assertTrue(plain_key.startswith("ctx_"))
        self.assertEqual(api_key.name, "test-agent")
        self.assertEqual(api_key.tenant, "local")
        self.assertIn("read", api_key.scopes)
        self.assertEqual(api_key.rate_limit, 1000)
        
        # Verify key
        verified = verify_api_key(plain_key)
        self.assertIsNotNone(verified)
        self.assertEqual(verified.key_id, api_key.key_id)
    
    def test_invalid_key(self):
        """Test invalid key rejection."""
        init_auth_db()
        verified = verify_api_key("ctx_invalid_key_12345")
        self.assertIsNone(verified)
    
    def test_rate_limiting(self):
        """Test rate limiting."""
        init_auth_db()
        plain_key, api_key = generate_api_key("rate-test", "local", ["read"], 5, 1)
        
        # First 5 requests should pass
        for i in range(5):
            info = check_rate_limit(api_key.key_id, api_key.rate_limit)
            self.assertEqual(info.remaining, 5 - i)
        
        # 6th should fail
        info = check_rate_limit(api_key.key_id, api_key.rate_limit)
        self.assertEqual(info.remaining, 0)
        self.assertIsNotNone(info.retry_after)


class TestIngestion(unittest.TestCase):
    """Test ingestion pipeline."""
    
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.vault_root = self.temp_dir / "vault"
        self.vault_root.mkdir()
    
    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_web_parser(self):
            """Test web HTML parsing."""
            html = """
            <html>
            <head><title>Test Article</title><meta name="author" content="John Doe"></head>
            <body>
                <article>
                    <h1>Main Title</h1>
                    <p>This is a paragraph.</p>
                    <p>Another paragraph with <code>code</code>.</p>
                    <pre><code class="language-python">def hello():\\n    print("hi")</code></pre>
                    <table><tr><th>H1</th><th>H2</th></tr><tr><td>C1</td><td>C2</td></tr></table>
                </article>
            </body>
            </html>
            """
            parsed = parse_web_html(html, base_url="https://example.com/article")
        
            self.assertEqual(parsed["title"], "Test Article")
            self.assertIn("John Doe", parsed["authors"])
            self.assertGreater(len(parsed["chunks"]), 0)
        
            # Check chunk types
            chunk_types = [c["type"] for c in parsed["chunks"]]
            self.assertIn("heading", chunk_types)
            self.assertIn("text", chunk_types)
            self.assertIn("code", chunk_types)
            self.assertIn("table", chunk_types)
    
    def test_pdf_parser(self):
        """Test PDF parsing (basic)."""
        # Create a minimal PDF-like content
        # Note: Full PDF testing requires actual PDF files
        pass
    
    def test_image_parser(self):
        """Test image parsing (basic)."""
        # Create a simple test image
        from PIL import Image
        img = Image.new('RGB', (100, 100), color='red')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        img_bytes = buf.getvalue()
        
        parsed = parse_image(img_bytes, filename="test.png")
        
        self.assertIn("title", parsed)
        self.assertIn("chunks", parsed)
        self.assertIsInstance(parsed["chunks"], list)
    
    def test_fetcher(self):
        """Test source fetcher."""
        # Test local file
        test_file = Path(tempfile.mktemp(suffix=".txt"))
        test_file.write_text("Hello, World!")
        
        try:
            content, mime = fetch_source_sync(f"file://{test_file}")
            self.assertEqual(content.decode(), "Hello, World!")
            self.assertEqual(mime, "text/plain")
        finally:
            test_file.unlink(missing_ok=True)
    
    def test_converter(self):
        """Test .ctx file converter."""
        parsed = {
            "title": "Test",
            "authors": ["Tester"],
            "date": "2024-01-01",
            "tags": ["test"],
            "chunks": [
                {"type": "text", "content": "Hello"},
                {"type": "code", "content": "print(1)", "language": "python"},
            ]
        }
        
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ctx", delete=False) as f:
            tmp_path = Path(f.name)
        
        try:
            converter_build_ctx_file(parsed, tmp_path)
            content = tmp_path.read_text()
            
            self.assertIn("---CTX-HEADER---", content)
            self.assertIn("Test", content)
            self.assertIn("Hello", content)
            self.assertIn("print(1)", content)
        finally:
            tmp_path.unlink(missing_ok=True)


class TestBackup(unittest.TestCase):
    """Test backup/restore system."""
    
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.vault_root = self.temp_dir / "vault"
        self.vault_root.mkdir()
        
        # Create test vault with some files
        for i in range(3):
            ctx_file = self.vault_root / f"note{i}.ctx"
            ctx_file.write_text(f"""---CTX-HEADER---
{{"v": 2, "id": "sha256:test{i}", "title": "Note {i}", "tags": ["test"]}}
---CTX-HEADER---
::::<text> {{ordinal:0}}:::
Content of note {i}.""")
        
        # Create DB - use the same path as CTX_DB_PATH env var
        os.environ["CTX_DB_PATH"] = str(self.vault_root / "vault.db")
        self.db_path = self.vault_root / "vault.db"
        init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        for i in range(3):
            header = {"title": f"Note {i}", "tags": ["test"]}
            body = f"Content of note {i}."
            upsert_file(conn, self.vault_root, Path(f"note{i}.ctx"), header, body)
        conn.commit()
        conn.close()
    
    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_create_backup(self):
        """Test creating a backup."""
        manager = BackupManager(self.vault_root)
        metadata = manager.create_backup("test_backup", "Test notes")
        
        self.assertEqual(metadata.backup_id, "test_backup")
        self.assertEqual(metadata.total_files, 3)
        self.assertGreater(metadata.total_size_bytes, 0)
    
    def test_list_backups(self):
        """Test listing backups."""
        manager = BackupManager(self.vault_root)
        manager.create_backup("backup1")
        manager.create_backup("backup2")
        
        backups = manager.list_backups()
        self.assertEqual(len(backups), 2)
    
    def test_restore_backup(self):
        """Test restoring a backup."""
        manager = BackupManager(self.vault_root)
        metadata = manager.create_backup("restore_test")
        
        # Clear vault contents but keep the directory (so backup_dir still exists)
        for ctx_file in self.vault_root.glob("*.ctx"):
            ctx_file.unlink()
        if (self.vault_root / "vault.db").exists():
            (self.vault_root / "vault.db").unlink()
        
        # Restore
        manager.restore_backup("restore_test", self.vault_root, overwrite=True)
        
        # Verify files restored
        restored_files = list(self.vault_root.glob("*.ctx"))
        self.assertEqual(len(restored_files), 3)
    
    def test_verify_backup(self):
        """Test backup verification."""
        manager = BackupManager(self.vault_root)
        metadata = manager.create_backup("verify_test")
        
        result = manager.verify_backup("verify_test")
        self.assertTrue(result["valid"])
        self.assertEqual(result["file_count"], 3)


class TestIncrementalCrawler(unittest.TestCase):
    """Test incremental crawler."""
    
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.vault_root = self.temp_dir / "vault"
        self.vault_root.mkdir()
        self.db_path = self.temp_dir / "crawl.db"
    
    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_add_rss_source(self):
        """Test adding RSS source."""
        crawler = IncrementalCrawler(self.vault_root, self.db_path)
        source = CrawlSource(
            source_id="rss_test",
            name="Test RSS",
            source_type="rss",
            url="https://example.com/feed.xml",
            schedule="0 */6 * * *",
        )
        crawler.add_source(source)
        
        sources = crawler.list_sources()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].source_id, "rss_test")
    
    def test_add_sitemap_source(self):
        """Test adding sitemap source."""
        crawler = IncrementalCrawler(self.vault_root, self.db_path)
        source = CrawlSource(
            source_id="sitemap_test",
            name="Test Sitemap",
            source_type="sitemap",
            url="https://example.com/sitemap.xml",
            schedule="0 */12 * * *",
        )
        crawler.add_source(source)
        
        sources = crawler.list_sources()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].source_type, "sitemap")


class TestReranker(unittest.TestCase):
    """Test cross-encoder reranker."""
    
    def test_rerank_mock(self):
        """Test reranking with mock results."""
        mock_results = [
            {"path": "a.ctx", "title": "Python Async", "snippet": "asyncio.gather runs coroutines", "score": -2.5, "links": [], "tags": []},
            {"path": "b.ctx", "title": "Database", "snippet": "Connection pooling", "score": -3.1, "links": [], "tags": []},
            {"path": "c.ctx", "title": "Network", "snippet": "TCP vs UDP", "score": -4.2, "links": [], "tags": []},
        ]
        
        # Test without model (should return original)
        reranked = search_with_rerank("asyncio", mock_results, top_k=2)
        self.assertEqual(len(reranked), 2)
    
    def test_hybrid_rerank(self):
        """Test hybrid reranking."""
        mock_results = [
            {"path": "a.ctx", "title": "Python", "snippet": "async def", "score": -2.0, "links": [], "tags": []},
            {"path": "b.ctx", "title": "Java", "snippet": "public class", "score": -3.0, "links": [], "tags": []},
        ]
        
        # Should not crash even without model
        reranked = search_with_rerank("async", mock_results, top_k=2, use_hybrid=True)
        self.assertEqual(len(reranked), 2)


class TestSkillSystem(unittest.TestCase):
    """Test skill system."""
    
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.db_path = self.temp_dir / "skills.db"
        self.registry = SkillRegistry(str(self.db_path))
        initialize_default_skills(self.registry)
    
    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_default_skills(self):
        """Test default skills are initialized."""
        skills = self.registry.list_skills()
        self.assertGreaterEqual(len(skills), 6)  # At least 6 default skills
        
        skill_names = [s.name for s in skills]
        self.assertIn("Knowledge Search", skill_names)
        self.assertIn("Code Generation", skill_names)
        self.assertIn("Deep Research", skill_names)
        self.assertIn("Multimodal Ingestor", skill_names)
    
    def test_create_skill(self):
        """Test creating a custom skill."""
        skill = Skill(
            id="skill_custom",
            name="Custom Skill",
            type=SkillType.CUSTOM,
            description="A custom skill for testing",
            required_tags=["test"],
            max_tokens=2000,
        )
        self.registry.register_skill(skill)
        
        retrieved = self.registry.get_skill("skill_custom")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.name, "Custom Skill")
    
    def test_agent_context(self):
        """Test agent context creation and management."""
        root = self.registry.create_agent_context(AgentRole.ROOT, session_id="test_session")
        self.assertEqual(root.role, AgentRole.ROOT)
        self.assertEqual(root.session_id, "test_session")
        
        subagent = self.registry.create_agent_context(
            AgentRole.SUBAGENT, parent_id=root.agent_id, session_id="test_session"
        )
        self.assertEqual(subagent.role, AgentRole.SUBAGENT)
        self.assertEqual(subagent.parent_id, root.agent_id)
        
        # Test skill assignment
        root.skill_ids = ["skill_search", "skill_code_gen"]
        self.registry.save_agent_context(root)
        
        retrieved = self.registry.get_agent_context(root.agent_id)
        self.assertEqual(set(retrieved.skill_ids), {"skill_search", "skill_code_gen"})
    
    def test_shared_insights(self):
        """Test sharing and retrieving insights."""
        agent = self.registry.create_agent_context(AgentRole.SUBAGENT, session_id="test")
        
        self.registry.share_insight(
            agent_id=agent.agent_id,
            skill_id="skill_code_gen",
            insight="Use async/await for I/O",
            context_tags=["python", "async"],
            related_files=["notes/async.ctx"]
        )
        
        insights = self.registry.get_shared_insights(skill_id="skill_code_gen", context_tags=["python"])
        self.assertGreaterEqual(len(insights), 1)
        self.assertIn("async", insights[0]["insight"])


class TestEndToEnd(unittest.TestCase):
    """End-to-end integration tests."""
    
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.vault_root = self.temp_dir / "vault"
        self.vault_root.mkdir()
        
        # Set up DB
        os.environ["CTX_DB_PATH"] = str(self.temp_dir / "vault.db")
        os.environ["CTX_VAULT_ROOT"] = str(self.vault_root)
        
        init_auth_db()
        self.conn = init_db(self.temp_dir / "vault.db")
    
    def tearDown(self):
        if "CTX_DB_PATH" in os.environ:
            del os.environ["CTX_DB_PATH"]
        if "CTX_VAULT_ROOT" in os.environ:
            del os.environ["CTX_VAULT_ROOT"]
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.conn.close()
    
    async def test_full_ingestion_flow(self):
        """Test complete ingestion flow: fetch -> parse -> store -> index -> search."""
        # 1. Create test content
        test_content = """
        <html>
        <head><title>E2E Test</title><meta name="author" content="E2E Tester"></head>
        <body><article><h1>E2E Test Article</h1><p>This is end-to-end test content about Python async.</p></article></body>
        </html>
        """
        
        # 2. Parse
        parsed = parse_web_html(test_content, base_url="https://example.com/e2e")
        self.assertEqual(parsed["title"], "E2E Test Article")
        
        # 3. Convert to .ctx
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ctx", delete=False) as f:
            tmp_path = Path(f.name)
        
        try:
            converter_build_ctx_file(parsed, tmp_path)
            
            # 4. Read and index
            header, body, chunks = parse_ctx_file(tmp_path)
            upsert_file(self.conn, self.vault_root, Path("e2e_test.ctx"), header, body)
            
            # 5. Search
            results = await api_search(self.conn, "async", limit=5)
            self.assertGreater(len(results), 0)
            self.assertIn("async", results[0]["snippet"].lower())
        finally:
            tmp_path.unlink(missing_ok=True)
    
    def test_agent_skill_workflow(self):
        """Test agent using skills to interact with vault."""
        # Initialize skill registry
        registry = SkillRegistry(str(Path(self.temp_dir) / "skills.db"))
        initialize_default_skills(registry)
        
        # Create root agent
        root = registry.create_agent_context(AgentRole.ROOT, session_id="e2e_test")
        root.skill_ids = ["skill_search", "skill_ingestor", "skill_code_gen"]
        registry.save_agent_context(root)
        
        # Create subagent for ingestion
        ingestor = registry.create_agent_context(
            AgentRole.SUBAGENT, parent_id=root.agent_id, session_id="e2e_test"
        )
        ingestor.skill_ids = ["skill_ingestor", "skill_search"]
        registry.save_agent_context(ingestor)
        
        # Verify skill inheritance
        self.assertIn("skill_ingestor", ingestor.skill_ids)
        self.assertIn("skill_search", ingestor.skill_ids)
        
        # Share insight from subagent
        registry.share_insight(
            agent_id=ingestor.agent_id,
            skill_id="skill_ingestor",
            insight="RSS feeds update every 6 hours",
            context_tags=["rss", "crawl", "schedule"],
            related_files=["config/crawl_sources.ctx"]
        )
        
        # Root agent can access insight
        insights = registry.get_shared_insights(skill_id="skill_ingestor", context_tags=["rss"])
        self.assertGreaterEqual(len(insights), 1)
        self.assertIn("6 hours", insights[0]["insight"])


class TestPerformance(unittest.TestCase):
    """Performance benchmarks."""
    
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.vault_root = self.temp_dir / "vault"
        self.vault_root.mkdir()
        self.db_path = self.temp_dir / "perf.db"
        self.conn = init_db(self.db_path)
    
    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    async def test_search_latency(self):
        """Benchmark search latency."""
        import time
        
        # Index 1000 documents
        for i in range(1000):
            header = {"title": f"Doc {i}", "tags": ["perf", f"topic{i%10}"]}
            body = f"Document {i} content about topic {i%10}. " * 10
            upsert_file(self.conn, self.vault_root, Path(f"doc{i}.ctx"), header, body)
        
        # Warm up
        for _ in range(10):
            await api_search(self.conn, "topic", limit=10)
        
        # Benchmark
        start = time.perf_counter()
        for _ in range(100):
            await api_search(self.conn, "topic 5", limit=10)
        elapsed = time.perf_counter() - start
        
        avg_ms = (elapsed / 100) * 1000
        print(f"\nAvg search latency: {avg_ms:.2f}ms")
        self.assertLess(avg_ms, 50)  # Should be under 50ms
    
    def test_ingestion_throughput(self):
        """Benchmark ingestion throughput."""
        import time
        from ingest.parsers.web import parse_web_html
        from ingest.converter import build_ctx_file as converter_build
        
        html = """
        <html><head><title>Perf Test</title></head>
        <body><article><h1>Title</h1><p>Content """ + "x" * 1000 + """</p></article></body></html>
        """
        
        start = time.perf_counter()
        for _ in range(100):
            parsed = parse_web_html(html, base_url="https://example.com")
            with tempfile.NamedTemporaryFile(mode="w", suffix=".ctx", delete=False) as f:
                tmp = Path(f.name)
            try:
                build_ctx_file(parsed, tmp)
                tmp.unlink()
            finally:
                pass
        elapsed = time.perf_counter() - start
        
        print(f"\nIngestion throughput: {100/elapsed:.1f} docs/sec")
        self.assertGreater(100/elapsed, 10)  # At least 10 docs/sec


def run_all_tests():
    """Run all tests with detailed output."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # Add all test classes
    test_classes = [
        TestParseCtx,
        TestIndexer,
        TestAuth,
        TestIngestion,
        TestBackup,
        TestIncrementalCrawler,
        TestReranker,
        TestSkillSystem,
        TestEndToEnd,
        TestPerformance,
    ]
    
    for tc in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(tc))
    
    runner = unittest.TextTestRunner(verbosity=2, buffer=True)
    result = runner.run(suite)
    
    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)