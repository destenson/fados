# Filesystem-as-database Overlay System (fados)

This is a simple proof-of-concept script that builds a lightweight
queryable overlay on top of a filesystem, enabling full-text, metadata,
and semantic (vector) search over files — without moving, modifying, or
duplicating them. The index holds only pointers (path + byte offset +
length) back into the source tree; snippets are re-read from disk at
query time. The index is always rebuildable from the source tree.

## Core Principles

- **Non-destructive** — no files are moved, renamed, or modified.
- **Reconstructible** — the index can be blown away and rebuilt at any time.
- **Transparent** — the source tree remains usable by all other tools; FADOS is
  a lens, not a lock-in.
- **Agent-friendly** — all commands output newline-delimited JSON with `path` in
  every record.

## Requirements

**Python dependencies** (managed automatically by `uv`):

- `click`
- `python-magic`

**Optional** (for semantic search):

- `sentence-transformers`
- `numpy`

**Optional external tools** (installed separately):

| Tool            | Used for                        |
| --------------- | ------------------------------- |
| `exiftool`      | EXIF / file metadata extraction          |
| `pdftotext`     | PDF text extraction                      |
| `pandoc`        | Word/ODF document extraction             |
| `ripgrep` (`rg`)| Intent-based search (definition, etc.)   |
| `inotify-tools` | `watch` command (Linux only)             |

## Installation

No installation needed. Run directly with
[`uv`](https://github.com/astral-sh/uv):

```sh
uv run scripts/fados.py <command> [args]
```

`uv` will install Python dependencies automatically on first run. The index is
stored at `<indexed-path>/.fados/index.db` by default (colocated with the data).
Use `--user` to store the index at `~/.fados/index.db` instead.

## Commands

```
Global flags (before any command):
  --user           Use ~/.fados/ instead of local .fados/ in the indexed tree.
  --dir <path>     Target directory (uses <dir>/.fados/ for the index). Applies to all commands.

Index-based commands:
  reindex [path] [--embed]  Force a full rebuild. Path overrides --dir. Auto-indexes on first run.
  embed [path]              Generate/refresh semantic embeddings for indexed content.
  query <sql>               Raw SQL against the index database.
  search <terms>            Full-text keyword search (FTS5).
  semantic <query> [-n N]   Semantic/conceptual search using embeddings (default: top 20).
  similar <file> [-n N]     Find files with similar content (default: top 10).
  find <key> <value>        Search metadata by key/value.

Intent-based search (uses ripgrep, no index required):
  definition <term> [-n N]      Find where a term is defined (class, function, type, etc.).
  implementation <term> [-n N]  Find usage in code (excludes tests and docs).
  documentation <term> [-n N]   Find references in docs (markdown, rst, etc.).
  tests <term> [-n N]           Find references in test files.

File info and annotation:
  tag <file> <tag>          Add a user tag to a file.
  annotate <file> <k> <v>   Add arbitrary user metadata to a file.
  info <file>               Show all indexed data for a file.
  watch [path]              Watch for changes and reindex incrementally (requires inotify-tools).
```

## Examples

```sh
# Full-text search (auto-indexes CWD on first run)
uv run scripts/fados.py search gradient descent

# Semantic / conceptual search
uv run scripts/fados.py semantic "attention mechanism transformers" -n 5

# Find files similar to a given file
uv run scripts/fados.py similar /path/to/paper.pdf -n 10

# Raw SQL query
uv run scripts/fados.py query "SELECT path, mime FROM files WHERE mime = 'application/pdf'"

# Search by extracted metadata
uv run scripts/fados.py find camera_model Sony

# Tag and annotate files
uv run scripts/fados.py tag /path/to/file.md needs-review
uv run scripts/fados.py annotate /path/to/file.md project alpha

# Find where a function/class/type is defined
uv run scripts/fados.py definition MyClass

# Find usage of a term in implementation code (not tests or docs)
uv run scripts/fados.py implementation MyClass -n 30

# Find references in documentation
uv run scripts/fados.py documentation MyClass

# Find references in test code
uv run scripts/fados.py tests MyClass

# Inspect everything known about a file
uv run scripts/fados.py info /path/to/file.md

# Watch a directory and reindex on changes
uv run scripts/fados.py watch .
```

## Index Schema

The SQLite index lives at `<indexed-path>/.fados/index.db` (or `~/.fados/index.db`
with `--user`) and contains:

- `files` — path, mtime, size, MIME type, checksum
- `chunks` — `(path, chunk_index, byte_offset, byte_length)` pointers to
  every indexed fragment back in the source file
- `content` — **contentless** FTS5 inverted index (no stored text).
  `content.rowid = chunks.id`, so matches resolve to a byte range on disk
- `embeddings` — one vector per chunk, keyed by `chunks.id`
- `metadata` — arbitrary key/value pairs (EXIF, user-supplied, inferred)
- `tags` — user and inferred tags

The index never stores file contents. Snippets are re-read from the
source file at query time using the chunk's byte range. The index is a
cache — delete it and run `reindex` to start fresh.

## Index Location

By default, `fados reindex <path>` creates `.fados/index.db` inside the indexed
directory. Query commands (`search`, `semantic`, etc.) discover the index by
walking up from CWD, git-style — stopping before `~/`.

Use `--user` on any command to use `~/.fados/` instead.

## Notes

- Semantic search requires embeddings to be generated first (`--embed` flag or
  `embed` command).
- Ignored directories: `.git`, `.fados`, `__pycache__`, `node_modules`, `.venv`.
- All commands write results as newline-delimited JSON to stdout, suitable for
  piping or agent consumption.
