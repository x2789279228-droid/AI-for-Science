"""模块七 R1：timeparse 单测——中英文自然语言时间表达式 → UTC 区间（≥20 组，全离线零 LLM）。

基准时刻固定为 2026-11-21（周六）15:30 本地（UTC+8）；期望边界均独立手算，
不复用实现内部的日历算术（避免同源镜像断言）。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.memory.timeparse import LOCAL_TZ, find_time_ranges, parse_expr

TZ = LOCAL_TZ
NOW = datetime(2026, 11, 21, 15, 30, tzinfo=TZ).timestamp()   # 2026-11-21 周六 15:30 +08


def _d(*args) -> float:
    return datetime(*args, tzinfo=TZ).timestamp()


def _dt(days: float) -> float:
    return NOW - days * 86400.0


# (表达式, t_start 期望, t_end 期望)——日历档手算边界；滚动档按 now-偏移
CASES = [
    # ---- 日档 ----
    ("今天", _d(2026, 11, 21), _d(2026, 11, 22)),
    ("昨天", _d(2026, 11, 20), _d(2026, 11, 21)),
    ("前天", _d(2026, 11, 19), _d(2026, 11, 20)),
    ("today", _d(2026, 11, 21), _d(2026, 11, 22)),
    ("yesterday", _d(2026, 11, 20), _d(2026, 11, 21)),
    # ---- 周档（周一本地 00:00 切割）----
    ("本周", _d(2026, 11, 16), _d(2026, 11, 23)),
    ("这周", _d(2026, 11, 16), _d(2026, 11, 23)),
    ("上周", _d(2026, 11, 9), _d(2026, 11, 16)),
    ("下周", _d(2026, 11, 23), _d(2026, 11, 30)),
    ("this week", _d(2026, 11, 16), _d(2026, 11, 23)),
    ("last week", _d(2026, 11, 9), _d(2026, 11, 16)),
    ("next week", _d(2026, 11, 23), _d(2026, 11, 30)),
    # ---- 月档 ----
    ("本月", _d(2026, 11, 1), _d(2026, 12, 1)),
    ("上个月", _d(2026, 10, 1), _d(2026, 11, 1)),
    ("上月", _d(2026, 10, 1), _d(2026, 11, 1)),
    ("下个月", _d(2026, 12, 1), _d(2027, 1, 1)),
    ("this month", _d(2026, 11, 1), _d(2026, 12, 1)),
    ("last month", _d(2026, 10, 1), _d(2026, 11, 1)),
    ("next month", _d(2026, 12, 1), _d(2027, 1, 1)),
    # ---- 年档 ----
    ("今年", _d(2026, 1, 1), _d(2027, 1, 1)),
    ("去年", _d(2025, 1, 1), _d(2026, 1, 1)),
    ("明年", _d(2027, 1, 1), _d(2028, 1, 1)),
    ("前年", _d(2024, 1, 1), _d(2025, 1, 1)),
    ("this year", _d(2026, 1, 1), _d(2027, 1, 1)),
    ("last year", _d(2025, 1, 1), _d(2026, 1, 1)),
    # ---- 季度档 ----
    ("本季度", _d(2026, 10, 1), _d(2027, 1, 1)),
    ("上季度", _d(2026, 7, 1), _d(2026, 10, 1)),
    ("下季度", _d(2027, 1, 1), _d(2027, 4, 1)),
    ("this quarter", _d(2026, 10, 1), _d(2027, 1, 1)),
    ("last quarter", _d(2026, 7, 1), _d(2026, 10, 1)),
    # ---- 半年档 ----
    ("上半年", _d(2026, 1, 1), _d(2026, 7, 1)),
    ("下半年", _d(2026, 7, 1), _d(2027, 1, 1)),
    # ---- 滚动窗（[now-N*86400, now]；月为日历减法）----
    ("最近两周", _dt(14), NOW),
    ("近三天", _dt(3), NOW),
    ("过去一个月", _d(2026, 10, 21, 15, 30), NOW),
    ("past 7 days", _dt(7), NOW),
    ("last 14 days", _dt(14), NOW),
    ("last 3 months", _d(2026, 8, 21, 15, 30), NOW),
    # ---- N<粒度>前（粒度阶梯：天/ISO周/日历月/日历年）----
    ("三天前", _d(2026, 11, 18), _d(2026, 11, 19)),
    ("两周前", _d(2026, 11, 2), _d(2026, 11, 9)),
    ("三个月前", _d(2026, 8, 1), _d(2026, 9, 1)),
    ("两年前", _d(2024, 1, 1), _d(2025, 1, 1)),
    ("十个月前", _d(2026, 1, 1), _d(2026, 2, 1)),
    ("3 days ago", _d(2026, 11, 18), _d(2026, 11, 19)),
    ("2 weeks ago", _d(2026, 11, 2), _d(2026, 11, 9)),
    ("3 months ago", _d(2026, 8, 1), _d(2026, 9, 1)),
    ("2 years ago", _d(2024, 1, 1), _d(2025, 1, 1)),
]


@pytest.mark.parametrize("expr,t0,t1", CASES, ids=[c[0] for c in CASES])
def test_expr_to_utc_range(expr: str, t0: float, t1: float):
    hit = parse_expr(expr, now=NOW)
    assert hit is not None, f"表达式未命中：{expr}"
    assert hit.expr == expr, f"命中原文不符：{hit.expr!r} vs {expr!r}"
    assert hit.t_start == t0, f"{expr} 起点：{datetime.fromtimestamp(hit.t_start, TZ)} != 期望"
    assert hit.t_end == t1, f"{expr} 终点：{datetime.fromtimestamp(hit.t_end, TZ)} != 期望"
    assert hit.t_start < hit.t_end


def test_no_expression_returns_none_or_empty():
    assert parse_expr("总结这篇论文的方法与主要贡献", now=NOW) is None
    assert find_time_ranges("帮我对比这两篇工作的实验设置", now=NOW) == []


def test_find_multiple_ordered_by_span():
    text = "上周提出假设，本周做验证，下周出结果。"
    hits = find_time_ranges(text, now=NOW)
    assert [h.expr for h in hits] == ["上周", "本周", "下周"]
    spans = [h.span for h in hits]
    assert spans == sorted(spans)
    assert spans[0][0] == text.index("上周")


def test_span_offsets_exact():
    text = "上季度综述里提到三个月前的实验。"
    hits = find_time_ranges(text, now=NOW)
    first = hits[0]
    assert first.expr == "上季度"
    assert first.span == (0, 3)
    assert text[first.span[0]:first.span[1]] == "上季度"


def test_utc_epoch_conversion():
    """本地周一切割 → UTC epoch：+8 下本地 00:00 即前一日 16:00 UTC。"""
    hit = parse_expr("今天", now=NOW)
    assert hit.t_start == datetime(2026, 11, 21, 0, 0, tzinfo=TZ).timestamp()
    assert hit.t_start == datetime(2026, 11, 20, 16, 0, tzinfo=UTC).timestamp()
    assert hit.t_end - hit.t_start == 86400.0


def test_week_start_is_monday():
    """NOW 为周六：本周起点必须是周一（ISO），而非周日切割。"""
    hit = parse_expr("本周", now=NOW)
    assert datetime.fromtimestamp(hit.t_start, TZ).weekday() == 0
    assert datetime.fromtimestamp(hit.t_start, TZ).strftime("%Y-%m-%d") == "2026-11-16"


def test_rolling_month_calendar_subtraction():
    """滚动月窗按日历减法：11-21 往前 1 个月 = 10-21 同时刻（而非固定 30 天）。"""
    hit = parse_expr("过去一个月", now=NOW)
    assert datetime.fromtimestamp(hit.t_start, TZ) == datetime(2026, 10, 21, 15, 30, tzinfo=TZ)


def test_month_end_clamp():
    """3 月 31 日往前 1 个月截到 2 月末（月长钳制）。"""
    now2 = datetime(2026, 3, 31, 10, 0, tzinfo=TZ).timestamp()
    hit = parse_expr("过去一个月", now=now2)
    assert datetime.fromtimestamp(hit.t_start, TZ) == datetime(2026, 2, 28, 10, 0, tzinfo=TZ)
