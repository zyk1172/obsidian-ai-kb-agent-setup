# Obsidian AI KB Agent Setup

This repository documents how to connect Codex, Hermes, Claude Code, OpenClaw, or another MCP-capable agent to the shared Obsidian AI knowledge base.

The Markdown vault remains the source of truth:

```text
/Users/zhengyunkai/Documents/obsidian/zyk的obsidian仓库/AI
```

The MCP server provides search, note reads, note creation, inbox append, reindexing, and update-time listing.

## Tools

- `kb_search`: semantic and keyword search.
- `kb_get`: read one exact note.
- `kb_list`: list all notes.
- `kb_time_list`: list notes by footer `更新时间` for review and cleanup.
- `kb_append_inbox`: append unprocessed fragments to `AI Inbox`.
- `kb_create_note`: create a formal Markdown note.
- `kb_reindex`: rebuild or synchronize the vector index.
- `kb_status`: inspect vault, index, and embedding service state.

## Human-First Rules

- The knowledge base serves the user first, then agents.
- Notes use plain Markdown. Do not use YAML frontmatter or Obsidian properties.
- Every note should have a first-line `# Title`.
- Every note should have useful `[[wiki links]]`.
- Every note must end with:

```md
## 更新时间

更新时间：YYYY-MM-DD HH:mm Asia/Shanghai
```

- Sensitive information is indexed through `[[敏感信息]]`.
- Ordinary notes reference sensitive entries, for example:

```md
密码：见 [[敏感信息#NAS 密码|NAS 密码]]
```

## Server Files

- `mcp-server/obsidian_ai_kb.py`: current MCP server implementation.
- `mcp-server/obsidian-ai-kb`: launcher script.
- `configs/`: client config examples.
- `docs/`: setup and operating rules.

## Quick Install

1. Put `obsidian_ai_kb.py` under `~/.local/share/obsidian-ai-kb/`.
2. Put `obsidian-ai-kb` under `~/.local/bin/` and make it executable.
3. Add one of the config snippets under `configs/` to your agent.
4. Restart the agent so it reloads MCP tools.
5. Run `kb_status`.
6. Run `kb_time_list` to confirm update-time parsing works.

