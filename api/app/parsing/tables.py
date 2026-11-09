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
    """HTML 表格收集器：矩阵填充式（细粒度 P2）——处理 colspan/rowspan。

    rowspan 在后续行的对应列补占位值（重复原值，矩阵保持矩形）；
    colspan 向右扩展（值重复）。占位追踪用 _taken 集合，与真实空单元格区分。
    """

    def __init__(self) -> None:
        super().__init__()
        self.grid: list[list[str]] = []
        self._taken: set[tuple[int, int]] = set()
        self._row_idx = -1
        self._cell: list[str] = []
        self._span: tuple[int, int] = (1, 1)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row_idx += 1
        elif tag in ("td", "th") and self._row_idx >= 0:
            self._cell = []
            ad = dict(attrs or [])
            self._span = (_int_clamp(ad.get("colspan")), _int_clamp(ad.get("rowspan")))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._row_idx >= 0:
            value = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            c = 0
            while (self._row_idx, c) in self._taken:
                c += 1
            cs, rs = self._span
            for rr in range(self._row_idx, self._row_idx + rs):
                for cc in range(c, c + cs):
                    while len(self.grid) <= rr:
                        self.grid.append([])
                    while len(self.grid[rr]) <= cc:
                        self.grid[rr].append("")
                    self.grid[rr][cc] = value
                    self._taken.add((rr, cc))
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._row_idx >= 0:
            self._cell.append(data)


def _int_clamp(v: str | None) -> int:
    try:
        return max(1, min(50, int(str(v or 1))))
    except (TypeError, ValueError):
        return 1


def html_table_to_rows(html: str) -> tuple[list[str], list[list[str]]]:
    """HTML <table> → (headers, rows)（colspan/rowspan 已展开为矩形矩阵）；
    无表结构返回 ([], [])。"""
    if "<table" not in (html or "").lower():
        return [], []
    p = _HtmlTableParser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 —— 非法 HTML 退回 markdown 管道解析
        return [], []
    if len(p.grid) < 2:
        return [], []
    width = max(len(r) for r in p.grid)
    grid = [r + [""] * (width - len(r)) for r in p.grid]
    return grid[0], grid[1:]
