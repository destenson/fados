#!/usr/bin/env python3
# /// script
# dependencies = ["python-magic", "rich", "sentence-transformers", "numpy"]
# ///
"""
FADOS - Filesystem As Database Overlay System
Single-file prototype. Run with: uv run fados.py <command> [args]

Commands:
  index <path> [--embed]    index a directory tree (--embed also generates vector embeddings)
  reindex <path> [--embed]  force full reindex
  embed <path>              generate/refresh semantic embeddings for indexed content
  query <sql>               raw SQL against the index
  search <terms>            full-text keyword search (FTS5)
  semantic <query> [-n N]   semantic/conceptual search using embeddings (default n=20)
  similar <path> [-n N]     find files with similar content (default n=10)
  find <key> <value>        search metadata
  tag <path> <tag>          add a user tag
  annotate <path> <k> <v>   add user metadata
  info <path>               show all indexed info for a file
  watch <path>              watch for changes and reindex (requires inotify-tools)
"""

import sys
import os
import sqlite3
import subprocess
import json
import hashlib
import time
import mimetypes
from pathlib import Path
from typing import Optional
import argparse

# --- Config ---

FADOS_DIR = Path.home() / ".fados"
DB_PATH = FADOS_DIR / "index.db"

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

def db_connect() -> sqlite3.Connection:
    FADOS_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
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

def index_tree(root: Path, force: bool = False):
    con = db_connect()
    IGNORE = {".git", ".fados", "__pycache__", "node_modules", ".venv"}
    count = 0
    errors = 0
    for path in root.rglob("*"):
        if any(part in IGNORE for part in path.parts):
            continue
        if not path.is_file():
            continue
        try:
            index_file(con, path, force)
            count += 1
            if count % 100 == 0:
                con.commit()
                print(f"  indexed {count}...", end="\r", flush=True)
        except Exception as e:
            errors += 1
            print(f"  error: {path}: {e}", file=sys.stderr)
    con.commit()
    con.close()
    print(f"indexed {count} files ({errors} errors)")

# --- Query ---

def query(sql: str, params=()):
    con = db_connect()
    try:
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()

def search(terms: str):
    return query(
        "SELECT path, snippet(content, 1, '[', ']', '...', 20) AS snippet "
        "FROM content WHERE text MATCH ? ORDER BY rank",
        (terms,)
    )

def find_meta(key: str, value: str):
    return query(
        "SELECT f.path, f.mime, m.value FROM files f "
        "JOIN metadata m ON m.path = f.path "
        "WHERE m.key = ? AND m.value LIKE ?",
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

def embed_tree(root: Path):
    """Generate / refresh embeddings for all indexed text content under root."""
    import numpy as np
    con = db_connect()
    rows = con.execute(
        "SELECT c.path, c.text FROM content c "
        "JOIN files f ON f.path = c.path "
        "WHERE f.path LIKE ?",
        (str(root.resolve()) + "%",)
    ).fetchall()
    total = len(rows)
    print(f"embedding {total} files...")
    errors = 0
    for i, row in enumerate(rows, 1):
        try:
            _store_embedding(con, Path(row["path"]), row["text"])
            if i % 10 == 0:
                con.commit()
                print(f"  {i}/{total}", end="\r", flush=True)
        except Exception as e:
            errors += 1
            print(f"  error {row['path']}: {e}", file=sys.stderr)
    con.commit()
    con.close()
    print(f"\nembedded {total - errors} files ({errors} errors)")

def semantic(query_text: str, n: int = 20) -> list:
    """Semantic search using cosine similarity over stored embeddings."""
    import numpy as np
    q_vec = np.frombuffer(_embed(query_text), dtype=np.float32)
    con = db_connect()
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

def similar(path: str, n: int = 10) -> list:
    """Find files with content similar to the given path."""
    import numpy as np
    con = db_connect()
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

# --- Mutations ---

def tag(path: str, tag: str):
    con = db_connect()
    con.execute("INSERT OR REPLACE INTO tags(path, tag) VALUES (?,?)", (path, tag))
    con.commit()
    con.close()

def annotate(path: str, key: str, value: str):
    con = db_connect()
    con.execute("""
        INSERT OR REPLACE INTO metadata(path, key, value, source)
        VALUES (?,?,?,'user')
    """, (path, key, value))
    con.commit()
    con.close()

def info(path: str):
    con = db_connect()
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

def watch(root: Path):
    """Incrementally reindex on filesystem changes. Requires inotify-tools."""
    print(f"watching {root} ...")
    proc = subprocess.Popen(
        ["inotifywait", "-m", "-r", "-e", "close_write,moved_to,create",
         "--format", "%w%f", str(root)],
        stdout=subprocess.PIPE, text=True
    )
    con = db_connect()
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

def print_results(rows: list):
    if not rows:
        print("(no results)")
        return
    for row in rows:
        print(json.dumps(row, default=str))

def main():
    ap = argparse.ArgumentParser(prog="fados", description="Filesystem As Database Overlay System")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("index");    p.add_argument("path"); p.add_argument("--embed", action="store_true", help="also generate semantic embeddings")
    p = sub.add_parser("reindex");  p.add_argument("path"); p.add_argument("--embed", action="store_true")
    p = sub.add_parser("embed");    p.add_argument("path", help="root path whose indexed content to embed")
    p = sub.add_parser("query");    p.add_argument("sql")
    p = sub.add_parser("search");   p.add_argument("terms", nargs="+")
    p = sub.add_parser("semantic"); p.add_argument("query", nargs="+"); p.add_argument("-n", type=int, default=20)
    p = sub.add_parser("similar");  p.add_argument("path"); p.add_argument("-n", type=int, default=10)
    p = sub.add_parser("find");     p.add_argument("key"); p.add_argument("value")
    p = sub.add_parser("tag");      p.add_argument("path"); p.add_argument("tag")
    p = sub.add_parser("annotate"); p.add_argument("path"); p.add_argument("key"); p.add_argument("value")
    p = sub.add_parser("info");     p.add_argument("path")
    p = sub.add_parser("watch");    p.add_argument("path")

    args = ap.parse_args()

    match args.cmd:
        case "index":
            index_tree(Path(args.path))
            if args.embed:
                embed_tree(Path(args.path))
        case "reindex":
            index_tree(Path(args.path), force=True)
            if args.embed:
                embed_tree(Path(args.path))
        case "embed":
            embed_tree(Path(args.path))
        case "query":
            print_results(query(args.sql))
        case "search":
            print_results(search(" ".join(args.terms)))
        case "semantic":
            print_results(semantic(" ".join(args.query), args.n))
        case "similar":
            print_results(similar(args.path, args.n))
        case "find":
            print_results(find_meta(args.key, args.value))
        case "tag":
            tag(args.path, args.tag)
        case "annotate":
            annotate(args.path, args.key, args.value)
        case "info":
            print(json.dumps(info(args.path), indent=2, default=str))
        case "watch":
            watch(Path(args.path))
        case _:
            ap.print_help()

if __name__ == "__main__":
    main()

