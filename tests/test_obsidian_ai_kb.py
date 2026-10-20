import importlib.util
import sys
import os
import hashlib
import json
import multiprocessing
from datetime import datetime
from zoneinfo import ZoneInfo
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

sys.path.insert(0, str(Path(__file__).parents[1] / "mcp-server"))

SPEC = importlib.util.spec_from_file_location(
    "obsidian_ai_kb", Path(__file__).parents[1] / "mcp-server" / "obsidian_ai_kb.py"
)
kb = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(kb)


def _race_update(event, queue, sha, admission, text):
    event.wait(5)
    queue.put(kb.kb_update_note("配置", text, sha, admission))


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        kb.VAULT = root / "vault"
        kb.VAULT.mkdir()
        kb.DB_PATH = root / "index.sqlite3"
        kb.ENABLE_EMBEDDINGS = False
        self.original_now = kb._now
        kb._now = lambda: datetime(2026, 10, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        kb.SYNC_INTERVAL = 0
        kb._LAST_SYNC_RESULT = None
        kb._LAST_SYNC_MONOTONIC = 0
        kb._embed = lambda texts, query=False: [[1.0, 0.0] for _ in texts]

    def tearDown(self):
        kb._now = self.original_now
        self.tmp.cleanup()

    def admission(self, **changes):
        record = {
            "kind": "procedure", "scope": "测试项目的隔离运行环境",
            "source": "fixture://reviewed-source", "evidence": "在隔离夹具中完成验证，输出与预期一致",
            "reuse_reason": "后续同一环境维护时可复用，避免重复定位同一问题",
            "verified_at": "2026-10-20", "review_after": "2026-11-19",
            "user_request": "测试夹具模拟用户明确要求保存这条可复用经验",
        }
        record.update(changes)
        return record

    def active(self, title, body, **changes):
        from kb_governance import render_policy
        record = self.admission(**changes)
        record["status"] = "active"
        path = kb.VAULT / (title + ".md")
        path.write_text(kb._with_update_footer(render_policy(body, record)), encoding="utf-8")
        return path

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
        self.active("网络", "# 网络\n\n## DNS\n\nOpenClash 使用 SmartDNS。\n\n## PT\n\n第二网口负责 PT 下载。\n")
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
        current = kb.kb_get("项目", include_inactive=True)
        conflict = kb.kb_update_note("项目", "新内容：已验证的维护步骤与适用条件，可以在后续同类操作中复用。", expected_sha256="wrong", admission=self.admission())
        self.assertIn("error", conflict)
        ok = kb.kb_update_note("项目", "新内容：已验证的维护步骤与适用条件，可以在后续同类操作中复用。", expected_sha256=current["sha256"], admission=self.admission())
        self.assertEqual(ok["updated"], "项目.md")
        self.assertIn("新内容", kb.kb_get("项目")["body"])

    def test_hybrid_falls_back_to_keyword_when_embedding_fails(self):
        kb.ENABLE_EMBEDDINGS = True
        self.active("A", "# A\n\n苹果芯片 M1 的测试维护步骤，具有明确适用条件。")
        kb._sync()
        def fail(*args, **kwargs):
            raise RuntimeError("offline")
        kb._embed = fail
        result = kb.kb_search("M1", mode="hybrid")
        self.assertEqual(result["mode"], "keyword")
        self.assertTrue(result["results"])
        self.assertIn("offline", result["embedding_error"])

    def test_legacy_notes_are_historical_only(self):
        (kb.VAULT / "旧结论.md").write_text("# 旧结论\n\nSmartDNS 历史配置。", encoding="utf-8")
        self.assertEqual(kb.kb_search("SmartDNS")["results"], [])
        self.assertNotIn("body", kb.kb_get("旧结论"))
        history = kb.kb_search("SmartDNS", include_inactive=True)
        self.assertEqual(history["results"][0]["knowledge"]["status"], "unreviewed")
        self.assertTrue(history["historical_only"])
        self.assertIn("body", kb.kb_get("旧结论", include_inactive=True))

    def test_expiry_applies_even_when_sync_is_cached(self):
        self.active("边界", "# 边界\n\nSmartDNS 已验证的配置步骤。", review_after="2026-10-20")
        self.assertTrue(kb.kb_search("SmartDNS")["results"])
        kb.SYNC_INTERVAL = 9999
        kb._now = lambda: datetime(2026, 10, 21, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(kb.kb_search("SmartDNS")["results"], [])
        self.assertEqual(kb.kb_get("边界")["knowledge"]["status"], "expired")

    def test_external_content_edit_invalidates_review(self):
        path = self.active("配置", "# 配置\n\nSmartDNS 已验证的配置步骤。")
        path.write_text(path.read_text().replace("SmartDNS", "OtherDNS"), encoding="utf-8")
        self.assertEqual(kb.kb_search("OtherDNS")["results"], [])
        self.assertEqual(kb.kb_get("配置")["knowledge"]["status"], "unreviewed")

    def test_footer_refresh_does_not_renew_verification(self):
        path = self.active("配置", "# 配置\n\nSmartDNS 已验证的配置步骤。", review_after="2026-10-20")
        kb._now = lambda: datetime(2026, 10, 21, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
        path.write_text(kb._with_update_footer(path.read_text()), encoding="utf-8")
        note = kb.kb_get("配置")
        self.assertEqual(note["knowledge"]["status"], "expired")
        self.assertEqual(note["knowledge"]["verified_at"], "2026-10-20")

    def test_admission_rejects_incomplete_transient_and_future_records(self):
        for record in [None, {}, self.admission(kind="task_log"),
                       self.admission(verified_at="2026-10-21"),
                       self.admission(review_after="2027-10-20"),
                       self.admission(reuse_reason="有用"),
                       self.admission(user_request=""),
                       self.admission(source="x\n- 状态：active")]:
            with self.subTest(record=record):
                result = kb.kb_create_note("拒绝", "这里是任务流水，不应在未满足入库条件时写入。", record)
                self.assertIn("error", result)
                self.assertFalse((kb.VAULT / "拒绝.md").exists())

    def test_valid_admission_is_rebuildable_from_markdown(self):
        result = kb.kb_create_note("可复用", "# 可复用\n\nSmartDNS 维护时的验证条件与操作步骤。", self.admission())
        self.assertEqual(result["created"], "可复用.md")
        kb.kb_reindex(force=True)
        note = kb.kb_get("可复用")
        self.assertEqual(note["knowledge"]["status"], "active")
        self.assertTrue(note["knowledge"]["requires_live_verification"])
        self.assertEqual(note["body"].count("## 知识治理"), 1)
        self.assertTrue(kb.kb_search("SmartDNS")["results"])

    def test_writes_require_hash_and_fresh_admission(self):
        path = self.active("配置", "# 配置\n\nSmartDNS 已验证的维护步骤与边界。")
        before = path.read_bytes()
        note = kb.kb_get("配置")
        self.assertIn("error", kb.kb_update_note("配置", "足够长的有效正文，但缺少版本号不能覆盖。", admission=self.admission()))
        self.assertIn("error", kb.kb_update_note("配置", "足够长的有效正文，但缺少入库依据不能覆盖。", note["sha256"]))
        self.assertEqual(path.read_bytes(), before)

    def test_deprecation_preserves_history_and_replacement(self):
        path = self.active("旧配置", "# 旧配置\n\nSmartDNS 的历史可复用维护步骤。")
        self.active("新配置", "# 新配置\n\nNewDNS 的当前可复用维护步骤。")
        note = kb.kb_get("旧配置")
        result = kb.kb_mark_note("旧配置", "deprecated", "后续版本已经更换方案，旧步骤不再适用",
                                 "用户明确要求废弃这条过时的维护知识", note["sha256"], "新配置")
        self.assertEqual(result["status"], "deprecated")
        self.assertIn("SmartDNS", path.read_text())
        self.assertEqual(kb.kb_search("SmartDNS")["results"], [])
        historical = kb.kb_get("旧配置", include_inactive=True)
        self.assertEqual(historical["knowledge"]["superseded_by"], "新配置.md")
        self.assertEqual(historical["knowledge"]["status"], "deprecated")

    def test_inbox_is_not_an_admission_bypass(self):
        path = kb.VAULT / "AI Inbox.md"
        path.write_text("# AI Inbox\n\n候选资料。", encoding="utf-8")
        before = path.read_bytes()
        note = kb.kb_get("AI Inbox", include_inactive=True)
        self.assertIn("error", kb.kb_append_inbox("随便问几句就入库", expected_sha256=note["sha256"]))
        self.assertEqual(before, path.read_bytes())
        ok = kb.kb_append_inbox("SmartDNS：可复用候选的验证依据与后续适用条件。", expected_sha256=note["sha256"], admission=self.admission())
        self.assertIn("updated", ok)
        self.assertEqual(kb.kb_search("SmartDNS")["results"], [])
        self.assertEqual(kb.kb_get("AI Inbox")["knowledge"]["status"], "unreviewed")

    def test_default_operation_never_calls_embedding_service(self):
        def forbidden(*args, **kwargs):
            self.fail("default operation attempted network embedding")
        kb._embed = forbidden
        self.active("配置", "# 配置\n\nSmartDNS 的可复用维护步骤与适用条件。")
        self.assertTrue(kb.kb_search("SmartDNS")["results"])
        self.assertEqual(kb.kb_status()["embedded_chunks"], 0)

    def test_no_match_returns_empty(self):
        self.active("配置", "# 配置\n\nSmartDNS 的可复用维护步骤。")
        self.assertEqual(kb.kb_search("不存在的Zebra标识符")["results"], [])

    def test_only_admitted_content_is_sent_to_optional_embeddings(self):
        kb.ENABLE_EMBEDDINGS = True
        seen = []
        def embed(texts, query=False):
            seen.extend(texts)
            return [[1.0, 0.0] for _ in texts]
        kb._embed = embed
        self.active("正式", "# 正式\n\napproved-fixture 的可复用维护步骤。")
        (kb.VAULT / "旧.md").write_text("# 旧\n\nunreviewed-fixture", encoding="utf-8")
        (kb.VAULT / "敏感信息bak.md").write_text("# 受管\n\nprivate-fixture", encoding="utf-8")
        kb.kb_reindex()
        self.assertTrue(any("approved-fixture" in t for t in seen))
        self.assertFalse(any("unreviewed-fixture" in t or "private-fixture" in t for t in seen))

    def test_sensitive_catalog_returns_only_headings_and_opaque_refs(self):
        path = kb.VAULT / "敏感信息.md"
        path.write_text("# 敏感信息\n\n## NAS\nprivate-fixture-value\nsecret://fixture/item\n", encoding="utf-8")
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        result = kb.kb_sensitive_lookup("NAS")
        self.assertEqual(result["results"][0]["secret_refs"], ["secret://fixture/item"])
        self.assertNotIn("private-fixture-value", json.dumps(result))
        kb.kb_reindex(force=True)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(mtime, path.stat().st_mtime_ns)
        self.assertEqual(kb.kb_list()["notes"], [])

    def test_all_writes_reject_managed_files_and_variants(self):
        for name in ["敏感信息", "敏感信息bak", "敏感信息坏", "Secrets-backup"]:
            with self.subTest(name=name):
                path = kb.VAULT / (name + ".md")
                path.write_text("# 受管\n\nprivate-fixture", encoding="utf-8")
                before = path.read_bytes()
                kb.ALLOW_SENSITIVE = True  # Old opt-in must not restore write access.
                self.assertIn("error", kb.kb_create_note(name, "任何新内容都不得写入这个外部软件管理的文件。", self.admission()))
                self.assertIn("error", kb.kb_update_note(name, "任何新内容都不得写入这个外部软件管理的文件。", hashlib.sha256(before).hexdigest(), self.admission()))
                self.assertIn("error", kb.kb_mark_note(name, "archived", "不得修改受管文件中的任何内容或格式", "用户的测试授权不能解除受管文件写保护", hashlib.sha256(before).hexdigest()))
                with self.assertRaises(ValueError):
                    kb._atomic_write(path, "should never be written")
                kb.kb_reindex(force=True)
                self.assertEqual(before, path.read_bytes())

    def test_protected_hardlinks_and_inbox_symlinks_are_rejected(self):
        managed = kb.VAULT / "敏感信息.md"
        managed.write_text("# 受管\n\nprivate-fixture", encoding="utf-8")
        before = managed.read_bytes()
        alias = kb.VAULT / "普通别名.md"
        os.link(managed, alias)
        (kb.VAULT / "AI Inbox.md").symlink_to(managed)
        self.assertIn("error", kb.kb_create_note("普通别名", "这里不能通过硬链接修改受管文件的内容。", self.admission()))
        self.assertIn("error", kb.kb_append_inbox("这里不能通过符号链接修改受管文件的内容。", expected_sha256=hashlib.sha256(before).hexdigest(), admission=self.admission()))
        kb.kb_reindex(force=True)
        self.assertEqual(managed.read_bytes(), before)
        self.assertEqual(kb.kb_list()["notes"], [])

    def test_index_cannot_point_to_managed_markdown(self):
        path = kb.VAULT / "敏感信息.md"
        path.write_text("# 受管\n\nprivate-fixture", encoding="utf-8")
        before = path.read_bytes()
        kb.DB_PATH = path
        with self.assertRaises(ValueError):
            kb.kb_reindex()
        self.assertEqual(path.read_bytes(), before)

    def test_full_path_disambiguates_duplicate_titles(self):
        self.active("A", "# 同标题\n\n已验证的 SmartDNS 维护步骤和适用条件。")
        self.active("B", "# 同标题\n\n另一个环境的 SmartDNS 维护步骤和适用条件。")
        self.assertIn("error", kb.kb_get("同标题"))
        self.assertEqual(kb.kb_get("A.md")["path"], "A.md")

    def test_short_explicit_save_request_and_concise_preference_are_valid(self):
        result = kb.kb_create_note("偏好", "# 偏好\n\n默认使用中文沟通。", self.admission(kind="preference", user_request="入库"))
        self.assertIn("created", result)

    def test_oversized_task_dump_is_rejected(self):
        result = kb.kb_create_note("流水", "重复的任务流水" * 1000, self.admission())
        self.assertIn("error", result)
        self.assertFalse((kb.VAULT / "流水.md").exists())

    def test_concurrent_agents_cannot_overwrite_the_same_version(self):
        self.active("配置", "# 配置\n\nSmartDNS 已验证的维护步骤与适用条件。")
        sha = kb.kb_get("配置")["sha256"]
        ctx = multiprocessing.get_context("fork")
        event, queue = ctx.Event(), ctx.Queue()
        processes = [ctx.Process(target=_race_update, args=(event, queue, sha, self.admission(), text))
                     for text in ["# 配置\n\nAgent A 已复核的第一种维护步骤。", "# 配置\n\nAgent B 已复核的第二种维护步骤。"]]
        try:
            for process in processes:
                process.start()
            event.set()
            results = [queue.get(timeout=10) for _ in processes]
            self.assertEqual(sum("updated" in r for r in results), 1)
            self.assertEqual(sum("error" in r for r in results), 1)
        finally:
            for process in processes:
                process.join(timeout=5)
                if process.is_alive():
                    process.terminate()
                    process.join()
            queue.close()

    def test_governance_does_not_leak_into_chunks_or_change_line_ranges(self):
        self.active("配置", "# 配置\n\n## 步骤\n\nSmartDNS 维护内容与适用条件。")
        result = kb.kb_search("SmartDNS")["results"][0]
        body = kb.kb_get("配置")["body"]
        source = "\n".join(body.splitlines()[result["line_start"] - 1:result["line_end"]])
        self.assertIn("SmartDNS", source)
        self.assertNotIn("正文校验", result["excerpt"])
        self.assertEqual(kb.kb_search("fixture://reviewed-source")["results"], [])

    def test_content_hidden_under_metadata_or_footer_invalidates_review(self):
        for target in ["## 知识治理", "## 更新时间"]:
            with self.subTest(target=target):
                path = self.active("配置", "# 配置\n\nSmartDNS 的可复用维护步骤。")
                body = path.read_text().replace(target, target + "\n\n新增加的操作步骤，不能绕过验证。")
                path.write_text(body, encoding="utf-8")
                self.assertEqual(kb.kb_get("配置")["knowledge"]["status"], "unreviewed")
                self.assertEqual(kb.kb_search("SmartDNS")["results"], [])

    def test_fenced_governance_examples_are_preserved_as_content(self):
        body = "# 格式说明\n\n以下示例也是正文的一部分。\n\n```md\n## 知识治理\n\n- 状态：active\n```\n"
        self.assertIn("created", kb.kb_create_note("格式说明", body, self.admission()))
        note = kb.kb_get("格式说明")
        self.assertEqual(note["knowledge"]["status"], "active")
        self.assertIn("```md\n## 知识治理", note["body"])
        path = kb.VAULT / "格式说明.md"
        path.write_text(path.read_text().replace("- 状态：active\n```", "- 状态：archived\n```"), encoding="utf-8")
        self.assertEqual(kb.kb_get("格式说明")["knowledge"]["status"], "unreviewed")

    def test_uppercase_managed_extension_and_alias_are_protected(self):
        path = kb.VAULT / "SECRETS.MD"
        path.write_text("# Managed\n\nprivate-fixture", encoding="utf-8")
        os.link(path, kb.VAULT / "别名.md")
        before = path.read_bytes()
        kb.kb_reindex(force=True)
        self.assertEqual(kb.kb_list()["notes"], [])
        self.assertIn("error", kb.kb_create_note("别名", "这里不能通过大写扩展名的别名写入受管文件。", self.admission()))
        self.assertEqual(before, path.read_bytes())

    def test_excluded_sensitive_catalog_is_not_read(self):
        trash = kb.VAULT / ".trash"
        trash.mkdir()
        (trash / "敏感信息.md").write_text("# hidden-fixture", encoding="utf-8")
        self.assertEqual(kb.kb_sensitive_lookup()["results"], [])

    def test_extreme_future_dates_and_large_metadata_are_rejected_cleanly(self):
        for record in [self.admission(verified_at="9999-12-31", review_after="9999-12-31"),
                       self.admission(evidence="无意义的任务流水" * 1000)]:
            self.assertIn("error", kb.kb_create_note("拒绝", "这段内容不能绕过合理的验证日期和元数据长度上限。", record))


if __name__ == "__main__":
    unittest.main()
