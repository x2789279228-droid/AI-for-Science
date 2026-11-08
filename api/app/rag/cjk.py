"""CJK bigram 文本变换：让 FTS5(unicode61) 能命中中文。

unicode61 把连续汉字串当成一个长 token，中文查询与中文文档永远无法按词相交。
入库与查询两侧统一做 bigram 预切（CJK 连续段 → 二字词、空格分隔），英文/数字原样，
对英文 BM25 零影响（spike 已验证：P100/BLEU 命中不变，中文查询正常命中）。
"""
from __future__ import annotations

import re

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def cjk_bigram(text: str) -> str:
    """CJK 连续段切成二字词（空格分隔），其余字符原样返回。"""

    def repl(m: re.Match) -> str:
        s = m.group(0)
        if len(s) < 2:
            return f" {s} "
        return " " + " ".join(s[i:i + 2] for i in range(len(s) - 1)) + " "

    return _CJK_RUN.sub(repl, text)
