#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click",
#     "python-magic",
# ]
# ///
"""
FADOS - Filesystem As Database Overlay System
Single-file prototype. Run with: uv run scripts/fados.py <command> [args]

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
import hashlib
import time
import mimetypes
from pathlib import Path
from typing import Optional

import click

# --- Config ---

IGNORE_DIRS = {".git", ".fados", "__pycache__", "node_modules", "target", ".venv"}
USER_FADOS_DIR = Path.home() / ".fados"

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

def _count_tree(root: Path, limit: int) -> int:
    """Count files under root, stopping early once limit is exceeded."""
    ignore = IGNORE_DIRS
    count = 0
    for path in root.rglob("*"):
        if any(part in ignore for part in path.parts):
            continue
        if path.is_file():
            count += 1
            if count > limit:
                return count
    return count

def resolve_fados_dir(*, user: bool = False,
                      dir_override: Optional[Path] = None) -> Path:
    """Determine where .fados/ lives.

    --user flag:    always ~/.fados/
    --dir override: <dir>/.fados/
    default:        walk up from CWD to find .fados/, or CWD/.fados/ for auto-index
    """
    if user:
        return USER_FADOS_DIR
    if dir_override is not None:
        return dir_override.resolve() / ".fados"
    found = _find_local_fados_dir()
    if found:
        return found
    # No existing index — will auto-index from CWD
    return Path.cwd().resolve() / ".fados"

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

def _auto_index(fados_dir: Path):
    """Auto-index CWD on first run. Bail if the tree is too large or indexing is in progress."""
    if _is_indexing_in_progress(fados_dir):
        print("indexing is already in progress (another process holds the DB lock). "
              "Wait for it to finish, then retry.",
              file=sys.stderr)
        sys.exit(1)
    root = fados_dir.parent
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
    checksum    TEXT,
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
CREATE VIRTUAL TABLE IF NOT EXISTS content USING fts5(path UNINDEXED, text);
CREATE TABLE IF NOT EXISTS embeddings (
    path    TEXT PRIMARY KEY,
    vector  BLOB
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
    con.execute("PRAGMA journal_mode=WAL")
    return con

# --- MIME detection ---

def detect_mime(path: Path) -> str:
    try:
        import magic
        return magic.from_file(str(path), mime=True)
    except Exception:
        mime, _ = mimetypes.guess_type(str(path))
        return mime or "application/octet-stream"

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

def extract_text(path: Path, mime: str) -> Optional[str]:
    if mime.startswith("text/"):
        return path.read_text(errors="replace")
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
    return None

def extract_exif(path: Path) -> dict:
    """Extract EXIF/file metadata via exiftool as flat key:value dict."""
    try:
        result = subprocess.run(
            ["exiftool", "-json", "-fast", str(path)],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(result.stdout)
        if data:
            # Drop noisy/path fields
            skip = {"SourceFile", "ExifToolVersion", "Directory", "FileName",
                    "FilePermissions", "FileAccessDate", "FileInodeChangeDate"}
            return {k: str(v) for k, v in data[0].items() if k not in skip}
    except Exception:
        pass
    return {}

# --- Indexing ---

def checksum(path: Path) -> str:
    h = hashlib.blake2b(digest_size=8)
    h.update(path.read_bytes())
    return h.hexdigest()

def needs_reindex(con: sqlite3.Connection, path: Path, force: bool) -> bool:
    if force:
        return True
    stat = path.stat()
    row = con.execute("SELECT mtime FROM files WHERE path=?", (str(path),)).fetchone()
    return row is None or row["mtime"] != stat.st_mtime

def index_file(con: sqlite3.Connection, path: Path, force: bool = False):
    if not needs_reindex(con, path, force):
        return

    stat = path.stat()
    mime = detect_mime(path)
    csum = checksum(path)
    now = time.time()

    con.execute("""
        INSERT OR REPLACE INTO files(path, mtime, size, mime, checksum, indexed_at)
        VALUES (?,?,?,?,?,?)
    """, (str(path), stat.st_mtime, stat.st_size, mime, csum, now))

    # Content
    text = extract_text(path, mime)
    con.execute("DELETE FROM content WHERE path=?", (str(path),))
    if text:
        con.execute("INSERT INTO content(path, text) VALUES (?,?)", (str(path), text))

    # EXIF / extracted metadata
    con.execute("DELETE FROM metadata WHERE path=? AND source != 'user'", (str(path),))
    for k, v in extract_exif(path).items():
        con.execute("""
            INSERT OR REPLACE INTO metadata(path, key, value, source)
            VALUES (?,?,?,'exif')
        """, (str(path), k, v))

def index_tree(root: Path, fados_dir: Path, force: bool = False):
    con = db_connect(fados_dir)
    IGNORE = IGNORE_DIRS
    count = 0
    errors = 0
    t0 = time.time()
    for path in root.rglob("*"):
        if any(part in IGNORE for part in path.parts):
            continue
        if not path.is_file():
            continue
        try:
            index_file(con, path, force)
            count += 1
            if count % 500 == 0:
                con.commit()
                elapsed = time.time() - t0
                print(f"  indexing: {count} files ({elapsed:.0f}s)...",
                      file=sys.stderr, flush=True)
        except Exception as e:
            errors += 1
            print(f"  error: {path}: {e}", file=sys.stderr)
    con.commit()
    con.close()
    elapsed = time.time() - t0
    msg = f"indexed {count} files in {elapsed:.1f}s"
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

def search(terms: str, fados_dir: Path):
    return query(
        "SELECT path, snippet(content, 1, '[', ']', '...', 20) AS snippet "
        "FROM content WHERE text MATCH ? ORDER BY rank",
        fados_dir,
        (terms,)
    )

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

def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model

def _embed(text: str) -> bytes:
    import numpy as np
    vec = _get_model().encode(text[:8192], normalize_embeddings=True)
    return vec.astype(np.float32).tobytes()

def _store_embedding(con: sqlite3.Connection, path: Path, text: str):
    vec_bytes = _embed(text)
    con.execute(
        "INSERT OR REPLACE INTO embeddings(path, vector) VALUES (?,?)",
        (str(path), vec_bytes)
    )

def embed_tree(root: Path, fados_dir: Path):
    """Generate / refresh embeddings for all indexed text content under root."""
    import numpy as np
    con = db_connect(fados_dir)
    rows = con.execute(
        "SELECT c.path, c.text FROM content c "
        "JOIN files f ON f.path = c.path "
        "WHERE f.path LIKE ?",
        (str(root.resolve()) + "%",)
    ).fetchall()
    total = len(rows)
    print(f"embedding {total} files...", file=sys.stderr)
    errors = 0
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        try:
            _store_embedding(con, Path(row["path"]), row["text"])
            if i % 50 == 0:
                con.commit()
                elapsed = time.time() - t0
                print(f"  embedding: {i}/{total} ({elapsed:.0f}s)...",
                      file=sys.stderr, flush=True)
        except Exception as e:
            errors += 1
            print(f"  error {row['path']}: {e}", file=sys.stderr)
    con.commit()
    con.close()
    elapsed = time.time() - t0
    msg = f"embedded {total - errors} files in {elapsed:.1f}s"
    if errors:
        msg += f" ({errors} errors)"
    print(msg, file=sys.stderr)

def semantic(query_text: str, fados_dir: Path, n: int = 20) -> list:
    """Semantic search using cosine similarity over stored embeddings."""
    import numpy as np
    q_vec = np.frombuffer(_embed(query_text), dtype=np.float32)
    con = db_connect(fados_dir)
    rows = con.execute("SELECT path, vector FROM embeddings").fetchall()
    con.close()
    if not rows:
        return []
    scores = []
    for row in rows:
        v = np.frombuffer(row["vector"], dtype=np.float32)
        scores.append((float(np.dot(q_vec, v)), row["path"]))
    scores.sort(reverse=True)
    return [{"path": p, "score": round(s, 4)} for s, p in scores[:n]]

def similar(path: str, fados_dir: Path, n: int = 10) -> list:
    """Find files with content similar to the given path."""
    import numpy as np
    con = db_connect(fados_dir)
    row = con.execute("SELECT vector FROM embeddings WHERE path=?", (path,)).fetchone()
    if not row:
        con.close()
        return []
    q_vec = np.frombuffer(row["vector"], dtype=np.float32)
    rows = con.execute(
        "SELECT path, vector FROM embeddings WHERE path!=?", (path,)
    ).fetchall()
    con.close()
    scores = [
        (float(np.dot(q_vec, np.frombuffer(r["vector"], dtype=np.float32))), r["path"])
        for r in rows
    ]
    scores.sort(reverse=True)
    return [{"path": p, "score": round(s, 4)} for s, p in scores[:n]]

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
        results = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(':', 2)
            if len(parts) >= 3:
                results.append({
                    "path": parts[0],
                    "line": int(parts[1]),
                    "match": parts[2].strip()
                })
                if len(results) >= max_results:
                    break
        return results
    except Exception:
        return []


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
    con.close()
    return {
        "file": dict(file_row) if file_row else None,
        "metadata": [dict(r) for r in meta],
        "tags": [dict(r) for r in tags_],
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
                index_file(con, path, force=True)
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
    """Resolved --dir/--user into fados_dir and root for all subcommands."""
    def __init__(self, fados_dir: Path):
        self.fados_dir = fados_dir
        self.root = fados_dir.parent

    def auto_index_if_needed(self):
        if _needs_auto_index(self.fados_dir):
            _auto_index(self.fados_dir)


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
    """Build Context, letting a subcommand's positional path override --dir."""
    dir_override = path_override or ctx.obj["dir"]
    fados_dir = resolve_fados_dir(user=ctx.obj["user"], dir_override=dir_override)
    return Context(fados_dir)


# --- Index commands (optional positional path, defaults to --dir / CWD) ---

@cli.command()
@click.argument("path", required=False, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--embed", "do_embed", is_flag=True, help="Also generate embeddings.")
@click.pass_context
def reindex(ctx, path, do_embed):
    """Force a full reindex of the tree."""
    c = _resolve_ctx(ctx, path)
    index_tree(c.root, c.fados_dir, force=True)
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
@click.pass_context
def search_cmd(ctx, terms):
    """Full-text keyword search (FTS5)."""
    c = _resolve_ctx(ctx)
    c.auto_index_if_needed()
    results = search(" ".join(terms), c.fados_dir)
    hint = ""
    if not results and _table_count(c.fados_dir, "content") == 0:
        n_files = _table_count(c.fados_dir, "files")
        if n_files == 0:
            hint = "index is empty; run: fados --dir <path> reindex"
        else:
            hint = f"{n_files} files indexed but no text content extracted"
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
            row = con.execute("SELECT 1 FROM embeddings WHERE path=?", (resolved,)).fetchone()
            con.close()
            if not row:
                hint = f"no embedding for '{file}' — file may not be indexed"
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

