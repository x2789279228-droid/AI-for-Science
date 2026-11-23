"""模块七 R2：轮末记忆抽取单测（全离线——测试环境无 LLM，规则兜底路径本身即被测对象）。

对应改造清单 §三#4（双路抽取 + 诚实降级）、§2.8（e_refs 双链 / 失败审计）。
"""
from __future__ import annotations

import time

from app import db
from app.agents import runner
from app.llm import prompts
from app.memory import extract, store

# ---------- 规则兜底路径 ----------

def test_rule_extraction_kinds():
    answer = ("我假设稀疏化注意力可以将显存降低三成。"
              "实验结果表明该方法在长文本上提速 1.8 倍。"
              "我决定采用 BLEU 作为主要评测指标。"
              "我们的实验数据显示基线被超过了。")
    out = extract.extract_turn("帮我验证一下", answer, conversation_id="r2a", turn=1,
                               thread_id="r2a:1")
    assert out["engine"] == "rule", "无 LLM key → 必须走规则兜底"
    assert out["error"] == ""
    kinds = [e["kind"] for e in out["created"]]
    assert "hypothesis" in kinds and "experiment_result" in kinds and "decision" in kinds
    for e in out["created"]:
        assert e["source"] == "rule", "降级路径 source 必须如实标注 rule"
        assert e["conversation_id"] == "r2a" and e["turn"] == 1
        assert e["thread_id"] == "r2a:1"
        assert 1 <= e["importance"] <= 10
    # 事件确实入库可查
    rows = store.list_events(conversation_id="r2a")
    assert {r["id"] for r in rows} >= {e["id"] for e in out["created"]}


def test_no_match_creates_nothing():
    out = extract.extract_turn("这篇论文讲什么？", "本文提出了一种高效的检索增强问答框架。",
                               conversation_id="r2b", turn=1)
    assert out["created"] == []
    assert store.list_events(conversation_id="r2b") == []


def test_meta_e_refs_dual_chain():
    out = extract.extract_turn("为什么选它", "我假设瓶颈在索引层。",
                               conversation_id="r2c", turn=2,
                               e_refs=["E2", "E5"])
    assert out["created"], "规则兜底应命中"
    for e in out["created"]:
        assert e["meta"]["e_refs"] == ["E2", "E5"], "M#↔E# 双链映射必须入 meta"


def test_rule_cap_per_turn():
    long_answer = "。".join(f"我假设编号{i}的方案可行" for i in range(12)) + "。"
    out = extract.extract_turn("q", long_answer, conversation_id="r2d", turn=1)
    assert len(out["created"]) <= extract._MAX_EVENTS_PER_TURN


# ---------- LLM 路：解析器（离线环境直接测纯函数） ----------

def test_parse_llm_payload_tolerant():
    text = '```json\n{"events": [{"kind": "hypothesis", "content": "假设 X", ' \
           '"subject": "X", "entities": ["A", "B"], "importance": 8.0}, ' \
           '{"kind": "bad_kind", "content": "应被丢弃"}, ' \
           '{"kind": "finding", "content": ""}, ' \
           '{"kind": "topic", "content": "主题", "importance": 99}]}\n```'
    out = extract._parse_llm_payload(text)
    assert [e["kind"] for e in out] == ["hypothesis", "topic"]
    assert out[0]["importance"] == 8
    assert out[1]["importance"] == 5, "非法 importance 回落默认 5"
    assert extract._parse_llm_payload("没有 json") == []
    assert extract._parse_llm_payload('{"events": "不是列表"}') == []


# ---------- 失败安全（清单 §2.8：抽取失败绝不影响问答闭环） ----------

def test_extract_never_raises_and_audits(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "add_event", boom)
    out = extract.extract_turn("q", "我假设会失败。", conversation_id="r2e", turn=1)
    assert out["error"].startswith("RuntimeError")
    rows = db.query("SELECT guardrail FROM qa_logs WHERE guardrail LIKE 'memory_extract_error%'")
    assert rows, "失败必须落 qa_logs 审计"


# ---------- runner 钩子（done 之后 daemon 线程） ----------

def test_runner_hook_spawns_and_writes():
    conv = "r2hook"
    runner._spawn_memory_extraction(
        {"answer": "我假设钩子在 done 之后运行。", "anchors": {"E1": {"page": 1}}},
        "测试钩子", f"{conv}:1", conv, 1)
    deadline = time.time() + 8
    events: list = []
    while time.time() < deadline:
        events = store.list_events(conversation_id=conv)
        if events:
            break
        time.sleep(0.1)
    assert events, "钩子必须异步产出记忆事件"
    assert events[0]["thread_id"] == f"{conv}:1"
    assert events[0]["meta"]["e_refs"] == ["E1"]


# ---------- prompt 版本化（进审计台账） ----------

def test_memory_extract_prompt_versioned():
    cat = {c["module"]: c for c in prompts.catalog()}
    assert "memory_extract" in cat, "记忆抽取 prompt 必须入审计目录"
    assert cat["memory_extract"]["version"] == "1.0.0"
    assert "default" in cat["memory_extract"]["slots"]
    p = prompts.get_prompt("memory_extract", "system_extract")
    assert p is not None and p.version == "1.0.0"


# ---------- r9：A/B 公平性——use_memory=false 时整条链关闭 ----------

def test_runner_hook_skips_when_use_memory_false():
    """无记忆口径 = 完全不写入（否则旧系统轮次污染记忆池，A/B 不公平）。"""
    conv = "r9_nomem"
    runner._spawn_memory_extraction(
        {"answer": "我假设这轮不该被抽取入库。", "anchors": {}},
        "测试", f"{conv}:1", conv, 1, use_memory=False)
    time.sleep(1.2)                                   # 给假想的 daemon 线程留时间
    assert store.list_events(conversation_id=conv) == [], "关闭口径下不得写入任何记忆"


def test_runner_hook_writes_when_use_memory_true():
    conv = "r9_mem"
    runner._spawn_memory_extraction(
        {"answer": "我假设开启口径正常写入。", "anchors": {}},
        "测试", f"{conv}:1", conv, 1, use_memory=True)
    deadline = time.time() + 8
    events: list = []
    while time.time() < deadline:
        events = store.list_events(conversation_id=conv)
        if events:
            break
        time.sleep(0.1)
    assert events, "开启口径必须照常写入"
