#!/usr/bin/env python3
"""Shared Obsidian AI knowledge-base MCP server.

Markdown remains the source of truth. SQLite stores only a rebuildable search
index and normalized embeddings obtained from an OpenAI-compatible endpoint.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

from mcp.server.fastmcp import FastMCP


VAULT = Path(
    os.environ.get(
        "OBSIDIAN_AI_PATH",
        "/Users/zhengyunkai/Documents/obsidian/zyk的obsidian仓库/AI",
    )
).expanduser().resolve()
DB_PATH = Path(
    os.environ.get(
        "OBSIDIAN_AI_INDEX",
        "/Users/zhengyunkai/.local/share/obsidian-ai-kb/index.sqlite3",
    )
).expanduser()
EMBED_BASE_URL = os.environ.get(
    "OBSIDIAN_AI_EMBED_BASE_URL", "http://192.168.2.100:1234/v1"
).rstrip("/")
EMBED_MODEL = os.environ.get(
    "OBSIDIAN_AI_EMBED_MODEL", "text-embedding-nomic-embed-text-v2-moe"
)
EMBED_TIMEOUT = float(os.environ.get("OBSIDIAN_AI_EMBED_TIMEOUT", "60"))
TIMEZONE = os.environ.get("OBSIDIAN_AI_TIMEZONE", "Asia/Shanghai")

INSTRUCTIONS = """Use this server as the shared Obsidian AI knowledge base.
Search before answering questions that may depend on durable local context.
For current-state questions, treat old evidence as historical and verify live.
Use kb_create_note or kb_append_inbox to add knowledge; never store secrets."""

mcp = FastMCP("obsidian-ai-kb", instructions=INSTRUCTIONS, log_level="ERROR")


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS notes (
            path TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL,
            metadata_json TEXT NOT NULL,
            body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            note_path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            heading TEXT NOT NULL,
            content TEXT NOT NULL,
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
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _parse_note(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _title(path: Path, body: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", body)
    return match.group(1).strip() if match else path.stem


def _chunks(title: str, body: str) -> list[tuple[str, str]]:
    prefix = f"标题: {title}\n"
    sections = re.split(r"(?m)(?=^#{1,4}\s+)", body.strip())
    result: list[tuple[str, str]] = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        heading_match = re.match(r"^#{1,4}\s+(.+?)\s*$", section.splitlines()[0])
        heading = heading_match.group(1).strip() if heading_match else title
        text = prefix + section
        while len(text) > 1600:
            cut = text.rfind("\n", 0, 1400)
            if cut < 600:
                cut = 1400
            result.append((heading, text[:cut].strip()))
            text = prefix + text[max(0, cut - 180) :].strip()
        if text.strip():
            result.append((heading, text.strip()))
    return result or [(title, prefix + body.strip())]


def _embed(texts: list[str], query: bool = False) -> list[list[float]]:
    if not texts:
        return []
    prefix = "search_query: " if query else "search_document: "
    payload = json.dumps(
        {"model": EMBED_MODEL, "input": [prefix + text for text in texts]},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{EMBED_BASE_URL}/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=EMBED_TIMEOUT) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"embedding 服务不可用: {exc}") from exc
    if "error" in result:
        raise RuntimeError(f"embedding 服务错误: {result['error']}")
    rows = sorted(result.get("data", []), key=lambda row: row.get("index", 0))
    vectors = [row.get("embedding", []) for row in rows]
    if len(vectors) != len(texts) or any(not vector for vector in vectors):
        raise RuntimeError("embedding 服务返回数量或内容异常")
    return [_normalize(vector) for vector in vectors]


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes, dimensions: int) -> tuple[float, ...]:
    return struct.unpack(f"<{dimensions}f", blob)


def _sync(force: bool = False) -> dict[str, Any]:
    if not VAULT.is_dir():
        raise RuntimeError(f"知识库目录不存在: {VAULT}")
    files = sorted(VAULT.rglob("*.md"))
    current_paths = {str(path.relative_to(VAULT)) for path in files}
    indexed = updated = removed = embedded_chunks = 0
    failures: list[str] = []
    with _connect() as conn:
        previous_model = conn.execute(
            "SELECT value FROM settings WHERE key='embed_model'"
        ).fetchone()
        if previous_model and previous_model["value"] != EMBED_MODEL:
            force = True
        missing_vectors = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE vector IS NULL"
        ).fetchone()["n"]
        if missing_vectors:
            force = True
        old_paths = {row["path"] for row in conn.execute("SELECT path FROM notes")}
        for stale in old_paths - current_paths:
            conn.execute("DELETE FROM notes WHERE path=?", (stale,))
            removed += 1
        pending: list[tuple[str, int, str, str]] = []
        for path in files:
            relative = str(path.relative_to(VAULT))
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            existing = conn.execute(
                "SELECT sha256 FROM notes WHERE path=?", (relative,)
            ).fetchone()
            if not force and existing and existing["sha256"] == digest:
                indexed += 1
                continue
            body = _parse_note(path)
            title = _title(path, body)
            conn.execute(
                """
                INSERT INTO notes(path,title,sha256,mtime_ns,metadata_json,body)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                  title=excluded.title, sha256=excluded.sha256,
                  mtime_ns=excluded.mtime_ns, metadata_json=excluded.metadata_json,
                  body=excluded.body
                """,
                (
                    relative,
                    title,
                    digest,
                    path.stat().st_mtime_ns,
                    "{}",
                    body,
                ),
            )
            conn.execute("DELETE FROM chunks WHERE note_path=?", (relative,))
            for index, (heading, content) in enumerate(
                _chunks(title, body)
            ):
                pending.append((relative, index, heading, content))
            updated += 1
            indexed += 1
        if pending:
            contents = [row[3] for row in pending]
            try:
                vectors: list[list[float] | None] = []
                for start in range(0, len(contents), 32):
                    vectors.extend(_embed(contents[start : start + 32]))
            except RuntimeError as exc:
                failures.append(str(exc))
                vectors = [None] * len(contents)
            for row, vector in zip(pending, vectors):
                conn.execute(
                    """
                    INSERT INTO chunks
                    (note_path,chunk_index,heading,content,vector,dimensions)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        *row,
                        _pack(vector) if vector else None,
                        len(vector) if vector else None,
                    ),
                )
                embedded_chunks += int(vector is not None)
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('embed_model',?)",
            (EMBED_MODEL,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('embed_base_url',?)",
            (EMBED_BASE_URL,),
        )
    return {
        "notes": indexed,
        "updated": updated,
        "removed": removed,
        "embedded_chunks": embedded_chunks,
        "failures": failures,
    }


def _lexical_score(query: str, text: str) -> float:
    query_lower = query.lower().strip()
    text_lower = text.lower()
    if not query_lower:
        return 0.0
    terms = re.findall(r"[a-z0-9_.:/-]+|[\u4e00-\u9fff]{2,}", query_lower)
    score = 1.0 if query_lower in text_lower else 0.0
    score += sum(0.2 for term in terms if term in text_lower)
    return min(score, 2.0)


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


def _now_stamp() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M %Z")


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


def _extract_update_time(body: str) -> str | None:
    for line in reversed(body.splitlines()[-12:]):
        stripped = line.strip()
        if stripped.startswith("更新时间："):
            return stripped.removeprefix("更新时间：").strip()
    return None


def _parse_update_time(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.replace("Asia/Shanghai", "+0800")
    for fmt in ("%Y-%m-%d %H:%M %z", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=ZoneInfo(TIMEZONE))
            return parsed
        except ValueError:
            continue
    return None


@mcp.tool()
def kb_status() -> dict[str, Any]:
    """Show knowledge-base, index, and embedding service status."""
    sync = _sync()
    with _connect() as conn:
        chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        vectors = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE vector IS NOT NULL"
        ).fetchone()["n"]
        dimensions = conn.execute(
            "SELECT MAX(dimensions) AS n FROM chunks"
        ).fetchone()["n"]
    return {
        "vault": str(VAULT),
        "index": str(DB_PATH),
        "embedding_base_url": EMBED_BASE_URL,
        "embedding_model": EMBED_MODEL,
        "chunks": chunks,
        "embedded_chunks": vectors,
        "dimensions": dimensions,
        "sync": sync,
    }


@mcp.tool()
def kb_search(query: str, limit: int = 6) -> dict[str, Any]:
    """Hybrid semantic and keyword search over the shared Obsidian AI notes."""
    _sync()
    limit = max(1, min(int(limit), 20))
    query_vector: list[float] | None
    embedding_error = ""
    try:
        query_vector = _embed([query], query=True)[0]
    except RuntimeError as exc:
        query_vector = None
        embedding_error = str(exc)
    candidates: list[dict[str, Any]] = []
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT c.note_path,c.heading,c.content,c.vector,c.dimensions,
                   n.title,n.metadata_json
            FROM chunks c JOIN notes n ON n.path=c.note_path
            """
        ).fetchall()
        for row in rows:
            semantic = 0.0
            if (
                query_vector
                and row["vector"]
                and row["dimensions"] == len(query_vector)
            ):
                semantic = sum(
                    left * right
                    for left, right in zip(
                        query_vector, _unpack(row["vector"], row["dimensions"])
                    )
                )
            lexical = _lexical_score(
                query, f"{row['title']}\n{row['heading']}\n{row['content']}"
            )
            score = semantic * 0.82 + min(lexical, 1.5) * 0.18
            candidates.append(
                {
                    "score": round(score, 4),
                    "semantic_score": round(semantic, 4),
                    "keyword_score": round(lexical, 4),
                    "title": row["title"],
                    "path": row["note_path"],
                    "heading": row["heading"],
                    "excerpt": row["content"][:900],
                }
            )
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return {
        "query": query,
        "embedding_error": embedding_error or None,
        "results": candidates[:limit],
    }


@mcp.tool()
def kb_get(note: str) -> dict[str, Any]:
    """Read a note by title, filename, or relative path."""
    _sync()
    wanted = note.removesuffix(".md").lower()
    with _connect() as conn:
        rows = conn.execute("SELECT path,title,body FROM notes").fetchall()
    matches = [
        row
        for row in rows
        if wanted in {
            row["title"].lower(),
            Path(row["path"]).stem.lower(),
            row["path"].removesuffix(".md").lower(),
        }
    ]
    if not matches:
        return {"error": f"未找到笔记: {note}"}
    row = matches[0]
    return {
        "title": row["title"],
        "path": row["path"],
        "body": row["body"],
    }


@mcp.tool()
def kb_list() -> dict[str, Any]:
    """List all notes in the shared knowledge base."""
    _sync()
    result = []
    with _connect() as conn:
        for row in conn.execute("SELECT path,title FROM notes ORDER BY title"):
            result.append(
                {
                    "title": row["title"],
                    "path": row["path"],
                }
            )
    return {"count": len(result), "notes": result}


@mcp.tool()
def kb_time_list(order: str = "oldest", limit: int = 100) -> dict[str, Any]:
    """List notes with their footer update time for cleanup and review."""
    _sync()
    order = order.lower().strip()
    if order not in {"oldest", "newest"}:
        order = "oldest"
    limit = max(1, min(int(limit), 500))
    rows: list[dict[str, Any]] = []
    now = datetime.now(ZoneInfo(TIMEZONE))
    with _connect() as conn:
        notes = conn.execute("SELECT path,title,body FROM notes").fetchall()
    for row in notes:
        updated_text = _extract_update_time(row["body"])
        parsed = _parse_update_time(updated_text)
        age_days = None
        sort_key = float("inf")
        if parsed:
            age_days = max(0, (now - parsed.astimezone(ZoneInfo(TIMEZONE))).days)
            sort_key = parsed.timestamp()
        rows.append(
            {
                "title": row["title"],
                "path": row["path"],
                "updated": updated_text,
                "age_days": age_days,
                "has_update_time": updated_text is not None,
                "_sort_key": sort_key,
            }
        )
    rows.sort(
        key=lambda item: item["_sort_key"],
        reverse=(order == "newest"),
    )
    for item in rows:
        item.pop("_sort_key", None)
    return {
        "count": len(rows),
        "order": order,
        "timezone": TIMEZONE,
        "notes": rows[:limit],
    }


@mcp.tool()
def kb_create_note(title: str, body: str) -> dict[str, Any]:
    """Create a plain Markdown note. Existing notes are never overwritten."""
    safe_title = _safe_title(title)
    path = VAULT / f"{safe_title}.md"
    if path.exists():
        return {"error": f"笔记已存在，不会覆盖: {path.name}"}
    text = _with_update_footer(f"# {safe_title}\n\n{body.strip()}\n")
    _atomic_write(path, text)
    sync = _sync()
    return {"created": path.name, "sync": sync}


@mcp.tool()
def kb_append_inbox(content: str, source: str = "agent") -> dict[str, Any]:
    """Append a dated knowledge fragment to AI Inbox for later distillation."""
    path = VAULT / "AI Inbox.md"
    if not path.exists():
        return {"error": "AI Inbox.md 不存在"}
    text = path.read_text(encoding="utf-8")
    entry = (
        f"\n\n## {date.today().isoformat()} · {source}\n\n"
        f"{content.strip()}\n"
    )
    _atomic_write(path, _with_update_footer(text.rstrip() + entry))
    sync = _sync()
    return {"updated": path.name, "sync": sync}


@mcp.tool()
def kb_reindex(force: bool = False) -> dict[str, Any]:
    """Synchronize or fully rebuild the shared vector index."""
    return _sync(force=force)


if __name__ == "__main__":
    mcp.run(transport="stdio")
