"""Real MCP stdio round-trip against disposable files; no live services or notes."""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    with tempfile.TemporaryDirectory(prefix="kb-mcp-smoke-") as temporary:
        root = Path(temporary)
        vault = root / "vault"
        vault.mkdir()
        (vault / "旧笔记.md").write_text("# 旧笔记\n\nLegacyDNS 旧步骤，尚未复核。", encoding="utf-8")
        managed = vault / "敏感信息.md"
        managed.write_text("# 敏感信息\n\n## NAS\nprivate-fixture-value\nsecret://fixture/nas\n", encoding="utf-8")
        before = managed.read_bytes(), managed.stat().st_mtime_ns
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        admission = {
            "kind": "procedure", "scope": "隔离 MCP 测试环境",
            "source": "fixture://roundtrip", "evidence": "在临时服务中实际验证了工具响应与版本冲突",
            "reuse_reason": "后续同类维护可复用验证步骤与失败边界",
            "verified_at": today.isoformat(), "review_after": (today + timedelta(days=30)).isoformat(),
            "user_request": "测试夹具模拟明确要求保存这条可复用经验",
        }
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).parents[1] / "mcp-server" / "obsidian_ai_kb.py")],
            env={**os.environ, "OBSIDIAN_AI_PATH": str(vault), "OBSIDIAN_AI_INDEX": str(root / "index.sqlite3"),
                 "OBSIDIAN_AI_ENABLE_EMBEDDINGS": "false", "OBSIDIAN_AI_ALLOW_SENSITIVE": "true"},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()

                async def call(name, **arguments):
                    result = await session.call_tool(name, arguments)
                    assert not result.isError, (name, result)
                    if result.structuredContent is not None:
                        return result.structuredContent
                    return json.loads(result.content[0].text)

                tools = await session.list_tools()
                assert len(tools.tools) == 11
                status = await call("kb_status")
                assert status["schema_version"] == 3
                assert status["knowledge_states"] == {"unreviewed": 1}
                assert (await call("kb_search", query="LegacyDNS"))["results"] == []
                legacy = await call("kb_get", note="旧笔记")
                assert "body" not in legacy
                assert "body" in await call("kb_get", note="旧笔记", include_inactive=True)
                assert "error" in await call("kb_create_note", title="未授权", body="未授权的任务总结不允许自动进入长期知识库。")
                catalog = await call("kb_sensitive_lookup", query="NAS")
                assert catalog["results"][0]["secret_refs"] == ["secret://fixture/nas"]
                assert "private-fixture-value" not in json.dumps(catalog)
                created = await call("kb_create_note", title="维护", body="# 维护\n\nNewDNS 可复用维护步骤，适用于隔离环境。", admission=admission)
                assert created["created"] == "维护.md"
                hits = await call("kb_search", query="NewDNS")
                assert hits["results"][0]["knowledge"]["status"] == "active"
                note = await call("kb_get", note="维护")
                assert "error" in await call("kb_update_note", note="维护", body="新正文及完整的可复用维护步骤。", expected_sha256="wrong", admission=admission)
                marked = await call("kb_mark_note", note="维护", status="deprecated", reason="此步骤已经由新方案取代，保留历史供查证",
                                    user_request="测试中明确要求废弃这条过时知识", expected_sha256=note["sha256"])
                assert marked["status"] == "deprecated"
                assert (await call("kb_search", query="NewDNS"))["results"] == []
                assert "error" in await call("kb_update_note", note="敏感信息", body="不能修改外部软件管理的文件格式和内容。",
                                             expected_sha256=hashlib.sha256(before[0]).hexdigest(), admission=admission)
                await call("kb_reindex", force=True)
        assert before == (managed.read_bytes(), managed.stat().st_mtime_ns)
        print("PASS: 11 real MCP schemas; admission, freshness, history, version conflict and managed-file immutability")


if __name__ == "__main__":
    asyncio.run(main())
