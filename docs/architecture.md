# Architecture

## Goal

Keep the system small enough to audit and operate locally while borrowing proven retrieval patterns from mature personal-knowledge systems.

Markdown in the Obsidian vault is always the source of truth. SQLite is disposable and can be rebuilt at any time.

## Data flow

```text
Obsidian Markdown
      |
      v
incremental sync (mtime + size -> sha256 on change)
      |
      +--> note metadata: title / tags / links / 更新时间
      |
      +--> heading-aware chunks + source line ranges
      |          |
      |          +--> FTS5 lexical index
      |          +--> embedding vectors
      |
      v
kb_search
  |- semantic ranking (cosine via normalized vectors)
  |- keyword ranking (FTS5, with Unicode substring fallback)
  |- exact title/wiki-link boost
  `- Reciprocal Rank Fusion (RRF) + per-note deduplication
```

## Why RRF

Semantic similarity and lexical/BM25 scores live on different scales. A fixed weighted sum can become unstable when the embedding model or corpus changes. RRF combines ranks instead of raw score magnitudes, so the hybrid search is less sensitive to model-specific calibration.

## Sync behavior

- Repeated tool calls are throttled by `OBSIDIAN_AI_SYNC_INTERVAL`.
- Unchanged files are skipped using filesystem mtime and size.
- SHA-256 is calculated only for files that appear changed.
- If the embedding model changes, vectors are invalidated and backfilled without re-chunking every note.
- If the embedding endpoint is unavailable, keyword search still works and missing vectors remain rebuildable.

## Search modes

`kb_search` supports:

- `hybrid` (default): semantic + keyword + RRF.
- `semantic`: vector similarity only.
- `keyword`: FTS5/substring lexical search only.

Results include note path, heading, chunk index and source line range so an Agent can retrieve the exact note instead of treating an excerpt as authoritative context.

## Write safety

`kb_update_note` supports `expected_sha256`. An Agent can read a note, edit it, and provide the hash it read. If another process changed the note in between, the update is rejected rather than silently overwriting newer content.

No delete tool is exposed by design.
