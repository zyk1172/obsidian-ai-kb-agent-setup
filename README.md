# Obsidian AI KB Agent Setup

供 Codex、Claude、Hermes、OpenClaw 共享可复用长期背景的轻量 MCP 服务。Markdown 是原始资料，SQLite 是可重建的派生索引。

## v3：先判断能不能用，再检索

- **旧知识隔离**：没有治理记录的笔记默认为 `unreviewed`。默认搜索不返回其片段，默认读取只返回元数据；有意追溯历史时使用 `include_inactive=true`。
- **有效性独立于修改时间**：正文验证日期和复查期限决定是否可用；刷新 `更新时间` 不续期。正文改变会使原验证失效。到期笔记自动成为 `expired`，不改动原文件。
- **严格入库**：创建、更新和 Inbox 追加都要求本轮用户明确要求保存，以及来源、验证依据、适用范围、复用价值和验证/复查日期。普通问答、任务流水、临时状态和推测不自动入库。
- **受管文件永远只读**：`敏感信息*.md`、`secrets*.md` 及配置的其他受管文件不进入全文索引或 embedding。`kb_sensitive_lookup` 仅查询标题目录和不透明 `secret://` 引用。所有写入路径禁止修改它们，旧的敏感区 opt-in 不能解除写保护。
- **简单检索优先**：默认 `keyword`，默认禁用 embedding。可显式启用语义/混合检索；无匹配时返回空。向量只为已复核且有效的笔记生成。
- **共享更新保护**：更新和追加必须提供读取时的 SHA-256；同一 vault 的 MCP 写入使用跨进程锁，防止多个 Agent 同时覆盖同一版本。

这是一次有意收紧的接口升级。旧客户端缺少 admission/hash 的写请求会被拒绝；现有笔记不会被批量改写、自动认可或删除。

## 什么值得保存

优先保存你独有、将来值得复用的偏好、项目决策及原因、验证过的操作方法、可复现的教训、可靠参考入口。完整资料保留在原处，知识库记录必要结论和来源。

一次任务完成不构成入库授权。入库字段是调用者提交的证据声明，服务只能检查结构、日期和版本，不能证明其事实真实性或用户确实说过该句话。Agent 必须忠实引用当前任务里的授权，不得编造。

详细门槛、日期上限及调用示例见 [入库与有效性规则](docs/admission.md)。

## Tools

| Tool | Purpose |
| --- | --- |
| `kb_search` | 默认只检索已复核、未过期知识；历史查询显式启用 `include_inactive` |
| `kb_get` | 从 Markdown 读取新鲜内容和 SHA-256；未复核正文默认不返回 |
| `kb_list` | 按有效状态列出笔记元数据，用于复核清单 |
| `kb_time_list` | 按修改时间维护审查，同时返回验证日期和知识状态 |
| `kb_sensitive_lookup` | 只读查询外部软件管理的敏感文件目录与不透明引用 |
| `kb_create_note` | 满足 admission 后新增，不覆盖已有笔记 |
| `kb_update_note` | 用新证据重新确认笔记，要求 admission 和 SHA-256 |
| `kb_mark_note` | 将普通笔记标为废弃/归档，可指向有效替代笔记，保留历史 |
| `kb_append_inbox` | 仅追加明确要求保存且有复用依据的候选；Inbox 不参与正式检索 |
| `kb_reindex` | 重建派生索引，不改写任何笔记 |
| `kb_status` | 检查版本、有效状态数量、检索配置和受管文件数量 |

## 安装与升级

```sh
./scripts/install.sh
```

安装使用独立 venv，不覆盖已有 `env`。v3 新安装默认索引为 `~/.local/share/obsidian-ai-kb/index-v3.sqlite3`，默认不连接 embedding 服务。

**已有安装先读 [v3 迁移说明](docs/migration-v3.md)**。必须更新全部客户端所运行的服务；只修改本仓库不会改变已安装的旧版 MCP。旧笔记初始没有 active 结果是预期的复核隔离状态。

配置示例见 `configs/`；操作规则见 [operating-rules.md](docs/operating-rules.md)，接入见 [new-agent-setup.md](docs/new-agent-setup.md)，安全边界见 [security.md](docs/security.md)，实现见 [architecture.md](docs/architecture.md)。

## 本地验证

```sh
python3 -m py_compile mcp-server/obsidian_ai_kb.py mcp-server/kb_governance.py
python3 -m unittest discover -s tests -v
```

在已安装 `requirements.txt` 的运行时中，执行 `python3 tests/mcp_smoke.py` 可验证真实 MCP stdio 往返。

测试使用临时 vault 和本地 embedding 夹具，不访问真实笔记、保险箱或模型服务。CI 在 Python 3.11/3.12/3.13 上执行。运行平台为 macOS / Linux（跨进程写锁使用 POSIX flock）。
