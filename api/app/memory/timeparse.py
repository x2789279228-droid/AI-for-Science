"""中英文自然语言时间表达式 → [t_start, t_end] UTC 区间（模块七 R1，清单 §2.4）。

设计约定：
- 时区：DB/API 一律 UTC epoch；日历算术用固定 +08:00（Asia/Shanghai 无夏令时，
  固定偏移精确且不引入 tzdata 依赖），输出 UTC epoch 半开区间 [t_start, t_end)。
- 周以周一为界（ISO）；季度为日历季；粒度阶梯：N天前=那一天 / N周前=那一个ISO周 /
  N个月前=那个日历月 / N年前=那个日历年（相对当前对应周/月/年的起点回退，区间稳定）。
- 「最近N天 / past N days」为滚动窗 [now - N*86400, now]，与日历档对齐档区分。
- 纯正则 + 日历算术，零 LLM 成本；解析失败返回 None/空表，绝不猜（踩坑 #1）。
"""
from __future__ import annotations

import calendar
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Asia/Shanghai 全年 UTC+8（1988 年后无夏令时）——固定偏移精确且跨平台无依赖
LOCAL_TZ = timezone(timedelta(hours=8))

_DAY = 86400.0

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


@dataclass(frozen=True)
class TimeHit:
    """一次时间表达式命中：原文、UTC epoch 半开区间、文本跨度。"""
    expr: str
    t_start: float
    t_end: float
    span: tuple[int, int]


def _num(m: re.Match) -> int:
    """捕获组 n：阿拉伯数字或中文数字（一~十、两）。"""
    s = m.group("n")
    if s.isdigit():
        return int(s)
    return _CN_NUM.get(s, 0)


def _day_start(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_start(d: datetime) -> datetime:
    d0 = _day_start(d)
    return d0 - timedelta(days=d0.weekday())          # 周一 00:00


def _month_start(d: datetime, back: int = 0) -> datetime:
    y, mo = d.year, d.month - back
    while mo <= 0:
        mo += 12
        y -= 1
    while mo > 12:
        mo -= 12
        y += 1
    return datetime(y, mo, 1, tzinfo=LOCAL_TZ)


def _quarter_start(d: datetime, back: int = 0) -> datetime:
    qm = ((d.month - 1) // 3) * 3 + 1 - back * 3
    y = d.year
    while qm <= 0:
        qm += 12
        y -= 1
    while qm > 12:
        qm -= 12
        y += 1
    return datetime(y, qm, 1, tzinfo=LOCAL_TZ)


def _quarter_end(d: datetime) -> float:
    return _quarter_start(d, back=-1).timestamp()     # 下一季起点 = 本季终点


# 每条规则：(正则, 构造器(match, now_local) -> (t_start, t_end))。
# 顺序即优先级（parse_expr 取最先命中的规则里最早的跨度）。
_RULES: list[tuple[re.Pattern, Callable[[re.Match, datetime], tuple[float, float]]]] = [
    # ---------- 中文：日档 ----------
    (re.compile(r"今天|今日"),
     lambda m, n: (_ts(_day_start(n)), _ts(_day_start(n) + timedelta(days=1)))),
    (re.compile(r"昨天|昨日"),
     lambda m, n: (_ts(_day_start(n) - timedelta(days=1)), _ts(_day_start(n)))),
    (re.compile(r"前天"),
     lambda m, n: (_ts(_day_start(n) - timedelta(days=2)), _ts(_day_start(n) - timedelta(days=1)))),
    # ---------- 中文：周档 ----------
    (re.compile(r"本周|这周|这一周"),
     lambda m, n: (_ts(_week_start(n)), _ts(_week_start(n) + timedelta(weeks=1)))),
    (re.compile(r"上周"),
     lambda m, n: (_ts(_week_start(n) - timedelta(weeks=1)), _ts(_week_start(n)))),
    (re.compile(r"下周"),
     lambda m, n: (_ts(_week_start(n) + timedelta(weeks=1)), _ts(_week_start(n) + timedelta(weeks=2)))),
    # ---------- 中文：月档 ----------
    (re.compile(r"本月|这个月|这月"),
     lambda m, n: (_ts(_month_start(n)), _ts(_month_start(n, back=-1)))),
    (re.compile(r"上个月|上月"),
     lambda m, n: (_ts(_month_start(n, back=1)), _ts(_month_start(n)))),
    (re.compile(r"下个月|下月"),
     lambda m, n: (_ts(_month_start(n, back=-1)), _ts(_month_start(n, back=-2)))),
    # ---------- 中文：年档 ----------
    (re.compile(r"今年|这一年"),
     lambda m, n: (_ts(datetime(n.year, 1, 1, tzinfo=LOCAL_TZ)),
                   _ts(datetime(n.year + 1, 1, 1, tzinfo=LOCAL_TZ)))),
    (re.compile(r"去年|上一年"),
     lambda m, n: (_ts(datetime(n.year - 1, 1, 1, tzinfo=LOCAL_TZ)),
                   _ts(datetime(n.year, 1, 1, tzinfo=LOCAL_TZ)))),
    (re.compile(r"明年"),
     lambda m, n: (_ts(datetime(n.year + 1, 1, 1, tzinfo=LOCAL_TZ)),
                   _ts(datetime(n.year + 2, 1, 1, tzinfo=LOCAL_TZ)))),
    (re.compile(r"前年"),
     lambda m, n: (_ts(datetime(n.year - 2, 1, 1, tzinfo=LOCAL_TZ)),
                   _ts(datetime(n.year - 1, 1, 1, tzinfo=LOCAL_TZ)))),
    # ---------- 中文：季度档 / 半年档 ----------
    (re.compile(r"本季度|这季度"),
     lambda m, n: (_ts(_quarter_start(n)), _quarter_end(n))),
    (re.compile(r"上季度|上个季度"),
     lambda m, n: (_ts(_quarter_start(n, back=1)), _ts(_quarter_start(n)))),
    (re.compile(r"下季度|下个季度"),
     lambda m, n: (_ts(_quarter_start(n, back=-1)), _ts(_quarter_start(n, back=-2)))),
    (re.compile(r"上半年"),
     lambda m, n: (_ts(datetime(n.year, 1, 1, tzinfo=LOCAL_TZ)),
                   _ts(datetime(n.year, 7, 1, tzinfo=LOCAL_TZ)))),
    (re.compile(r"下半年"),
     lambda m, n: (_ts(datetime(n.year, 7, 1, tzinfo=LOCAL_TZ)),
                   _ts(datetime(n.year + 1, 1, 1, tzinfo=LOCAL_TZ)))),
    # ---------- 中文：滚动窗 ----------
    (re.compile(r"(?:最近|近|过去)(?P<n>[一二两三四五六七八九十\d]+)个?天"),
     lambda m, n: (n.timestamp() - _num(m) * _DAY, n.timestamp())),
    (re.compile(r"(?:最近|近|过去)(?P<n>[一二两三四五六七八九十\d]+)个?(?:周|星期)"),
     lambda m, n: (n.timestamp() - _num(m) * 7 * _DAY, n.timestamp())),
    (re.compile(r"(?:最近|近|过去)(?P<n>[一二两三四五六七八九十\d]+)个?月"),
     lambda m, n: (_months_before(n, _num(m)), n.timestamp())),
    # ---------- 中文：N<粒度>前（粒度阶梯，清单 §2.4） ----------
    (re.compile(r"(?P<n>[一二两三四五六七八九十\d]+)个?天前"),
     lambda m, n: (_ts(_day_start(n) - timedelta(days=_num(m))),
                   _ts(_day_start(n) - timedelta(days=_num(m) - 1)))),
    (re.compile(r"(?P<n>[一二两三四五六七八九十\d]+)个?(?:周|星期)前"),
     lambda m, n: (_ts(_week_start(n) - timedelta(weeks=_num(m))),
                   _ts(_week_start(n) - timedelta(weeks=_num(m) - 1)))),
    (re.compile(r"(?P<n>[一二两三四五六七八九十\d]+)个?月前"),
     lambda m, n: (_ts(_month_start(n, back=_num(m))), _ts(_month_start(n, back=_num(m) - 1)))),
    (re.compile(r"(?P<n>[一二两三四五六七八九十\d]+)年前"),
     lambda m, n: (_ts(datetime(n.year - _num(m), 1, 1, tzinfo=LOCAL_TZ)),
                   _ts(datetime(n.year - _num(m) + 1, 1, 1, tzinfo=LOCAL_TZ)))),
    # ---------- 英文 ----------
    (re.compile(r"\btoday\b", re.IGNORECASE),
     lambda m, n: (_ts(_day_start(n)), _ts(_day_start(n) + timedelta(days=1)))),
    (re.compile(r"\byesterday\b", re.IGNORECASE),
     lambda m, n: (_ts(_day_start(n) - timedelta(days=1)), _ts(_day_start(n)))),
    (re.compile(r"\bthis week\b", re.IGNORECASE),
     lambda m, n: (_ts(_week_start(n)), _ts(_week_start(n) + timedelta(weeks=1)))),
    (re.compile(r"\blast week\b", re.IGNORECASE),
     lambda m, n: (_ts(_week_start(n) - timedelta(weeks=1)), _ts(_week_start(n)))),
    (re.compile(r"\bnext week\b", re.IGNORECASE),
     lambda m, n: (_ts(_week_start(n) + timedelta(weeks=1)), _ts(_week_start(n) + timedelta(weeks=2)))),
    (re.compile(r"\bthis month\b", re.IGNORECASE),
     lambda m, n: (_ts(_month_start(n)), _ts(_month_start(n, back=-1)))),
    (re.compile(r"\blast month\b", re.IGNORECASE),
     lambda m, n: (_ts(_month_start(n, back=1)), _ts(_month_start(n)))),
    (re.compile(r"\bnext month\b", re.IGNORECASE),
     lambda m, n: (_ts(_month_start(n, back=-1)), _ts(_month_start(n, back=-2)))),
    (re.compile(r"\bthis year\b", re.IGNORECASE),
     lambda m, n: (_ts(datetime(n.year, 1, 1, tzinfo=LOCAL_TZ)),
                   _ts(datetime(n.year + 1, 1, 1, tzinfo=LOCAL_TZ)))),
    (re.compile(r"\blast year\b", re.IGNORECASE),
     lambda m, n: (_ts(datetime(n.year - 1, 1, 1, tzinfo=LOCAL_TZ)),
                   _ts(datetime(n.year, 1, 1, tzinfo=LOCAL_TZ)))),
    (re.compile(r"\bnext year\b", re.IGNORECASE),
     lambda m, n: (_ts(datetime(n.year + 1, 1, 1, tzinfo=LOCAL_TZ)),
                   _ts(datetime(n.year + 2, 1, 1, tzinfo=LOCAL_TZ)))),
    (re.compile(r"\bthis quarter\b", re.IGNORECASE),
     lambda m, n: (_ts(_quarter_start(n)), _quarter_end(n))),
    (re.compile(r"\blast quarter\b", re.IGNORECASE),
     lambda m, n: (_ts(_quarter_start(n, back=1)), _ts(_quarter_start(n)))),
    (re.compile(r"\b(?:last|past|recent)\s+(?P<n>\d+)\s+days?\b", re.IGNORECASE),
     lambda m, n: (n.timestamp() - int(m.group("n")) * _DAY, n.timestamp())),
    (re.compile(r"\b(?:last|past|recent)\s+(?P<n>\d+)\s+weeks?\b", re.IGNORECASE),
     lambda m, n: (n.timestamp() - int(m.group("n")) * 7 * _DAY, n.timestamp())),
    (re.compile(r"\b(?:last|past|recent)\s+(?P<n>\d+)\s+months?\b", re.IGNORECASE),
     lambda m, n: (_months_before(n, int(m.group("n"))), n.timestamp())),
    (re.compile(r"\b(?P<n>\d+)\s+days?\s+ago\b", re.IGNORECASE),
     lambda m, n: (_ts(_day_start(n) - timedelta(days=int(m.group("n")))),
                   _ts(_day_start(n) - timedelta(days=int(m.group("n")) - 1)))),
    (re.compile(r"\b(?P<n>\d+)\s+weeks?\s+ago\b", re.IGNORECASE),
     lambda m, n: (_ts(_week_start(n) - timedelta(weeks=int(m.group("n")))),
                   _ts(_week_start(n) - timedelta(weeks=int(m.group("n")) - 1)))),
    (re.compile(r"\b(?P<n>\d+)\s+months?\s+ago\b", re.IGNORECASE),
     lambda m, n: (_ts(_month_start(n, back=int(m.group("n")))),
                   _ts(_month_start(n, back=int(m.group("n")) - 1)))),
    (re.compile(r"\b(?P<n>\d+)\s+years?\s+ago\b", re.IGNORECASE),
     lambda m, n: (_ts(datetime(n.year - int(m.group("n")), 1, 1, tzinfo=LOCAL_TZ)),
                   _ts(datetime(n.year - int(m.group("n")) + 1, 1, 1, tzinfo=LOCAL_TZ)))),
]


def _ts(d: datetime) -> float:
    return d.timestamp()


def _months_before(now_local: datetime, n: int) -> float:
    """now 往前推 n 个自然月的同一时刻（日超出月长则截到月末），滚动窗「按月」语义。"""
    y, mo = now_local.year, now_local.month - n
    while mo <= 0:
        mo += 12
        y -= 1
    day = min(now_local.day, calendar.monthrange(y, mo)[1])
    return datetime(y, mo, day, now_local.hour, now_local.minute,
                    now_local.second, now_local.microsecond, tzinfo=LOCAL_TZ).timestamp()


def find_time_ranges(text: str, now: float | None = None) -> list[TimeHit]:
    """扫描全部时间表达式命中，按文本跨度排序（规则优先级 → 跨度起点）。"""
    now_local = datetime.fromtimestamp(now if now is not None else datetime.now().timestamp(),
                                       tz=LOCAL_TZ)
    hits: list[TimeHit] = []
    for pat, build in _RULES:
        for m in pat.finditer(text):
            t0, t1 = build(m, now_local)
            hits.append(TimeHit(m.group(0), t0, t1, (m.start(), m.end())))
    hits.sort(key=lambda h: (h.span[0], h.span[1]))
    return hits


def parse_expr(text: str, now: float | None = None) -> TimeHit | None:
    """返回文本中第一个（优先级最高的）时间表达式区间；无命中返回 None。"""
    now_local = datetime.fromtimestamp(now if now is not None else datetime.now().timestamp(),
                                       tz=LOCAL_TZ)
    for pat, build in _RULES:
        m = pat.search(text)
        if m:
            t0, t1 = build(m, now_local)
            return TimeHit(m.group(0), t0, t1, (m.start(), m.end()))
    return None
