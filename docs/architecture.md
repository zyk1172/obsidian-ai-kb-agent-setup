# Architecture

Markdown remains the source of truth. SQLite holds disposable metadata, chunks, FTS5 and optional embeddings. v3 adds evidence admission and effective validity before retrieval.

## Data flow

```text
Markdown files
  |-- externally managed sensitive notes -> headings + opaque refs lookup (read-only, no body index)
  `-- ordinary notes -> metadata + content fingerprint -> chunks with source lines
                            |
                     effective knowledge state
                            |
                 active and unexpired by default
                            |
                  FTS5 / Unicode fallback
                            `-- optional semantic search + RRF
```

Governance is an ordinary Markdown section. `kb_governance.py` validates evidence records, hashes content excluding managed metadata/footer, and computes state. Editing content invalidates review; expiry is computed on every read/search, even if filesystem sync is cached. Metadata/footer changes do not renew verification.

Legacy notes lack approval and remain unreviewed. The first v3 sync reparses old metadata; it never adds governance to source files. Default get returns inactive metadata without its body; explicit historical access returns warnings with the content.

## Retrieval

Keyword is default; embeddings are disabled by default. Enabling embeddings only backfills active knowledge. Sensitive bodies and unreviewed notes are never sent for embedding. Semantic candidates below the configured similarity floor are omitted. Hybrid combines rank positions with RRF and limits duplicate chunks. These thresholds do not establish factual truth or current system state.

State filters run before candidate truncation. Governance fields and timestamp footers are blanked when chunking, preserving real Markdown line numbers without retrieving admission boilerplate as domain knowledge.

## Writes and concurrency

Create/update/append require admission. Update/append/mark require a fresh SHA-256. Per-vault `.kb-write.lock` uses POSIX flock to serialize participating MCP processes around read/check/write; writes use temporary files and atomic replace. Connections are committed/rolled back and closed explicitly.

Every write also checks managed names, excluded paths, symlinks and aliases to managed inodes. Even directly calling the low-level atomic writer cannot write a protected note. The deprecated sensitive opt-in has no effect. Index paths cannot point to Markdown or managed-file aliases.

The lock coordinates this server's processes, not external editors. An external writer that ignores the lock can still race a write; no host-wide sandbox is claimed. Callers must review evidence honestly: admission is a structural quality gate, not independent human authentication.

## Derived data migration

New installs use `index-v3.sqlite3`, allowing old indexes to remain separate for controlled migration. The installer preserves env and does not run synchronization. No migration edits the externally managed file or silently approves old content. See [migration-v3.md](migration-v3.md).
