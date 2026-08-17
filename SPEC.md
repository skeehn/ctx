# Context File (.ctx) Specification

## Overview
A `.ctx` file is a superset of Markdown designed for AI‑agent‑first knowledge bases. It adds three clearly delimited sections while remaining fully editable in any text editor.

## File Layout
```
---CTX-HEADER---
{ ... JSON header ... }
---CTX-HEADER---

# Optional Title (can be overridden by header.title)
Free‑form Markdown‑like prose …
###::note{
  "type":"summary",
  "weight":1.2
}
This is a named block (chunk) that agents can address directly.
:::
```

### Sections

1. **`---CTX-HEADER--- … ---CTX-HEADER---`**  
   JSON object with the following fields:
   - `v` (integer): format version, currently `1`.
   - `id` (string): `"sha256:<hash‑of‑body>"` where the hash is computed over the entire file **excluding** the header block (including newlines). Updated on every save.
   - `updated` (integer): Unix epoch seconds of last modification.
   - `author` (string, optional): author name or identifier.
   - `tags` (array of strings): free‑form tags for categorisation.
   - `links` (array of objects): each object has:
     - `target` (string): relative path to another `.ctx` file.
     - `type` (string): semantic link type, e.g. `"defines"`, `"example"`, `"see-also"`, `"contradicts"`.
   - `embeddings` (object, optional): mapping from name to base64‑encoded vector (e.g. `"summary":"AAAA…"`). If omitted, the indexer may compute vectors on‑demand.

2. **Free‑form prose**  
   Any Markdown‑compatible text (headings, lists, code fences, tables, etc.). This section is intended for human readers and is ignored by the structured parser except for chunk extraction.

3. **Named blocks (`::<TYPE>{JSON}…:::`) **  
   A fenced block that starts with `::<JSON>{` and ends with `:::` is extracted as a *chunk*.
   - The JSON inside the opening fuse may contain arbitrary attributes; at minimum it should include `"type"` (string) to categorise the chunk.
   - The block’s content (everything between the closing `}` of the JSON and the opening `:::`) is the chunk’s raw text.
   - Each chunk receives a stable ID derived from the SHA‑256 of its raw text (excluding the fences). This ID can be used by agents to cache or reference the chunk across versions.

### Conventions
- All text is UTF‑8.
- Whitespace inside the JSON header is insignificant; pretty‑printing is encouraged for readability.
- If a file lacks the header, treat it as a legacy note: synthesize a header on‑the‑fly (hash the whole file, empty tags/links, empty embeddings) and store it alongside the file (or in the index) so future edits gain the header automatically.
- Link targets must be relative to the vault root and must point to files with the `.ctx` extension.
- Chunk types are free‑form strings; recommended common types include `"note"`, `"summary"`, `"code/<language>"`, `"definition"`, `"example"`, `"table"`.
- The optional `embeddings` field allows pre‑computed vectors to be stored with the source, eliminating an external vector store when desired.

### Example
```ctx
---CTX-HEADER---
{
  "v":1,
  "id":"sha256:3a7bd3e2360a3d29eea436fcfb7e44c735d11ca844758ae990906b927b88f4fa",
  "updated":1726051200,
  "author":"kstephenkeehn",
  "tags":["ai","context","file-format"],
  "links":[
    {"target":"notes/architecture.ctx","type":"defines"},
    {"target":"notes/faq.ctx","type":"see-also"}
  ],
  "embeddings":{
    "summary":"AAAA...",
    "full":"BBBB..."
  }
}
---CTX-HEADER---

# Context File Format
This document defines the `.ctx` specification.

###::note{
  "type":"summary",
  "weight":1.2
}
A .ctx file combines a JSON header, free‑form markdown, and typed named blocks for efficient agent access.
:::
```