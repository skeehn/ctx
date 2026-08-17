"""
Benchmark script comparing .ctx vs plain .md for AI agent context retrieval.
Generates a test vault, indexes .ctx, runs queries via API, and compares latency/token usage
against a naive markdown baseline (linear scan).
"""
import os
import sys
import time
import random
import string
import subprocess
import threading
import json
from pathlib import Path
from typing import List, Tuple, Dict

# ---------- Configuration ----------
NUM_NOTES = 200          # number of test notes
AVG_WORDS_PER_NOTE = 150  # approximate words per note
QUERY_COUNT = 10         # number of random queries to run
VAULT_CTX = Path("./test_vault_ctx")
VAULT_MD = Path("./test_vault_md")
DB_CTX = Path("./test_vault_ctx/vault.db")
API_PORT = 8080
API_HOST = "127.0.0.1"
BASE_URL = f"http://{API_HOST}:{API_PORT}"
SEED = 42
random.seed(SEED)

# ---------- Helper functions ----------
def random_word(length: int = 5) -> str:
    return ''.join(random.choices(string.ascii_lowercase, k=length))

def random_sentence(word_count: int = 7) -> str:
    words = [random_word(random.randint(3, 8)) for _ in range(word_count)]
    return ' '.join(words).capitalize() + '.'

def random_paragraph(sentence_count: int = 5) -> str:
    return ' '.join(random_sentence(random.randint(3, 9)) for _ in range(sentence_count))

def random_note_content(note_id: int) -> str:
    # Generate a note with title, some paragraphs, and maybe a code block
    title = f"Note {note_id}: {random_word().capitalize()} {random_word()}"
    paras = []
    for _ in range(random.randint(2, 4)):
        paras.append(random_paragraph(random.randint(3, 6)))
    # Occasionally add a code block
    if random.random() < 0.2:
        lang = random.choice(["python", "bash", "sql"])
        code = "\n".join([f"# {random_sentence()}" for _ in range(random.randint(2,5))])
        paras.append(f"```{lang}\n{code}\n```")
    return f"# {title}\n\n" + "\n\n".join(paras)

def random_tags() -> List[str]:
    tags = [random_word() for _ in range(random.randint(1, 3))]
    return [t for t in tags if t]

def random_links(existing_ids: List[int]) -> List[Dict[str, str]]:
    links = []
    for _ in range(random.randint(0, 2)):
        if not existing_ids:
            break
        target_id = random.choice(existing_ids)
        link_type = random.choice(["defines", "example", "see-also", "contradicts"])
        links.append({"target": f"note{target_id}.ctx", "type": link_type})
    return links

def create_ctx_note(note_id: int, existing_ids: List[int]) -> Tuple[str, str, dict]:
    """Returns (filename, full_text, header_dict)"""
    body = random_note_content(note_id)
    tags = random_tags()
    links = random_links(existing_ids)
    # Header
    header = {
        "v": 1,
        "id": "sha256:placeholder",  # will be replaced by indexer
        "updated": int(time.time()),
        "author": "benchmark",
        "tags": tags,
        "links": links,
        "embeddings": {}
    }
    # Build full text with placeholder header
    import json
    header_json = json.dumps(header, indent=2)
    full_text = f"---CTX-HEADER---\n{header_json}\n---CTX-HEADER---\n\n{body}"
    filename = f"note{note_id}.ctx"
    return filename, full_text, header

def create_md_note(note_id: int) -> Tuple[str, str]:
    body = random_note_content(note_id)
    filename = f"note{note_id}.md"
    return filename, body

# ---------- Test vault creation ----------
def create_test_vaults():
    print(f"Creating test vaults with {NUM_NOTES} notes each...")
    VAULT_CTX.mkdir(parents=True, exist_ok=True)
    VAULT_MD.mkdir(parents=True, exist_ok=True)
    existing_ids = []
    for i in range(1, NUM_NOTES+1):
        # .ctx
        ctx_filename, ctx_text, _ = create_ctx_note(i, existing_ids)
        (VAULT_CTX / ctx_filename).write_text(ctx_text, encoding="utf-8")
        # .md
        md_filename, md_text = create_md_note(i)
        (VAULT_MD / md_filename).write_text(md_text, encoding="utf-8")
        existing_ids.append(i)
        if i % 200 == 0:
            print(f"  Created {i}/{NUM_NOTES} notes")
    print("Test vaults created.")

# ---------- Indexer ----------
def start_indexer():
    print("Starting .ctx indexer...")
    # Run indexer as subprocess
    cmd = [sys.executable, "indexer.py", "--vault", str(VAULT_CTX), "--db", str(DB_CTX)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Wait a bit for initial scan
    time.sleep(5)
    # Check if process still alive
    if proc.poll() is not None:
        stdout, stderr = proc.communicate()
        print(f"Indexer failed to start: {stderr.decode()}")
        raise RuntimeError("Indexer failed to start")
    print("Indexer started (background).")
    return proc

# ---------- API server ----------
def start_api_server():
    print("Starting FastAPI server...")
    # Set environment variable for the vault root
    os.environ["CTX_VAULT_ROOT"] = str(VAULT_CTX)
    cmd = [sys.executable, "-m", "uvicorn", "api:app", "--host", API_HOST, "--port", str(API_PORT)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # Wait for server to be ready
    for _ in range(30):
        try:
            import requests
            resp = requests.get(f"{BASE_URL}/stats", timeout=1)
            if resp.status_code == 200:
                print("API server is ready.")
                return proc
        except Exception:
            # Check if process died
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                print(f"API server exited early: {stderr.decode()}")
                break
            time.sleep(0.5)
    stdout, stderr = proc.communicate()
    print(f"API server failed to start. Stdout: {stdout.decode()}\nStderr: {stderr.decode()}")
    raise RuntimeError("API server failed to start")

# ---------- Query functions ----------
def ctx_search(query: str) -> Tuple[List[dict], float]:
    start = time.perf_counter()
    try:
        import requests
        resp = requests.get(f"{BASE_URL}/search", params={"q": query, "limit": 10}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        elapsed = (time.perf_counter() - start) * 1000  # ms
        return data, elapsed
    except Exception as e:
        print(f"Error querying API: {e}")
        return [], 0.0

def md_baseline_search(query: str, md_files: List[Path]) -> Tuple[List[dict], float]:
    start = time.perf_counter()
    results = []
    query_lower = query.lower()
    for md_file in md_files:
        try:
            text = md_file.read_text(encoding="utf-8", errors='ignore')
            if query_lower in text.lower():
                # Extract a snippet (first 200 chars around match)
                idx = text.lower().find(query_lower)
                start_idx = max(0, idx - 100)
                end_idx = min(len(text), idx + len(query) + 100)
                snippet = text[start_idx:end_idx].replace("\n", " ")
                results.append({
                    "path": str(md_file.relative_to(VAULT_MD)),
                    "title": md_file.stem,
                    "chunk_type": "note",
                    "snippet": snippet,
                    "score": 1.0,  # dummy
                    "links": [],
                    "tags": []
                })
                if len(results) >= 10:
                    break
        except Exception:
            continue
    elapsed = (time.perf_counter() - start) * 1000
    return results, elapsed

def approximate_tokens_from_results(results: List[dict]) -> int:
    # Rough estimate: each word ~ 0.75 tokens, plus overhead.
    total_chars = sum(len(r.get('snippet', '')) for r in results)
    # Approx tokens = chars / 4 (since English ~4 chars per token)
    return int(total_chars / 4)

# ---------- Main benchmark ----------
def main():
    create_test_vaults()
    
    # Start indexer and API
    indexer_proc = start_indexer()
    api_proc = start_api_server()
    
    try:
        # Prepare list of markdown files for baseline
        md_files = list(VAULT_MD.glob("*.md"))
        print(f"Prepared {len(md_files)} markdown files for baseline.")
        
        # Generate random queries based on vocabulary from notes
        # We'll pick random words from a sample of notes
        sample_notes = random.sample(list(VAULT_CTX.glob("*.ctx")), min(50, NUM_NOTES))
        vocab = set()
        for note_path in sample_notes:
            text = note_path.read_text(encoding="utf-8", errors='ignore')
            # extract words from body (skip header)
            if "---CTX-HEADER---" in text:
                _, _, body = text.partition("---CTX-HEADER---")
                _, _, body = body.partition("---CTX-HEADER---")
            else:
                body = text
            words = [w.strip('.,!?:;"\'()[]{}') for w in body.split()]
            vocab.update([w.lower() for w in words if len(w) > 3])
        vocab_list = list(vocab)
        if not vocab_list:
            vocab_list = ["test", "sample", "note"]
        queries = [random.choice(vocab_list) for _ in range(QUERY_COUNT)]
        
        # Run ctx queries
        print("Running .ctx queries via API...")
        ctx_latencies = []
        ctx_tokens = []
        for q in queries:
            results, latency = ctx_search(q)
            ctx_latencies.append(latency)
            ctx_tokens.append(approximate_tokens_from_results(results))
        # Run md baseline
        print("Running markdown baseline (linear scan)...")
        md_latencies = []
        md_tokens = []
        for q in queries:
            results, latency = md_baseline_search(q, md_files)
            md_latencies.append(latency)
            md_tokens.append(approximate_tokens_from_results(results))
        
        # Compute averages
        avg_ctx_lat = sum(ctx_latencies)/len(ctx_latencies) if ctx_latencies else 0
        avg_md_lat = sum(md_latencies)/len(md_latencies) if md_latencies else 0
        avg_ctx_tok = sum(ctx_tokens)/len(ctx_tokens) if ctx_tokens else 0
        avg_md_tok = sum(md_tokens)/len(md_tokens) if md_tokens else 0
        
        print("\n=== Benchmark Results ===")
        print(f"Number of notes: {NUM_NOTES}")
        print(f"Queries run: {QUERY_COUNT}")
        print(f".ctx avg latency per query: {avg_ctx_lat:.2f} ms")
        print(f"Markdown avg latency per query: {avg_md_lat:.2f} ms")
        print(f"Latency improvement (md/ctx): {avg_md_lat/avg_ctx_lat if avg_ctx_lat>0 else 'inf':.2f}×")
        token_reduction = avg_md_tok/avg_ctx_tok if avg_ctx_tok>0 else float('inf')
        token_reduction = avg_md_tok/avg_ctx_tok if avg_ctx_tok>0 else float('inf')
        print(f".ctx avg tokens returned per query: {avg_ctx_tok:.1f}")
        print(f"Markdown avg tokens returned per query: {avg_md_tok:.1f}")
        print(f"Token reduction (ctx/md): {token_reduction:.2f}×")
        # Determine if we met 10× goals
        latency_improvement = avg_md_lat/avg_ctx_lat if avg_ctx_lat>0 else float('inf')
        token_improvement = avg_md_tok/avg_ctx_tok if avg_ctx_tok>0 else float('inf')
        if latency_improvement >= 10 and token_improvement >= 10:
            print("\n✅ SUCCESS: Achieved ≥10× latency and token improvements over plain markdown.")
        else:
            print("\n⚠️  NOTE: Did not reach 10× improvement on both metrics.")
            print(f"   Latency improvement: {latency_improvement:.2f}×")
            print(f"   Token improvement: {token_improvement:.2f}×")
    finally:
        # Cleanup: kill API server and indexer
        print("\nShutting down API server and indexer...")
        for proc_name, proc in [("API server", api_proc), ("Indexer", indexer_proc)]:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            print(f"{proc_name} stopped.")
        # Optionally remove test vaults
        # import shutil
        # shutil.rmtree(VAULT_CTX, ignore_errors=True)
        # shutil.rmtree(VAULT_MD, ignore_errors=True)
        # DB_CTX.unlink(missing_ok=True)

if __name__ == "__main__":
    main()