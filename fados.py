#!/usr/bin/env python3
# /// script
# dependencies = ["python-magic", "rich"]
# ///
"""
FADOS - Filesystem As Database Overlay System
Single-file prototype. Run with: uv run fados.py <command> [args]

Commands:
  index <path>              index a directory tree
  reindex <path>            force full reindex
  query <sql>               raw SQL against the index
  search <terms>            full-text search
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
        return run(["pdftotext", "-"], path)  # pdftotext reads from arg, writes to stdout with -
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

    p = sub.add_parser("index");    p.add_argument("path")
    p = sub.add_parser("reindex");  p.add_argument("path")
    p = sub.add_parser("query");    p.add_argument("sql")
    p = sub.add_parser("search");   p.add_argument("terms", nargs="+")
    p = sub.add_parser("find");     p.add_argument("key"); p.add_argument("value")
    p = sub.add_parser("tag");      p.add_argument("path"); p.add_argument("tag")
    p = sub.add_parser("annotate"); p.add_argument("path"); p.add_argument("key"); p.add_argument("value")
    p = sub.add_parser("info");     p.add_argument("path")
    p = sub.add_parser("watch");    p.add_argument("path")

    args = ap.parse_args()

    match args.cmd:
        case "index":
            index_tree(Path(args.path))
        case "reindex":
            index_tree(Path(args.path), force=True)
        case "query":
            print_results(query(args.sql))
        case "search":
            print_results(search(" ".join(args.terms)))
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

