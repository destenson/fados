---
description: >
  Intent-based search (find where something is defined, its implementation, its docs, or its
  tests — not just every mention), semantic/conceptual search by meaning, find files similar
  to a reference file, query file metadata (dates, sizes, MIME types, EXIF), tag/annotate
  files for later retrieval, or run SQL over a file tree. Useful for conceptual queries, document collections, research papers,
  images, or structured file metadata.
---

# FADOS — Filesystem As Database Overlay

FADOS is a lightweight queryable overlay on a filesystem. It indexes a
directory into a disposable SQLite database, enabling full-text,
metadata, and semantic (vector) search over files — without moving,
modifying, or duplicating them. The DB never stores file contents: it
holds only pointers `(path, byte_offset, byte_length)` back into the
source tree, and snippets are re-read from disk at query time. The
index is always rebuildable from the source tree.

Indexing happens automatically on first run. The index is stored at `<path>/.fados/index.db`,
colocated with the data. Query commands discover the index by walking up from CWD (git-style).
Use `--dir <path>` to target a specific directory, or `--user` to use `~/.fados/` instead.
These flags go **before** the command: `uv run scripts/fados.py --dir /some/path search foo`.

## When to invoke

Use FADOS when you need more than raw text matching:

- **Intent-based search** — find where something is *defined*, its *implementation*, its *documentation*, or its *tests* — not every file that mentions the term. Grep gives you all matches; FADOS gives you the right ones.
- **Conceptual/semantic search** — finding files about a topic using natural language, when you don't know the exact keywords ("techniques for reducing hallucination")
- **Similar-file discovery** — finding files with similar content to a reference file
- **File metadata queries** — filtering by modification date, size, MIME type, or combinations via SQL
- **EXIF/document metadata** — searching by author, camera model, or other extracted properties
- **Tagging and annotation** — labeling files with persistent tags or key-value metadata for later retrieval
- **Document collections** — searching research papers, notes, PDFs, images, or other non-code files where Grep is weak

Do **not** use FADOS to read or write file content — use standard file tools for that.

---

## Running

```bash
uv run scripts/fados.py <command> [args]
```

The script path is relative to this SKILL.md file.

---

## Commands

### Semantic and index-based search

| Command | Purpose |
|---------|---------|
| `semantic <query> [-n N]` | Conceptual/meaning-based search via embeddings (default n=20) |
| `similar <path> [-n N]` | Find files with similar content to a given file (default n=10) |
| `search <terms>` | Full-text keyword search (FTS5) |
| `query <sql>` | SQL query against the index (see schema below) |
| `find <key> <value>` | Search extracted/EXIF metadata by key+value |

### File info and annotation

| Command | Purpose |
|---------|---------|
| `info <path>` | Show all indexed data for a file (metadata, tags, MIME, etc.) |
| `tag <path> <tag>` | Add a user tag to a file |
| `annotate <path> <key> <value>` | Add arbitrary metadata to a file |

### Code search (ripgrep wrappers — no index required)

| Command | Purpose |
|---------|---------|
| `definition <term> [-n N]` | Find where a term is defined (class, function, type, const, etc.) |
| `implementation <term> [-n N]` | Find usage in code (excludes tests and docs) |
| `documentation <term> [-n N]` | Find references in docs (markdown, rst, etc.) |
| `tests <term> [-n N]` | Find references in test files |

### Indexing (usually automatic)

| Command | Purpose |
|---------|---------|
| `reindex [path] [--embed]` | Force full reindex (path overrides --dir) |
| `embed [path]` | Generate/refresh semantic embeddings for indexed content |

---

## Search strategy guide

| Goal | Best command |
|------|-------------|
| Find files about a concept (natural language) | `semantic` |
| Find more files like a specific file | `similar` |
| Find files containing specific keywords or tokens | `search` |
| Filter by path pattern, date, size, MIME type | `query` (SQL) |
| Find files by EXIF or extracted document metadata | `find` |
| Find where something is defined (class, function, type) | `definition` |
| Find code that uses/calls something (not tests or docs) | `implementation` |
| Find documentation about something | `documentation` |
| Find test code for something | `tests` |

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

**chunks** — one row per indexed fragment. `id` matches `content.rowid`
and `embeddings.chunk_id`.
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER (PK) | Stable chunk id |
| path | TEXT | File this chunk came from |
| chunk_index | INTEGER | Ordinal within the file |
| byte_offset | INTEGER | Start byte in the source file |
| byte_length | INTEGER | Length in bytes |

**content** — contentless FTS5 virtual table. Holds only the inverted
index; there is no `text` column to select. Query via `content MATCH 'term'`
and JOIN `chunks ON chunks.id = content.rowid` to recover the location;
re-read the bytes from disk to render a snippet. `snippet()` is not
available on contentless tables.

**embeddings** — one vector per chunk.
| Column | Type | Description |
|--------|------|-------------|
| chunk_id | INTEGER (PK) | Matches `chunks.id` |
| vector | BLOB | Normalized float32 embedding |

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
// search — FTS hit with chunk location and a snippet re-read from disk
{"path": "notes/ml.md", "byte_offset": 4096, "byte_length": 4096,
 "snippet": "...the [gradient descent] optimizer...", "rank": -5.21}

// semantic — chunk-level cosine hit, deduped to best chunk per file
{"path": "papers/attention.pdf", "byte_offset": 0, "byte_length": 12288,
 "snippet": "...self-[attention] layers...", "score": 0.8821}

// similar — no snippet, just the top chunk's location + score
{"path": "papers/attention.pdf", "byte_offset": 0, "byte_length": 12288, "score": 0.78}

// definition / implementation / documentation / tests — line + matched text
{"path": "src/model.py", "line": 42, "match": "class ModelConfig:"}

// query — returns selected columns
{"path": "README.md", "mime": "text/markdown", "size": 4096}

// info — full detail for one file, including chunk and embedding counts
// (pretty-printed JSON with "file", "metadata", "tags", "chunks" keys)
```

---

## Examples

```bash
# Semantic / conceptual search — finds files by meaning, not keywords
uv run scripts/fados.py semantic "techniques for reducing model hallucination" -n 10

# Find files similar to a reference file
uv run scripts/fados.py similar papers/attention.pdf -n 5

# Keyword search (when you need FTS5 ranking, not just grep matches)
uv run scripts/fados.py search "gradient descent fine-tuning"

# Markdown files modified in the last 30 days mentioning LoRA
uv run scripts/fados.py query "SELECT DISTINCT f.path FROM files f
  JOIN chunks ch ON ch.path = f.path
  JOIN content co ON co.rowid = ch.id
  WHERE f.mime = 'text/markdown'
    AND f.mtime > strftime('%s','now','-30 days')
    AND content MATCH 'LoRA'"

# Find by EXIF/extracted metadata
uv run scripts/fados.py find Author "Vaswani"

# Tag and annotate files
uv run scripts/fados.py tag papers/attention.pdf seminal
uv run scripts/fados.py annotate papers/attention.pdf topic "self-attention transformers"

# Code search (ripgrep wrappers — also available via Grep directly)
uv run scripts/fados.py definition ModelConfig
uv run scripts/fados.py implementation ModelConfig -n 30
```

---

## Notes

- Source files are **never modified**
- `semantic` and `similar` require embeddings — use `--embed` on first index or run `embed` separately
- `reindex` forces a full rebuild; normal indexing is incremental
- `embed` is idempotent — safe to re-run after adding new files
