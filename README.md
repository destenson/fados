# Filesystem-as-database Overlay System (fados)

This is a simple proof-of-concept script that indexes the current working
directory into a disposable SQLite database, enabling full-text, metadata, and
semantic (vector) search over files — without moving or modifying them. The
index is always rebuildable from the source tree.

## Core Principles

- **Non-destructive** — no files are moved, renamed, or modified.
- **Reconstructible** — the index can be blown away and rebuilt at any time.
- **Transparent** — the source tree remains usable by all other tools; FADOS is
  a lens, not a lock-in.
- **Agent-friendly** — all commands output newline-delimited JSON with `path` in
  every record.

## Requirements

**Python dependencies** (managed automatically by `uv`):

- `python-magic`
- `rich`
- `sentence-transformers`
- `numpy`

**Optional external tools** (installed separately):

| Tool            | Used for                        |
| --------------- | ------------------------------- |
| `exiftool`      | EXIF / file metadata extraction |
| `pdftotext`     | PDF text extraction             |
| `pandoc`        | Word/ODF document extraction    |
| `inotify-tools` | `watch` command (Linux only)    |

## Installation

No installation needed. Run directly with
[`uv`](https://github.com/astral-sh/uv):

```sh
uv run scripts/fados.py <command> [args]
```

`uv` will install Python dependencies automatically on first run. The index is
stored at `~/.fados/index.db`.

## Commands

```
index <path> [--embed]    Index a directory tree. --embed also generates vector embeddings.
reindex <path> [--embed]  Force a full rebuild of the index.
embed <path>              Generate/refresh semantic embeddings for already-indexed content.
query <sql>               Raw SQL against the index database.
search <terms>            Full-text keyword search (FTS5).
semantic <query> [-n N]   Semantic/conceptual search using embeddings (default: top 20).
similar <path> [-n N]     Find files with similar content (default: top 10).
find <key> <value>        Search metadata by key/value.
tag <path> <tag>          Add a user tag to a file.
annotate <path> <k> <v>   Add arbitrary user metadata to a file.
info <path>               Show all indexed data for a file.
watch <path>              Watch for changes and reindex incrementally (requires inotify-tools).
```

## Examples

```sh
# Index the current directory
uv run scripts/fados.py index .

# Index and also build semantic embeddings in one pass
uv run scripts/fados.py index . --embed

# Full-text search
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

# Inspect everything known about a file
uv run scripts/fados.py info /path/to/file.md

# Watch a directory and reindex on changes
uv run scripts/fados.py watch .
```

## Index Schema

The SQLite index lives at `~/.fados/index.db` and contains:

- `files` — path, mtime, size, MIME type, checksum
- `content` — FTS5 full-text index of extracted file content
- `metadata` — arbitrary key/value pairs (EXIF, user-supplied, inferred)
- `tags` — user and inferred tags
- `embeddings` — binary sentence-transformer vectors for semantic search

The index is a cache. Delete it and run `index` again to start fresh.

## Notes

- Semantic search requires embeddings to be generated first (`--embed` flag or
  `embed` command).
- Ignored directories: `.git`, `.fados`, `__pycache__`, `node_modules`, `.venv`.
- All commands write results as newline-delimited JSON to stdout, suitable for
  piping or agent consumption.
