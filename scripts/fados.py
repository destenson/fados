#!/usr/bin/env python3
"""
FADOS - Filesystem As Database Overlay System
Single-file prototype. Run with: uv run scripts/fados.py <command> [args]

Dependencies come from pyproject.toml. Semantic commands (embed,
semantic, similar) additionally need the `semantic` extra:
    uv sync --extra semantic

Auto-indexes CWD on first run. Index stored in <path>/.fados/index.db.
Use --user for ~/.fados/ instead.

Commands:
  reindex [--embed]         force full reindex
  embed                     generate/refresh semantic embeddings for indexed content
  query <sql>               raw SQL against the index
  search <terms>            full-text keyword search (FTS5)
  semantic <query> [-n N]   semantic/conceptual search using embeddings (default n=20)
  similar <path> [-n N]     find files with similar content (default n=10)
  find <key> <value>        search metadata
  definition <term> [-n N]  find where a term is defined (class, function, const, etc.)
  implementation <term>     find usage of a term in code (excludes tests and docs)
  documentation <term>      find references in docs (markdown, rst, etc.)
  tests <term>              find references in test files
  tag <path> <tag>          add a user tag
  annotate <path> <k> <v>   add user metadata
  info <path>               show all indexed info for a file
  watch                     watch for changes and reindex (requires inotify-tools)
"""

import sys
import os
import re
import sqlite3
import subprocess
import json
import time
import mimetypes
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import click

# --- Config ---

# Directories that never contain anything worth indexing and never should
# even when the user opts into hidden files. VCS internals, bytecode
# caches, test-runner caches — all derivable from source, none legible.
HARD_IGNORE_DIRS = {
    ".git", ".fados", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
}

# Dependency / build-output dirs. Skipped by default because they bloat
# the index with generated or third-party code, but occasionally the
# library source inside is exactly what the user wants to search — so
# `--deps` on reindex opts them in.
DEP_DIRS = {
    "node_modules", "target", ".venv", "venv",
    "dist", "build", ".next", ".nuxt",
}

# Legacy name retained for the ripgrep wrappers: they always exclude
# both sets since interactive code search rarely wants either.
IGNORE_DIRS = HARD_IGNORE_DIRS | DEP_DIRS

USER_FADOS_DIR = Path.home() / ".fados"


def _should_ignore(rel_parts: tuple, include_hidden: bool,
                   include_deps: bool) -> bool:
    """Return True if any path component means 'don't index this'.

    `rel_parts` is the path split into components *relative to the
    indexed root*, so a root like `~/.config` doesn't trigger the
    hidden-dir filter on the root itself."""
    for part in rel_parts:
        if part in HARD_IGNORE_DIRS:
            return True
        if not include_deps and part in DEP_DIRS:
            return True
        if not include_hidden and part.startswith(".") and part not in (".", ".."):
            return True
    return False

def _find_local_fados_dir() -> Optional[Path]:
    """Walk up from CWD looking for .fados/, git-style. Stops before ~/."""
    home = Path.home()
    cur = Path.cwd().resolve()
    while True:
        if cur == home or cur.parent == cur:
            break
        candidate = cur / ".fados"
        if candidate.is_dir():
            return candidate
        cur = cur.parent
    return None

MAX_AUTO_INDEX_ENTRIES = 10_000

def _count_tree(root: Path, limit: int,
                include_hidden: bool = False,
                include_deps: bool = False) -> int:
    """Count files under root, stopping early once limit is exceeded."""
    count = 0
    for path in root.rglob("*"):
        if _should_ignore(path.relative_to(root).parts,
                          include_hidden, include_deps):
            continue
        if path.is_file():
            count += 1
            if count > limit:
                return count
    return count

def _is_indexing_in_progress(fados_dir: Path) -> bool:
    """Check if another process is currently indexing (holds a write lock)."""
    db_path = fados_dir / "index.db"
    if not db_path.exists():
        return False
    con = None
    try:
        con = sqlite3.connect(db_path, timeout=0.1)
        # BEGIN IMMEDIATE fails with "database is locked" if another
        # writer already holds the lock — that's our signal.
        con.execute("BEGIN IMMEDIATE")
        con.rollback()
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        if con is not None:
            con.close()


def _needs_auto_index(fados_dir: Path) -> bool:
    """True if the index DB doesn't exist or has no indexed files."""
    db_path = fados_dir / "index.db"
    if not db_path.exists():
        return True
    try:
        con = sqlite3.connect(db_path)
        row = con.execute("SELECT COUNT(*) FROM files").fetchone()
        con.close()
        return row[0] == 0
    except Exception:
        return True

def _auto_index(fados_dir: Path, root: Path):
    """Auto-index `root` on first run. Bail if the tree is too large or
    indexing is in progress."""
    if _is_indexing_in_progress(fados_dir):
        print("indexing is already in progress (another process holds the DB lock). "
              "Wait for it to finish, then retry.",
              file=sys.stderr)
        sys.exit(1)
    count = _count_tree(root, MAX_AUTO_INDEX_ENTRIES)
    if count > MAX_AUTO_INDEX_ENTRIES:
        print(f"error: {root} has more than {MAX_AUTO_INDEX_ENTRIES} files. "
              f"Auto-indexing skipped to avoid a long wait.\n"
              f"Run 'fados reindex {root}' to index explicitly.",
              file=sys.stderr)
        sys.exit(1)
    print(f"no index found — auto-indexing {root} ({count} files)...",
          file=sys.stderr)
    index_tree(root, fados_dir)

# --- DB setup ---

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    mtime       REAL,
    size        INTEGER,
    mime        TEXT,
    indexed_at  REAL
);
CREATE TABLE IF NOT EXISTS metadata (
    path    TEXT,
    key     TEXT,
    value   TEXT,
    source  TEXT,
    PRIMARY KEY (path, key, source)
);
CREATE TABLE IF NOT EXISTS tags (
    path    TEXT,
    tag     TEXT,
    source  TEXT DEFAULT 'user',
    PRIMARY KEY (path, tag)
);
-- chunks: a chunk is the granularity at which both FTS tokens and
-- embeddings live. chunks.id is the rowid used by `content` (contentless
-- FTS5) and by `embeddings`. byte_offset/byte_length point back into the
-- source file; for non-text files extracted via pdftotext/pandoc those
-- span the whole file and chunk_index disambiguates across chunks.
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    byte_length INTEGER NOT NULL,
    UNIQUE (path, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);

-- Contentless FTS5: stores the inverted index only, not the text. The
-- source file on disk is the authoritative copy; snippets are re-read
-- from there via chunks.byte_offset/byte_length at query time.
CREATE VIRTUAL TABLE IF NOT EXISTS content USING fts5(
    text, content='', contentless_delete=1
);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER PRIMARY KEY,
    vector   BLOB
);

CREATE INDEX IF NOT EXISTS idx_meta_key ON metadata(key, value);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
"""

def db_connect(fados_dir: Path) -> sqlite3.Connection:
    fados_dir.mkdir(parents=True, exist_ok=True)
    db_path = fados_dir / "index.db"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS silently accepts an older, differently
    # shaped `files` table — so validate column sets against the current
    # schema and refuse to operate on an incompatible DB. The fix is a
    # full rebuild, not a migration.
    _assert_schema_compatible(con, db_path)
    # WAL + relaxed fsync + bigger cache make bulk indexing much faster;
    # NORMAL is still crash-safe (losing the tail of an uncommitted txn
    # is fine — the index is rebuildable from the source tree anyway).
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-65536")
    return con


EXPECTED_FILES_COLUMNS = {"path", "mtime", "size", "mime", "indexed_at"}


def _assert_schema_compatible(con: sqlite3.Connection, db_path: Path):
    """Compare the `files` table's actual columns to what this version
    expects. A mismatch means the DB was written by a prior fados whose
    schema has since changed — bail with a clear remediation."""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(files)").fetchall()}
    if cols != EXPECTED_FILES_COLUMNS:
        extra = cols - EXPECTED_FILES_COLUMNS
        missing = EXPECTED_FILES_COLUMNS - cols
        diff_parts = []
        if extra:
            diff_parts.append(f"unexpected columns: {sorted(extra)}")
        if missing:
            diff_parts.append(f"missing columns: {sorted(missing)}")
        con.close()
        print(
            f"error: incompatible fados index at {db_path}\n"
            f"  {'; '.join(diff_parts)}\n"
            f"  this index was written by a different version of fados. "
            f"no migration is provided — delete the .fados directory and "
            f"reindex:\n"
            f"    rm -rf {db_path.parent} && fados reindex <path>",
            file=sys.stderr,
        )
        sys.exit(2)

# --- MIME detection ---

# Extension → MIME for files whose type is unambiguously determined by
# extension. Checked before libmagic so common cases (.md, .py, .gz, .pdf,
# ...) skip the header read entirely — that's the dominant per-file cost
# on deep trees of mostly-plain-text files. Extensions not in this map
# fall through to libmagic.
_EXT_MIME_FAST = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".rst": "text/x-rst",
    ".adoc": "text/asciidoc",
    ".org": "text/x-org",
    ".tex": "text/x-tex",
    ".toml": "application/toml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".json": "application/json",
    ".jsonl": "application/json",
    ".ndjson": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".css": "text/css",
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".ts": "application/typescript",
    ".tsx": "application/typescript",
    ".py": "text/x-script.python",
    ".pyi": "text/x-script.python",
    ".sh": "text/x-shellscript",
    ".bash": "text/x-shellscript",
    ".c": "text/x-c",
    ".h": "text/x-c",
    ".cpp": "text/x-c++",
    ".cc": "text/x-c++",
    ".hpp": "text/x-c++",
    ".rs": "text/rust",
    ".go": "text/x-go",
    ".rb": "text/x-ruby",
    ".java": "text/x-java",
    ".kt": "text/x-kotlin",
    ".sql": "application/sql",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".pdf": "application/pdf",
    ".gz": "application/gzip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

# libmagic sometimes returns generic text/plain for formats that lack
# magic bytes (e.g. markdown). When that happens, re-override by
# extension so `WHERE mime='text/markdown'`-style filters match.
_EXT_MIME_OVERRIDES = _EXT_MIME_FAST


def detect_mime(path: Path) -> str:
    ext = path.suffix.lower()
    fast = _EXT_MIME_FAST.get(ext)
    if fast:
        return fast
    try:
        import magic
        mime = magic.from_file(str(path), mime=True)
    except Exception:
        guessed, _ = mimetypes.guess_type(str(path))
        mime = guessed or "application/octet-stream"
    if mime in ("text/plain", "application/octet-stream"):
        override = _EXT_MIME_OVERRIDES.get(ext)
        if override:
            return override
    return mime

# --- Content extractors ---

def run(cmd: list[str], input_path: Path) -> Optional[str]:
    """Run external command, return stdout or None on failure."""
    try:
        result = subprocess.run(
            cmd + [str(input_path)],
            capture_output=True, text=True, timeout=30
        )
        return result.stdout.strip() or None
    except Exception:
        return None

# Chunk sizing targets MiniLM-L6-v2's 256-token context (~1000 chars of
# English text). The overlap keeps concepts that straddle a boundary from
# being invisible to semantic search.
CHUNK_BYTES = 4096
CHUNK_OVERLAP_BYTES = 800


def _chunk_bytes_utf8(raw: bytes):
    """Yield (byte_offset, byte_length, text) windows over a UTF-8 byte
    string, snapping window edges to code-point boundaries so we never
    emit a mojibake-prefixed chunk."""
    n = len(raw)
    if n == 0:
        return
    i = 0
    while i < n:
        end = min(i + CHUNK_BYTES, n)
        # Walk forward off any UTF-8 continuation bytes so we cut at a
        # code point boundary.
        while end < n and (raw[end] & 0xC0) == 0x80:
            end += 1
        text = raw[i:end].decode("utf-8", errors="replace")
        yield i, end - i, text
        if end >= n:
            break
        step = max(1, CHUNK_BYTES - CHUNK_OVERLAP_BYTES)
        i += step
        while i < n and (raw[i] & 0xC0) == 0x80:
            i += 1


# Cap on decompressed gzip output. Keeps a pathological gzip bomb from
# ballooning memory, and keeps chunking-memory bounded to something sane
# on the huge end of normal (kernel changelog.gz is a few MiB).
GZIP_MAX_DECOMPRESSED = 16 * 1024 * 1024  # 16 MiB


def _extract_non_text(path: Path, mime: str) -> Optional[str]:
    """Best-effort plaintext extraction for non-text MIME types."""
    if mime == "application/pdf":
        try:
            result = subprocess.run(
                ["pdftotext", str(path), "-"],
                capture_output=True, text=True, timeout=30
            )
            return result.stdout.strip() or None
        except Exception:
            return None
    if mime in ("application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
        return run(["pandoc", "--to=plain"], path)
    if mime.startswith("application/vnd.oasis"):
        return run(["pandoc", "--to=plain"], path)
    if mime == "application/gzip":
        # Stream-decompress with a hard cap. If the decompressed content
        # is valid UTF-8 we index it as plaintext; anything else (binary
        # tarballs, non-UTF-8 encodings) is skipped rather than guessed at.
        import gzip as _gzip
        try:
            with _gzip.open(str(path), "rb") as f:
                raw = f.read(GZIP_MAX_DECOMPRESSED + 1)
        except Exception:
            return None
        if len(raw) > GZIP_MAX_DECOMPRESSED:
            return None
        try:
            return raw.decode("utf-8") or None
        except UnicodeDecodeError:
            return None
    return None


def extract_chunks(path: Path, mime: str):
    """Yield (chunk_index, byte_offset, byte_length, text) for a file.

    For text/* files byte_offset/byte_length refer to the file's own
    bytes, so snippets can be re-read directly. For non-text files the
    byte range spans the whole source file (chunk_index distinguishes
    individual chunks) and snippet rendering must re-extract via the
    same tool."""
    if mime.startswith("text/"):
        with path.open("rb") as f:
            raw = f.read()
        for idx, (off, length, text) in enumerate(_chunk_bytes_utf8(raw)):
            yield idx, off, length, text
        return
    text = _extract_non_text(path, mime)
    if not text:
        return
    size = path.stat().st_size
    raw = text.encode("utf-8")
    for idx, (_, _, chunk_text) in enumerate(_chunk_bytes_utf8(raw)):
        yield idx, 0, size, chunk_text

# Files per batched exiftool call. 50 is well under the shell ARG_MAX
# and keeps each call's timeout (10 + N seconds) short enough that a
# single hung file can't stall indexing for long.
EXIF_BATCH = 50


_EXIFTOOL_SKIP_KEYS = {
    "SourceFile", "ExifToolVersion", "Directory", "FileName",
    "FilePermissions", "FileAccessDate", "FileInodeChangeDate",
}


def _wants_exif(mime: str) -> bool:
    """Files where exiftool has no real metadata to contribute. Text and
    gzip both fall out here — the subprocess cost would dominate
    indexing of text-heavy trees with zero benefit."""
    return not (mime.startswith("text/") or mime == "application/gzip")


def batch_exiftool(paths: list[str]) -> dict[str, dict]:
    """Run one exiftool invocation over many paths and return a
    {path: {key: value}} map. Empty dict on total failure; a partial
    parse still returns whatever succeeded.

    Batching is the main win here: exiftool's startup (Perl interpreter +
    its module graph) dominates per-file cost for small files. 50 paths
    per call cuts fork/exec overhead ~50×."""
    if not paths:
        return {}
    try:
        result = subprocess.run(
            ["exiftool", "-json", "-fast", "--", *paths],
            capture_output=True, text=True,
            # Startup budget + per-file budget. -fast skips slow probes.
            timeout=10 + len(paths),
        )
        data = json.loads(result.stdout or "[]")
    except Exception:
        return {}
    # SourceFile is what exiftool echoes back; because we always pass
    # absolute paths in, the echo matches the input string exactly.
    out: dict[str, dict] = {}
    for entry in data:
        src = entry.get("SourceFile")
        if not src:
            continue
        out[src] = {
            k: str(v) for k, v in entry.items()
            if k not in _EXIFTOOL_SKIP_KEYS
        }
    return out

# --- Indexing ---
#
# Indexing splits into two phases that can run independently:
#   1. Extraction (stat, MIME, chunk text, EXIF) — pure per-file work,
#      no DB access. Farmed out to a process pool.
#   2. Writing — single-threaded, one SQLite connection, drains results
#      as the pool completes them.
# SQLite on a single connection does not parallelize writes, so there's
# no benefit to a second writer; the win is keeping the writer fed while
# extraction runs on N cores.

def _extract_fast(path_str: str) -> dict:
    """Pool worker: the work that doesn't need a subprocess and doesn't
    touch the DB (stat, MIME, chunk extraction). EXIF is deliberately
    NOT done here — it's batched across many files in the main loop, so
    each worker returning quickly is what keeps the pool fed."""
    path = Path(path_str)
    stat = path.stat()
    mime = detect_mime(path)
    chunks = list(extract_chunks(path, mime))
    return {
        "path": path_str,
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "mime": mime,
        "chunks": chunks,
    }


def _write_indexed(con: sqlite3.Connection, data: dict):
    """Apply one extracted file's results to the DB. Drops prior chunk
    rows for the path so re-indexing is idempotent; the FTS rowid mirrors
    chunks.id so we delete from `content` in lockstep."""
    path = data["path"]
    old_ids = [r[0] for r in con.execute(
        "SELECT id FROM chunks WHERE path=?", (path,)).fetchall()]
    if old_ids:
        con.executemany(
            "DELETE FROM content WHERE rowid=?", [(i,) for i in old_ids])
        con.executemany(
            "DELETE FROM embeddings WHERE chunk_id=?", [(i,) for i in old_ids])
        con.execute("DELETE FROM chunks WHERE path=?", (path,))

    con.execute(
        "INSERT OR REPLACE INTO files(path, mtime, size, mime, indexed_at) "
        "VALUES (?,?,?,?,?)",
        (path, data["mtime"], data["size"], data["mime"], time.time()))

    for idx, off, length, chunk_text in data["chunks"]:
        cur = con.execute(
            "INSERT INTO chunks(path, chunk_index, byte_offset, byte_length) "
            "VALUES (?,?,?,?)",
            (path, idx, off, length))
        con.execute(
            "INSERT INTO content(rowid, text) VALUES (?, ?)",
            (cur.lastrowid, chunk_text))

    con.execute("DELETE FROM metadata WHERE path=? AND source != 'user'", (path,))
    for k, v in data["exif"].items():
        con.execute(
            "INSERT OR REPLACE INTO metadata(path, key, value, source) "
            "VALUES (?,?,?,'exif')",
            (path, k, v))


def index_tree(root: Path, fados_dir: Path, force: bool = False,
               include_hidden: bool = False,
               include_deps: bool = False):
    con = db_connect(fados_dir)

    # Pre-load existing mtimes so the change check is one query, not one
    # per file. On a full reindex we skip the load and treat everything
    # as stale.
    existing = {}
    if not force:
        existing = {r["path"]: r["mtime"] for r in
                    con.execute("SELECT path, mtime FROM files").fetchall()}

    todo: list[str] = []
    for path in root.rglob("*"):
        if _should_ignore(path.relative_to(root).parts,
                          include_hidden, include_deps):
            continue
        if not path.is_file():
            continue
        try:
            mt = path.stat().st_mtime
        except OSError:
            continue
        path_str = str(path)
        if force or existing.get(path_str) != mt:
            todo.append(path_str)

    if not todo:
        con.close()
        print("nothing to index", file=sys.stderr)
        return

    cpu = os.cpu_count() or 4
    max_workers = min(cpu, 16)
    # Each exiftool is single-threaded; cap the thread pool so we don't
    # over-fork on small boxes. Cap at 8 because past that you hit fork
    # contention faster than you gain throughput.
    exif_workers = min(cpu, 8)
    print(f"indexing {len(todo)} files with {max_workers} extract workers "
          f"+ {exif_workers} exif workers...", file=sys.stderr)
    t0 = time.time()
    count = 0
    skipped = 0
    errors = 0

    pending: list[dict] = []
    # Exif futures run concurrently with extraction; each future holds
    # the batch of dicts it was submitted with so we can merge results
    # back without a separate lookup.
    exif_futures: dict = {}

    def write_batch(batch: list[dict], exif_map: dict[str, dict]):
        nonlocal count, skipped
        for d in batch:
            d["exif"] = exif_map.get(d["path"], {})
            if not d["chunks"] and not d["exif"]:
                skipped += 1
                continue
            _write_indexed(con, d)
            count += 1

    def drain_exif(block: bool = False):
        """Move any exif futures whose subprocess has returned onto the
        writer. With block=True wait for at least one to complete —
        used when we need to bound memory growth of pending futures
        (the pool would queue more work behind them) or at shutdown."""
        if not exif_futures:
            return
        done = [f for f in exif_futures if f.done()]
        if block and not done:
            done = [next(iter(as_completed(list(exif_futures))))]
        for f in done:
            batch = exif_futures.pop(f)
            try:
                write_batch(batch, f.result())
            except Exception as e:
                # exif subprocess crashed — write without metadata rather
                # than losing the chunks we already extracted.
                print(f"  error: exif batch failed: {e}", file=sys.stderr)
                write_batch(batch, {})

    def submit_batch(exif_pool: ThreadPoolExecutor):
        if not pending:
            return
        batch = list(pending)
        pending.clear()
        want = [d["path"] for d in batch if _wants_exif(d["mime"])]
        if not want:
            # Skip the subprocess entirely for pure-text batches.
            write_batch(batch, {})
            return
        fut = exif_pool.submit(batch_exiftool, want)
        exif_futures[fut] = batch

    # Cap the number of in-flight exif batches so pending memory doesn't
    # balloon if extraction outruns exiftool. Twice the worker count gives
    # each worker a next-batch already queued without starving.
    max_inflight = exif_workers * 2

    with ProcessPoolExecutor(max_workers=max_workers) as pool, \
            ThreadPoolExecutor(max_workers=exif_workers,
                               thread_name_prefix="exif") as exif_pool:
        futures = {pool.submit(_extract_fast, p): p for p in todo}
        batches = 0
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                pending.append(fut.result())
            except Exception as e:
                errors += 1
                print(f"  error: {p}: {e}", file=sys.stderr)
                continue
            if len(pending) >= EXIF_BATCH:
                if len(exif_futures) >= max_inflight:
                    drain_exif(block=True)
                submit_batch(exif_pool)
                batches += 1
                if batches % 20 == 0:
                    drain_exif()
                    con.commit()
                    elapsed = time.time() - t0
                    print(f"  indexing: {count + skipped}/{len(todo)} "
                          f"({elapsed:.0f}s)...", file=sys.stderr, flush=True)
            else:
                drain_exif()
        # Flush partial last batch, then drain all outstanding exif work.
        submit_batch(exif_pool)
        while exif_futures:
            drain_exif(block=True)
    con.commit()
    con.close()
    elapsed = time.time() - t0
    msg = f"indexed {count} files in {elapsed:.1f}s"
    if skipped:
        msg += f" ({skipped} skipped — no content)"
    if errors:
        msg += f" ({errors} errors)"
    print(msg, file=sys.stderr)

# --- Query ---

def query(sql: str, fados_dir: Path, params=()):
    con = db_connect(fados_dir)
    try:
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()

def _read_chunk_text(path: str, byte_offset: int, byte_length: int,
                     mime: Optional[str] = None) -> Optional[str]:
    """Re-read a chunk's text from the source file. Returns None if the
    file is gone or the byte range no longer exists."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        if mime is None or mime.startswith("text/"):
            with p.open("rb") as f:
                f.seek(byte_offset)
                raw = f.read(byte_length)
            return raw.decode("utf-8", errors="replace")
        # Non-text: byte range spans the whole file, so we re-extract and
        # return the concatenated plaintext. Callers still have only
        # chunk-index granularity here — good enough for snippet display.
        return _extract_non_text(p, mime)
    except OSError:
        return None


def _make_snippet(text: str, terms: str, width: int = 200) -> str:
    """Return a ~width-char slice of text centered on the first term hit,
    with each matching term wrapped in [ ]. Tokens shorter than 3 chars
    are skipped for highlighting so 'a'/'on'/'in' don't light up every
    word in the snippet."""
    if not text:
        return ""
    toks = [t for t in re.findall(r"\w+", terms) if len(t) >= 3]
    lower = text.lower()
    hit = -1
    for t in toks:
        j = lower.find(t.lower())
        if j != -1 and (hit == -1 or j < hit):
            hit = j
    if hit == -1:
        snippet = text[:width]
    else:
        start = max(0, hit - width // 2)
        snippet = text[start:start + width]
    for t in toks:
        snippet = re.sub(rf"(?i)\b({re.escape(t)})\b", r"[\1]", snippet)
    return snippet.strip()


def search(terms: str, fados_dir: Path, n: int = 20):
    con = db_connect(fados_dir)
    rows = con.execute(
        "SELECT c.path, c.byte_offset, c.byte_length, f.mime, "
        "       bm25(content) AS rank "
        "FROM content "
        "JOIN chunks c ON c.id = content.rowid "
        "LEFT JOIN files f ON f.path = c.path "
        "WHERE content MATCH ? "
        "ORDER BY rank LIMIT ?",
        (terms, n),
    ).fetchall()
    con.close()
    results = []
    for r in rows:
        text = _read_chunk_text(r["path"], r["byte_offset"],
                                r["byte_length"], r["mime"])
        results.append({
            "path": r["path"],
            "byte_offset": r["byte_offset"],
            "byte_length": r["byte_length"],
            "snippet": _make_snippet(text or "", terms),
            "rank": round(r["rank"], 4),
        })
    return results

def find_meta(key: str, value: str, fados_dir: Path):
    return query(
        "SELECT f.path, f.mime, m.value FROM files f "
        "JOIN metadata m ON m.path = f.path "
        "WHERE m.key = ? AND m.value LIKE ?",
        fados_dir,
        (key, f"%{value}%")
    )

# --- Vector / semantic search ---

_model = None


def _require_semantic_deps():
    """Import numpy + sentence_transformers or exit with an install hint."""
    try:
        import numpy  # noqa: F401
        import sentence_transformers  # noqa: F401
    except ImportError as e:
        print(f"error: semantic commands need the 'semantic' extra "
              f"(missing: {e.name}). Install with: uv sync --extra semantic",
              file=sys.stderr)
        sys.exit(2)


def _get_model():
    global _model
    if _model is None:
        _require_semantic_deps()
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def _embed(text: str) -> bytes:
    # Don't pre-truncate — the model's tokenizer already truncates to its
    # own max_seq_length. Adding an arbitrary char-level cap on top just
    # hides a second, invisible truncation with a number we made up.
    import numpy as np
    vec = _get_model().encode(text, normalize_embeddings=True)
    return vec.astype(np.float32).tobytes()


# Batch size for embedding: collect this many chunk texts, then call
# model.encode() once. Bigger = better GPU/CPU utilization but more RAM.
# 256 is a reasonable balance for MiniLM-L6-v2 on CPU.
EMBED_BATCH = 256


def embed_tree(root: Path, fados_dir: Path):
    """Generate / refresh embeddings for all chunks under root. Chunk
    text is re-read from the filesystem — the DB doesn't hold it.

    Batches chunk texts through model.encode() instead of encoding one
    at a time; on CPU this is typically an order of magnitude faster
    because the transformer amortizes Python/tensor overhead across the
    batch."""
    import numpy as np

    con = db_connect(fados_dir)
    rows = con.execute(
        "SELECT c.id, c.path, c.byte_offset, c.byte_length, f.mime "
        "FROM chunks c LEFT JOIN files f ON f.path = c.path "
        "WHERE c.path LIKE ?",
        (str(root.resolve()) + "%",)
    ).fetchall()
    total = len(rows)
    print(f"embedding {total} chunks...", file=sys.stderr)
    model = _get_model()
    t0 = time.time()
    done = 0
    skipped = 0
    errors = 0
    pending_texts: list[str] = []
    pending_ids: list[int] = []

    def flush():
        nonlocal done
        if not pending_texts:
            return
        vecs = model.encode(
            pending_texts,
            normalize_embeddings=True,
            batch_size=64,
            show_progress_bar=False,
        )
        con.executemany(
            "INSERT OR REPLACE INTO embeddings(chunk_id, vector) VALUES (?,?)",
            [(cid, np.asarray(v, dtype=np.float32).tobytes())
             for cid, v in zip(pending_ids, vecs)],
        )
        con.commit()
        done += len(pending_ids)
        pending_texts.clear()
        pending_ids.clear()
        elapsed = time.time() - t0
        print(f"  embedding: {done}/{total} ({elapsed:.0f}s)...",
              file=sys.stderr, flush=True)

    for row in rows:
        try:
            text = _read_chunk_text(row["path"], row["byte_offset"],
                                    row["byte_length"], row["mime"])
        except Exception as e:
            errors += 1
            print(f"  error chunk {row['id']} ({row['path']}): {e}",
                  file=sys.stderr)
            continue
        if not text:
            skipped += 1
            continue
        pending_texts.append(text)
        pending_ids.append(row["id"])
        if len(pending_texts) >= EMBED_BATCH:
            flush()
    flush()
    con.close()
    elapsed = time.time() - t0
    msg = f"embedded {done} chunks in {elapsed:.1f}s"
    if skipped:
        msg += f" ({skipped} skipped — source unreadable)"
    if errors:
        msg += f" ({errors} errors)"
    print(msg, file=sys.stderr)


def _score_chunks(q_vec, con: sqlite3.Connection):
    """Return [(score, chunk_row)] for every chunk with an embedding."""
    import numpy as np
    rows = con.execute(
        "SELECT c.id, c.path, c.byte_offset, c.byte_length, f.mime, e.vector "
        "FROM embeddings e "
        "JOIN chunks c ON c.id = e.chunk_id "
        "LEFT JOIN files f ON f.path = c.path"
    ).fetchall()
    scored = []
    for r in rows:
        v = np.frombuffer(r["vector"], dtype=np.float32)
        scored.append((float(np.dot(q_vec, v)), r))
    scored.sort(reverse=True, key=lambda x: x[0])
    return scored


def _dedupe_best_per_path(scored, n: int):
    """Keep the single best-scoring chunk per path, top n."""
    seen = {}
    for score, r in scored:
        if r["path"] in seen:
            continue
        seen[r["path"]] = (score, r)
        if len(seen) >= n:
            break
    return list(seen.values())


def semantic(query_text: str, fados_dir: Path, n: int = 20) -> list:
    """Semantic search: cosine similarity of the query embedding against
    all stored chunk embeddings. Dedupes to the best chunk per file."""
    import numpy as np
    q_vec = np.frombuffer(_embed(query_text), dtype=np.float32)
    con = db_connect(fados_dir)
    scored = _score_chunks(q_vec, con)
    con.close()
    if not scored:
        return []
    top = _dedupe_best_per_path(scored, n)
    out = []
    for score, r in top:
        text = _read_chunk_text(r["path"], r["byte_offset"],
                                r["byte_length"], r["mime"])
        out.append({
            "path": r["path"],
            "byte_offset": r["byte_offset"],
            "byte_length": r["byte_length"],
            "snippet": _make_snippet(text or "", query_text),
            "score": round(score, 4),
        })
    return out


def similar(path: str, fados_dir: Path, n: int = 10) -> list:
    """Find files whose chunks most resemble any chunk of the given
    file. File-level rank is the max similarity across chunk pairs."""
    import numpy as np
    con = db_connect(fados_dir)
    src_rows = con.execute(
        "SELECT e.vector FROM embeddings e "
        "JOIN chunks c ON c.id = e.chunk_id WHERE c.path=?",
        (path,)
    ).fetchall()
    if not src_rows:
        con.close()
        return []
    src_vecs = [np.frombuffer(r["vector"], dtype=np.float32) for r in src_rows]
    other_rows = con.execute(
        "SELECT c.path, c.byte_offset, c.byte_length, f.mime, e.vector "
        "FROM embeddings e "
        "JOIN chunks c ON c.id = e.chunk_id "
        "LEFT JOIN files f ON f.path = c.path "
        "WHERE c.path != ?",
        (path,)
    ).fetchall()
    con.close()
    best = {}
    for r in other_rows:
        v = np.frombuffer(r["vector"], dtype=np.float32)
        s = max(float(np.dot(sv, v)) for sv in src_vecs)
        cur = best.get(r["path"])
        if cur is None or s > cur[0]:
            best[r["path"]] = (s, r)
    ranked = sorted(best.values(), key=lambda x: x[0], reverse=True)[:n]
    return [
        {"path": r["path"], "byte_offset": r["byte_offset"],
         "byte_length": r["byte_length"], "score": round(s, 4)}
        for s, r in ranked
    ]

# --- Intent-based search (ripgrep) ---

TEST_GLOBS = [
    "test_*", "*_test.*", "*_spec.*", "*Test.*", "*Tests.*",
    "tests/**", "spec/**", "test/**",
]

DOC_GLOBS = [
    "*.md", "*.rst", "*.txt", "*.adoc",
    "README*", "CHANGELOG*", "LICENSE*",
    "docs/**", "doc/**",
]

def _rg(pattern: str, root: Path, *,
        glob_include: list[str] | None = None,
        glob_exclude: list[str] | None = None,
        max_results: int = 50) -> list[dict]:
    """Run ripgrep and return structured results as [{path, line, match}]."""
    cmd = ["rg", "-n", "--no-heading", "-e", pattern]
    for d in IGNORE_DIRS:
        cmd.extend(["--glob", f"!{d}/"])
    if glob_include:
        for g in glob_include:
            cmd.extend(["--glob", g])
    if glob_exclude:
        for g in glob_exclude:
            cmd.extend(["--glob", f"!{g}"])
    cmd.append(str(root))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        # Surface a useful error instead of pretending there were zero hits.
        print("error: ripgrep (rg) not found on PATH", file=sys.stderr)
        return []
    except subprocess.TimeoutExpired:
        print(f"warning: ripgrep timed out after 30s scanning {root}",
              file=sys.stderr)
        return []
    results = []
    for line in result.stdout.strip().split('\n'):
        if not line:
            continue
        parts = line.split(':', 2)
        if len(parts) >= 3:
            try:
                lineno = int(parts[1])
            except ValueError:
                continue
            results.append({"path": parts[0], "line": lineno,
                            "match": parts[2].strip()})
            if len(results) >= max_results:
                break
    return results


def search_definition(term: str, root: Path, n: int = 20) -> list[dict]:
    """Find where a term is defined (classes, functions, constants, types)."""
    escaped = re.escape(term)
    pattern = (
        rf'(?:def|class|fn|func|function|struct|enum|trait|impl|interface|type|module)'
        rf'\s+{escaped}\b'
        rf'|(?:const|let|var|static|#define)\s+(?:\w+\s+)*\b{escaped}\b'
    )
    return _rg(pattern, root, max_results=n)


def search_tests(term: str, root: Path, n: int = 20) -> list[dict]:
    """Find test code referencing a term."""
    escaped = re.escape(term)
    return _rg(rf'\b{escaped}\b', root, glob_include=TEST_GLOBS, max_results=n)


def search_documentation(term: str, root: Path, n: int = 20) -> list[dict]:
    """Find documentation referencing a term."""
    escaped = re.escape(term)
    return _rg(rf'\b{escaped}\b', root, glob_include=DOC_GLOBS, max_results=n)


def search_implementation(term: str, root: Path, n: int = 20) -> list[dict]:
    """Find implementation code referencing a term (excludes tests and docs)."""
    escaped = re.escape(term)
    return _rg(rf'\b{escaped}\b', root,
               glob_exclude=TEST_GLOBS + DOC_GLOBS, max_results=n)


# --- Mutations ---

def tag(path: str, tag_name: str, fados_dir: Path):
    con = db_connect(fados_dir)
    con.execute("INSERT OR REPLACE INTO tags(path, tag) VALUES (?,?)", (path, tag_name))
    con.commit()
    con.close()

def annotate(path: str, key: str, value: str, fados_dir: Path):
    con = db_connect(fados_dir)
    con.execute("""
        INSERT OR REPLACE INTO metadata(path, key, value, source)
        VALUES (?,?,?,'user')
    """, (path, key, value))
    con.commit()
    con.close()

def info(path: str, fados_dir: Path):
    con = db_connect(fados_dir)
    file_row = con.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
    meta = con.execute("SELECT key, value, source FROM metadata WHERE path=?", (path,)).fetchall()
    tags_ = con.execute("SELECT tag, source FROM tags WHERE path=?", (path,)).fetchall()
    chunk_stats = con.execute(
        "SELECT COUNT(*) AS n_chunks, "
        "       (SELECT COUNT(*) FROM embeddings e "
        "        JOIN chunks c ON c.id = e.chunk_id WHERE c.path=?) AS n_embedded "
        "FROM chunks WHERE path=?",
        (path, path)
    ).fetchone()
    con.close()
    return {
        "file": dict(file_row) if file_row else None,
        "metadata": [dict(r) for r in meta],
        "tags": [dict(r) for r in tags_],
        "chunks": dict(chunk_stats) if chunk_stats else None,
    }

# --- Watch (inotifywait) ---

def watch(root: Path, fados_dir: Path):
    """Incrementally reindex on filesystem changes. Requires inotify-tools."""
    print(f"watching {root} ...")
    proc = subprocess.Popen(
        ["inotifywait", "-m", "-r", "-e", "close_write,moved_to,create",
         "--format", "%w%f", str(root)],
        stdout=subprocess.PIPE, text=True
    )
    con = db_connect(fados_dir)
    for line in proc.stdout:
        path = Path(line.strip())
        if path.is_file():
            try:
                data = _extract_fast(str(path))
                data["exif"] = (batch_exiftool([data["path"]]).get(data["path"], {})
                                if _wants_exif(data["mime"]) else {})
                if not data["chunks"] and not data["exif"]:
                    continue
                _write_indexed(con, data)
                con.commit()
                print(f"  reindexed: {path}")
            except Exception as e:
                print(f"  error: {path}: {e}", file=sys.stderr)

# --- CLI ---

def _resolve_indexed_path(file: str) -> str:
    """Indexed paths are absolute (from root.rglob). Normalize user input
    to match — relative paths would silently miss otherwise."""
    return str(Path(file).resolve())


def _table_count(fados_dir: Path, table: str) -> int:
    """Return row count for a table, 0 if table doesn't exist."""
    try:
        con = sqlite3.connect(fados_dir / "index.db")
        row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        con.close()
        return row[0]
    except Exception:
        return 0


def print_results(rows: list, *, empty_hint: str = ""):
    if not rows:
        if empty_hint:
            print(f"(no results — {empty_hint})", file=sys.stderr)
        else:
            print("(no results)")
        return
    for row in rows:
        print(json.dumps(row, default=str))


class Context:
    """fados_dir (where the DB lives) and root (what gets indexed).
    These are deliberately independent — e.g. `--user` puts the DB at
    ~/.fados while `root` is still the tree the user wants to query."""
    def __init__(self, fados_dir: Path, root: Path):
        self.fados_dir = fados_dir
        self.root = root

    def auto_index_if_needed(self):
        if _needs_auto_index(self.fados_dir):
            _auto_index(self.fados_dir, self.root)


pass_ctx = click.make_pass_decorator(Context)


@click.group()
@click.option("--user", is_flag=True, help="Use ~/.fados/ instead of local .fados/.")
@click.option("--dir", "dir_", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None, help="Target directory (uses <dir>/.fados/ for the index).")
@click.pass_context
def cli(ctx, user, dir_):
    """FADOS — Filesystem As Database Overlay System."""
    ctx.ensure_object(dict)
    # Store raw values; subcommands with a path arg can override before resolving
    ctx.obj["user"] = user
    ctx.obj["dir"] = dir_


def _resolve_ctx(ctx, path_override: Optional[Path] = None) -> Context:
    """Build Context. fados_dir and root are derived independently so
    `--user` doesn't accidentally force $HOME as the indexing root.

    fados_dir (where index.db lives):
      --user             → ~/.fados/
      --dir <d>          → <d>/.fados/
      positional <p>     → <p>/.fados/ (reindex/embed/watch only)
      otherwise          → walk up from CWD, else CWD/.fados/

    root (what to index/query-against):
      positional <p>     → <p>
      --dir <d>          → <d>
      --user (no path)   → CWD
      otherwise          → parent of fados_dir
    """
    user = ctx.obj["user"]
    dir_ = ctx.obj["dir"]

    if user:
        fados_dir = USER_FADOS_DIR
    elif path_override is not None:
        fados_dir = path_override.resolve() / ".fados"
    elif dir_ is not None:
        fados_dir = dir_.resolve() / ".fados"
    else:
        found = _find_local_fados_dir()
        fados_dir = found if found else Path.cwd().resolve() / ".fados"

    if path_override is not None:
        root = path_override.resolve()
    elif dir_ is not None:
        root = dir_.resolve()
    elif user:
        root = Path.cwd().resolve()
    else:
        root = fados_dir.parent

    return Context(fados_dir, root)


# --- Index commands (optional positional path, defaults to --dir / CWD) ---

@cli.command()
@click.argument("path", required=False, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--embed", "do_embed", is_flag=True, help="Also generate embeddings.")
@click.option("--hidden", "include_hidden", is_flag=True,
              help="Include dot-prefixed files and directories (except hard-ignored ones like .git, __pycache__).")
@click.option("--deps", "include_deps", is_flag=True,
              help="Include dependency/build dirs (node_modules, target, .venv, dist, build, ...).")
@click.pass_context
def reindex(ctx, path, do_embed, include_hidden, include_deps):
    """Force a full reindex of the tree."""
    c = _resolve_ctx(ctx, path)
    index_tree(c.root, c.fados_dir, force=True,
               include_hidden=include_hidden, include_deps=include_deps)
    if do_embed:
        embed_tree(c.root, c.fados_dir)


@cli.command()
@click.argument("path", required=False, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.pass_context
def embed(ctx, path):
    """Generate/refresh semantic embeddings for indexed content."""
    c = _resolve_ctx(ctx, path)
    embed_tree(c.root, c.fados_dir)


@cli.command("watch")
@click.argument("path", required=False, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.pass_context
def watch_cmd(ctx, path):
    """Watch for changes and reindex incrementally (requires inotify-tools)."""
    c = _resolve_ctx(ctx, path)
    watch(c.root, c.fados_dir)


# --- Index-based search ---

@cli.command("query")
@click.argument("sql")
@click.pass_context
def query_cmd(ctx, sql):
    """Raw SQL against the index database."""
    c = _resolve_ctx(ctx)
    c.auto_index_if_needed()
    print_results(query(sql, c.fados_dir))


@cli.command("search")
@click.argument("terms", nargs=-1, required=True)
@click.option("-n", default=20, help="Max results.")
@click.pass_context
def search_cmd(ctx, terms, n):
    """Full-text keyword search (FTS5)."""
    c = _resolve_ctx(ctx)
    c.auto_index_if_needed()
    results = search(" ".join(terms), c.fados_dir, n)
    hint = ""
    if not results and _table_count(c.fados_dir, "chunks") == 0:
        n_files = _table_count(c.fados_dir, "files")
        if n_files == 0:
            hint = "index is empty; run: fados --dir <path> reindex"
        else:
            hint = f"{n_files} files indexed but no chunks were extracted"
    print_results(results, empty_hint=hint)


@cli.command("semantic")
@click.argument("terms", nargs=-1, required=True)
@click.option("-n", default=20, help="Max results.")
@click.pass_context
def semantic_cmd(ctx, terms, n):
    """Semantic/conceptual search using embeddings."""
    c = _resolve_ctx(ctx)
    c.auto_index_if_needed()
    results = semantic(" ".join(terms), c.fados_dir, n)
    hint = ""
    if not results and _table_count(c.fados_dir, "embeddings") == 0:
        hint = "no embeddings generated; run: fados --dir <path> embed"
    print_results(results, empty_hint=hint)


@cli.command("similar")
@click.argument("file", type=click.Path())
@click.option("-n", default=10, help="Max results.")
@click.pass_context
def similar_cmd(ctx, file, n):
    """Find files with similar content to a given file."""
    c = _resolve_ctx(ctx)
    c.auto_index_if_needed()
    resolved = _resolve_indexed_path(file)
    results = similar(resolved, c.fados_dir, n)
    hint = ""
    if not results:
        n_embed = _table_count(c.fados_dir, "embeddings")
        if n_embed == 0:
            hint = "no embeddings generated; run: fados --dir <path> embed"
        else:
            con = sqlite3.connect(c.fados_dir / "index.db")
            row = con.execute(
                "SELECT 1 FROM embeddings e "
                "JOIN chunks c ON c.id = e.chunk_id WHERE c.path=?",
                (resolved,)
            ).fetchone()
            con.close()
            if not row:
                hint = f"no embedding for '{file}' — file may not be indexed, or embeddings haven't been generated"
    print_results(results, empty_hint=hint)


@cli.command("find")
@click.argument("key")
@click.argument("value")
@click.pass_context
def find_cmd(ctx, key, value):
    """Search metadata by key/value."""
    c = _resolve_ctx(ctx)
    c.auto_index_if_needed()
    results = find_meta(key, value, c.fados_dir)
    hint = ""
    if not results and _table_count(c.fados_dir, "metadata") == 0:
        n_files = _table_count(c.fados_dir, "files")
        if n_files == 0:
            hint = "index is empty; run: fados --dir <path> reindex"
        else:
            hint = "no metadata extracted — exiftool may not be installed"
    print_results(results, empty_hint=hint)


# --- Intent-based search (ripgrep, no index) ---

def _rg_empty_hint(root: Path) -> str:
    """Hint for when ripgrep-based commands return nothing."""
    if not root.is_dir():
        return f"search root '{root}' is not a directory"
    import shutil
    if not shutil.which("rg"):
        return "ripgrep (rg) is not installed"
    return ""


@cli.command()
@click.argument("term")
@click.option("-n", default=20, help="Max results.")
@click.pass_context
def definition(ctx, term, n):
    """Find where a term is defined (class, function, type, const, etc.)."""
    c = _resolve_ctx(ctx)
    results = search_definition(term, c.root, n)
    print_results(results, empty_hint=_rg_empty_hint(c.root) or f"no definitions found for '{term}'")


@cli.command()
@click.argument("term")
@click.option("-n", default=20, help="Max results.")
@click.pass_context
def implementation(ctx, term, n):
    """Find usage in code (excludes tests and docs)."""
    c = _resolve_ctx(ctx)
    results = search_implementation(term, c.root, n)
    print_results(results, empty_hint=_rg_empty_hint(c.root) or f"no implementation references found for '{term}'")


@cli.command()
@click.argument("term")
@click.option("-n", default=20, help="Max results.")
@click.pass_context
def documentation(ctx, term, n):
    """Find references in docs (markdown, rst, etc.)."""
    c = _resolve_ctx(ctx)
    results = search_documentation(term, c.root, n)
    print_results(results, empty_hint=_rg_empty_hint(c.root) or f"no documentation references found for '{term}'")


@cli.command()
@click.argument("term")
@click.option("-n", default=20, help="Max results.")
@click.pass_context
def tests(ctx, term, n):
    """Find references in test files."""
    c = _resolve_ctx(ctx)
    results = search_tests(term, c.root, n)
    print_results(results, empty_hint=_rg_empty_hint(c.root) or f"no test references found for '{term}'")


# --- File info and annotation ---

@cli.command("tag")
@click.argument("file", type=click.Path())
@click.argument("tag_name", metavar="TAG")
@click.pass_context
def tag_cmd(ctx, file, tag_name):
    """Add a user tag to a file."""
    c = _resolve_ctx(ctx)
    tag(_resolve_indexed_path(file), tag_name, c.fados_dir)


@cli.command("annotate")
@click.argument("file", type=click.Path())
@click.argument("key")
@click.argument("value")
@click.pass_context
def annotate_cmd(ctx, file, key, value):
    """Add arbitrary metadata to a file."""
    c = _resolve_ctx(ctx)
    annotate(_resolve_indexed_path(file), key, value, c.fados_dir)


@cli.command("info")
@click.argument("file", type=click.Path())
@click.pass_context
def info_cmd(ctx, file):
    """Show all indexed data for a file."""
    c = _resolve_ctx(ctx)
    c.auto_index_if_needed()
    print(json.dumps(info(_resolve_indexed_path(file), c.fados_dir), indent=2, default=str))


if __name__ == "__main__":
    cli()

