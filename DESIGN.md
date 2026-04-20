# FADOS: Filesystem Database Overlay

## Vision

A query layer that treats an existing directory tree as a database — imposing structure without
owning the data. The filesystem is ground truth. The index is disposable and always rebuildable.

---

## Core Principles

1. **Non-destructive** — no files are moved, renamed, or modified. Metadata lives in sidecars or
   a shadow tree.
2. **Reconstructible** — the index can be blown away and rebuilt entirely from the source tree.
3. **Transparent** — the tree remains usable by all other tools. FADOS is a lens, not a lock-in.
4. **Agent-friendly** — query interface designed for LLM agent consumption (structured output,
   path-addressable results, tool-call ergonomics).

---

## Architecture

```
Source Tree (read-only, source of truth)
    /projects/
    /documents/
    /photos/
        │
        ▼
  Content Extraction Layer
    tika / exiftool / pdftotext / pandoc / ffprobe
        │
        ▼
  Index (SQLite, local — no file contents, only pointers)
    files(path, mtime, size, mime, checksum)
    chunks(id, path, chunk_index, byte_offset, byte_length)
    content USING fts5(text, content='')     ← contentless FTS5
    embeddings(chunk_id, vector)             ← per-chunk vectors
    metadata(path, key, value)               ← extracted + user
    tags(path, tag)
        │
        ▼
  Query Layer
    FTS MATCH → chunk rowid → re-read bytes from disk for snippet
    cosine over embeddings → chunk rowid → source file
    rg / find for predicate pushdown
        │
        ▼
  Result Interface
    CLI / Python API / HTTP (agent tool calls)
```

The index never holds file contents. It stores the inverted FTS index,
per-chunk embedding vectors, and `(path, byte_offset, byte_length)`
pointers back into the source tree. Snippet rendering re-reads the
relevant byte range from disk at query time.

---

## Index Schema

```sql
CREATE TABLE files (
    path        TEXT PRIMARY KEY,
    mtime       REAL,
    size        INTEGER,
    mime        TEXT,
    checksum    TEXT,
    indexed_at  REAL
);

-- Every indexed fragment of a file. For text/* files byte_offset and
-- byte_length refer to the file's own bytes so snippets can be re-read
-- directly; for PDF/office/etc. the byte range spans the whole source
-- file and chunk_index distinguishes fragments.
CREATE TABLE chunks (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    byte_length INTEGER NOT NULL,
    UNIQUE (path, chunk_index)
);

-- Contentless FTS5: the inverted index only — no stored text. rowid is
-- the matching chunks.id, so matches resolve to a byte range in the
-- source file on disk.
CREATE VIRTUAL TABLE content USING fts5(
    text, content='', contentless_delete=1
);

-- One vector per chunk (not per file). chunk_id = chunks.id = content rowid.
CREATE TABLE embeddings (
    chunk_id INTEGER PRIMARY KEY,
    vector   BLOB
);

-- Arbitrary key-value metadata (extracted + user-supplied)
-- source: 'extracted' | 'exif' | 'user' | 'inferred'
CREATE TABLE metadata (
    path    TEXT,
    key     TEXT,
    value   TEXT,
    source  TEXT,
    PRIMARY KEY (path, key, source)
);

CREATE TABLE tags (
    path    TEXT,
    tag     TEXT,
    source  TEXT,
    PRIMARY KEY (path, tag)
);
```

### Chunking

Text files are split into ~4 KiB windows with 800-byte overlap, snapping
each window edge to a UTF-8 code-point boundary. Each window becomes one
`chunks` row, one contentless FTS5 document, and (once `embed` runs) one
embedding vector. The overlap keeps concepts that straddle a boundary
visible to semantic search.

---

## Query Language

A SQL-compatible query interface over the index tables, plus integration with external tools:
- SQLite queries (content, metadata, tags)
- `rg` (ripgrep) for exact phrase/literal matching and fast single-pass content search
- `find` for path-based predicate pushdown

### Examples

```sql
-- Path-native predicates (no file reads needed)
SELECT * FROM files WHERE path LIKE '%/reality-floats/%' AND mime = 'application/pdf'

-- Full-text search
SELECT path FROM content WHERE text MATCH 'gradient descent'

-- Combined
SELECT f.path FROM files f
  JOIN content c ON c.path = f.path
  WHERE f.path LIKE '%/projects/%'
    AND f.mime IN ('text/markdown', 'text/plain')
    AND c.text MATCH 'LoRA'
    AND f.mtime > strftime('%s', 'now', '-30 days')

-- Metadata / EXIF
SELECT path FROM metadata WHERE key = 'camera_model' AND value LIKE '%Sony%'

-- Tags
SELECT path FROM tags WHERE tag = 'needs-review'
```

---

## Query Planner

Predicate classification determines execution strategy:

| Predicate type          | Strategy                          |
|------------------------|-----------------------------------|
| Path pattern           | `find` with `-path`, or SQLite LIKE|
| File metadata (mtime, size, mime) | SQLite `files` table     |
| Content search         | FTS5 or `rg` for raw speed        |
| EXIF / extracted meta  | SQLite `metadata` table           |
| User tags              | SQLite `tags` table               |
| Cross-cutting (JOIN)   | SQLite query over materialized data|

For large trees, `rg` is faster than FTS5 for single-pass content search. The planner chooses
based on result set size and index freshness.

---

## Indexing Pipeline

### Triggers
- **Automatic**: on first run in a directory, FADOS indexes the CWD. If the tree is large
  (many thousands of files/directories), it prints a warning explaining why indexing will be
  slow and exits with instructions on how to force it.
- Manual: `fados reindex` forces a full rebuild of the CWD.
- Incremental: `inotifywait` watches the tree, queues changed paths (user-managed, not
  agent-facing).
- Scheduled: cron-based full scan to catch missed events.

### Per-file pipeline

```
file detected / changed
    │
    ├─ stat() → files table (mtime, size, mime, checksum)
    ├─ mime detection (python-magic)
    ├─ content extraction (dispatcher by mime)
    │     text/*          → read bytes directly
    │     application/pdf → pdftotext
    │     image/*         → exiftool + optional OCR
    │     office docs     → pandoc / tika
    │     video/*         → ffprobe for metadata
    │     code            → read bytes directly, language tagged
    │
    ├─ chunk → (chunk_index, byte_offset, byte_length, text)
    │     chunks row for each window
    │     text tokens → contentless FTS5 (rowid = chunks.id)
    │     vector      → embeddings (once `embed` runs)
    │
    └─ extracted metadata → metadata table
```

### Content Extractors (pluggable)

```python
EXTRACTORS = {
    'application/pdf':    extract_pdf,      # pdftotext
    'image/*':            extract_image,    # exiftool + optional tesseract
    'application/vnd.*':  extract_office,   # pandoc
    'text/*':             extract_text,     # direct read
    'video/*':            extract_video,    # ffprobe
    'audio/*':            extract_audio,    # ffprobe + optional whisper
}
```

Extractors are registered plugins — adding new mime types doesn't touch core.

---

## Metadata Sidecar Strategy

Two options, non-exclusive:

**Option A: Shadow tree**
```
~/.fados/meta/
    projects/
        my-project/
            README.md.meta.json
```

**Option B: Inline sidecar**
```
/projects/my-project/
    README.md
    .README.md.fados.json    ← hidden, travels with the file
```

Shadow tree is cleaner (source tree untouched). Inline sidecars survive copies/moves.
Both supported; shadow tree is default.

---

## Agent Interface

FADOS is designed to be used as a **Claude Code skill**. A `SKILL.md` file describes when,
why, and how to invoke the CLI — agents read the skill file and shell out to the script
directly via `uv run scripts/fados.py <command>`.

All commands return newline-delimited JSON with `path` in every result record, so agents can
reference files directly.

```
fados query <sql>               → [{path, mime, mtime, ...}]
fados info <path>               → {file, metadata, tags, chunks}
fados tag <path> <tag>          → (silent on success)
fados annotate <path> <k> <v>   → (silent on success)
fados search <terms> [-n N]     → [{path, byte_offset, byte_length, snippet, rank}]
fados semantic <query> [-n N]   → [{path, byte_offset, byte_length, snippet, score}]
fados similar <path> [-n N]     → [{path, byte_offset, byte_length, score}]
```

> **Future (low priority)**: An MCP server or HTTP API could wrap the CLI for networked or
> tighter protocol-level integration, but the skill + CLI approach covers current use cases.

---

## Schema Inference (for existing trees)

On first index of an unknown tree, a lightweight analysis pass:

1. Sample directory structure up to depth 3
2. Collect mime distribution per subtree
3. Infer "collection types": photos, code projects, documents, etc.
4. Optionally: one LLM call over the sampled structure → named schema with suggested tags

User can confirm, adjust, or ignore. The inferred schema gates no functionality — it only
suggests tagging rules and display hints.

---

## Implementation Phases

### Phase 1 — Core index
- `fados reindex <path>` builds SQLite index
- Basic SQL query interface
- File watcher for incremental updates

### Phase 2 — Content extraction
- PDF, image (EXIF), office docs, plain text
- FTS5 full-text search
- `rg`-based predicate pushdown

### Phase 3 — Semantic layer
- Embedding extraction on indexed content
- `fados_similar` via sqlite-vec

### Phase 4 — Intent-based search ✓
- `definition <term>` — find where something is defined (classes, functions, constants, types)
- `implementation <term>` — find where something is used/implemented (excludes tests and docs)
- `documentation <term>` — search within documentation files (markdown, rst, etc.)
- `tests <term>` — search within test files

Uses ripgrep directly (no index required) with language-aware regex patterns for definitions
and glob-based file classification for tests vs docs vs implementation.

### Phase 5 — Agent protocol integration (future, low priority)
- MCP server wrapping the CLI
- HTTP API for remote/networked access

---

## Open Questions

- **Multi-root**: single index spanning multiple trees, or one index per root?
- **Conflict resolution**: when sidecar metadata contradicts extracted metadata, who wins?
- **Large trees**: index memory footprint for 1M+ files — probably need chunked FTS5 or
  separate content DB per subtree.
- **Migrations**: when extraction logic improves, selective re-index without full rebuild.

