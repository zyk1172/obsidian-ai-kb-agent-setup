# New Agent Setup

## 1. Install

From the repository root:

```sh
./scripts/install.sh
```

The installer creates:

```text
~/.local/share/obsidian-ai-kb/.venv/
~/.local/share/obsidian-ai-kb/obsidian_ai_kb.py
~/.local/share/obsidian-ai-kb/env
~/.local/bin/obsidian-ai-kb
```

Existing `env` configuration is never overwritten.

## 2. Configure

Edit:

```text
~/.local/share/obsidian-ai-kb/env
```

At minimum verify:

- `OBSIDIAN_AI_PATH`
- `OBSIDIAN_AI_EMBED_BASE_URL`
- `OBSIDIAN_AI_EMBED_MODEL`

Keep `OBSIDIAN_AI_ALLOW_SENSITIVE=false` unless there is a specific reason to expose sensitive notes.

## 3. Connect the MCP client

Use the matching example under `configs/` and replace `YOUR_USERNAME` with the macOS username. Absolute paths are recommended because GUI applications may not inherit the shell `PATH`.

## 4. Agent startup rule

```md
Use `obsidian-ai-kb` as the normal access path for durable local context.

- Search first with `kb_search`; use `hybrid` unless there is a reason to force semantic or keyword mode.
- Treat search excerpts as retrieval hints. Use `kb_get` when full-note context is required.
- Prefer results whose path/heading/line range directly support the task.
- Use `kb_list` for inventory and `kb_time_list` for maintenance review.
- Put raw/unprocessed fragments in `AI Inbox` with `kb_append_inbox`.
- Use `kb_create_note` for new durable notes.
- Before changing an existing note, read it with `kb_get`, then call `kb_update_note` with the returned `sha256` as `expected_sha256`.
- Use `kb_reindex` only for explicit synchronization or repair; normal search syncs incrementally.
- Treat old local evidence as historical for current-state questions and verify live facts separately.
- Sensitive notes are excluded by default. Never copy secrets into ordinary notes or chat.
```

## 5. Verify

After restarting the Agent:

1. Call `kb_status` and confirm the vault path and embedding model.
2. Confirm `sensitive_notes_allowed` is `false`.
3. Call `kb_search` for a known phrase in `keyword` mode.
4. Call the same query in `hybrid` mode; if embeddings are offline it should fall back to keyword mode with an `embedding_error` instead of failing completely.
5. Call `kb_get` on one result and confirm the note path/body are correct.
