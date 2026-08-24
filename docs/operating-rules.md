# Operating Rules

## Read path

Use the cheapest reliable path:

```text
kb_search -> kb_get only when needed -> live verification for current facts
```

Search results contain `path`, `heading`, `line_start` and `line_end`. Prefer those provenance fields over relying on a generated summary.

For deliberate navigation, start from existing map/index notes such as `AI 总览`, `实体地图`, `任务地图` and `Agent 读取约定`.

## Write path

Use:

```text
raw fragment -> kb_append_inbox
new durable knowledge -> kb_create_note
existing durable knowledge -> kb_get -> edit -> kb_update_note(expected_sha256=...)
```

Every changed note ends with:

```md
## 更新时间

更新时间：YYYY-MM-DD HH:mm Asia/Shanghai
```

The server maintains this footer automatically for MCP writes.

## Search policy

- Default to `hybrid`.
- Use `keyword` for exact identifiers, filenames, IPs, error strings, model names and commands.
- Use `semantic` when wording differs substantially and the embedding service is healthy.
- Do not treat a high retrieval score as proof that a note is current or correct.
- Limit duplicate chunks from one note unless deep local context is specifically needed.

## Cleanup review

Use `kb_time_list` to build a maintenance table. The user decides what to archive or delete; the MCP server intentionally exposes no delete tool.

## Sensitive information

Real secrets belong in the user's encrypted secret-management workflow. `敏感信息.md` may remain as a human reference note, but it is excluded from Agent indexing/reading by default.

Ordinary notes should reference sensitive headings without duplicating plaintext:

```md
密码：见 [[敏感信息#NAS 密码|NAS 密码]]
```
