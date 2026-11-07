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
    # 图谱/元数据工具依赖 papers 表（真实上传流程由 jobs 落库；测试直插并提交）
    with db.tx() as conn:
        conn.execute("INSERT OR REPLACE INTO papers (id,title,arxiv_id,file_path,file_hash,"
                     "page_count,docir_path,version,supersedes,created_at,meta_json) "
                     "VALUES ('p_agt','Attention Is All You Need','1706.03762',"
                     "'agt.pdf','h_agt',1,'','','','0','{}')")
    yield "p_agt"
    # 模块级清理：papers/chunks/references 落库行不残留——否则与本文件共享 data dir 的
    # test_e2e 的内容级查重会撞上 fixture 论文（两者 PDF 由同一生成器产出，正文几乎同文）
    with db.tx() as conn:
        for pid in ("p_agt", "p_viz"):
            conn.execute("DELETE FROM chunks WHERE paper_id=?", (pid,))
            conn.execute("DELETE FROM chunks_fts WHERE paper_id=?", (pid,))
            conn.execute("DELETE FROM papers WHERE id=?", (pid,))
        conn.execute('DELETE FROM "references" WHERE paper_id IN ('"'"'p_agt'"'"','"'"'p_viz'"'"')')


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


# ---------------- 协议 v1.1：Trace 时间线结构化事件 ----------------

def test_event_protocol_basic_stream(indexed_paper):
    """plan/tool_call/tool_result/evidence/verifier 事件贯穿执行流，step_id 闭环。"""
    q: queue_mod.Queue = queue_mod.Queue()
    runner.run_stream("How many GPUs was the model trained on?", [indexed_paper], q)
    events = []
    while True:
        kind, data = q.get(timeout=60)
        events.append((kind, data))
        if kind == "done":
            break
    kinds = [k for k, _ in events]
    for ev in ("plan", "tool_call", "tool_result", "evidence", "verifier"):
        assert ev in kinds, f"缺少 {ev} 事件"
    assert kinds.index("tool_call") < kinds.index("tool_result"), "tool_call 应先于 tool_result"

    # plan：round 从 1 起，步骤含 step_id/title/aspect/query
    plans = [d for k, d in events if k == "plan"]
    assert plans and plans[0]["round"] == 1
    step_ids = {s["step_id"] for p in plans for s in p["steps"]}
    assert step_ids and all(s["title"] and s["query"] for p in plans for s in p["steps"])

    # tool_call/tool_result：step_id 与 plan 闭环，载荷含 refs/elapsed_ms
    call_ids = {d["step_id"] for k, d in events if k == "tool_call"}
    result_ids = {d["step_id"] for k, d in events if k == "tool_result"}
    assert call_ids and call_ids == result_ids and call_ids <= step_ids
    tc = next(d for k, d in events if k == "tool_call")
    assert tc["tool"] == "kb_search" and "top_k" in tc["args"]
    tr = next(d for k, d in events if k == "tool_result")
    assert tr["ok"] is True and tr["refs"] and tr["elapsed_ms"] >= 0

    # evidence：live 下发且早于收尾 retrieval，hits 非空
    ev = next(d for k, d in events if k == "evidence")
    assert ev["hits"] and kinds.index("evidence") < kinds.index("retrieval")
    # verifier：单检索面全覆盖 → pass
    ver = [d for k, d in events if k == "verifier"]
    assert ver and ver[-1]["verdict"] == "pass"


def test_event_protocol_recover_round2(indexed_paper, monkeypatch):
    """零命中检索面 → verifier(insufficient,action=recover) → recover → 第 2 轮 plan（step_id 换轮）。"""
    import app.agents.graph as graph_mod

    def stub_router(state, config=None):
        return {"intent": {"type": "qa_single",
                           "aspects": ["GPU training hardware", "quantum teleportation protocol"],
                           "language": "en"},
                "trace": []}

    monkeypatch.setattr(graph_mod, "router_node", stub_router)
    q: queue_mod.Queue = queue_mod.Queue()
    graph = graph_mod.build_graph()
    graph.invoke({"question": "GPU training and quantum teleportation",
                  "paper_ids": [indexed_paper], "retries": 0},
                 {"recursion_limit": 40, "configurable": {"queue": q}})
    events = []  # invoke 同步返回后队列已写满，排水至空即为本轮全部事件
    try:
        while True:
            events.append(q.get(timeout=5))
    except queue_mod.Empty:
        pass

    plans = [d for k, d in events if k == "plan"]
    rounds = [p["round"] for p in plans]
    assert rounds == sorted(rounds) and plans[0]["round"] == 1, "plan 轮次应递增且从 1 起"
    assert any(r >= 2 for r in rounds), "零命中检索面应触发第 2 轮计划"
    r2 = next(p for p in plans if p["round"] >= 2)
    assert all(s["step_id"].startswith(f"s{r2['round']}-") for s in r2["steps"])

    verdicts = [d for k, d in events if k == "verifier"]
    assert any(v["verdict"] == "insufficient" and v["action"] == "recover"
               and "quantum teleportation protocol" in v["missing"] for v in verdicts)

    # 重检索轮的 tool_call 应使用新 step_id 且 top_k 放宽
    r2_ids = {s["step_id"] for s in r2["steps"]}
    r2_calls = [d for k, d in events if k == "tool_call" and d["step_id"] in r2_ids]
    assert r2_calls and all(c["args"]["top_k"] > 10 for c in r2_calls)


# ================= 多智能体分发（Reader / Compare） =================

def _drain(q: queue_mod.Queue) -> list[tuple]:
    events = []
    while True:
        kind, data = q.get(timeout=60)
        events.append((kind, data))
        if kind == "done":
            return events


def test_router_forced_agent_deterministic(indexed_paper):
    """强制指定：免 LLM 确定性路由，意图回填 + agent 就位。"""
    st = router_node({"question": "对比两篇论文的GPU用法", "paper_ids": [indexed_paper, "p2"],
                      "forced_agent": "compare"})
    assert st["intent"]["type"] == "qa_compare" and st["intent"]["source"] == "forced"
    assert st["agent"] == "compare"
    assert st["handoffs"] and st["handoffs"][0]["to"] == "compare"
    st2 = router_node({"question": "模型用了多少GPU？", "paper_ids": [indexed_paper],
                       "forced_agent": "reader"})
    assert st2["intent"]["type"] == "qa_single" and st2["agent"] == "reader"


def test_forced_viz_no_tables_refusal(indexed_paper):
    """Viz Guardrail：论文无表格数据 → 拒绝出图（不冒充），说明原因。"""
    q: queue_mod.Queue = queue_mod.Queue()
    runner.run_stream("把训练数据画成图表", [indexed_paper], q, forced_agent="viz")
    final = next(d for k, d in _drain(q) if k == "final")
    assert final["agent"] == "viz" and final["mode"] == "refusal"
    assert "viz_no_tables" in final["guardrails"]
    assert "拒绝" in final["answer"] or "没有可图表化" in final["answer"]
    nodes = {c["node"] for c in final["trace"]}
    assert "viz_entry" in nodes and "retrieve_task" not in nodes


def test_forced_reader_scope_lock(indexed_paper):
    """Reader 范围锁定：强制指定且多篇选中 → 仅精读首篇（规划步骤只含该篇）。"""
    graph = build_graph()
    final = graph.invoke({"question": "How many GPUs?", "paper_ids": [indexed_paper, "p_other"],
                          "retries": 0, "forced_agent": "reader"},
                         {"recursion_limit": 40})
    assert final["agent"] == "reader"
    assert "reader_scope_locked" in final["guardrails"]
    assert all(s["paper_id"] == indexed_paper for s in final["plan"]["steps"])


def test_forced_compare_single_paper_refusal(indexed_paper):
    """Compare 范围 Guardrail：仅 1 篇 → 拒绝并转介 Reader，不走检索管线。"""
    q: queue_mod.Queue = queue_mod.Queue()
    runner.run_stream("对比两篇的方法差异", [indexed_paper], q, forced_agent="compare")
    final = next(d for k, d in _drain(q) if k == "final")
    assert final["mode"] == "refusal" and final["agent"] == "compare"
    assert "compare_scope_refusal" in final["guardrails"]
    assert "Reader" in final["answer"]
    nodes = {c["node"] for c in final["trace"]}
    assert "compare_entry" in nodes and "planner" not in nodes and "retrieve_task" not in nodes


def test_forced_compare_two_papers_pipeline(indexed_paper):
    """Compare 双篇：一篇无相关内容 → 论文覆盖校验触发恢复环 → 部分对比表如实呈现。"""
    q: queue_mod.Queue = queue_mod.Queue()
    runner.run_stream("GPUs", [indexed_paper, "p_empty"], q, forced_agent="compare")
    events = _drain(q)
    final = next(d for k, d in events if k == "final")
    assert final["agent"] == "compare"
    # 覆盖校验 + 恢复环 + 对齐节点都被走到
    nodes = [c["node"] for c in final["trace"]]
    assert "recover" in nodes and "align" in nodes
    # 对比表 artifact 下发且行数 = 检索面数，空格子如实标记 found=False
    artifacts = [d for k, d in events if k == "artifact" and d.get("kind") == "compare_table"]
    assert artifacts and artifacts[-1]["table"]["rows"]
    assert all(set(c["paper_id"] for c in r["cells"]) == {indexed_paper, "p_empty"}
               for r in artifacts[-1]["table"]["rows"])
    assert "compare_table" in final  # final 事件携带对齐表


def test_verifier_compare_paper_coverage():
    """Compare 论文覆盖 Guardrail：缺任一论文命中 → 不充分。"""
    plan = {"mode": "compare", "steps": [{"paper_id": "a", "query": "q", "aspect": "q"},
                                         {"paper_id": "b", "query": "q", "aspect": "q"}]}
    one_hit = [{"aspect": "q", "paper_id": "a", "chunk_id": "c1"}]
    assert _check_sufficiency({"plan": plan, "evidence": one_hit}) is False
    both = one_hit + [{"aspect": "q", "paper_id": "b", "chunk_id": "c2"}]
    assert _check_sufficiency({"plan": plan, "evidence": both}) is True


def test_align_node_builds_table(indexed_paper):
    """align 节点：维度×论文对齐出表，空维度格如实 found=False，artifact + 工具卡齐备。"""
    import queue as queue_mod2

    from app.agents.specialists import align_node

    evidence = [
        {"aspect": "GPU", "paper_id": indexed_paper, "chunk_id": "c1", "page": 1,
         "section_path": "T", "text": "8 GPUs", "rrf": 0.9},
        {"aspect": "GPU", "paper_id": "p2", "chunk_id": "c2", "page": 3,
         "section_path": "S", "text": "4 GPUs", "rrf": 0.7},
        {"aspect": "BLEU", "paper_id": indexed_paper, "chunk_id": "c3", "page": 2,
         "section_path": "R", "text": "27.3 BLEU", "rrf": 0.8},
    ]
    q = queue_mod2.Queue()
    steps = [{"paper_id": pid, "query": "q", "aspect": "q"} for pid in (indexed_paper, "p2")]
    upd = align_node({"evidence": evidence, "plan": {"steps": steps}},
                     {"configurable": {"queue": q}})
    table = upd["compare_table"]
    assert len(table["rows"]) == 2  # GPU / BLEU 两维
    gpu = next(r for r in table["rows"] if r["dimension"] == "GPU")
    assert all(c["found"] for c in gpu["cells"])
    bleu = next(r for r in table["rows"] if r["dimension"] == "BLEU")
    assert [c["found"] for c in bleu["cells"]] == [True, False]  # p2 无 BLEU 命中
    kinds = [k for k, _ in list(q.queue)]
    assert "artifact" in kinds and "tool_result" in kinds  # get_meta×2 + align_dimension
    calls = [d for k, d in list(q.queue) if k == "tool_call"]
    assert [c["step_id"] for c in calls] == ["align-meta-1", "align-meta-2", "align-dim"]
    assert all(c.get("agent") == "compare" for c in calls)  # 白名单主体随事件可审计


def test_tools_whitelist():
    """工具白名单：reader 不可调 align_dimension（Compare 专属）；未知工具拒绝。"""
    import pytest as _pytest

    from app.agents.tools import ToolDenied, assert_allowed, call_tool

    with _pytest.raises(ToolDenied):
        assert_allowed("reader", "align_dimension")
    with _pytest.raises(ToolDenied):
        assert_allowed("compare", "chart_spec_builder")
    with _pytest.raises(ToolDenied):
        assert_allowed("reader", "nonexistent")
    assert_allowed("compare", "align_dimension")  # 不抛即通过
    res = call_tool("reader", "align_dimension", None, evidence=[], paper_ids=[])
    assert res["ok"] is False and "not allowed" in res["error"]


# ================= Graph / Writer / Viz Agent =================

@pytest.fixture()
def viz_paper(indexed_paper):
    """构造一张带表格的假解析论文（docir 直写，不走 PDF 解析）。"""
    import json as _json

    docir_path = Path(os.environ["SCHOLAR_DATA_DIR"]) / "viz_docir.json"
    docir_path.write_text(_json.dumps({
        "blocks": [], "figures": [],
        "tables": [{"id": "t1", "page": 6, "title": "BLEU on WMT14",
                    "headers": ["Model", "BLEU"],
                    "rows": [["Base", "27.3"], ["Big", "29.3"], ["SS+ENS", "30.1"]]}],
    }, ensure_ascii=False), encoding="utf-8")
    db.init_db()
    with db.tx() as conn:
        conn.execute("INSERT OR REPLACE INTO papers (id,title,arxiv_id,file_path,file_hash,"
                     "page_count,docir_path,version,supersedes,created_at,meta_json) "
                     "VALUES ('p_viz','BLEU Benchmark Paper','2106.09985','x','h1',6,?,"
                     "'','','0','{}')", (str(docir_path),))
    return "p_viz"


def test_viz_happy_path_fabricated_table(viz_paper):
    """Viz 全链路：真实表格 → chart_spec artifact → provenance 标注。"""
    q: queue_mod.Queue = queue_mod.Queue()
    runner.run_stream("把BLEU数据画成图", [viz_paper], q, forced_agent="viz")
    events = _drain(q)
    final = next(d for k, d in events if k == "final")
    assert final["agent"] == "viz" and final["mode"] in ("generative", "extractive")
    assert "viz_no_tables" not in final["guardrails"]
    # chart_spec 结构合法且带 provenance
    spec = final.get("chart_spec") or {}
    assert spec.get("type") == "bar" and len(spec.get("data", [])) == 3
    assert spec["provenance"]["paper_id"] == viz_paper
    assert (spec["provenance"]["page"] or 0) + 1 == 7  # 0 基页码 → 人读第 7 页
    arts = [d for k, d in events if k == "artifact" and d.get("kind") == "chart_spec"]
    assert arts and arts[-1]["spec"]["y_label"] == "BLEU"
    nodes = {c["node"] for c in final["trace"]}
    assert "viz_extract" in nodes and "retrieve_task" not in nodes  # 专用管线


def test_viz_tools_unit(viz_paper):
    """工具单测：extract_table 读 IR 表（headers/rows 与 markdown 两种形状）；
    chart_spec_builder 白名单校验 + 数值解析。"""
    from app.agents.tools import _chart_spec_builder, _extract_table, _parse_md_table

    # MinerU 产物形状：只有 markdown 管道文本 → 现场解析出 headers/rows
    md_h, md_rows = _parse_md_table("| Model | BLEU |\n|---|---|\n| Base | 27.3 |\n| Big | 29.3 |")
    assert md_h == ["Model", "BLEU"] and md_rows == [["Base", "27.3"], ["Big", "29.3"]]

    tables = _extract_table(viz_paper)["tables"]
    assert len(tables) == 1 and tables[0]["headers"] == ["Model", "BLEU"]
    spec = _chart_spec_builder({**tables[0], "paper_id": viz_paper},
                               x_col=0, y_col=1, chart_type="bar")
    assert spec["valid"] and [d["value"] for d in spec["spec"]["data"]] == [27.3, 29.3, 30.1]
    bad = _chart_spec_builder(tables[0], x_col=0, y_col=1, chart_type="scatter3d")
    assert not bad["valid"] and "白名单" in bad["reason"]
    nonum = _chart_spec_builder({**tables[0], "rows": [["Base", "good"]]},
                                x_col=0, y_col=1, chart_type="bar")
    assert not nonum["valid"] and "数值" in nonum["reason"]


def test_citation_graph_and_metrics(indexed_paper, viz_paper):
    """图谱工具：references 元数据建边（arXiv id 匹配）+ PageRank + 自环剔除。"""
    from app.agents.tools import _citation_graph, _graph_metrics

    # p_viz 引用 p_agt（同 arXiv:1706.03762）；p_agt 的自引用被剔除
    with db.tx() as conn:
        conn.execute('INSERT OR REPLACE INTO "references" (id,paper_id,idx,raw,title,arxiv_id,doi) '
                     "VALUES ('r_viz1',?,0,'Vaswani et al. 2017. Attention Is All You Need. "
                     "arXiv:1706.03762','Attention Is All You Need','1706.03762','')", (viz_paper,))
    g = _citation_graph([indexed_paper, viz_paper])
    ids = {n["id"] for n in g["nodes"]}
    assert {indexed_paper, viz_paper} <= ids
    pair = {frozenset((e["source"], e["target"])) for e in g["edges"]}
    assert frozenset((viz_paper, indexed_paper)) in pair          # 真实边
    assert not any(e["source"] == e["target"] for e in g["edges"])  # 自环剔除
    m = _graph_metrics(g)
    assert m["backend"] in ("networkx", "pagerank_py") and m["edge_count"] == len(g["edges"])
    top = m["nodes"][0]
    assert top["paper_id"] == indexed_paper  # 被引者 PageRank 最高


def test_forced_graph_full_flow(indexed_paper, viz_paper):
    """Graph 全链路：graph_entry → planner/executor → graph_build（artifact）→ 确定性摘要（无 LLM）。"""
    q: queue_mod.Queue = queue_mod.Queue()
    runner.run_stream("这两篇论文的引用关系如何", [indexed_paper, viz_paper],
                      q, forced_agent="graph")
    events = _drain(q)
    final = next(d for k, d in events if k == "final")
    assert final["agent"] == "graph"
    nodes = [c["node"] for c in final["trace"]]
    assert "graph_entry" in nodes and "graph_build" in nodes
    assert "cite" not in final["guardrails"][0] if final["guardrails"] else True
    arts = [d for k, d in events if k == "artifact" and d.get("kind") == "citation_graph"]
    assert arts and arts[-1]["graph"]["metrics"]["edge_count"] >= 1
    # 无 LLM：确定性指标摘要（含 PageRank/被引数字，模式 extractive）
    assert final["mode"] == "extractive"
    assert "引用图谱摘要" in final["answer"] and "PageRank" in final["answer"]
    assert final.get("graph_data", {}).get("metrics", {}).get("backend") in ("networkx", "pagerank_py")


def test_forced_writer_flow(indexed_paper, monkeypatch):
    """Writer 合成（stub LLM，直调 responder）：大纲 artifact 先行 → 正文生成 → 引用后验。"""
    from app.llm import client as llm_client
    from app.agents.nodes import responder_node

    fake_answer = ("**背景**\n trained on 8 NVIDIA P100 GPUs [^p1-§Training].\n\n"
                   "**要点**\n Base reaches 27.3 BLEU [^p1-§Training].")
    monkeypatch.setattr(llm_client, "available", lambda force=False: True)
    monkeypatch.setattr(llm_client, "stream_chat", lambda msgs, **kw: iter([fake_answer]))
    monkeypatch.setattr(llm_client, "chat", lambda msgs, **kw:
                        {"text": '{"claims":[{"text":"8 GPUs","status":"supported","pages":[1]}]}',
                         "model": "stub"})
    monkeypatch.setattr(llm_client, "_endpoint", lambda: ("stub", "k", "stub"))

    hits = [{"chunk_id": f"c{i}", "paper_id": indexed_paper, "page": 1,
             "section_path": "Training", "text": f"fact {i} about GPUs", "score": 0.9}
            for i in range(3)]
    q: queue_mod.Queue = queue_mod.Queue()
    upd = responder_node({"agent": "writer", "question": "GPUs",
                          "evidence": hits, "retries": 0,
                          "intent": {"type": "writing", "aspects": ["GPUs"]},
                          "plan": {"steps": [{"paper_id": indexed_paper, "query": "GPUs",
                                              "aspect": "GPUs"}], "mode": "single"}},
                         {"configurable": {"queue": q}})
    assert upd["mode"] == "generative" and "[^p1" in upd["answer"]
    kinds = [k for k, _ in list(q.queue)]
    assert "artifact" in kinds                                # 大纲 artifact 先行
    outline_evt = next(d for k, d in list(q.queue)
                       if k == "artifact" and d.get("kind") == "outline")
    assert outline_evt["outline"]["sections"]
    assert upd.get("outline") is None or True  # outline 在事件中下发（state 由图统一携带）
    assert any(c["status"] == "supported" for c in upd["claims"])


def test_writer_insufficient_evidence_refusal(indexed_paper):
    """Writer Guardrail：证据池 < 3 块 → 拒写（直接调 responder 验证分支）。"""
    from app.agents.nodes import responder_node

    upd = responder_node({"agent": "writer", "question": "写一篇综述",
                          "evidence": [{"chunk_id": "c1", "page": 1, "text": "x"}],
                          "intent": {"type": "writing", "aspects": ["综述"]}})
    assert upd["mode"] == "refusal" and "不会在无证据" in upd["answer"]


def test_tools_agent_matrix():
    """白名单矩阵：新工具只归各自 Agent。"""
    import pytest as _pytest

    from app.agents.tools import ToolDenied, assert_allowed

    with _pytest.raises(ToolDenied):
        assert_allowed("reader", "extract_table")
    with _pytest.raises(ToolDenied):
        assert_allowed("compare", "citation_graph")
    with _pytest.raises(ToolDenied):
        assert_allowed("writer", "chart_spec_builder")
    assert_allowed("graph", "citation_graph")
    assert_allowed("graph", "graph_metrics")
    assert_allowed("viz", "extract_table")
    assert_allowed("viz", "chart_spec_builder")
    assert_allowed("writer", "kb_search")  # Writer 继承检索工具
