"""
ctx parser: extracts header, body, and named blocks from a .ctx file.
"""
import json
import re
import hashlib
import time
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

HEADER_START = "---CTX-HEADER---"
HEADER_END = "---CTX-HEADER---"

NAMED_BLOCK_PATTERN = re.compile(
    r"::::<([^}>]+?)>(?:\s*\{([^}]*)\})?\s*:::\n(.*?)\n:::(?:\n\n|$)",
    re.DOTALL,
)


def sha256_of_body(full_text: str) -> str:
    """Compute SHA-256 of the file body (everything outside the header)."""
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
        # Split on first two occurrences of HEADER_START
        parts = raw.split(HEADER_START, 2)
        if len(parts) < 3:
            return {}
        # parts[1] is the JSON content between the first and second marker
        json_part = parts[1]
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
            # Try key=value format
            attrs = {}
            if attrs_raw:
                for part in attrs_raw.split(','):
                    part = part.strip()
                    if '=' in part:
                        k, v = part.split('=', 1)
                        attrs[k.strip()] = v.strip()
                    elif ':' in part:
                        k, v = part.split(':', 1)
                        attrs[k.strip()] = v.strip()
        # Ensure type exists
        if "type" not in attrs:
            attrs["type"] = chunk_type_raw or "unknown"
        chunk_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
        # Flatten attrs into chunk dict for backward compatibility
        chunk = {
            "type": attrs["type"],
            "ordinal": i,
            "hash": chunk_hash,
            "text": chunk_text,
        }
        # Add all attrs at top level (except type which is already set)
        for k, v in attrs.items():
            if k != "type":
                chunk[k] = v
        chunks.append(chunk)
    return chunks


def parse_ctx_string(text: str) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    """
    Parse .ctx content from a string and return (header_dict, body_text, chunks_list).
    If the header is missing, a synthetic header is generated.
    """
    full_text = text
    header = parse_header(full_text)

    # Extract body by stripping header if present
    if HEADER_START in full_text:
        parts = full_text.split(HEADER_START)
        if len(parts) >= 3:
            body = parts[0] + parts[2]
        else:
            body = full_text
    else:
        body = full_text

    # If header missing or id placeholder, compute real id
    if not header or header.get("id", "").startswith("sha256:placeholder"):
        content_hash = sha256_of_body(full_text)
        header = {
            "v": 1,
            "id": f"sha256:{content_hash}",
            "updated": int(time.time()),
            "author": "",
            "tags": [],
            "links": [],
            "embeddings": {},
        }

    chunks = extract_chunks(body)
    return header, body, chunks


def parse_ctx_file(path: Path) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
    """
    Read a .ctx file and return (header_dict, body_text, chunks_list).
    If the header is missing, a synthetic header is generated.
    """
    # Accept both Path and string
    if isinstance(path, str):
        path = Path(path)
    full_text = path.read_text(encoding="utf-8")
    return parse_ctx_string(full_text)


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