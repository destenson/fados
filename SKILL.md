# FADOS — Filesystem As Database Overlay

FADOS indexes a local directory tree into a disposable SQLite database, enabling full-text,
metadata, SQL, and semantic (vector) search over files — without moving or modifying them.
The index is always rebuildable from the source tree.

## When to invoke

- Searching a local document or knowledge base by content, topic, or meaning
- Finding code files that discuss or implement a concept
- Discovering files related to a topic when you don't know the exact filename/path
- Annotating files with tags or metadata for later retrieval
- Querying files by structured attributes (date, size, MIME type, EXIF metadata)

Do **not** use FADOS to read or write file content — use standard file tools for that.

---

## Setup (one-time per collection)

```bash
# Fast index (keyword + metadata search only)
uv run scripts/fados.py index /path/to/collection

# Index + generate semantic embeddings (enables semantic/similar commands; slower)
uv run scripts/fados.py index /path/to/collection --embed

# Add embeddings to an already-indexed tree
uv run scripts/fados.py embed /path/to/collection
```

The `all-MiniLM-L6-v2` embedding model (~80 MB) is downloaded automatically on first use.
Index lives at `~/.fados/index.db` — safe to delete; rebuilt with `reindex`.

---

## Commands

All commands: `uv run scripts/fados.py <command> [args]`

### Indexing

| Command | Purpose |
|---------|---------|
| `index <path> [--embed]` | Index a directory tree |
| `reindex <path> [--embed]` | Force full reindex of a tree |
| `embed <path>` | Generate/refresh semantic embeddings only |
| `watch <path>` | Watch for file changes and auto-reindex (requires `inotify-tools`) |

### Searching

| Command | Purpose |
|---------|---------|
| `search <terms>` | Full-text keyword search (FTS5, fast) |
| `semantic <query> [-n N]` | Semantic/conceptual search via embeddings (default n=20) |
| `similar <path> [-n N]` | Find files with similar content to a given file (default n=10) |
| `query <sql>` | Raw SQL against the index |
| `find <key> <value>` | Search extracted/EXIF metadata by key+value |

### File info and annotation

| Command | Purpose |
|---------|---------|
| `info <path>` | Show all indexed data for a file (metadata, tags, MIME, etc.) |
| `tag <path> <tag>` | Add a user tag to a file |
| `annotate <path> <key> <value>` | Add arbitrary metadata to a file |

---

## Search strategy guide

| Goal | Best command |
|------|-------------|
| Find files containing an exact phrase or identifier | `search` |
| Find files about a concept (natural language) | `semantic` |
| Find more files like a specific file | `similar` |
| Filter by path pattern, date, size, MIME type | `query` (SQL) |
| Find files by EXIF or extracted document metadata | `find` |

---

## Output format

All commands return newline-delimited JSON. Every result includes `path`.

```jsonc
// search — includes snippet with context
{"path": "/home/.../notes/ml.md", "snippet": "...the [gradient descent] optimizer..."}

// semantic / similar — includes similarity score [0.0–1.0]
{"path": "/home/.../papers/attention.pdf", "score": 0.8821}

// query — returns selected columns
{"path": "/home/.../README.md", "mime": "text/markdown", "size": 4096}

// info — full detail for one file
// (pretty-printed JSON with "file", "metadata", "tags" keys)
```

---

## Examples

```bash
FADOS="uv run /home/dennis/src/fados/fados.py"

# Keyword search
$FADOS search "gradient descent fine-tuning"

# Semantic / conceptual search
$FADOS semantic "techniques for reducing model hallucination" -n 10

# Find files similar to a reference file
$FADOS similar /home/dennis/notes/transformers.md -n 5

# Recent markdown files containing "LoRA" (SQL)
$FADOS query "SELECT f.path, snippet(c.content,1,'>>','<<','...',20) AS ctx
  FROM files f JOIN content c ON c.path=f.path
  WHERE f.mime='text/markdown'
    AND f.mtime > strftime('%s','now','-30 days')
    AND c.text MATCH 'LoRA'"

# All PDFs in a subtree
$FADOS query "SELECT path FROM files WHERE path LIKE '/home/dennis/papers/%' AND mime='application/pdf'"

# Find by EXIF/extracted metadata
$FADOS find Author "Vaswani"
$FADOS find camera_model "Sony"

# Tag and annotate files
$FADOS tag /home/dennis/papers/attention.pdf seminal
$FADOS annotate /home/dennis/papers/attention.pdf topic "self-attention transformers"

# Inspect everything FADOS knows about a file
$FADOS info /home/dennis/papers/attention.pdf
```

---

## Useful SQL patterns

```sql
-- Files modified in the last week
SELECT path, mime FROM files WHERE mtime > strftime('%s','now','-7 days') ORDER BY mtime DESC

-- All unique MIME types in a subtree
SELECT DISTINCT mime FROM files WHERE path LIKE '/home/dennis/projects/%'

-- Files that have a specific tag
SELECT f.path FROM files f JOIN tags t ON t.path=f.path WHERE t.tag='needs-review'

-- Files with user annotations
SELECT f.path, m.key, m.value FROM files f
  JOIN metadata m ON m.path=f.path WHERE m.source='user'

-- Cross: recent code files mentioning a function name
SELECT f.path FROM files f JOIN content c ON c.path=f.path
  WHERE f.mime LIKE 'text/%'
    AND f.mtime > strftime('%s','now','-14 days')
    AND c.text MATCH 'my_function_name'
```

---

## Notes

- Source files are **never modified**
- `semantic` and `similar` load all embedding vectors into RAM; fine for tens of thousands of files
- Run `reindex` after large bulk changes; `watch` handles incremental updates
- `embed` is idempotent — safe to re-run after adding new files to an indexed tree
