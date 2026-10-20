"""Durable-knowledge admission and freshness rules, without filesystem writes."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Any

KINDS = {"preference", "decision", "procedure", "lesson", "reference"}
MAX_REVIEW_DAYS = {"preference": 365, "decision": 180, "procedure": 90, "lesson": 180, "reference": 365}
FIELDS = {
    "status": "状态", "kind": "类型", "scope": "适用范围", "source": "来源",
    "evidence": "验证依据", "reuse_reason": "复用价值", "verified_at": "验证日期",
    "review_after": "复查期限", "user_request": "入库授权原文", "reason": "状态说明",
    "superseded_by": "替代笔记", "content_sha256": "正文校验",
}
def _sections(body: str) -> list[tuple[str, int, int]]:
    """Find real H2 sections, ignoring headings inside fenced examples."""
    headings: list[tuple[str, int]] = []
    offset = 0
    fence_char, fence_length = "", 0
    for line in body.splitlines(keepends=True):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence_char:
            if fence and fence[1][0] == fence_char and len(fence[1]) >= fence_length and not line[fence.end():].strip():
                fence_char = ""
        elif fence:
            fence_char, fence_length = fence[1][0], len(fence[1])
        else:
            heading = re.match(r"^(#{1,2})[ \t]+(.+?)[ \t]*\r?\n?$", line)
            if heading:
                headings.append((heading[2] if heading[1] == "##" else "", offset))
        offset += len(line)
    return [(name, start, headings[i + 1][1] if i + 1 < len(headings) else len(body))
            for i, (name, start) in enumerate(headings) if name in {"知识治理", "更新时间"}]


def _parse_policy_section(section: str) -> dict[str, str]:
    labels = {v: k for k, v in FIELDS.items()}
    policy: dict[str, str] = {}
    for line in section.splitlines()[1:]:
        if not line.strip():
            continue
        match = re.fullmatch(r"- ([^：]+)：(.*)", line)
        if not match or match[1] not in labels or labels[match[1]] in policy:
            return {}
        policy[labels[match[1]]] = match[2].strip()
    return policy


def strip_managed(body: str, preserve_lines: bool = False) -> str:
    for name, start, end in reversed(_sections(body)):
        section = body[start:end]
        valid = bool(_parse_policy_section(section)) if name == "知识治理" else bool(re.fullmatch(
            r"## 更新时间[ \t]*\r?\n(?:[ \t]*\r?\n)*更新时间：[^\r\n]+(?:\r?\n[ \t]*)*", section))
        if valid:
            replacement = "\n" * section.count("\n") if preserve_lines else ""
            body = body[:start] + replacement + body[end:]
    return body


def content_text(body: str) -> str:
    """Exclude machine-managed sections from content identity."""
    return strip_managed(body).strip()


def content_hash(body: str) -> str:
    return hashlib.sha256(content_text(body).encode("utf-8")).hexdigest()


def parse_policy(body: str) -> dict[str, str]:
    sections = [(start, end) for name, start, end in _sections(body) if name == "知识治理"]
    if len(sections) != 1:
        return {}
    start, end = sections[0]
    return _parse_policy_section(body[start:end])


def validate_admission(admission: Any, today: date) -> list[str]:
    """Validate a caller's evidence record, not the truth of their claims."""
    if not isinstance(admission, dict):
        return ["需要 admission：kind, scope, source, evidence, reuse_reason, verified_at, review_after, user_request"]
    errors: list[str] = []
    minimum = {"scope": 2, "source": 3, "evidence": 12, "reuse_reason": 12, "user_request": 2}
    maximum = {"scope": 256, "source": 2048, "evidence": 1200, "reuse_reason": 600, "user_request": 512}
    for key, length in minimum.items():
        value = admission.get(key)
        if not isinstance(value, str) or not length <= len(value.strip()) <= maximum[key] or "\n" in value or "\r" in value:
            errors.append(f"{key} 必须提供 {length} 至 {maximum[key]} 字符的单行具体说明")
    kind = admission.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        errors.append("kind 只允许 preference / decision / procedure / lesson / reference；聊天、任务流水和临时状态不入库")
    dates: dict[str, date] = {}
    for key in ("verified_at", "review_after"):
        try:
            value = admission.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            dates[key] = date.fromisoformat(value)
        except ValueError:
            errors.append(f"{key} 必须为有效的 YYYY-MM-DD")
    verified, review = dates.get("verified_at"), dates.get("review_after")
    if verified and verified > today:
        errors.append("验证日期不能在未来")
    if review and review < today:
        errors.append("复查期限已过，必须先重新验证")
    if verified and review and not 0 <= (review - verified).days <= MAX_REVIEW_DAYS.get(str(kind), 90):
        errors.append("复查期限必须在验证日期之后，且不得超过该类型的最长复查间隔")
    return errors


def knowledge_state(body: str, today: date) -> dict[str, Any]:
    policy = parse_policy(body)
    return policy_state(policy, policy.get("content_sha256") == content_hash(body), today)


def policy_state(policy: dict[str, str], content_matches: bool, today: date) -> dict[str, Any]:
    status = policy.get("status", "unreviewed")
    warning = ""
    if status in {"deprecated", "archived"}:
        warning = "仅供历史追溯，不得作为当前操作依据"
    elif status != "active" or validate_admission(policy, today):
        # Expiry is distinguishable from missing/invalid provenance.
        try:
            expired = date.fromisoformat(policy.get("review_after", "")) < today
        except ValueError:
            expired = False
        status = "expired" if policy.get("status") == "active" and expired else "unreviewed"
        warning = "已到复查期限，重新验证前不得采用" if status == "expired" else "尚未通过入库复核，不得作为当前操作依据"
    if policy and not content_matches:
        status, warning = "unreviewed", "正文已改变，原有验证失效，需要重新复核"
    return {
        "status": status, "kind": policy.get("kind"), "scope": policy.get("scope"),
        "source": policy.get("source"), "evidence": policy.get("evidence"),
        "verified_at": policy.get("verified_at"), "review_after": policy.get("review_after"),
        "superseded_by": policy.get("superseded_by") or None, "warning": warning or None,
        "requires_live_verification": True,
    }


def render_policy(body: str, policy: dict[str, str]) -> str:
    if any(name == "知识治理" for name, _, _ in _sections(body)) and not parse_policy(body):
        raise ValueError("存在非规范或重复治理段，请在复核时明确整理正文后再入库；禁止静默删除其中内容")
    text = content_text(body)
    record = {**policy, "content_sha256": content_hash(text)}
    lines = [f"- {label}：{record[key]}" for key, label in FIELDS.items() if record.get(key)]
    return text + "\n\n## 知识治理\n\n" + "\n".join(lines) + "\n"
