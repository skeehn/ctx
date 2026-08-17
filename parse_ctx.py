"""
ctx parser: extracts header, body, and named blocks from a .ctx file.
"""
import json
import re
import hashlib
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

HEADER_START = "---CTX-HEADER---"
HEADER_END = "---CTX-HEADER---"

NAMED_BLOCK_PATTERN = re.compile(
    r"::<\s*([^}>]+?)\s*\{([^}]*)\}\s*:::(.*?):::",
    re.DOTALL,
)


def sha256_of_body(full_text: str) -> str:
    """Compute SHA‑256 of the file body (everything outside the header)."""
    # Remove header block if present
    if HEADER_START in full_text:
        parts = full_text.split(HEADER_START)
        if len(parts) >= 3:
            # parts[0] = before first marker (usually empty or whitespace)
            # parts[1] = header json
            # parts[2] = rest after second marker
            body = parts[0] + parts[2]
        else:
            # malformed, fallback to whole file
            body = full_text
    else:
        body = full_text
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def parse_header(raw: str) -> Dict[str, Any]:
    """Parse the JSON header; return empty dict if missing or invalid."""
    if not raw.startswith(HEADER_START):
        return {}
    try:
        _, json_part, _ = raw.split(HEADER_START, 2)
        json_part, _ = json_part.split(HEADER_END, 1)
        return json.loads(json_part.strip())
    except (ValueError, json.JSONDecodeError):
        return {}


def extract_chunks(body: str) -> List[Dict[str, Any]]:
    """Return list of named blocks with metadata."""
    chunks: List[Dict[str, Any]] = []
    for i, m in enumerate(NAMED_BLOCK_PATTERN.finditer(body)):
        chunk_type_raw = m.group(1).strip()
        attrs_raw = m.group(2).strip()
        chunk_text = m.group(3)
        try:
            attrs = json.loads(attrs_raw) if attrs_raw else {}
        except json.JSONDecodeError:
            attrs = {}
        # Ensure type exists
        if "type" not in attrs:
            attrs["type"] = chunk_type_raw or "unknown"
        chunk_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
        chunks.append(
            {
                "type": attrs["type"],
                "attrs": attrs,
                "ordinal": i,
                "hash": chunk_hash,
                "text": chunk_text,
            }
        )
    return chunks


def parse_ctx_file(path: Path) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    """
    Read a .ctx file and return (header_dict, body_text, chunks_list).
    If the header is missing, a synthetic header is generated.
    """
    full_text = path.read_text(encoding="utf-8")
    header = parse_header(full_text)
    # If header missing or id placeholder, compute real id
    if not header or header.get("id", "").startswith("sha256:placeholder"):
        body = full_text  # no header to strip
        content_hash = sha256_of_body(full_text)
        header = {
            "v": 1,
            "id": f"sha256:{content_hash}",
            "updated": int(path.stat().st_mtime),
            "author": "",
            "tags": [],
            "links": [],
            "embeddings": {},
        }
    else:
        # Strip header to get body for further processing
        if HEADER_START in full_text:
            parts = full_text.split(HEADER_START)
            if len(parts) >= 3:
                body = parts[0] + parts[2]
            else:
                body = full_text
        else:
            body = full_text
    chunks = extract_chunks(body)
    return header, body, chunks


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python parse_ctx.py <path-to-.ctx>")
        sys.exit(1)
    p = Path(sys.argv[1])
    header, body, chunks = parse_ctx_file(p)
    print("Header:")
    print(json.dumps(header, indent=2))
    print("\nBody (first 200 chars):")
    print(body[:200])
    print(f"\nFound {len(chunks)} named blocks:")
    for c in chunks:
        print(f"  - [{c['type']}] {c['hash'][:8]}... ({len(c['text'])} chars)")