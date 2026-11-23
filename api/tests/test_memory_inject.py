"""模块七 R4：responder 注入块与 [M#] 独立校验单测（全离线）。

对应清单 §三#12（时间线注入 + kind 过滤）、§2.9（[M#] L1 存在性校验、与 E# 互不干扰）、
ChatBody.use_memory 开关接线。
"""
from __future__ import annotations

from datetime import datetime

from app.memory import retrieve, store
from app.memory.timeparse import LOCAL_TZ
from app.rag.answering import build_qa_messages, validate_memory_citations

TZ = LOCAL_TZ
NOW = datetime(2026, 11, 21, 10, 0, tzinfo=TZ).timestamp()
U = "u_inj"


# ---------- [M#] 独立编号校验 ----------

def test_validate_memory_citations_strips_invalid():
    answer = "记忆里你提过 [M1]，另外 [M9] 不存在，证据见 [E2]。"
    cleaned, guards = validate_memory_citations(answer, n_memory=2)
    assert "[M1]" in cleaned and "[M9]" not in cleaned
    assert "[E2]" in cleaned, "E# 编号空间必须完全不受 M# 校验影响"
    assert guards == ["invalid_memory_citation:M9"]


def test_validate_memory_citations_no_memory():
    cleaned, guards = validate_memory_citations("提到 [M1] 和 [M3]", n_memory=0)
    assert "[M" not in cleaned
    assert guards == ["invalid_memory_citation:M1", "invalid_memory_citation:M3"]


def test_validate_memory_no_false_no_citation():
    """M# 是可选回指：回答不含任何 [M#] 不得产生 guardrail（不参与 no_citation）。"""
    cleaned, guards = validate_memory_citations("普通回答无记忆引用 [E1]。", n_memory=3)
    assert guards == [] and cleaned == "普通回答无记忆引用 [E1]。"
    cleaned2, guards2 = validate_memory_citations("含 [M1] 的合法引用。", n_memory=1)
    assert guards2 == [] and "[M1]" in cleaned2


# ---------- build_qa_messages 注入 ----------

def test_build_qa_messages_memory_block():
    msgs = build_qa_messages("问题", [], memory_block="[M1] (hypothesis · 3 天前) 假设 X")
    user_content = msgs[1]["content"]
    assert "<memory_timeline>" in user_content and "[M1]" in user_content
    # r9：合法例外必须声明在 system 层级（否则被「禁止外部知识」压成分级拒答——实测复现）
    assert "记忆时间线例外" in msgs[0]["content"]
    assert "[M编号]" in msgs[0]["content"]
    msgs_plain = build_qa_messages("问题", [])
    assert "<memory_timeline>" not in msgs_plain[1]["content"], "空注入不得改变旧消息形态"
    assert "记忆时间线例外" not in msgs_plain[0]["content"], "空注入不得改系统提示词"


# ---------- build_memory_block（检索 + 时间线路径 → 注入块） ----------

def _seed(user: str):
    h = store.add_event("hypothesis", "我假设稀疏注意力可以降低显存占用", user_id=user,
                        conversation_id="inj1", turn=1, thread_id="inj1:1",
                        ts=datetime(2026, 11, 10, 9, 0, tzinfo=TZ).timestamp(), source="rule")
    p = store.add_event("preference", "用户偏好中文回答论文问题", user_id=user,
                        conversation_id="inj1", turn=2, thread_id="inj1:2",
                        ts=datetime(2026, 11, 12, 9, 0, tzinfo=TZ).timestamp(), source="rule")
    return h, p


def test_build_memory_block_paper_mode_filters_kinds():
    _seed(U)
    block, anchors, _gate = retrieve.build_memory_block("稀疏注意力 显存", doc_mode="paper",
                                                        user_id=U, now_ts=NOW)
    assert "[M1]" in block
    assert "偏好中文回答" not in block,         "专业模式必须过滤 preference 等非研究性记忆（清单 §三#12；引导语含『偏好』一词不算）"
    assert set(anchors) == {"M1"}
    a = anchors["M1"]
    assert a["conversation_id"] == "inj1" and a["turn"] == 1 and a["thread_id"] == "inj1:1"
    assert a["content"].startswith("我假设")


def test_build_memory_block_general_mode_full():
    _seed(U + "g")
    block, anchors, gate = retrieve.build_memory_block("稀疏注意力 显存 偏好", doc_mode="general",
                                                       user_id=U + "g", now_ts=NOW)
    assert gate["trigger"] is True and gate["top_relevance_raw"] > 0
    assert "[M1]" in block and "[M2]" in block, "普通模式注入全量 kind"
    assert set(anchors) == {"M1", "M2"}


def test_build_memory_block_no_match_empty():
    _seed(U + "n")
    block, anchors, gate = retrieve.build_memory_block("量子纠错码的表面码阈值", doc_mode="paper",
                                                       user_id=U + "n", now_ts=NOW)
    assert block == "" and anchors == {} and gate["trigger"] is False


def test_build_memory_block_never_raises(monkeypatch):
    _seed(U + "x")
    monkeypatch.setattr(retrieve, "retrieve_memories",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    block, anchors, _gate = retrieve.build_memory_block("稀疏注意力", doc_mode="paper",
                                                        user_id=U + "x", now_ts=NOW)
    assert block == "" and anchors == {}, "注入路径任何失败都必须静默降级为空"


# ---------- ChatBody 开关 ----------

def test_chat_body_use_memory_default():
    from app.api_routes.routes import ChatBody
    body = ChatBody(question="q", paper_ids=["p1"])
    assert body.use_memory is True, "use_memory 默认开启（清单 §三#15）"
