#!/usr/bin/env python3
"""Lightweight MCP server for an Obsidian-backed AI knowledge base.

Markdown files remain the source of truth. SQLite stores only a rebuildable
search index, metadata and embeddings from an OpenAI-compatible endpoint.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP


SCHEMA_VERSION = 2
RRF_K = 60


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


HOME = Path.home()
VAULT = Path(os.environ.get("OBSIDIAN_AI_PATH", HOME / "Documents/obsidian/zyk的obsidian仓库/AI")).expanduser().resolve()
DB_PATH = Path(
    os.environ.get("OBSIDIAN_AI_INDEX", HOME / ".local/share/obsidian-ai-kb/index.sqlite3")
).expanduser()
EMBED_BASE_URL = os.environ.get("OBSIDIAN_AI_EMBED_BASE_URL", "http://192.168.2.100:1234/v1").rstrip("/")
EMBED_MODEL = os.environ.get("OBSIDIAN_AI_EMBED_MODEL", "text-embedding-nomic-embed-text-v2-moe")
EMBED_API_KEY = os.environ.get("OBSIDIAN_AI_EMBED_API_KEY", "")
EMBED_TIMEOUT = float(os.environ.get("OBSIDIAN_AI_EMBED_TIMEOUT", "30"))
EMBED_BATCH_SIZE = max(1, min(int(os.environ.get("OBSIDIAN_AI_EMBED_BATCH_SIZE", "32")), 256))
QUERY_PREFIX = os.environ.get("OBSIDIAN_AI_EMBED_QUERY_PREFIX", "search_query: ")
DOCUMENT_PREFIX = os.environ.get("OBSIDIAN_AI_EMBED_DOCUMENT_PREFIX", "search_document: ")
TIMEZONE = os.environ.get("OBSIDIAN_AI_TIMEZONE", "Asia/Shanghai")
SYNC_INTERVAL = max(0.0, float(os.environ.get("OBSIDIAN_AI_SYNC_INTERVAL", "2")))
MAX_CHUNK_CHARS = max(600, int(os.environ.get("OBSIDIAN_AI_CHUNK_CHARS", "1600")))
CHUNK_OVERLAP_CHARS = max(0, min(int(os.environ.get("OBSIDIAN_AI_CHUNK_OVERLAP", "180")), MAX_CHUNK_CHARS // 2))
ALLOW_SENSITIVE = _env_bool("OBSIDIAN_AI_ALLOW_SENSITIVE", False)
EXCLUDE_GLOBS = _env_list(
    "OBSIDIAN_AI_EXCLUDE",
    ".obsidian/**,.trash/**,**/.git/**,**/.DS_Store",
)
SENSITIVE_GLOBS = _env_list(
    "OBSIDIAN_AI_SENSITIVE",
    "敏感信息.md,**/敏感信息.md,secrets.md,**/secrets.md",
)

INSTRUCTIONS = """Use this server as the shared Obsidian AI knowledge base.
Search before answering questions that may depend on durable local context.
Prefer kb_search results with precise note paths and line ranges, then kb_get when full context is needed.
Treat old knowledge as historical when answering current-state questions and verify live facts separately.
Use kb_create_note, kb_update_note, or kb_append_inbox for durable writes.
Sensitive notes are excluded by default and must never be copied into chat or ordinary notes."""

mcp = FastMCP("obsidian-ai-kb", instructions=INSTRUCTIONS, log_level="ERROR")
_SYNC_LOCK = threading.Lock()
_LAST_SYNC_MONOTONIC = 0.0
_LAST_SYNC_RESULT: dict[str, Any] | None = None


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS notes (
            path TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL,
            body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            note_path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            heading TEXT NOT NULL,
            content TEXT NOT NULL,
            line_start INTEGER NOT NULL DEFAULT 1,
            line_end INTEGER NOT NULL DEFAULT 1,
            vector BLOB,
            dimensions INTEGER,
            UNIQUE(note_path, chunk_index),
            FOREIGN KEY(note_path) REFERENCES notes(path) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_note ON chunks(note_path);
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    _ensure_column(conn, "notes", "size", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "chunks", "line_start", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "chunks", "line_end", "INTEGER NOT NULL DEFAULT 1")
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                note_path UNINDEXED,
                chunk_index UNINDEXED,
                title,
                heading,
                content,
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
    except sqlite3.OperationalError:
        pass
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _fts_available(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    return row is not None


def _relative(path: Path) -> str:
    return path.relative_to(VAULT).as_posix()


def _matches_glob(relative: str, patterns: Iterable[str]) -> bool:
    posix = PurePosixPath(relative)
    return any(posix.match(pattern) or fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def _is_sensitive(relative: str) -> bool:
    return _matches_glob(relative, SENSITIVE_GLOBS)


def _is_excluded(relative: str) -> bool:
    return _matches_glob(relative, EXCLUDE_GLOBS) or (_is_sensitive(relative) and not ALLOW_SENSITIVE)


def _iter_markdown_files() -> list[Path]:
    if not VAULT.is_dir():
        raise RuntimeError(f"知识库目录不存在: {VAULT}")
    files: list[Path] = []
    for path in VAULT.rglob("*.md"):
        if not _is_excluded(_relative(path)):
            files.append(path)
    return sorted(files)


def _parse_note(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _title(path: Path, body: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", body)
    return match.group(1).strip() if match else path.stem


def _extract_update_time(body: str) -> str | None:
    for line in reversed(body.splitlines()[-16:]):
        stripped = line.strip()
        if stripped.startswith("更新时间："):
            return stripped.removeprefix("更新时间：").strip()
    return None


def _metadata(body: str) -> dict[str, Any]:
    links: list[str] = []
    for match in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", body):
        target = match.group(1).strip()
        if target and target not in links:
            links.append(target)
    tags = sorted(set(re.findall(r"(?<![\w#])#([\w\-/\u4e00-\u9fff]+)", body)))
    return {"links": links, "tags": tags, "updated": _extract_update_time(body)}


def _chunks(title: str, body: str) -> list[dict[str, Any]]:
    lines = body.splitlines()
    if not lines:
        return [{"heading": title, "content": f"标题: {title}", "line_start": 1, "line_end": 1}]

    section_starts = [i for i, line in enumerate(lines) if re.match(r"^#{1,4}\s+", line)]
    if not section_starts or section_starts[0] != 0:
        section_starts.insert(0, 0)
    section_starts.append(len(lines))

    result: list[dict[str, Any]] = []
    prefix = f"标题: {title}\n"
    for section_no in range(len(section_starts) - 1):
        start = section_starts[section_no]
        end = section_starts[section_no + 1]
        section = lines[start:end]
        if not any(line.strip() for line in section):
            continue
        heading_match = re.match(r"^#{1,4}\s+(.+?)\s*$", section[0]) if section else None
        heading = heading_match.group(1).strip() if heading_match else title

        cursor = 0
        while cursor < len(section):
            chunk_lines: list[str] = []
            char_count = len(prefix)
            end_cursor = cursor
            while end_cursor < len(section):
                line = section[end_cursor]
                extra = len(line) + 1
                if chunk_lines and char_count + extra > MAX_CHUNK_CHARS:
                    break
                chunk_lines.append(line)
                char_count += extra
                end_cursor += 1
            if not chunk_lines:
                chunk_lines.append(section[cursor][: MAX_CHUNK_CHARS - len(prefix)])
                end_cursor = cursor + 1
            content = prefix + "\n".join(chunk_lines).strip()
            result.append(
                {
                    "heading": heading,
                    "content": content.strip(),
                    "line_start": start + cursor + 1,
                    "line_end": start + end_cursor,
                }
            )
            if end_cursor >= len(section):
                break
            overlap_chars = 0
            overlap_lines = 0
            for line in reversed(chunk_lines):
                if overlap_chars + len(line) + 1 > CHUNK_OVERLAP_CHARS:
                    break
                overlap_chars += len(line) + 1
                overlap_lines += 1
            cursor = max(cursor + 1, end_cursor - overlap_lines)
    return result or [{"heading": title, "content": prefix + body.strip(), "line_start": 1, "line_end": len(lines)}]


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes, dimensions: int) -> tuple[float, ...]:
    return struct.unpack(f"<{dimensions}f", blob)


def _embed(texts: list[str], query: bool = False) -> list[list[float]]:
    if not texts:
        return []
    prefix = QUERY_PREFIX if query else DOCUMENT_PREFIX
    payload = json.dumps(
        {"model": EMBED_MODEL, "input": [prefix + text for text in texts]}, ensure_ascii=False
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if EMBED_API_KEY:
        headers["Authorization"] = f"Bearer {EMBED_API_KEY}"
    request = urllib.request.Request(
        f"{EMBED_BASE_URL}/embeddings", data=payload, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=EMBED_TIMEOUT) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"embedding 服务不可用: {exc}") from exc
    if "error" in result:
        raise RuntimeError(f"embedding 服务错误: {result['error']}")
    rows = sorted(result.get("data", []), key=lambda row: row.get("index", 0))
    vectors = [row.get("embedding", []) for row in rows]
    if len(vectors) != len(texts) or any(not vector for vector in vectors):
        raise RuntimeError("embedding 服务返回数量或内容异常")
    return [_normalize(vector) for vector in vectors]


def _replace_fts_for_note(
    conn: sqlite3.Connection, note_path: str, title: str, chunk_rows: list[dict[str, Any]]
) -> None:
    if not _fts_available(conn):
        return
    conn.execute("DELETE FROM chunks_fts WHERE note_path=?", (note_path,))
    conn.executemany(
        "INSERT INTO chunks_fts(note_path,chunk_index,title,heading,content) VALUES(?,?,?,?,?)",
        [
            (note_path, idx, title, chunk["heading"], chunk["content"])
            for idx, chunk in enumerate(chunk_rows)
        ],
    )


def _backfill_missing_vectors(conn: sqlite3.Connection, failures: list[str]) -> int:
    rows = conn.execute(
        "SELECT id,content FROM chunks WHERE vector IS NULL ORDER BY id"
    ).fetchall()
    embedded = 0
    for start in range(0, len(rows), EMBED_BATCH_SIZE):
        batch = rows[start : start + EMBED_BATCH_SIZE]
        try:
            vectors = _embed([row["content"] for row in batch])
        except RuntimeError as exc:
            failures.append(str(exc))
            break
        for row, vector in zip(batch, vectors):
            conn.execute(
                "UPDATE chunks SET vector=?,dimensions=? WHERE id=?",
                (_pack(vector), len(vector), row["id"]),
            )
            embedded += 1
    return embedded


def _sync(force: bool = False) -> dict[str, Any]:
    files = _iter_markdown_files()
    current_paths = {_relative(path) for path in files}
    indexed = updated = removed = 0
    failures: list[str] = []

    with _connect() as conn:
        previous_model = conn.execute(
            "SELECT value FROM settings WHERE key='embed_model'"
        ).fetchone()
        if previous_model and previous_model["value"] != EMBED_MODEL:
            conn.execute("UPDATE chunks SET vector=NULL,dimensions=NULL")

        old_paths = {row["path"] for row in conn.execute("SELECT path FROM notes")}
        for stale in old_paths - current_paths:
            conn.execute("DELETE FROM notes WHERE path=?", (stale,))
            if _fts_available(conn):
                conn.execute("DELETE FROM chunks_fts WHERE note_path=?", (stale,))
            removed += 1

        for path in files:
            relative = _relative(path)
            stat = path.stat()
            existing = conn.execute(
                "SELECT sha256,mtime_ns,size FROM notes WHERE path=?", (relative,)
            ).fetchone()
            if (
                not force
                and existing
                and existing["mtime_ns"] == stat.st_mtime_ns
                and existing["size"] == stat.st_size
            ):
                indexed += 1
                continue

            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if not force and existing and existing["sha256"] == digest:
                conn.execute(
                    "UPDATE notes SET mtime_ns=?,size=? WHERE path=?",
                    (stat.st_mtime_ns, stat.st_size, relative),
                )
                indexed += 1
                continue

            body = raw.decode("utf-8", errors="replace")
            title = _title(path, body)
            metadata = _metadata(body)
            chunk_rows = _chunks(title, body)
            conn.execute(
                """
                INSERT INTO notes(path,title,sha256,mtime_ns,size,metadata_json,body)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                  title=excluded.title,sha256=excluded.sha256,mtime_ns=excluded.mtime_ns,
                  size=excluded.size,metadata_json=excluded.metadata_json,body=excluded.body
                """,
                (
                    relative,
                    title,
                    digest,
                    stat.st_mtime_ns,
                    stat.st_size,
                    json.dumps(metadata, ensure_ascii=False),
                    body,
                ),
            )
            conn.execute("DELETE FROM chunks WHERE note_path=?", (relative,))
            conn.executemany(
                """
                INSERT INTO chunks(note_path,chunk_index,heading,content,line_start,line_end)
                VALUES(?,?,?,?,?,?)
                """,
                [
                    (
                        relative,
                        idx,
                        chunk["heading"],
                        chunk["content"],
                        chunk["line_start"],
                        chunk["line_end"],
                    )
                    for idx, chunk in enumerate(chunk_rows)
                ],
            )
            _replace_fts_for_note(conn, relative, title, chunk_rows)
            updated += 1
            indexed += 1

        embedded_chunks = _backfill_missing_vectors(conn, failures)
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('embed_model',?)", (EMBED_MODEL,)
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('embed_base_url',?)", (EMBED_BASE_URL,)
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('last_sync',?)", (_now_stamp(),)
        )
        total_chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        vector_chunks = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE vector IS NOT NULL"
        ).fetchone()["n"]
        fts = _fts_available(conn)

    return {
        "notes": indexed,
        "updated": updated,
        "removed": removed,
        "chunks": total_chunks,
        "embedded_chunks": vector_chunks,
        "new_embeddings": embedded_chunks,
        "fts": fts,
        "failures": failures,
    }


def _sync_now(force: bool = False) -> dict[str, Any]:
    global _LAST_SYNC_MONOTONIC, _LAST_SYNC_RESULT
    with _SYNC_LOCK:
        result = _sync(force=force)
        _LAST_SYNC_RESULT = result
        _LAST_SYNC_MONOTONIC = time.monotonic()
        return {**result, "cached": False}


def _sync_if_due(force: bool = False) -> dict[str, Any]:
    global _LAST_SYNC_MONOTONIC, _LAST_SYNC_RESULT
    now = time.monotonic()
    if not force and _LAST_SYNC_RESULT is not None and now - _LAST_SYNC_MONOTONIC < SYNC_INTERVAL:
        return {**_LAST_SYNC_RESULT, "cached": True}
    with _SYNC_LOCK:
        now = time.monotonic()
        if not force and _LAST_SYNC_RESULT is not None and now - _LAST_SYNC_MONOTONIC < SYNC_INTERVAL:
            return {**_LAST_SYNC_RESULT, "cached": True}
        result = _sync(force=force)
        _LAST_SYNC_RESULT = result
        _LAST_SYNC_MONOTONIC = time.monotonic()
        return {**result, "cached": False}


def _tokenize_query(query: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9_.:/-]+|[\u4e00-\u9fff]{2,}", query.lower())


def _lexical_score(query: str, text: str) -> float:
    query_lower = query.lower().strip()
    text_lower = text.lower()
    if not query_lower:
        return 0.0
    terms = _tokenize_query(query_lower)
    score = 1.0 if query_lower in text_lower else 0.0
    score += sum(0.2 for term in terms if term in text_lower)
    return min(score, 2.0)


def _fts_query(query: str) -> str:
    terms = _tokenize_query(query)
    escaped = [term.replace('"', '""') for term in terms if term]
    return " OR ".join(f'"{term}"' for term in escaped[:24])


def _semantic_rankings(conn: sqlite3.Connection, query: str, candidate_limit: int) -> tuple[list[dict[str, Any]], str | None]:
    try:
        query_vector = _embed([query], query=True)[0]
    except RuntimeError as exc:
        return [], str(exc)
    rows = conn.execute(
        """
        SELECT c.id,c.note_path,c.chunk_index,c.heading,c.content,c.line_start,c.line_end,
               c.vector,c.dimensions,n.title,n.metadata_json
        FROM chunks c JOIN notes n ON n.path=c.note_path
        WHERE c.vector IS NOT NULL
        """
    ).fetchall()
    ranked: list[dict[str, Any]] = []
    for row in rows:
        if row["dimensions"] != len(query_vector):
            continue
        semantic = sum(
            left * right
            for left, right in zip(query_vector, _unpack(row["vector"], row["dimensions"]))
        )
        ranked.append(_row_to_result(row, semantic_score=semantic))
    ranked.sort(key=lambda item: item["semantic_score"], reverse=True)
    return ranked[:candidate_limit], None


def _keyword_rankings(conn: sqlite3.Connection, query: str, candidate_limit: int) -> list[dict[str, Any]]:
    match_query = _fts_query(query)
    if match_query and _fts_available(conn):
        try:
            rows = conn.execute(
                """
                SELECT f.note_path,f.chunk_index,bm25(chunks_fts,0.0,0.0,2.0,1.4,1.0) AS bm25_score,
                       c.id,c.heading,c.content,c.line_start,c.line_end,n.title,n.metadata_json
                FROM chunks_fts f
                JOIN chunks c ON c.note_path=f.note_path AND c.chunk_index=f.chunk_index
                JOIN notes n ON n.path=f.note_path
                WHERE chunks_fts MATCH ?
                ORDER BY bm25_score ASC
                LIMIT ?
                """,
                (match_query, candidate_limit),
            ).fetchall()
            if rows:
                return [
                    _row_to_result(row, keyword_score=1.0 / (1.0 + abs(float(row["bm25_score"]))))
                    for row in rows
                ]
        except sqlite3.OperationalError:
            pass

    rows = conn.execute(
        """
        SELECT c.id,c.note_path,c.chunk_index,c.heading,c.content,c.line_start,c.line_end,
               n.title,n.metadata_json
        FROM chunks c JOIN notes n ON n.path=c.note_path
        """
    ).fetchall()
    ranked = [
        _row_to_result(
            row,
            keyword_score=_lexical_score(
                query, f"{row['title']}\n{row['heading']}\n{row['content']}"
            ),
        )
        for row in rows
    ]
    ranked = [item for item in ranked if item["keyword_score"] > 0]
    ranked.sort(key=lambda item: item["keyword_score"], reverse=True)
    return ranked[:candidate_limit]


def _row_to_result(
    row: sqlite3.Row, semantic_score: float = 0.0, keyword_score: float = 0.0
) -> dict[str, Any]:
    metadata = json.loads(row["metadata_json"] or "{}")
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "path": row["note_path"],
        "chunk_index": int(row["chunk_index"]),
        "heading": row["heading"],
        "line_start": int(row["line_start"]),
        "line_end": int(row["line_end"]),
        "excerpt": row["content"][:1200],
        "updated": metadata.get("updated"),
        "tags": metadata.get("tags", []),
        "semantic_score": round(float(semantic_score), 4),
        "keyword_score": round(float(keyword_score), 4),
    }


def _title_bonus(query: str, item: dict[str, Any]) -> float:
    query_lower = query.lower()
    title = item["title"].lower()
    stem = Path(item["path"]).stem.lower()
    wiki_targets = {
        match.group(1).split("#", 1)[0].strip().lower()
        for match in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", query)
    }
    if title in wiki_targets or stem in wiki_targets:
        return 0.04
    if title and title in query_lower:
        return 0.02
    return 0.0


def _fuse_results(
    query: str,
    semantic: list[dict[str, Any]],
    keyword: list[dict[str, Any]],
    mode: str,
) -> list[dict[str, Any]]:
    merged: dict[int, dict[str, Any]] = {}
    if mode in {"hybrid", "semantic"}:
        for rank, item in enumerate(semantic, start=1):
            record = merged.setdefault(item["id"], dict(item))
            record["semantic_score"] = item["semantic_score"]
            record["semantic_rank"] = rank
            record["score"] = record.get("score", 0.0) + 1.0 / (RRF_K + rank)
    if mode in {"hybrid", "keyword"}:
        for rank, item in enumerate(keyword, start=1):
            record = merged.setdefault(item["id"], dict(item))
            record["keyword_score"] = item["keyword_score"]
            record["keyword_rank"] = rank
            record["score"] = record.get("score", 0.0) + 1.0 / (RRF_K + rank)
    for item in merged.values():
        item.setdefault("semantic_rank", None)
        item.setdefault("keyword_rank", None)
        item.setdefault("semantic_score", 0.0)
        item.setdefault("keyword_score", 0.0)
        item["score"] = round(item.get("score", 0.0) + _title_bonus(query, item), 6)
    return sorted(merged.values(), key=lambda item: item["score"], reverse=True)


def _safe_title(title: str) -> str:
    cleaned = re.sub(r"[/\\:\x00-\x1f]", "-", title).strip(" .")
    if not cleaned or cleaned.startswith("."):
        raise ValueError("标题无效")
    return cleaned[:120]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".kb-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _now() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def _now_stamp() -> str:
    return _now().strftime("%Y-%m-%d %H:%M") + f" {TIMEZONE}"


def _strip_update_footer(text: str) -> str:
    lines = text.rstrip().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].startswith("更新时间："):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip() == "## 更新时间":
            lines.pop()
    return "\n".join(lines).rstrip()


def _with_update_footer(text: str) -> str:
    return f"{_strip_update_footer(text)}\n\n## 更新时间\n\n更新时间：{_now_stamp()}\n"


def _parse_update_time(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.replace("Asia/Shanghai", "+0800").replace("CST", "+0800")
    for fmt in ("%Y-%m-%d %H:%M %z", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(TIMEZONE))
        except ValueError:
            continue
    return None


def _resolve_note(note: str) -> sqlite3.Row | None:
    wanted = note.removesuffix(".md").lower().strip()
    with _connect() as conn:
        rows = conn.execute("SELECT path,title,sha256,metadata_json,body FROM notes").fetchall()
    exact = [
        row
        for row in rows
        if wanted
        in {
            row["title"].lower(),
            Path(row["path"]).stem.lower(),
            row["path"].removesuffix(".md").lower(),
        }
    ]
    return exact[0] if exact else None


@mcp.tool()
def kb_status() -> dict[str, Any]:
    """Show vault, index, search and embedding health."""
    sync = _sync_if_due()
    with _connect() as conn:
        chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        vectors = conn.execute("SELECT COUNT(*) AS n FROM chunks WHERE vector IS NOT NULL").fetchone()["n"]
        dimensions = conn.execute("SELECT MAX(dimensions) AS n FROM chunks").fetchone()["n"]
    return {
        "vault": str(VAULT),
        "index": str(DB_PATH),
        "schema_version": SCHEMA_VERSION,
        "embedding_base_url": EMBED_BASE_URL,
        "embedding_model": EMBED_MODEL,
        "chunks": chunks,
        "embedded_chunks": vectors,
        "dimensions": dimensions,
        "sync_interval_seconds": SYNC_INTERVAL,
        "sensitive_notes_allowed": ALLOW_SENSITIVE,
        "exclude_globs": EXCLUDE_GLOBS,
        "sensitive_globs": SENSITIVE_GLOBS,
        "sync": sync,
    }


@mcp.tool()
def kb_search(
    query: str,
    limit: int = 6,
    mode: str = "hybrid",
    max_chunks_per_note: int = 2,
) -> dict[str, Any]:
    """Search notes with hybrid RRF, semantic-only, or keyword-only retrieval."""
    if not query.strip():
        return {"error": "query 不能为空", "results": []}
    _sync_if_due()
    limit = max(1, min(int(limit), 30))
    max_chunks_per_note = max(1, min(int(max_chunks_per_note), 8))
    mode = mode.lower().strip()
    if mode not in {"hybrid", "semantic", "keyword"}:
        mode = "hybrid"
    candidate_limit = max(40, limit * 10)
    with _connect() as conn:
        semantic: list[dict[str, Any]] = []
        embedding_error = None
        if mode in {"hybrid", "semantic"}:
            semantic, embedding_error = _semantic_rankings(conn, query, candidate_limit)
        keyword = _keyword_rankings(conn, query, candidate_limit) if mode in {"hybrid", "keyword"} else []
    effective_mode = mode
    if mode == "hybrid" and not semantic:
        effective_mode = "keyword"
    fused = _fuse_results(query, semantic, keyword, effective_mode)
    chosen: list[dict[str, Any]] = []
    per_note: dict[str, int] = {}
    for item in fused:
        count = per_note.get(item["path"], 0)
        if count >= max_chunks_per_note:
            continue
        per_note[item["path"]] = count + 1
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return {
        "query": query,
        "mode": effective_mode,
        "requested_mode": mode,
        "embedding_error": embedding_error,
        "results": chosen,
    }


@mcp.tool()
def kb_get(note: str) -> dict[str, Any]:
    """Read one exact note by title, filename, or relative path."""
    _sync_if_due()
    row = _resolve_note(note)
    if row is None:
        return {"error": f"未找到笔记: {note}"}
    if _is_sensitive(row["path"]) and not ALLOW_SENSITIVE:
        return {"error": "该笔记属于敏感区，默认禁止 Agent 读取"}
    metadata = json.loads(row["metadata_json"] or "{}")
    return {
        "title": row["title"],
        "path": row["path"],
        "sha256": row["sha256"],
        "updated": metadata.get("updated"),
        "tags": metadata.get("tags", []),
        "links": metadata.get("links", []),
        "body": row["body"],
    }


@mcp.tool()
def kb_list(limit: int = 500) -> dict[str, Any]:
    """List indexed notes with lightweight metadata."""
    _sync_if_due()
    limit = max(1, min(int(limit), 2000))
    result: list[dict[str, Any]] = []
    with _connect() as conn:
        rows = conn.execute("SELECT path,title,metadata_json FROM notes ORDER BY title LIMIT ?", (limit,)).fetchall()
    for row in rows:
        metadata = json.loads(row["metadata_json"] or "{}")
        result.append(
            {
                "title": row["title"],
                "path": row["path"],
                "updated": metadata.get("updated"),
                "tags": metadata.get("tags", []),
            }
        )
    return {"count": len(result), "notes": result}


@mcp.tool()
def kb_time_list(order: str = "oldest", limit: int = 100) -> dict[str, Any]:
    """List notes by their Markdown footer update time for cleanup and review."""
    _sync_if_due()
    order = order.lower().strip()
    if order not in {"oldest", "newest"}:
        order = "oldest"
    limit = max(1, min(int(limit), 500))
    now = _now()
    rows: list[dict[str, Any]] = []
    with _connect() as conn:
        notes = conn.execute("SELECT path,title,metadata_json FROM notes").fetchall()
    for row in notes:
        metadata = json.loads(row["metadata_json"] or "{}")
        updated_text = metadata.get("updated")
        parsed = _parse_update_time(updated_text)
        rows.append(
            {
                "title": row["title"],
                "path": row["path"],
                "updated": updated_text,
                "age_days": max(0, (now - parsed.astimezone(ZoneInfo(TIMEZONE))).days) if parsed else None,
                "has_update_time": updated_text is not None,
                "_sort_key": parsed.timestamp() if parsed else float("inf"),
            }
        )
    rows.sort(key=lambda item: item["_sort_key"], reverse=(order == "newest"))
    for item in rows:
        item.pop("_sort_key", None)
    return {"count": len(rows), "order": order, "timezone": TIMEZONE, "notes": rows[:limit]}


@mcp.tool()
def kb_create_note(title: str, body: str) -> dict[str, Any]:
    """Create a plain Markdown note. Existing notes are never overwritten."""
    safe_title = _safe_title(title)
    relative = f"{safe_title}.md"
    if _is_sensitive(relative) and not ALLOW_SENSITIVE:
        return {"error": "敏感区默认禁止 Agent 创建或写入"}
    path = VAULT / relative
    if path.exists():
        return {"error": f"笔记已存在，不会覆盖: {path.name}"}
    text = body.strip()
    if not re.match(r"^#\s+", text):
        text = f"# {safe_title}\n\n{text}"
    _atomic_write(path, _with_update_footer(text))
    sync = _sync_now(force=False)
    return {"created": relative, "sync": sync}


@mcp.tool()
def kb_update_note(note: str, body: str, expected_sha256: str = "") -> dict[str, Any]:
    """Safely replace an existing note body with optional optimistic locking."""
    _sync_if_due()
    row = _resolve_note(note)
    if row is None:
        return {"error": f"未找到笔记: {note}"}
    if _is_sensitive(row["path"]) and not ALLOW_SENSITIVE:
        return {"error": "敏感区默认禁止 Agent 创建或写入"}
    path = VAULT / row["path"]
    current_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected_sha256 and expected_sha256 != current_sha256:
        return {
            "error": "笔记已发生变化，拒绝覆盖",
            "expected_sha256": expected_sha256,
            "current_sha256": current_sha256,
        }
    text = body.strip()
    if not re.match(r"^#\s+", text):
        text = f"# {row['title']}\n\n{text}"
    _atomic_write(path, _with_update_footer(text))
    sync = _sync_now(force=False)
    new_row = _resolve_note(row["path"])
    return {
        "updated": row["path"],
        "previous_sha256": current_sha256,
        "sha256": new_row["sha256"] if new_row else None,
        "sync": sync,
    }


@mcp.tool()
def kb_append_inbox(content: str, source: str = "agent") -> dict[str, Any]:
    """Append a dated fragment to AI Inbox for later distillation."""
    path = VAULT / "AI Inbox.md"
    if not path.exists():
        return {"error": "AI Inbox.md 不存在"}
    text = path.read_text(encoding="utf-8")
    entry = f"\n\n## {_now().date().isoformat()} · {source}\n\n{content.strip()}\n"
    _atomic_write(path, _with_update_footer(text.rstrip() + entry))
    sync = _sync_now(force=False)
    return {"updated": path.name, "sync": sync}


@mcp.tool()
def kb_reindex(force: bool = False) -> dict[str, Any]:
    """Synchronize immediately, or fully rebuild note/chunk metadata when force=true."""
    return _sync_now(force=bool(force))


if __name__ == "__main__":
    mcp.run(transport="stdio")
