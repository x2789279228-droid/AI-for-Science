"""模块七 R3：时间感知检索单测（全离线）。

覆盖清单 §三#6：三信号等权相加 × FadeMem 强度乘子 · timeparse 时间窗 ∩ 有效窗 ·
实体过滤 · 多跳 BFS 深度≤2（×0.5^hop）· embed off 词面降级（§2.10）· touch_access。
用例间用独立 user_id 隔离（conftest 会话级共享库，候选集按 user 过滤）。
"""
from __future__ import annotations

from datetime import datetime

from app.memory import retrieve, store
from app.memory.timeparse import LOCAL_TZ

TZ = LOCAL_TZ
NOW = datetime(2026, 11, 21, 15, 30, tzinfo=TZ).timestamp()


def _add(kind, content, *, user, ts=NOW - 3600, imp=5, strength=None, vec=None,
         t_valid_from=None, t_valid_to=None, entities=None, subject=""):
    ev = store.add_event(kind, content, user_id=user, ts=ts, importance=imp,
                         subject=subject, entities=entities or [],
                         t_valid_from=t_valid_from, t_valid_to=t_valid_to,
                         vec=vec, source="rule")
    if strength is not None:
        from app.db import tx
        with tx() as c:
            c.execute("UPDATE memory_events SET strength=? WHERE id=?", (strength, ev["id"]))
        ev["strength"] = strength
    return ev


def test_off_mode_keyword_channel():
    user = "u_off"
    _add("hypothesis", "稀疏注意力机制可以把显存占用降低三成", user=user, imp=7)
    _add("topic", "会议室预订流程需要行政审批", user=user, imp=3)
    out = retrieve.retrieve_memories("稀疏注意力 显存", user_id=user, now_ts=NOW, touch=False)
    assert out["meta"]["embed"] == "off", "无向量环境必须如实标注 off"
    assert out["items"][0]["content"].startswith("稀疏注意力")
    s = out["items"][0]["scores"]
    assert s["relevance"] == 1.0 and s["final"] > 0
    assert out["items"][-1]["scores"]["final"] <= out["items"][0]["scores"]["final"]


def test_dense_relevance_via_monkeypatch(monkeypatch):
    user = "u_dense"
    vec_hit, vec_miss = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]
    _add("finding", "结论甲", user=user, vec=retrieve.pack_vec(vec_hit))
    _add("finding", "结论乙", user=user, vec=retrieve.pack_vec(vec_miss))
    monkeypatch.setattr(retrieve, "_query_vector", lambda q: [1.0, 0.0, 0.0])
    out = retrieve.retrieve_memories("结论", user_id=user, now_ts=NOW, touch=False)
    assert out["meta"]["embed"] == "dense"
    first = out["items"][0]
    assert first["content"] == "结论甲"
    assert first["scores"]["relevance"] >= out["items"][1]["scores"]["relevance"]


def test_recency_signal_component():
    user = "u_rec"
    _add("preference", "回答用中文", user=user, imp=5)
    old = _add("preference", "回答用中文备选", user=user, imp=5)
    store.touch_access(old["id"], at=NOW - 24 * 3600)          # 24h 前访问过
    fresh = _add("preference", "回答用中文最新", user=user, imp=5)  # 从未访问 → 按 ts
    out = retrieve.retrieve_memories("回答用中文", user_id=user, now_ts=NOW, touch=False)
    by_id = {i["id"]: i for i in out["items"]}
    assert by_id[fresh["id"]]["scores"]["recency"] == 1.0
    assert by_id[old["id"]]["scores"]["recency"] < 1.0


def test_strength_multiplier_orders_equal_base():
    user = "u_str"
    a = _add("decision", "决定采用方案甲做主链路", user=user, imp=5)
    b = _add("decision", "决定采用方案乙做主链路", user=user, imp=5)
    from app.db import tx
    with tx() as c:
        c.execute("UPDATE memory_events SET strength=0.9 WHERE id=?", (a["id"],))
        c.execute("UPDATE memory_events SET strength=0.2 WHERE id=?", (b["id"],))
    out = retrieve.retrieve_memories("决定采用 方案 主链路", user_id=user,
                                     now_ts=NOW, touch=False)
    ids = [i["id"] for i in out["items"]]
    assert ids.index(a["id"]) < ids.index(b["id"]), "同基础分下强记忆必须排前（只影响排序）"


def test_time_window_filters_by_calendar():
    user = "u_win"
    q3 = datetime(2026, 8, 15, tzinfo=TZ).timestamp()
    q4 = datetime(2026, 11, 1, tzinfo=TZ).timestamp()
    _add("experiment_result", "三季度实验记录甲", user=user, ts=q3)
    _add("experiment_result", "十一月实验记录乙", user=user, ts=q4)
    out = retrieve.retrieve_memories("上季度的实验记录", user_id=user, now_ts=NOW, touch=False)
    assert out["meta"]["time_range"]["expr"] == "上季度"
    contents = {i["content"] for i in out["items"]}
    assert "三季度实验记录甲" in contents
    assert "十一月实验记录乙" not in contents


def test_time_window_respects_t_valid():
    """ts 在窗外但 t_valid 覆盖窗口的事件必须命中（有效窗语义，清单 §2.3）。"""
    user = "u_tval"
    t0 = datetime(2026, 7, 1, tzinfo=TZ).timestamp()
    t1 = datetime(2026, 10, 1, tzinfo=TZ).timestamp()
    _add("decision", "当时采用旧版评测协议", user=user, ts=datetime(2025, 1, 1, tzinfo=TZ).timestamp(),
         t_valid_from=t0, t_valid_to=t1)
    out = retrieve.retrieve_memories("上季度采用的协议", user_id=user, now_ts=NOW, touch=False)
    assert any("旧版评测协议" in i["content"] for i in out["items"])


def test_entity_filter():
    user = "u_ent"
    store.resolve_entity("SparseAttention", user_id=user)
    _add("finding", "关于其效率的结论", user=user, entities=["SparseAttention"])
    _add("topic", "无关话题", user=user)
    out = retrieve.retrieve_memories("SparseAttention 效率结论", user_id=user,
                                     now_ts=NOW, touch=False)
    assert out["meta"]["entity_filter"] == ["SparseAttention"]
    assert len(out["items"]) == 1 and "效率" in out["items"][0]["content"]


def test_bfs_multi_hop_expansion():
    user = "u_bfs"
    a = _add("hypothesis", "机制理论分析框架的核心断言", user=user, imp=7)
    b = _add("experiment_result", "对应的实验验证记录", user=user, imp=6)
    c = _add("finding", "最终的结论汇总文档", user=user, imp=6)
    store.upsert_edge(a["id"], b["id"], "supports")
    store.upsert_edge(b["id"], c["id"], "cause")
    # top_k=1：种子只有 A，B/C 只能经 BFS 进来（否则候选全进种子集，hops 恒为 0）
    out = retrieve.retrieve_memories("机制理论分析框架", user_id=user, now_ts=NOW,
                                     touch=False, top_k=1)
    by_id = {i["id"]: i for i in out["items"]}
    assert a["id"] in by_id
    assert c["id"] in by_id, "两跳邻居必须经 BFS 扩展进来"
    assert by_id[c["id"]]["scores"]["hops"] == 2
    assert by_id[c["id"]]["scores"]["via"] == [a["id"], b["id"]]
    assert by_id[c["id"]]["scores"]["final"] <= by_id[a["id"]]["scores"]["final"] * 0.26


def test_touch_access_on_retrieve():
    user = "u_touch"
    _add("preference", "偏好深色主题", user=user)
    out = retrieve.retrieve_memories("偏好 主题", user_id=user, now_ts=NOW, touch=True)
    got = store.get_event(out["items"][0]["id"])
    assert got["access_count"] >= 1 and got["last_access"] > 0


def test_empty_scope_returns_safe():
    out = retrieve.retrieve_memories("任何问题", user_id="u_none", now_ts=NOW)
    assert out["items"] == [] and out["meta"]["n_candidates"] == 0


def test_hours_since_label():
    assert retrieve.hours_since_label(NOW - 30, now=NOW) == "刚刚"
    assert retrieve.hours_since_label(NOW - 7200, now=NOW) == "2 小时前"
    assert retrieve.hours_since_label(NOW - 3 * 86400, now=NOW) == "3 天前"


def test_time_window_without_t_valid_uses_event_ts():
    """r7 评测驱动的语义修正：无 t_valid 的事件按**发生时间**落窗——
    窗外旧事件不得因 [ts,+inf) 有效窗与查询窗相交而冒充命中。"""
    user = "u_win2"
    old = _add("experiment_result", "一个月前的旧实验结论", user=user,
               ts=NOW - 30 * 86400)
    fresh = _add("experiment_result", "最近两天的新实验结论", user=user,
                 ts=NOW - 2 * 86400)
    out = retrieve.retrieve_memories("最近一周的实验结论", user_id=user, now_ts=NOW,
                                     touch=False)
    ids = {i["id"] for i in out["items"]}
    assert fresh["id"] in ids
    assert old["id"] not in ids, "窗外旧实验不得混入「最近」查询"
    # 有 t_valid 声明的事件走有效期交集语义（不受 ts 限制）
    spanning = _add("decision", "期间沿用的旧评测协议", user=user,
                    ts=NOW - 90 * 86400, t_valid_from=NOW - 5 * 86400,
                    t_valid_to=NOW + 5 * 86400)
    out2 = retrieve.retrieve_memories("最近一周的实验结论", user_id=user, now_ts=NOW,
                                      touch=False)
    assert spanning["id"] in {i["id"] for i in out2["items"]}, \
        "t_valid 覆盖查询窗的事实必须按有效期语义命中"


def test_stale_status_penalty():
    """superseded/refuted 记忆在无时间窗查询时降权（「当前不为真」一等公民）。"""
    user = "u_stale"
    a = _add("hypothesis", "关于显存优化的结论甲", user=user, imp=8)
    b = _add("hypothesis", "关于显存优化的结论乙", user=user, imp=8)
    from app.db import tx
    with tx() as c:
        c.execute("UPDATE memory_events SET status='superseded' WHERE id=?", (a["id"],))
    out = retrieve.retrieve_memories("显存优化 结论", user_id=user, now_ts=NOW, touch=False)
    by_id = {i["id"]: i for i in out["items"]}
    assert by_id[b["id"]]["scores"]["final"] > by_id[a["id"]]["scores"]["final"]
    assert by_id[a["id"]]["scores"]["stale_penalized"] is True
    assert by_id[a["id"]] in out["items"], "降权不删除——历史仍可召回"
