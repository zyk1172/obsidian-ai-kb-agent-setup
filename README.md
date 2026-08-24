# Obsidian AI KB Agent Setup

一个轻量、可审计的 Obsidian AI 知识库 MCP 服务：**Markdown 永远是真源，SQLite/向量索引随时可重建。** 用于让 Codex、Claude Code、Hermes、OpenClaw 等 MCP Agent 共用同一套长期知识。

## v2 重点

- **混合检索**：向量语义检索 + FTS5/关键词检索，通过 Reciprocal Rank Fusion (RRF) 融合，不再直接混加不同量纲的分数。
- **中文/故障回退**：FTS 无结果时自动回退 Unicode 子串检索；embedding 服务不可用时 `hybrid` 自动降级为 `keyword`。
- **可追溯结果**：搜索结果包含 `path`、`heading`、`chunk_index`、`line_start`、`line_end`。
- **低开销同步**：mtime/size 快速判断、变更后才算 SHA-256；短时间连续查询不会反复扫描；缺失向量独立回填。
- **安全写入**：新增 `kb_update_note`，支持 `expected_sha256` 乐观锁，避免 Agent 覆盖刚被其他程序修改的笔记。
- **敏感区硬隔离**：`敏感信息.md` / `secrets.md` 默认不索引、不读取、不写入，而不是只依赖提示词。
- **工程化**：便携 launcher、独立 venv 安装脚本、环境配置文件、单元测试和 GitHub Actions CI。

## Tools

| Tool | Purpose |
| --- | --- |
| `kb_search` | `hybrid` / `semantic` / `keyword` 检索，返回来源行号 |
| `kb_get` | 按标题、文件名或相对路径精确读取一篇笔记 |
| `kb_list` | 列出已索引笔记和轻量元数据 |
| `kb_time_list` | 按笔记页脚 `更新时间` 做维护审查 |
| `kb_append_inbox` | 把未整理片段追加到 `AI Inbox` |
| `kb_create_note` | 创建正式 Markdown 笔记，不覆盖已有文件 |
| `kb_update_note` | 更新已有笔记，可用 SHA-256 防并发覆盖 |
| `kb_reindex` | 立即同步；`force=true` 时完整重建 chunk 元数据 |
| `kb_status` | 查看 vault、索引、FTS、embedding 和安全配置状态 |

## Quick install

```sh
./scripts/install.sh
```

然后检查：

```text
~/.local/share/obsidian-ai-kb/env
```

安装脚本不会覆盖已经存在的 `env`。客户端配置示例见 `configs/`；新 Agent 的完整接入规则见 `docs/new-agent-setup.md`。

## Default knowledge-base conventions

- 笔记使用普通 Markdown，不要求 YAML frontmatter。
- 第一行建议为 `# Title`。
- 使用有意义的 `[[wiki links]]`。
- MCP 写入时自动维护：

```md
## 更新时间

更新时间：YYYY-MM-DD HH:mm Asia/Shanghai
```

- 普通笔记可以引用 `[[敏感信息#标题|别名]]`，但敏感笔记本身默认不进入 Agent 可访问索引。

## Configuration

主要环境变量：

| Variable | Meaning |
| --- | --- |
| `OBSIDIAN_AI_PATH` | Obsidian AI vault 路径 |
| `OBSIDIAN_AI_INDEX` | SQLite 索引路径 |
| `OBSIDIAN_AI_EMBED_BASE_URL` | OpenAI-compatible embedding endpoint |
| `OBSIDIAN_AI_EMBED_MODEL` | embedding 模型 |
| `OBSIDIAN_AI_EMBED_API_KEY` | 可选 API key |
| `OBSIDIAN_AI_SYNC_INTERVAL` | 连续请求之间的同步节流秒数，默认 2 |
| `OBSIDIAN_AI_EXCLUDE` | 不索引的 glob 列表 |
| `OBSIDIAN_AI_SENSITIVE` | 敏感文件 glob 列表 |
| `OBSIDIAN_AI_ALLOW_SENSITIVE` | 是否显式开放敏感区，默认 `false` |

完整示例见 `configs/env.example`。

## Design references

这次升级借鉴的不是成熟项目的 UI，而是它们已经验证过的检索/工程原则：

- **Khoj**：预计算 embedding、候选检索后可进一步重排、结果去重。
- **Obsidian Copilot**：把 lexical retrieval 做成独立检索层，强调 chunk 级结果和多阶段检索。
- **Smart Connections**：local-first、轻依赖、默认优先私密和可审计性。

本项目刻意不引入完整 Web 服务、LLM 对话层、Postgres/向量数据库或复杂框架，以保持单机个人知识库的维护成本足够低。

进一步说明：`docs/architecture.md`、`docs/security.md`、`docs/operating-rules.md`。
