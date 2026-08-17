# ctx-vault Quick Start for Hermes & Claude Code Agents

## Installation

### Option 1: Direct Installation (Recommended for Development)
```bash
# Clone the repository
git clone https://github.com/skeehn/ctx.git
cd ctx

# Install dependencies
pip install -r requirements.txt

# For development/testing
pip install -r requirements-dev.txt
```

### Option 2: Docker (For Production/Isolated Environment)
```bash
docker build -t ctx-vault .
```

## Setting Up Your Knowledge Base

### 1. Create a Vault Directory
```bash
mkdir -p ~/my-ctx-vault
cd ~/my-ctx-vault
```

### 2. Create Your First .ctx File
```bash
mkdir -p notes
cat > notes/welcome.ctx << 'CTX'
---CTX-HEADER---
{
  "v": 1,
  "id": "sha256:placeholder",
  "updated": 1786803636,
  "author": "you",
  "tags": ["welcome", "getting-started"],
  "links": [],
  "embeddings": {}
}
---CTX-HEADER---
# Welcome to ctx-vault

This is your first knowledge base note using the ctx-vault format.

::note{}
This is an addressable chunk that AI agents can reference directly.
:::

## Why ctx-vault?

ctx-vault provides >10 performance improvement over plain Markdown for AI agent context retrieval.
CTX
```

### 3. Start the Indexer (Watches for Changes)
```bash
# In one terminal window
python /path/to/ctx-vault/indexer.py --vault ~/my-ctx-vault --db ~/my-ctx-vault/vault.db
```

### 4. Start the API Server
```bash
# In another terminal window
cd /path/to/ctx-vault
CTX_VAULT_ROOT=~/my-ctx-vault uvicorn api:app --host 0.0.0.0 --port 8000
```

## Using with Hermes Agent

Hermes can automatically discover and use ctx-vault through its native MCP (Model Context Protocol) support:

### Hermes Configuration
Add to your `~/.hermes/config.yaml`:
```yaml
mcp:
  servers:
    ctx-vault:
      command: "python"
      args: ["/path/to/ctx-vault/api.py"]
      env:
        CTX_VAULT_ROOT: "/home/yourname/my-ctx-vault"
```

### Using in Hermes Conversations
Once configured, you can ask:
- "Search my knowledge base for information about [topic]"
- "What do I have saved about [subject]?"
- "Show me the contents of [specific.ctx file]"

## Using with Claude Code

Claude Code can access ctx-vault through MCP or direct API calls:

### MCP Configuration for Claude Code
Add to your Claude Code settings:
```json
{
  "mcpServers": {
    "ctx-vault": {
      "command": "python",
      "args": ["/path/to/ctx-vault/api.py"],
      "env": {
        "CTX_VAULT_ROOT": "/home/yourname/my-ctx-vault"
      }
    }
  }
}
```

### Direct API Usage
You can also make HTTP requests directly:
```bash
# Search your knowledge base
curl "http://localhost:8000/search?q=your+search+term&limit=5"

# Get a specific file
curl "http://localhost:8000/vault/notes/welcome.ctx"

# Get statistics
curl "http://localhost:8000/stats"
```

## Performance Verification

To verify the 10+ performance improvement:

```bash
# Run the benchmark script
cd /path/to/ctx-vault
python benchmark.py

# Expected output:
# .ctx avg latency per query: ~1ms
# Markdown avg latency per query: ~30ms
# Latency improvement: 30+
```

## Managing Files with Your Agent

Your AI agent can now:
1. **Create new knowledge**: Ask your agent to create/update .ctx files
2. **Search knowledge**: Have your agent search your vault for relevant context
3. **Link related information**: Your agent can create bidirectional links between .ctx files
4. **Extract insights**: Your agent can analyze patterns across your knowledge base
5. **Maintain consistency**: The indexer automatically updates when files change

## Example Agent Workflow

1. You ask: "What did we decide about the API design last week?"
2. Your agent searches ctx-vault: finds `notes/api-design.ctx` and `notes/meeting-2026-08-10.ctx`
3. Agent retrieves relevant chunks: shows the "API Design Decisions" chunk from the meeting note
4. Agent synthesizes answer: "Based on our meeting notes, we decided to use REST API with JSON payloads..."
5. Agent can optionally create a new .ctx file summarizing this conversation for future reference

## Next Steps

- Read [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) for enterprise deployment options
- Check [SPEC.md](SPEC.md) for the complete .ctx file format specification
- Look at [migrate_md_to_ctx.py](migrate_md_to_ctx.py) to convert your existing Markdown files
- Join the community: Star the repo, open issues, contribute improvements!

---

**Remember**: ctx-vault gets smarter as your knowledge base grows. The more you use it with your agent, the more valuable it becomes as a shared, persistent context that enhances your AI-assisted workflow.