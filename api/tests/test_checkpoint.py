"""断点续跑单元测试（密闭：独立数据目录，LLM 用 stub/禁用）。

覆盖：
① 图级崩溃 → invoke(None) 断点续跑 → 已完成节点不重跑（trace 无重复卡片）
② 同会话两轮独立 thread → 累加键不跨轮污染
③ runner 集成：run_stream 注入故障失败 → 线程落账 failed → resume_stream 续跑成功
④ threads REST 端点：列表/详情/恢复 SSE
"""
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
os.environ.pop("SCHOLAR_FAIL_AT", None)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import threads_store
from app.agents import (
    _debug,
    runner,
)
from app.agents.checkpointer import get_saver
from app.agents.graph import build_graph
from app.parsing.pipeline import parse_pdf
from app.rag import indexing
from app.rag.chunking import chunk_docir
from tests.fixtures_pdf import make_paper_pdf


@pytest.fixture(scope="module")
def indexed_paper() -> str:
    from app import db
    db.init_db()
    pdf = Path(os.environ["SCHOLAR_DATA_DIR"]) / "ckpt.pdf"
    make_paper_pdf(pdf, "Attention Is All You Need",
                   body_facts=["The model was trained on 8 NVIDIA P100 GPUs for 3.5 days.",
                               "The base model reaches 27.3 BLEU on English-to-German."],
                   refs=["Vaswani et al. 2017. Attention Is All You Need. arXiv:1706.03762"])
    docir = parse_pdf(pdf)
    chunks = chunk_docir(docir)
    indexing.index_chunks("p_ckpt", chunks)
    return "p_ckpt"


def _invoke_config(thread_id: str) -> dict:
    return {"recursion_limit": 40, "configurable": {"thread_id": thread_id}}


def _drain(q: queue_mod.Queue) -> list[tuple[str, dict]]:
    events = []
    while True:
        kind, data = q.get(timeout=60)
        events.append((kind, data))
        if kind == "done":
            return events


# ---------- ① 崩溃 → 断点续跑 → 不重跑 ----------

def test_crash_and_resume_no_rerun(indexed_paper, monkeypatch):
    import app.agents.graph as graph_mod

    real_verifier = graph_mod.verifier_node
    calls = {"n": 0}

    def flaky_verifier(state, config=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected crash at verifier")
        return real_verifier(state, config)

    monkeypatch.setattr(graph_mod, "verifier_node", flaky_verifier)
    graph = graph_mod.build_graph(checkpointer=get_saver())

    tid = f"conv_crash_{uuid.uuid4().hex[:8]}#1"
    inp = {"question": "How many GPUs was the model trained on?",
           "paper_ids": [indexed_paper], "retries": 0}
    with pytest.raises(RuntimeError, match="injected crash"):
        graph.invoke(inp, _invoke_config(tid))

    # 断点视角：下一个待执行节点应是 verifier
    snap = graph.get_state({"configurable": {"thread_id": tid}})
    assert "verifier" in snap.next

    # invoke(None) 续跑：从 verifier 继续，router/planner 不重跑
    final = graph.invoke(None, _invoke_config(tid))
    assert calls["n"] == 2, "verifier 应恰好重执行一次"
    assert final["answer"], "续跑后应产出答案"
    nodes_count: dict[str, int] = {}
    for card in final["trace"]:
        nodes_count[card["node"]] = nodes_count.get(card["node"], 0) + 1
    assert nodes_count.get("router") == 1, "router 卡片不应重复（已完成节点未重跑）"
    assert nodes_count.get("planner") == 1, "planner 卡片不应重复"
    assert nodes_count.get("verifier") == 1
    assert nodes_count.get("responder") == 1


# ---------- ② 会话轮次隔离 ----------

def test_turn_isolation(indexed_paper):
    graph = build_graph(checkpointer=get_saver())
    conv = f"conv_turn_{uuid.uuid4().hex[:8]}"
    inp1 = {"question": "How many GPUs was the model trained on?",
            "paper_ids": [indexed_paper], "retries": 0}
    inp2 = {"question": "What BLEU score does the base model reach?",
            "paper_ids": [indexed_paper], "retries": 0}
    final1 = graph.invoke(inp1, _invoke_config(threads_store.make_thread_id(conv, 1)))
    final2 = graph.invoke(inp2, _invoke_config(threads_store.make_thread_id(conv, 2)))

    def count(trace, node):
        return sum(1 for c in trace if c["node"] == node)

    assert count(final1["trace"], "router") == 1
    assert count(final2["trace"], "router") == 1, "第二轮不应携带第一轮的累加卡片"
    assert len(final1["trace"]) == len(final2["trace"]), "两轮轨迹长度应一致（无跨轮污染）"
    assert final2["question"] == inp2["question"]


# ---------- ③ runner 集成：失败落账 → 续跑成功 ----------

def test_runner_fail_then_resume(indexed_paper, monkeypatch):
    conv = f"conv_runner_{uuid.uuid4().hex[:8]}"
    tid = threads_store.make_thread_id(conv, 1)

    monkeypatch.setenv("SCHOLAR_FAIL_AT", "verifier:1")
    q: queue_mod.Queue = queue_mod.Queue()
    runner.run_stream("How many GPUs was the model trained on?", [indexed_paper], q,
                      thread_id=tid, conversation_id=conv, turn=1)
    events = _drain(q)
    final = next(d for k, d in events if k == "final")
    assert final["resumable"] is True and final["thread_id"] == tid
    assert any(g.startswith("graph_error:RuntimeError") for g in final["guardrails"])
    row = threads_store.get_thread(tid)
    assert row["status"] == "failed" and row["error"]

    # 手动恢复：清掉故障注入 → 从 verifier 断点继续
    monkeypatch.setenv("SCHOLAR_FAIL_AT", "")
    q2: queue_mod.Queue = queue_mod.Queue()
    runner.resume_stream(tid, q2)
    events2 = _drain(q2)
    final2 = next(d for k, d in events2 if k == "final")
    assert final2["answer"], "续跑应产出答案"
    kinds = [k for k, _ in events2]
    assert "retrieval" in kinds, "恢复成功路径应补发 retrieval 事件"
    row2 = threads_store.get_thread(tid)
    assert row2["status"] == "done" and row2["resume_attempts"] == 1


def test_resume_guards(indexed_paper):
    q: queue_mod.Queue = queue_mod.Queue()
    runner.resume_stream(f"nope_{uuid.uuid4().hex[:8]}:1", q)
    events = _drain(q)
    final = next(d for k, d in events if k == "final")
    assert "resume_not_found" in final["guardrails"]

    done_tid = f"conv_done_{uuid.uuid4().hex[:8]}:1"
    threads_store.start_thread(done_tid, done_tid.rsplit(":", 1)[0], 1, "q", [])
    threads_store.finish_thread(done_tid, "done", answer="ok")
    q2: queue_mod.Queue = queue_mod.Queue()
    runner.resume_stream(done_tid, q2)
    final2 = next(d for k, d in _drain(q2) if k == "final")
    assert "resume_already_done" in final2["guardrails"]


# ---------- ④ REST 端点 ----------

def test_threads_endpoints(indexed_paper, monkeypatch):
    from app.api_routes import threads_routes

    conv = f"conv_api_{uuid.uuid4().hex[:8]}"
    tid = threads_store.make_thread_id(conv, 1)
    threads_store.start_thread(tid, conv, 1, "How many GPUs?", [indexed_paper])
    threads_store.finish_thread(tid, "failed", error="RuntimeError: injected")

    app = FastAPI()
    app.include_router(threads_routes.router)
    client = TestClient(app)

    # 列表：按会话过滤到 failed 线程
    rows = client.get("/api/threads", params={"conversation_id": conv}).json()["threads"]
    assert len(rows) == 1 and rows[0]["status"] == "failed" and rows[0]["thread_id"] == tid

    # 详情：pending 为空（该线程无 checkpoint）
    detail = client.get(f"/api/threads/{tid}").json()
    assert detail["status"] == "failed" and detail["pending"] == []
    assert client.get("/api/threads/unknown_#1").status_code == 404

    # 恢复：SSE final+done，落账 done
    def fake_resume(thread_id, q):
        threads_store.finish_thread(thread_id, "done", answer="resumed ok")
        q.put(("final", {"answer": "resumed ok", "claims": [], "mode": "extractive",
                         "guardrails": [], "trace": [], "thread_id": thread_id}))
        q.put(("done", {}))

    monkeypatch.setattr(runner, "resume_stream", fake_resume)
    resp = client.post(f"/api/chat/{tid}/resume")
    assert resp.status_code == 200
    assert "event: final" in resp.text and "event: done" in resp.text
    assert threads_store.get_thread(tid)["status"] == "done"

    # 已完成线程再恢复 → 409
    assert client.post(f"/api/chat/{tid}/resume").status_code == 409


# ---------- ⑤ 故障注入钩子本身 ----------

def test_failable_hook(monkeypatch):
    monkeypatch.setenv("SCHOLAR_FAIL_AT", "verifier:2")
    _debug._fail_counts.clear()

    def fn(state, config=None):
        return "ok"

    w = _debug.failable("verifier", fn)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            w({})
    assert w({}) == "ok"
    monkeypatch.setenv("SCHOLAR_FAIL_AT", "")
    assert w({}) == "ok"
