"""FadeMem 双层软衰减（模块七 P1，清单 §2.3 / §三#8）。

    v_i(t) = v_i(0) · exp(−λ_i · (t−τ_i)^β_i)      λ_i = λ_base · exp(−μ · I_i(t))

- λ_base 锚定「I=0 时 LML 半衰期 = 11.25 天」：β 指数律下半衰期在 t 上有位移
  （exp(−λ_base·t^β)=0.5 的解），故 λ_base = ln2 / (11.25d)^β_LML——t=11.25d 处精确半衰。
- β：LML=0.8（亚线性慢衰减）/ SML=1.2（超线性快衰减）；μ：重要记忆衰减慢。
- 滞回迁移（防抖）：v ≥ θ_promote(0.7) → LML；v ≤ θ_demote(0.3) → SML；中间保持原层。
- 作用域：强度只作检索排序乘子（retrieve.score × v(t)），**永不物理删除**——
  t_valid 过期只是「当前不为真」，历史仍可按时间窗检索（THEANINE 前提）。
- β/μ/θ 经 settings 可 env 覆盖（清单 §三#14），P2 网格消融（踩坑 #2：不照搬论文参数）。
"""
from __future__ import annotations

import math

from ..config import settings
from ..db import now as db_now
from . import store

DAY = 86400.0


def _beta(layer: str) -> float:
    return settings.fade_beta_lml if layer == "LML" else settings.fade_beta_sml


def _lambda_base() -> float:
    """λ_base = ln2 / (11.25)^β_LML（单位：天^β）—— I=0 时 LML 在 11.25 天处精确半衰。
    运行时读取 settings（P2 网格消融需 env/monkeypatch 动态调 β）。"""
    return math.log(2) / ((11.25) ** settings.fade_beta_lml)


# 强度下限（占 v0 的 25%）：β>1 的超线性衰减在长跨度（周级以上）dt 上会使 v 指数归零
# ——数值上等价于「软删除」，违反本模块「永不删除」原则（r10 实测：SML 层 30 天事件
# v≈e^-274≈0，导致该记忆永久不可检索）。修复 = dt 以「天」为单位代入（论文原式秒单位
# 在长程场景数值不稳定）+ 下限钳制「衰减最低保留出厂电量 1/4」。见 r10 验收报告。
V_FLOOR_RATIO = 0.25


def strength_at(*, importance: int, ts: float, layer: str, now: float) -> float:
    """记忆当前强度 v(t) ∈ [v_floor, 1]。v(0) = importance/10（清单 §2.3 初值映射）。

    r10 数值稳定性修复：dt 以**天**为单位代入 β 指数律（论文原式秒单位在长程 dt 上
    使 β>1 的 SML 层指数爆炸归零）；并施加 v ≥ v_floor 的下限钳制——衰减可以
    把记忆压到排序底部，但数值上不得等价于删除。"""
    v0 = max(0.1, min(1.0, importance / 10.0))
    imp = max(0.0, min(1.0, importance / 10.0))
    lam = _lambda_base() * math.exp(-settings.fade_mu * imp)
    beta = _beta(layer)
    dt_days = max(0.0, (now - ts)) / DAY
    v = v0 * math.exp(-lam * (dt_days ** beta))
    floor = v0 * V_FLOOR_RATIO
    return max(floor, min(1.0, v))


def migrate_layer(v: float, layer: str) -> tuple[str, str]:
    """滞回层迁移：返回 (新层, 动作 promote/demote/keep)。中间区间保持原层防抖。"""
    if v >= settings.fade_theta_promote and layer != "LML":
        return "LML", "promote"
    if v <= settings.fade_theta_demote and layer != "SML":
        return "SML", "demote"
    return layer, "keep"


def recalculate_strengths(*, user_id: str = "local", limit: int = 1000,
                          now: float | None = None) -> dict:
    """批量重算强度 + 滞回迁移（worker 周期任务 1）。只 UPDATE strength/layer 两列，
    **永不删除**；返回摘要供 worker 日志/审计。"""
    from ..db import tx

    t = now if now is not None else db_now()
    events = [e for e in store.list_events(limit=limit) if e["user_id"] == user_id]
    promoted = demoted = kept = 0
    with tx() as c:
        for e in events:
            v = strength_at(importance=e["importance"], ts=e["ts"], layer=e["layer"], now=t)
            layer, action = migrate_layer(v, e["layer"])
            c.execute("UPDATE memory_events SET strength=?, layer=? WHERE id=?",
                      (round(v, 4), layer, e["id"]))
            if action == "promote":
                promoted += 1
            elif action == "demote":
                demoted += 1
            else:
                kept += 1
    return {"updated": len(events), "promoted": promoted, "demoted": demoted, "kept": kept}


def drift_hint(*, user_id: str = "local", limit: int = 20) -> str:
    """ATLAS 思想漂移的**降级口径**（清单 P1 裁剪预案，r8 落地）：文字提示。

    原指标为任务/角色/状态三锚 embedding 余弦（ATLAS 隐层干预，黑盒 API 不可行）；
    降级实现 = 近期与更早的 hypothesis/decision 记忆主题**词面重叠**近似，重叠度低
    → 简报尾部提示「研究关注点迁移」。借思想、非隐层干预，如实标注（清单 §七.5）。"""
    events = [e for e in store.list_events(limit=100)
              if e["user_id"] == user_id and e["kind"] in ("hypothesis", "decision")]
    events.sort(key=lambda e: e["ts"])
    if len(events) < 4:
        return ""
    half = len(events) // 2
    older, recent = events[:half], events[half:]

    def _grams(items: list[dict]) -> set[str]:
        g: set[str] = set()
        from .retrieve import _bigrams
        for e in items:
            g |= _bigrams(f"{e['content']} {e.get('subject') or ''}")
        return g

    union = _grams(older) | _grams(recent)
    if not union:
        return ""
    overlap = len(_grams(older) & _grams(recent)) / len(union)
    if overlap >= 0.15:
        return ""
    return (f"⚠ 研究关注点较早期发生明显迁移（近期假设/决定与此前主题词面重叠仅 "
            f"{overlap:.0%}）。ATLAS 漂移指标降级口径：文字提示（借思想，非隐层干预）。")
