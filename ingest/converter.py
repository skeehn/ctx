"""
Converter: turns parsed structured data into a .ctx file.
"""
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

HEADER_START = "---CTX-HEADER---"
HEADER_END = "---CTX-HEADER---"


def _compute_body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _format_chunk_as_markdown_block(chunk: Dict[str, Any]) -> str:
    """
    Convert a chunk dict to a markdown named block.
    Expected chunk keys: type, content, and optional attributes (page, index, ocr, etc.)
    We'll use key:value format for attributes inside single braces.
    """
    chunk_type = chunk.get("type", "unknown")
    # Build attributes dict, excluding type and content
    attrs = {k: v for k, v in chunk.items() if k not in ("type", "content")}
    # Ensure attributes is a dict
    if not isinstance(attrs, dict):
        attrs = {}
    # Format attributes as key:value pairs
    if attrs:
        attrs_list = [f"{k}:{v}" for k, v in attrs.items()]
        attrs_str = ",".join(attrs_list)
        # Single braces: {{attrs_str}} -> outputs {attrs_str}
        return f"::::<{chunk_type}> {{{attrs_str}}}:::\n{chunk['content']}\n:::"
    else:
        # Empty braces: {{}} -> outputs {}
        return f"::::<{chunk_type}> {{}}:::\n{chunk['content']}\n:::"


def build_ctx_file(parsed: Dict[str, Any], output_path: Path) -> None:
    """
    Write a .ctx file from parsed data.
    parsed dict should have: title, authors, date, tags, chunks, and optionally source_url, etc.
    We'll ignore extra fields for now.
    """
    # Build body from chunks
    body_parts = []
    for chunk in parsed.get("chunks", []):
        body_parts.append(_format_chunk_as_markdown_block(chunk))
    body = "\n\n".join(body_parts)  # double newline between blocks for readability

    # If no chunks, we still want a body (maybe empty)
    if not body_parts:
        body = ""

    # Compute header
    content_hash = _compute_body_hash(body)
    header = {
        "v": 2,
        "id": f"sha256:{content_hash}",
        "updated": int(datetime.now().timestamp()),
        "title": parsed.get("title", ""),
        "authors": parsed.get("authors", []),
        "date": parsed.get("date", datetime.now().isoformat()),
        "tags": parsed.get("tags", []),
        "links": [],  # TODO: infer from URLs in text or user input
        "embeddings": {},  # TODO: compute if sentence-transformers available
    }
    # Note: we could also add source information (URL, etc.) to the header if we want.
    # For now, we keep it simple.

    header_json = json.dumps(header, indent=2)

    # Write file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"{HEADER_START}\n")
        f.write(header_json)
        f.write(f"\n{HEADER_END}\n")
        f.write(body)

    # Optionally, we could verify the file by reading it back and checking the hash.


def build_ctx_string(parsed: Dict[str, Any]) -> str:
    """
    Return the .ctx file as a string (useful for testing or API).
    """
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ctx", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        build_ctx_file(parsed, tmp_path)
        result = tmp_path.read_text(encoding="utf-8")
        return result
    finally:
        tmp_path.unlink(missing_ok=True)


# For testing
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python converter.py <json-file>")
        sys.exit(1)
    parsed = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    # Build to a temporary file and print
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ctx", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        build_ctx_file(parsed, tmp_path)
        print(tmp_path.read_text(encoding="utf-8"))
    finally:
        tmp_path.unlink(missing_ok=True)