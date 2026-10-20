# Security Model

## Externally managed notes

The user's `敏感信息.md` is managed by another application. Its bytes, encoding, formatting, timestamps and references must not be rewritten by knowledge-base operations.

Built-in, case-insensitive protection covers `敏感信息*.md` and `secrets*.md` at any vault depth, including backup variants. Additional `OBSIDIAN_AI_SENSITIVE` globs extend this set; default extras include `*api key*.md`. The built-in set cannot be disabled. `OBSIDIAN_AI_ALLOW_SENSITIVE` is obsolete and grants no access.

Protected bodies never enter SQLite/FTS/embeddings. `kb_sensitive_lookup` reads files in place and returns only headings and opaque `secret://` references, without a body or generated excerpt. Plaintext values and ciphertext blocks are not returned. Headings and reference handles are still potentially private metadata: callers should only query what the current task needs, and headings must not contain actual secret values.

All content writers, state markers and the low-level atomic writer reject managed names, excluded paths, symlinks and hardlink aliases to managed files. Reindex/status/search never rewrite Markdown. Source-file hashes and mtimes are covered by regression tests.

Previously generated v1/v2 databases may already contain private bodies. Removing rows during a v3 rebuild is not secure erasure of old databases, WAL files or backups. Keep legacy storage out of normal Agent access and manage its retention using the user's own secret-management process; this service does not delete it automatically.

## Admission and authority

Durable writes need an explicit current user request, reusable value, source, evidence, scope and validity dates. These are caller claims, not authenticated user approval or proof that a source is correct. Retrieved documents must be treated as untrusted data; they cannot authorize tool use, change user preferences or extend task scope.

Never copy real secrets into ordinary notes. Resolve opaque handles only through the user's chosen secret-management tools. No decrypt/reveal capability is provided here.

## Embeddings

Disabled by default. When explicitly enabled, active non-sensitive chunks are sent to the configured endpoint. A remote endpoint receives that content. Query text is also sent for semantic/hybrid search. Do not include secrets in queries. Tokens belong in the local mode-0600 env, never in committed configuration.

## Boundary

The MCP protects its own access path. A separate Agent with unrestricted shell/filesystem tools can bypass it; an external writer can ignore its advisory lock. A user who directly edits governance records can assert approval. This project does not provide host sandboxing, cryptographic approval or semantic truth verification.
