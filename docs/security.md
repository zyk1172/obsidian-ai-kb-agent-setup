# Security Model

The MCP server has local filesystem access to the configured vault. Treat every connected Agent as a process that can read and write whatever the MCP tools expose.

## Sensitive notes

Sensitive files are **excluded by default**, not merely hidden by prompt instructions.

Default patterns:

```text
敏感信息.md
**/敏感信息.md
secrets.md
**/secrets.md
```

Configure patterns with `OBSIDIAN_AI_SENSITIVE`. Opt-in access requires:

```text
OBSIDIAN_AI_ALLOW_SENSITIVE=true
```

Do not enable this just to preserve wiki links. Ordinary notes can safely contain links such as:

```md
密码：见 [[敏感信息#NAS 密码|NAS 密码]]
```

The Agent does not need the target note contents to maintain that reference.

## General exclusions

Use `OBSIDIAN_AI_EXCLUDE` for folders or files that should never enter the index. `.obsidian`, `.trash` and Git internals are excluded by default.

## Embedding endpoint

Every indexed chunk is sent to the configured embedding endpoint. If that endpoint is remote, note content leaves the machine. Prefer a trusted local/LAN service. If authentication is required, put the token in the local `env` file and keep that file mode `0600`.

## Writes

- `kb_create_note` never overwrites an existing note.
- `kb_update_note` can use SHA-256 optimistic locking.
- Writes are atomic (`temp file -> os.replace`).
- There is intentionally no Agent delete tool.

## Threat boundary

This project does not attempt to sandbox the MCP client itself. A malicious or compromised Agent with independent shell/filesystem tools may bypass these MCP restrictions. The rules here protect the knowledge-base access path, not the entire host.
