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
    _check_sufficiency, planner_node, responder_node, router_node, verifier_node,
)
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


def test_verifier_replan_loop(indexed_paper):
    ok_state = {"plan": {"steps": [{"paper_id": indexed_paper, "query": "GPU", "aspect": "GPU"}]},
                "evidence": [{"chunk_id": "c1", "aspect": "GPU", "text": "x"}]}
    assert _check_sufficiency(ok_state) is True
    assert verifier_node(ok_state)["trace"][0]["payload"]["sufficient"] is True

    bad_state = {"plan": {"steps": [{"paper_id": indexed_paper, "query": "GPU", "aspect": "GPU"},
                                    {"paper_id": indexed_paper, "query": "BLEU", "aspect": "BLEU"}]},
                 "evidence": [{"chunk_id": "c1", "aspect": "GPU", "text": "x"}],
                 "retries": 0}
    upd = verifier_node(bad_state)
    assert upd["retries"] == 1
    assert _check_sufficiency(bad_state) is False


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
