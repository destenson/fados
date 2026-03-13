# FADOS — Filesystem As Database Overlay

FADOS indexes the current working directory into a disposable SQLite database, enabling
full-text, metadata, and semantic (vector) search over files — without moving or modifying them.
The index is always rebuildable from the source tree.

Indexing happens automatically on first run. The index is stored at `<path>/.fados/index.db`,
colocated with the data. Query commands discover the index by walking up from CWD (git-style).
Use `--dir <path>` to target a specific directory, or `--user` to use `~/.fados/` instead.
These flags go **before** the command: `uv run scripts/fados.py --dir /some/path search foo`.

## When to invoke

- Finding where a function, class, type, or constant is defined
- Finding implementation code that uses a term (excluding tests and docs)
- Finding documentation that references a term
- Finding test code that references a term
- Searching a local document or knowledge base by content, topic, or meaning
- Discovering files related to a topic when you don't know the exact filename/path
- Annotating files with tags or metadata for later retrieval
- Querying files by structured attributes (date, size, MIME type, EXIF metadata)

Do **not** use FADOS to read or write file content — use standard file tools for that.

---

## Running

All commands are run from the directory you want to search:

```bash
uv run scripts/fados.py <command> [args]
```

The script path is relative to this SKILL.md file.

---

## Commands

### Intent-based search (no index required — uses ripgrep)

| Command | Purpose |
|---------|---------|
| `definition <term> [-n N]` | Find where a term is defined (class, function, type, const, etc.) |
| `implementation <term> [-n N]` | Find usage in code (excludes tests and docs) |
| `documentation <term> [-n N]` | Find references in docs (markdown, rst, etc.) |
| `tests <term> [-n N]` | Find references in test files |

These are the fastest commands — they need no index, just `rg` installed.

### Index-based search

| Command | Purpose |
|---------|---------|
| `search <terms>` | Full-text keyword search (FTS5) |
| `semantic <query> [-n N]` | Conceptual/meaning-based search via embeddings (default n=20) |
| `similar <path> [-n N]` | Find files with similar content to a given file (default n=10) |
| `query <sql>` | SQL query against the index (see schema below) |
| `find <key> <value>` | Search extracted/EXIF metadata by key+value |

### File info and annotation

| Command | Purpose |
|---------|---------|
| `info <path>` | Show all indexed data for a file (metadata, tags, MIME, etc.) |
| `tag <path> <tag>` | Add a user tag to a file |
| `annotate <path> <key> <value>` | Add arbitrary metadata to a file |

### Indexing (usually automatic)

| Command | Purpose |
|---------|---------|
| `reindex [path] [--embed]` | Force full reindex (path overrides --dir) |
| `embed [path]` | Generate/refresh semantic embeddings for indexed content |

---

## Search strategy guide

| Goal | Best command |
|------|-------------|
| Find where something is defined (class, function, type) | `definition` |
| Find code that uses/calls something (not tests or docs) | `implementation` |
| Find documentation about something | `documentation` |
| Find test code for something | `tests` |
| Find files containing specific keywords or tokens | `search` |
| Find files about a concept (natural language) | `semantic` |
| Find more files like a specific file | `similar` |
| Filter by path pattern, date, size, MIME type | `query` (SQL) |
| Find files by EXIF or extracted document metadata | `find` |

---

## Query schema

The `query` command accepts SQL against these tables:

**files** — one row per indexed file
| Column | Type | Description |
|--------|------|-------------|
| path | TEXT (PK) | Absolute file path |
| mtime | REAL | Last modified timestamp (Unix epoch) |
| size | INTEGER | File size in bytes |
| mime | TEXT | MIME type |
| checksum | TEXT | BLAKE2b content hash |
| indexed_at | REAL | When this file was last indexed |

**content** — FTS5 virtual table for full-text search
| Column | Description |
|--------|-------------|
| path | File path (unindexed, for joining) |
| text | Extracted text content |

Use `text MATCH 'term'` for FTS5 queries, `snippet(content, 1, '[', ']', '...', 20)` for
highlighted excerpts.

**metadata** — key-value pairs extracted from files or added by users
| Column | Description |
|--------|-------------|
| path | File path |
| key | Metadata key (e.g. `Author`, `camera_model`) |
| value | Metadata value |
| source | Origin: `exif`, `extracted`, `user` |

**tags** — labels attached to files
| Column | Description |
|--------|-------------|
| path | File path |
| tag | Tag string |
| source | Origin (default: `user`) |

---

## Output format

All commands return newline-delimited JSON. Every result includes `path`.

```jsonc
// definition / implementation / documentation / tests — includes line number and matched text
{"path": "src/model.py", "line": 42, "match": "class ModelConfig:"}

// search — includes snippet with context
{"path": "notes/ml.md", "snippet": "...the [gradient descent] optimizer..."}

// semantic / similar — includes similarity score [0.0–1.0]
{"path": "papers/attention.pdf", "score": 0.8821}

// query — returns selected columns
{"path": "README.md", "mime": "text/markdown", "size": 4096}

// info — full detail for one file
// (pretty-printed JSON with "file", "metadata", "tags" keys)
```

---

## Examples

```bash
# Find where a class/function/type is defined
uv run scripts/fados.py definition ModelConfig

# Find implementation code using a term (excludes tests and docs)
uv run scripts/fados.py implementation ModelConfig -n 30

# Find documentation about a term
uv run scripts/fados.py documentation ModelConfig

# Find test code referencing a term
uv run scripts/fados.py tests ModelConfig

# Keyword search
uv run scripts/fados.py search "gradient descent fine-tuning"

# Semantic / conceptual search
uv run scripts/fados.py semantic "techniques for reducing model hallucination" -n 10

# Find files similar to a reference file
uv run scripts/fados.py similar papers/attention.pdf -n 5

# Markdown files modified in the last 30 days mentioning LoRA
uv run scripts/fados.py query "SELECT f.path FROM files f
  JOIN content c ON c.path = f.path
  WHERE f.mime = 'text/markdown'
    AND f.mtime > strftime('%s','now','-30 days')
    AND c.text MATCH 'LoRA'"

# Find by EXIF/extracted metadata
uv run scripts/fados.py find Author "Vaswani"

# Tag and annotate files
uv run scripts/fados.py tag papers/attention.pdf seminal
uv run scripts/fados.py annotate papers/attention.pdf topic "self-attention transformers"
```

---

## Notes

- Source files are **never modified**
- `semantic` and `similar` require embeddings — use `--embed` on first index or run `embed` separately
- `reindex` forces a full rebuild; normal indexing is incremental
- `embed` is idempotent — safe to re-run after adding new files
