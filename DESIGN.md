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
Source Tree (read-only)
    /projects/
    /documents/
    /photos/
        │
        ▼
  Content Extraction Layer
    tika / exiftool / pdftotext / pandoc / ffprobe
        │
        ▼
  Index (SQLite, local)
    files(path, mtime, size, mime, checksum)
    content(path, text)           ← FTS5
    metadata(path, key, value)    ← extracted + user-supplied
    tags(path, tag)
        │
        ▼
  Query Layer
    SQL dialect + filesystem-native shortcuts
    rg / find for predicate pushdown
        │
        ▼
  Result Interface
    CLI / Python API / HTTP (agent tool calls)
```

---

## Index Schema

```sql
CREATE TABLE files (
    path        TEXT PRIMARY KEY,
    mtime       INTEGER,
    size        INTEGER,
    mime        TEXT,
    checksum    TEXT,
    indexed_at  INTEGER
);

-- Full-text search over extracted content
CREATE VIRTUAL TABLE content USING fts5(path, text);

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
    ├─ stat() → files table (mtime, size)
    ├─ mime detection (python-magic)
    ├─ content extraction (dispatcher by mime)
    │     text/*          → read directly
    │     application/pdf → pdftotext
    │     image/*         → exiftool + optional OCR
    │     office docs     → pandoc / tika
    │     video/*         → ffprobe for metadata
    │     code            → read directly, language tagged
    │
    ├─ content → FTS5
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
fados query <sql>               → [{path, mime, mtime, snippet, ...}]
fados info <path>               → {file, metadata, tags}
fados tag <path> <tag>          → (silent on success)
fados annotate <path> <k> <v>   → (silent on success)
fados search <terms>            → [{path, snippet}]
fados semantic <query> [-n N]   → [{path, score}]
fados similar <path> [-n N]     → [{path, score}]
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

### Phase 4 — Intent-based search
- `search definition <term>` — find where something is defined (classes, functions, constants)
- `search implementation <term>` — find where something is used/implemented
- `search documentation <term>` — search within documentation content (docstrings, comments,
  markdown, READMEs), not by file location
- `search tests <term>` — search within test content (assertions, test functions, fixtures)

Strategy: leverage ripgrep's `--type` filters to restrict to source code file types, combined
with language-aware regex patterns (e.g. `def <term>`, `class <term>`, `fn <term>`,
`func <term>`) for definitions. For documentation vs tests vs implementation, classify by
content patterns rather than file paths — docstrings and comment blocks for documentation,
test assertions and test function signatures for tests, everything else for implementation.

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

