# Operating Rules

## Read Path

Start from:

- `AI 总览`
- `实体地图`
- `任务地图`
- `Obsidian AI 知识库规范`
- `Agent 读取约定`
- `AI Inbox`

Prefer:

```text
任务说明 -> 验证记录 -> 实体说明 -> 长期知识 -> 索引
```

## Write Path

For reusable knowledge, create or update a formal note.

For unprocessed fragments, append to `AI Inbox`.

Every changed note must end with:

```md
## 更新时间

更新时间：YYYY-MM-DD HH:mm Asia/Shanghai
```

## Cleanup Review

Use `kb_time_list` to build a review table. The user decides what to delete.

Suggested columns:

- title
- path
- updated
- age_days
- note category or topic
- suggested action

## Sensitive Information

Store and manage real secrets through the user's own encryption app and the `敏感信息` note.

Ordinary notes should reference the relevant heading:

```md
密码：见 [[敏感信息#NAS 密码|NAS 密码]]
```

Agents maintain links and headings. They should not expose plaintext in chat.

