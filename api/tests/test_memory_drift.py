"""模块七 P1：FadeMem 双层软衰减单测（全离线）。

对应清单 §2.3：λ_base 锚定「I=0 时 LML 半衰期 11.25 天」· β=0.8/1.2 · μ 重要性系数 ·
滞回迁移 0.7/0.3 · 只影响排序不物理删除 · 参数 env 覆盖（P2 消融入口）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.config import settings
from app.memory import drift, store
from app.memory.timeparse import LOCAL_TZ

TZ = LOCAL_TZ
DAY = 86400.0
T0 = datetime(2026, 11, 1, tzinfo=TZ).timestamp()


@pytest.fixture(autouse=True)
def _fade_defaults(monkeypatch):
    """消融参数回默认（其它测试可能污染 settings）。"""
    monkeypatch.setattr(settings, "fade_mu", 0.5)
    monkeypatch.setattr(settings, "fade_beta_lml", 0.8)
    monkeypatch.setattr(settings, "fade_beta_sml", 1.2)
    monkeypatch.setattr(settings, "fade_theta_promote", 0.7)
    monkeypatch.setattr(settings, "fade_theta_demote", 0.3)


def test_half_life_anchor_exact():
    """λ_base = ln2/(11.25d)^β：μ=0 时 LML 在 11.25 天处**精确**半衰（锚点定义自证）。"""
    monkey_mu = pytest.MonkeyPatch()
    monkey_mu.setattr(settings, "fade_mu", 0.0)
    try:
        v = drift.strength_at(importance=10, ts=T0, layer="LML", now=T0 + 11.25 * DAY)
        assert abs(v - 0.5) < 1e-9, f"I=0 时 LML 半衰期必须精确 11.25 天，得到 {v}"
    finally:
        monkey_mu.undo()


def test_mu_slows_decay_for_important_memory():
    """μ 项：重要性越高衰减越慢（λ = λ_base·e^(−μI)）。"""
    t = T0 + 30 * DAY
    v_withmu = drift.strength_at(importance=10, ts=T0, layer="LML", now=t)  # fixture 默认 μ=0.5
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, "fade_mu", 0.0)
    try:
        v_flat = drift.strength_at(importance=10, ts=T0, layer="LML", now=t)
    finally:
        monkey.undo()
    assert v_withmu > v_flat, "μ>0 必须让重要记忆衰减更慢"


def test_sml_decays_faster_than_lml():
    t = T0 + 60 * DAY
    v_lml = drift.strength_at(importance=5, ts=T0, layer="LML", now=t)
    v_sml = drift.strength_at(importance=5, ts=T0, layer="SML", now=t)
    assert v_sml <= v_lml, "SML β=1.2 必须不慢于 LML（触底下限后相等也是正确语义）"


def test_strength_bounded_and_monotonic():
    prev = 1.0
    for days in (1, 10, 50, 200, 800):
        v = drift.strength_at(importance=7, ts=T0, layer="LML", now=T0 + days * DAY)
        assert 0.0 <= v <= 1.0
        assert v <= prev, "时间越远强度必须单调不增"
        prev = v


def test_floor_prevents_numerical_extinction():
    """r10 数值稳定性修复的锁定测试：β>1 超线性衰减在长跨度 dt 上不得把强度
    指数归零（= 软删除）——必须触底于 v0·V_FLOOR_RATIO。"""
    v_800d = drift.strength_at(importance=5, ts=T0, layer="SML", now=T0 + 800 * DAY)
    floor = (5 / 10) * drift.V_FLOOR_RATIO  # = 0.125
    assert v_800d == pytest.approx(floor), "长跨度必须钳制在下限而非归零"
    # 30 天 SML（r9 漏报场景）不再归零：仍保有可检索的区分度
    v_30d = drift.strength_at(importance=6, ts=T0, layer="SML", now=T0 + 30 * DAY)
    assert v_30d > 0.1, f"30 天 SML 强度 {v_30d} 不得数值归零"
    # 下限钳制后触底 = v0 的 25%（衰减最低保留出厂电量 1/4，永不删除原则的量化表达）
    v_3000d = drift.strength_at(importance=10, ts=T0, layer="SML", now=T0 + 3000 * DAY)
    assert v_3000d == pytest.approx(1.0 * drift.V_FLOOR_RATIO)


def test_hysteresis_migration():
    assert drift.migrate_layer(0.85, "SML") == ("LML", "promote")
    assert drift.migrate_layer(0.5, "SML") == ("SML", "keep"), "中间区间保持原层（滞回防抖）"
    assert drift.migrate_layer(0.5, "LML") == ("LML", "keep")
    assert drift.migrate_layer(0.2, "LML") == ("SML", "demote")
    assert drift.migrate_layer(0.2, "SML") == ("SML", "keep")


def test_recalculate_updates_and_migrates_without_deletion():
    user = "u_drift"
    fresh = store.add_event("hypothesis", "重要新假设", user_id=user, ts=T0 + 300 * DAY,
                            importance=9, source="rule")          # v0=0.9，几乎无衰减 → LML
    old = store.add_event("topic", "陈旧低分话题", user_id=user, ts=T0, importance=1,
                          source="rule")                          # 300 天前 imp=1 → v≈0 → demote
    summary = drift.recalculate_strengths(user_id=user)
    assert summary["updated"] == 2
    f = store.get_event(fresh["id"])
    o = store.get_event(old["id"])
    assert f["layer"] == "LML" and f["strength"] >= 0.85
    assert o["layer"] == "SML", "陈旧低分记忆必须滞回迁入 SML（快衰减层）"
    assert o["strength"] < 0.05
    assert o["status"] == "active" and o["content"] == "陈旧低分话题", \
        "衰减只改 strength/layer，绝不物理删除或改状态"
    assert len([e for e in store.list_events(limit=500) if e["user_id"] == user]) == 2


def test_retrieve_uses_recalculated_strength():
    user = "u_drift2"
    a = store.add_event("decision", "决定采用方案甲", user_id=user, ts=T0 + 300 * DAY,
                        importance=5, source="rule")
    from app.db import tx
    with tx() as c:
        c.execute("UPDATE memory_events SET strength=0.05 WHERE id=?", (a["id"],))
    from app.memory import retrieve
    out = retrieve.retrieve_memories("决定 方案", user_id=user,
                                     now_ts=datetime.now(TZ).timestamp(), touch=False)
    assert out["items"][0]["scores"]["strength"] == 0.05, "检索排序乘子读 recalculated 值"


# ---------- r8：ATLAS 漂移降级口径（文字提示） ----------

def test_drift_hint_silent_when_coherent():
    user = "u_hint1"
    for i, text in enumerate(["我假设检索优化的方向是混合检索",
                              "我决定继续深入混合检索的评测",
                              "我假设混合检索的重排层可以精简",
                              "我决定混合检索的消融实验下周跑"]):
        store.add_event("hypothesis" if i % 2 == 0 else "decision", text,
                        user_id=user, conversation_id=f"h1_{i}", turn=1,
                        ts=T0 + i * 86400, source="rule")
    assert drift.drift_hint(user_id=user) == "", "主题连贯时不提示"


def test_drift_hint_fires_on_topic_migration():
    user = "u_hint2"
    for i, text in enumerate(["我假设稀疏注意力降低显存占用",
                              "我决定深入稀疏注意力的评测协议",
                              "我假设长文本推理需要分块缓存策略",
                              "我决定转向研究多智能体协作的调度问题"]):
        store.add_event("hypothesis" if i % 2 == 0 else "decision", text,
                        user_id=user, conversation_id=f"h2_{i}", turn=1,
                        ts=T0 + i * 86400, source="rule")
    hint = drift.drift_hint(user_id=user)
    assert "迁移" in hint and "ATLAS" in hint, "主题迁移必须触发文字提示"
    assert "降级口径" in hint, "必须如实标注这是降级口径（借思想非隐层干预）"


def test_drift_hint_silent_when_too_few():
    store.add_event("hypothesis", "我假设样本不足不提示", user_id="u_hint3",
                    conversation_id="h3", turn=1, source="rule")
    assert drift.drift_hint(user_id="u_hint3") == ""
