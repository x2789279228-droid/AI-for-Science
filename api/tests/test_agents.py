"""Agent 状态图单元测试（密闭：屏蔽真实凭据，LLM 用 stub monkeypatch）。"""
from __future__ import annotations

import os
import queue as queue_mod
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("SCHOLAR_DATA_DIR", str(Path(__file__).parent / f"_agt_{uuid.uuid4().hex[:8]}"))
os.environ["LLM_PROVIDER"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["MINIMAX_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["EMBEDDING_BACKEND"] = "off"
os.environ["EMBEDDING_API_KEY"] = ""

from app import db  # noqa: E402
from app.agents import runner  # noqa: E402
from app.agents.graph import build_graph  # noqa: E402
from app.agents.nodes import (  # noqa: E402
    _check_sufficiency, planner_node, recover_node, responder_node, router_node,
    verifier_node,
)
from app.agents.state import MAX_REGEN, MAX_RE_RETRIEVE  # noqa: E402
from app.parsing.pipeline import parse_pdf  # noqa: E402
from app.rag import indexing  # noqa: E402
from app.rag.chunking import chunk_docir  # noqa: E402
from tests.fixtures_pdf import make_paper_pdf  # noqa: E402


@pytest.fixture(scope="module")
def indexed_paper() -> str:
    db.init_db()
    pdf = Path(os.environ["SCHOLAR_DATA_DIR"]) / "agt.pdf"
    make_paper_pdf(pdf, "Attention Is All You Need",
                   body_facts=["The model was trained on 8 NVIDIA P100 GPUs for 3.5 days.",
                               "The base model reaches 27.3 BLEU on English-to-German."],
                   refs=["Vaswani et al. 2017. Attention Is All You Need. arXiv:1706.03762"])
    docir = parse_pdf(pdf)
    chunks = chunk_docir(docir)
    indexing.index_chunks("p_agt", chunks)
    return "p_agt"


def test_router_rules(indexed_paper):
    st = router_node({"question": "你好", "paper_ids": [indexed_paper]})
    assert st["intent"]["type"] == "chitchat"
    st = router_node({"question": "对比这两篇的BLEU", "paper_ids": ["a", "b"]})
    assert st["intent"]["type"] == "qa_compare"
    st = router_node({"question": "总结一下这篇论文", "paper_ids": [indexed_paper]})
    assert st["intent"]["type"] == "summarize"
    st = router_node({"question": "模型用了多少GPU训练？", "paper_ids": [indexed_paper]})
    assert st["intent"]["type"] == "qa_single"
    assert st["trace"][0]["node"] == "router"


def test_planner_rule_and_replan(indexed_paper):
    st = planner_node({"question": "GPU", "paper_ids": [indexed_paper],
                       "intent": {"type": "qa_single", "aspects": ["GPU"], "language": "zh"},
                       "retries": 0})
    assert len(st["plan"]["steps"]) == 1
    assert st["plan"]["steps"][0]["paper_id"] == indexed_paper
    # 两篇 × 两面 = 4 步
    st2 = planner_node({"question": "对比", "paper_ids": ["a", "b"],
                        "intent": {"type": "qa_compare", "aspects": ["BLEU", "GPU"]},
                        "retries": 0})
    assert len(st2["plan"]["steps"]) == 4


def test_verifier_recovery_signal(indexed_paper):
    ok_state = {"plan": {"steps": [{"paper_id": indexed_paper, "query": "GPU", "aspect": "GPU"}]},
                "evidence": [{"chunk_id": "c1", "aspect": "GPU", "text": "x"}]}
    assert _check_sufficiency(ok_state) is True
    assert verifier_node(ok_state)["trace"][0]["payload"]["sufficient"] is True

    bad_state = {"plan": {"steps": [{"paper_id": indexed_paper, "query": "GPU", "aspect": "GPU"},
                                    {"paper_id": indexed_paper, "query": "BLEU", "aspect": "BLEU"}]},
                 "evidence": [{"chunk_id": "c1", "aspect": "GPU", "text": "x"}],
                 "retries": 0}
    upd = verifier_node(bad_state)
    assert upd["pending_failure"]["kind"] == "insufficient_evidence"
    assert upd["pending_failure"]["missing"] == ["BLEU"]
    assert upd["failures"][0]["node"] == "verifier"
    assert _check_sufficiency(bad_state) is False


def test_recover_three_strategies(indexed_paper):
    """recover 节点单测：三种失败类型 → 三种路由与产物。"""
    # ① 证据不足重检索：改写缺失面、放宽 top_k、回 executor
    st = {"question": "GPU?", "paper_ids": [indexed_paper], "retries": 0,
          "pending_failure": {"kind": "insufficient_evidence", "missing": ["BLEU"]},
          "plan": {"steps": [{"paper_id": indexed_paper, "query": "GPU", "aspect": "GPU"},
                             {"paper_id": indexed_paper, "query": "BLEU", "aspect": "BLEU"}],
                   "mode": "single"}}
    upd = recover_node(st)
    assert upd["recover_route"] == "executor"
    assert upd["re_retrieve_count"] == 1 and upd["retries"] == 1
    assert {s["aspect"] for s in upd["plan"]["steps"]} == {"GPU", "BLEU"}
    assert upd["pending_failure"] is None
    assert upd["trace"][0]["node"] == "recover"

    # ② 工具失败降级：标记 degraded、回 responder
    upd = recover_node({"pending_failure": {"kind": "tool_failure", "detail": "LLM 空响应"}})
    assert upd["recover_route"] == "responder" and upd["degraded"] is True
    assert "tool_failure_degraded" in upd["guardrails"]

    # ③ 格式非法重生成：组装纠错反馈、回 responder；预算尽 → 警告放行收尾
    pend = {"kind": "format_invalid", "invalid_pages": [99], "valid_pages": [1, 3]}
    upd = recover_node({"pending_failure": pend, "regen_count": 0, "answer": "旧答案"})
    assert upd["recover_route"] == "responder" and upd["regen_count"] == 1
    assert "p99" in upd["retry_prompt"] and "1, 3" in upd["retry_prompt"]

    upd = recover_node({"pending_failure": pend, "regen_count": MAX_REGEN, "answer": "旧答案"})
    assert upd["recover_route"] == "end"
    assert "请人工核对原文" in upd["answer"]


def test_full_graph_reretrieve_on_missing_aspect(indexed_paper, monkeypatch):
    """图级策略①：一个检索面零命中 → verifier→recover→executor 重检索循环 → 预算尽带部分证据作答。"""
    import app.agents.graph as graph_mod

    def stub_router(state, config=None):
        return {"intent": {"type": "qa_single",
                           "aspects": ["GPU training hardware", "quantum teleportation protocol"],
                           "language": "en"},
                "trace": []}

    monkeypatch.setattr(graph_mod, "router_node", stub_router)
    graph = graph_mod.build_graph()
    final = graph.invoke(
        {"question": "GPU training and quantum teleportation", "paper_ids": [indexed_paper],
         "retries": 0}, {"recursion_limit": 40})
    assert final["re_retrieve_count"] >= 1, "零命中检索面应触发重检索"
    assert final["recover_count"] >= 1
    assert final["answer"], "预算用尽后应以部分证据作答"
    rec_cards = [c for c in final["trace"] if c["node"] == "recover"]
    assert rec_cards and rec_cards[0]["title"] == "恢复·重检索"


def test_full_graph_degrade_on_tool_failure(indexed_paper, monkeypatch):
    """图级策略②：LLM 流式+同步均失败 → responder→recover→responder 抽取式降级。"""
    from app.llm import client as llm_client

    def boom(msgs, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(llm_client, "available", lambda force=False: True)
    monkeypatch.setattr(llm_client, "stream_chat", boom)
    monkeypatch.setattr(llm_client, "chat", lambda msgs, **kw: {"text": "", "error": "boom"})

    graph = build_graph()
    final = graph.invoke({"question": "How many GPUs was the model trained on?",
                          "paper_ids": [indexed_paper], "retries": 0},
                         {"recursion_limit": 40})
    assert final["mode"] == "extractive"
    assert final["degraded"] is True
    assert final["answer"] and "抽取式证据模式" in final["answer"]
    assert "llm_call_failed" in final["guardrails"]
    assert "tool_failure_degraded" in final["guardrails"]
    rec = [c for c in final["trace"] if c["node"] == "recover"]
    assert rec and rec[0]["title"] == "恢复·降级"


def test_full_graph_regen_on_invalid_format(indexed_paper, monkeypatch):
    """图级策略③：首次回答引用非法页码 → recover 注入纠错反馈 → 第二次定向重生成为合法引用。"""
    from app.llm import client as llm_client

    calls = {"n": 0}

    def fake_stream(msgs, **kw):
        calls["n"] += 1
        if any("引用格式校验" in m.get("content", "") for m in msgs if m["role"] == "user"):
            return iter(["The model was trained on 8 P100 GPUs [^p1-§Training]."])
        return iter(["Trained on 8 GPUs [^p42-§Nowhere]."])  # 非法页码 → 触发重生成

    monkeypatch.setattr(llm_client, "available", lambda force=False: True)
    monkeypatch.setattr(llm_client, "stream_chat", fake_stream)
    monkeypatch.setattr(llm_client, "chat", lambda msgs, **kw:
                        {"text": '{"claims":[]}', "model": "stub"})

    graph = build_graph()
    final = graph.invoke({"question": "How many GPUs?", "paper_ids": [indexed_paper],
                          "retries": 0}, {"recursion_limit": 40})
    assert calls["n"] == 2, "应恰好重生成一次"
    assert final["regen_count"] == 1
    assert "[^p1" in final["answer"] and "[^p42" not in final["answer"]
    assert "invalid_citation:p42" in final["guardrails"]
    assert "format_regen:1" in final["guardrails"]
    assert final["pending_failure"] is None
    rec = [c for c in final["trace"] if c["node"] == "recover"]
    assert rec and rec[0]["title"] == "恢复·重生成"


def test_full_graph_extractive(indexed_paper):
    """无 LLM：规则路由 → 规划 → 并行检索 → 校验 → 抽取式应答，图完整跑通。"""
    q: queue_mod.Queue = queue_mod.Queue()
    runner.run_stream("How many GPUs was the model trained on?", [indexed_paper], q)
    events = []
    while True:
        kind, data = q.get(timeout=60)
        events.append((kind, data))
        if kind == "done":
            break
    kinds = [k for k, _ in events]
    assert kinds[-1] == "done" and "retrieval" in kinds and "final" in kinds
    # trace 卡片：live 阶段 + final 全量都要有，且覆盖全部主节点
    final = next(d for k, d in events if k == "final")
    nodes = {c["node"] for c in final["trace"]}
    assert {"router", "planner", "retrieve_task", "executor_fuse", "verifier", "responder"} <= nodes
    assert final["mode"] == "extractive" and final["answer"]
    retrieval = next(d for k, d in events if k == "retrieval")
    assert retrieval["hits"]


def test_llm_stub_generative_path(indexed_paper, monkeypatch):
    """stub LLM：验证生成式路径 + 引用后验 + ClaimTrace。"""
    from app.llm import client as llm_client

    fake_answer = ("The model was trained on 8 NVIDIA P100 GPUs [^p1-§Training]. "
                   "Training took 3.5 days [^p1-§Training].")
    monkeypatch.setattr(llm_client, "available", lambda force=False: True)
    monkeypatch.setattr(llm_client, "stream_chat", lambda msgs, **kw: iter([fake_answer]))
    monkeypatch.setattr(llm_client, "chat", lambda msgs, **kw:
                        {"text": '{"claims":[{"text":"8 GPUs on one machine","status":"supported","pages":[1]}]}',
                         "model": "stub"})
    monkeypatch.setattr(llm_client, "_endpoint", lambda: ("stub", "k", "stub"))

    q: queue_mod.Queue = queue_mod.Queue()
    runner.run_stream("How many GPUs?", [indexed_paper], q)
    events = []
    while True:
        kind, data = q.get(timeout=60)
        events.append((kind, data))
        if kind == "done":
            break
    deltas = [d for k, d in events if k == "delta"]
    assert deltas, "生成式模式应有 delta 流"
    final = next(d for k, d in events if k == "final")
    assert final["mode"] == "generative"
    assert "[^p1" in final["answer"]  # 有效引用锚点保留
    assert any(c["status"] == "supported" for c in final["claims"])


def test_fanout_fuse_dedup(indexed_paper, monkeypatch):
    """同篇两路扇出 → fuse 去重（chunk_id 唯一）。"""
    import app.agents.graph as graph_mod

    def stub_router(state, config=None):  # 强制双检索面 → 两路并行
        return {"intent": {"type": "qa_compare", "aspects": ["GPU training", "BLEU score"],
                           "language": "en"},
                "trace": []}

    monkeypatch.setattr(graph_mod, "router_node", stub_router)
    graph = graph_mod.build_graph()
    final = graph.invoke({"question": "GPUs and BLEU", "paper_ids": [indexed_paper],
                          "retries": 0}, {"recursion_limit": 25})
    ids = [h["chunk_id"] for h in final.get("evidence", [])]
    assert len(ids) == len(set(ids)), "融合后证据不应有重复 chunk"
    assert len(final.get("branch_stats", [])) >= 2
