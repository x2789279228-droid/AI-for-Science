"""表格解析共享工具（pipeline / tools / vlm_engine / mineru_engine 共用）。

- parse_md_table：GFM 管道表格 → (headers, rows)（原 tools._parse_md_table）
- table_to_md：单元格矩阵 → GFM（原 pipeline._table_to_md）
- html_table_to_rows：MinerU 表格 HTML → (headers, rows)（std lib HTMLParser，零依赖）
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_MD_SEP_RE = re.compile(r"^:?-{2,}:?$")


def parse_md_table(md: str) -> tuple[list[str], list[list[str]]]:
    """解析 Markdown 管道表格 → (headers, rows)；跳过分隔行；不足两行视为无效。"""
    lines = [l.strip() for l in (md or "").splitlines() if l.strip().startswith("|")]
    rows: list[list[str]] = []
    for l in lines:
        cells = [c.strip() for c in l.strip("|").split("|")]
        if cells and all(_MD_SEP_RE.match(c) for c in cells if c):
            continue  # |---|---| 分隔行
        rows.append(cells)
    if len(rows) < 2:
        return [], []
    return rows[0], rows[1:]


def table_to_md(rows: list[list[str | None]]) -> str:
    rows = [[("" if c is None else str(c).replace("\n", " ").strip()) for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    md = "| " + " | ".join(rows[0]) + " |\n"
    md += "|" + "---|" * len(rows[0]) + "\n"
    for r in rows[1:]:
        md += "| " + " | ".join(r + [""] * (len(rows[0]) - len(r))) + " |\n"
    return md


class _HtmlTableParser(HTMLParser):
    """最小 HTML 表格收集器：thead/tbody 首行作表头，其余为数据行。"""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._flush_row()
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = []
        elif tag == "tr":
            self._flush_row()

    def handle_data(self, data: str) -> None:
        if self._row is not None:
            self._cell.append(data)

    def _flush_row(self) -> None:
        if self._row and any(self._row):
            self.rows.append(self._row)
        self._row = None


def html_table_to_rows(html: str) -> tuple[list[str], list[list[str]]]:
    """HTML <table> → (headers, rows)；无表结构返回 ([], [])。"""
    if "<table" not in (html or "").lower():
        return [], []
    p = _HtmlTableParser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 —— 非法 HTML 退回 markdown 管道解析
        return [], []
    if len(p.rows) < 2:
        return [], []
    return p.rows[0], p.rows[1:]
