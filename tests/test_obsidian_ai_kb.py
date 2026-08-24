import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


class _FakeMCP:
    def __init__(self, *args, **kwargs):
        pass

    def tool(self):
        return lambda fn: fn

    def run(self, **kwargs):
        pass


mcp_mod = types.ModuleType("mcp")
server_mod = types.ModuleType("mcp.server")
fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
fastmcp_mod.FastMCP = _FakeMCP
sys.modules.setdefault("mcp", mcp_mod)
sys.modules.setdefault("mcp.server", server_mod)
sys.modules.setdefault("mcp.server.fastmcp", fastmcp_mod)

SPEC = importlib.util.spec_from_file_location(
    "obsidian_ai_kb", Path(__file__).parents[1] / "mcp-server" / "obsidian_ai_kb.py"
)
kb = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(kb)


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        kb.VAULT = root / "vault"
        kb.VAULT.mkdir()
        kb.DB_PATH = root / "index.sqlite3"
        kb.ALLOW_SENSITIVE = False
        kb.SYNC_INTERVAL = 0
        kb._LAST_SYNC_RESULT = None
        kb._LAST_SYNC_MONOTONIC = 0
        kb._embed = lambda texts, query=False: [[1.0, 0.0] for _ in texts]

    def tearDown(self):
        self.tmp.cleanup()

    def test_sensitive_note_is_excluded(self):
        (kb.VAULT / "普通.md").write_text("# 普通\n\n可搜索内容", encoding="utf-8")
        (kb.VAULT / "敏感信息.md").write_text("# 敏感信息\n\nsecret", encoding="utf-8")
        result = kb._sync()
        self.assertEqual(result["notes"], 1)
        listed = kb.kb_list()
        self.assertEqual([n["title"] for n in listed["notes"]], ["普通"])

    def test_footer_is_replaced_not_duplicated(self):
        original = "# A\n\ntext\n\n## 更新时间\n\n更新时间：2026-01-01 10:00 Asia/Shanghai\n"
        updated = kb._with_update_footer(original)
        self.assertEqual(updated.count("## 更新时间"), 1)
        self.assertIn("Asia/Shanghai", updated)

    def test_keyword_search_returns_line_ranges(self):
        (kb.VAULT / "网络.md").write_text(
            "# 网络\n\n## DNS\n\nOpenClash 使用 SmartDNS。\n\n## PT\n\n第二网口负责 PT 下载。\n",
            encoding="utf-8",
        )
        kb._sync()
        result = kb.kb_search("第二网口 PT", mode="keyword")
        self.assertTrue(result["results"])
        hit = result["results"][0]
        self.assertEqual(hit["title"], "网络")
        self.assertGreaterEqual(hit["line_start"], 1)
        self.assertGreaterEqual(hit["line_end"], hit["line_start"])

    def test_update_note_uses_optimistic_lock(self):
        (kb.VAULT / "项目.md").write_text("# 项目\n\n旧内容\n", encoding="utf-8")
        kb._sync()
        current = kb.kb_get("项目")
        conflict = kb.kb_update_note("项目", "新内容", expected_sha256="wrong")
        self.assertIn("error", conflict)
        ok = kb.kb_update_note("项目", "新内容", expected_sha256=current["sha256"])
        self.assertEqual(ok["updated"], "项目.md")
        self.assertIn("新内容", kb.kb_get("项目")["body"])

    def test_hybrid_falls_back_to_keyword_when_embedding_fails(self):
        (kb.VAULT / "A.md").write_text("# A\n\n苹果芯片 M1", encoding="utf-8")
        kb._sync()
        def fail(*args, **kwargs):
            raise RuntimeError("offline")
        kb._embed = fail
        result = kb.kb_search("M1", mode="hybrid")
        self.assertEqual(result["mode"], "keyword")
        self.assertTrue(result["results"])
        self.assertIn("offline", result["embedding_error"])


if __name__ == "__main__":
    unittest.main()
