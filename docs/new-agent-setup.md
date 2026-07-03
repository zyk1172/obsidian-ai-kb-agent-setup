# New Agent Setup

Use this when connecting a new agent to the shared Obsidian AI knowledge base.

## Required MCP Config

The minimum MCP entry is:

```json
{
  "mcpServers": {
    "obsidian-ai-kb": {
      "command": "/Users/zhengyunkai/.local/bin/obsidian-ai-kb"
    }
  }
}
```

For TOML or YAML clients, use the matching file under `configs/`.

## Required Startup Rule

Add this rule to the new agent's startup prompt, system instructions, or workspace `AGENTS.md`:

```md
Use the `obsidian-ai-kb` MCP server as the normal access path for durable local context.

- Search with `kb_search`.
- Read exact notes with `kb_get`.
- Browse inventory with `kb_list`.
- List old or recently updated notes with `kb_time_list`.
- Append unprocessed knowledge with `kb_append_inbox`.
- Create formal notes with `kb_create_note`.
- Rebuild the index with `kb_reindex` only when verification is needed.
- For current-state claims, treat old evidence as historical and verify live.
- Notes serve the user first and agents second.
- Keep notes in plain Markdown with useful `[[wiki links]]`.
- After editing a note, refresh the final footer line: `更新时间：YYYY-MM-DD HH:mm Asia/Shanghai`.
- Sensitive content is indexed through `[[敏感信息]]`; ordinary notes should link to the relevant heading and not duplicate the secret.
```

## Verification

After restarting the agent:

1. Call `kb_status`.
2. Call `kb_time_list` with `order: "oldest"` and `limit: 5`.
3. Call `kb_get` for `Agent 读取约定`.
4. Confirm the returned note mentions `kb_time_list`.

## Notes

`kb_time_list` reads the Markdown footer `更新时间`. It does not rely on filesystem modified times.

