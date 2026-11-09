"""CSV 数据可视化（模块六 P0-3）：上传护栏 → 类型推断 → 四类图 spec → 学术图注。

选型：stdlib csv + 手写类型推断（赛题只要求 CSV，不引入 pandas 重依赖）；
spec 兼容 ECharts（前端 echarts@5 渲染 table/line/bar/radar）。
Figure Caption 反幻觉：数字全部来自确定性统计（极值/趋势/对比），LLM 仅润色措辞
且数字以占位符注入；后验校验 caption 中数字 ⊆ 数据集统计（对不上→回退模板句）。
"""
from __future__ import annotations

import csv as _csv
import io
import json
import re
from typing import Any

from .. import db
from ..llm import client as llm

_MAX_BYTES = 5 * 1024 * 1024
_MAX_ROWS = 100_000
_CHART_TYPES = ("table", "line", "bar", "radar", "scatter3d")
_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_FORMULA_PREFIX = ("=", "+", "-", "@", "\t")


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except (ValueError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _sniff_dialect(text: str) -> tuple[str, _csv.Dialect]:
    sample = text[:4096]
    dialect = None
    try:
        dialect = _csv.Sniffer().sniff(sample, delimiters=",;\t|") if sample.strip() else None
    except _csv.Error:
        dialect = None   # 单列/样本太少嗅探失败 → 计数启发
    delim = dialect.delimiter if dialect else ("," if sample.count(",") >= sample.count(";") else ";")
    return delim, dialect


def parse_csv(raw: bytes, name: str) -> dict:
    """上传护栏 + 解析 + 类型推断。公式注入清洗（=+-@ 前缀转义）。"""
    if len(raw) > _MAX_BYTES:
        raise ValueError("file_too_large:超过 5MB 上限")
    text = _decode(raw)
    if not text.strip():
        raise ValueError("empty_csv")
    delim, _ = _sniff_dialect(text)
    rows = list(_csv.reader(io.StringIO(text), delimiter=delim))
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("csv_needs_header_and_one_row")
    header = [(h or f"col{i}").strip().lstrip("\ufeff") for i, h in enumerate(rows[0])]
    if len(header) != len(set(header)):
        header = [f"{h}_{i}" if header[:i].count(h) else h for i, h in enumerate(header)]
    truncated = 0
    data_rows = rows[1:]
    if len(data_rows) > _MAX_ROWS:
        truncated = len(data_rows) - _MAX_ROWS
        data_rows = data_rows[:_MAX_ROWS]

    def _clean(v: str) -> str:
        v = (v or "").strip()
        if v[:1] in _FORMULA_PREFIX:
            return "'" + v     # 公式注入转义
        return v

    cleaned = [[_clean(c) for c in r] + [""] * (len(header) - len(r)) for r in data_rows]
    cleaned = [r[:len(header)] for r in cleaned]

    columns = []
    for i, h in enumerate(header):
        vals = [r[i] for r in cleaned if r[i] != ""]
        numeric = sum(1 for v in vals if _NUM_RE.match(v))
        if vals and numeric / len(vals) >= 0.8:
            columns.append({"name": h, "dtype": "number"})
        elif vals and all(re.match(r"^\d{4}-\d{2}(-\d{2})?$", v) for v in vals[:50]):
            columns.append({"name": h, "dtype": "date"})
        else:
            columns.append({"name": h, "dtype": "string"})
    return {"name": name, "columns": columns, "rows": cleaned,
            "row_count": len(cleaned), "truncated": truncated}


def _serialize_rows_bounded(rows: list[list[str]], columns: list[dict],
                            budget: int = _MAX_BYTES) -> tuple[str, int]:
    """按**行**预算截断序列化（整行丢弃），保证产物永远是合法 JSON——
    旧实现对 dumps 字符串做字节切片，超预算时会截出非法 JSON 导致库损坏。"""
    import json as _json
    kept: list[list[str]] = []
    size = 2   # '[]'
    for r in rows:
        s = len(_json.dumps([r], ensure_ascii=False)) - 2 + 1
        if size + s > budget and kept:
            break
        kept.append(r)
        size += s
    return _json.dumps(kept, ensure_ascii=False), len(kept)


def store_dataset(raw: bytes, name: str) -> dict:
    import hashlib
    import time

    parsed = parse_csv(raw, name)
    data_json, stored_rows = _serialize_rows_bounded(parsed["rows"], parsed["columns"])
    ds_id = db.new_id("ds_")
    with db.tx() as c:
        c.execute("INSERT INTO csv_datasets(id,name,columns,row_count,data,sha256,created_at)"
                  " VALUES(?,?,?,?,?,?,?)",
                  (ds_id, parsed["name"][:120],
                   json.dumps(parsed["columns"], ensure_ascii=False),
                   parsed["row_count"], data_json,
                   hashlib.sha256(raw).hexdigest()[:16], time.time()))
    return {"id": ds_id, "name": parsed["name"], "columns": parsed["columns"],
            "row_count": parsed["row_count"], "truncated": parsed["truncated"],
            "stored_rows": stored_rows,
            "storage_truncated": stored_rows < parsed["row_count"]}


def load_dataset(ds_id: str) -> dict | None:
    rows = db.query("SELECT * FROM csv_datasets WHERE id=?", (ds_id,))
    if not rows:
        return None
    r = rows[0]
    return {"id": r["id"], "name": r["name"],
            "columns": json.loads(r["columns"]), "row_count": r["row_count"],
            "rows": json.loads(r["data"])}


def build_chart(ds: dict, chart_type: str, x: str | None, y: list[str] | None,
                title: str = "") -> dict:
    """图表 spec 构建（服务端校验：列存在、y 数值、radar ≥3 指标、scatter3d ≥3 数值维）。"""
    if chart_type not in _CHART_TYPES:
        raise ValueError(f"chart_type 只支持 {'|'.join(_CHART_TYPES)}")
    cols = {c["name"]: c for c in ds["columns"]}
    names = list(cols)
    if chart_type == "table":
        spec = {"type": "table", "title": title or f"{ds['name']} 预览",
                "columns": names, "rows": ds["rows"][:50],
                "row_count": ds["row_count"],
                "stored_rows": len(ds["rows"])}
        return {"spec": spec}

    x_col = x if x in cols else names[0]
    y_cols = [c for c in (y or []) if c in cols]
    if not y_cols:
        y_cols = [c["name"] for c in ds["columns"] if c["dtype"] == "number"][:3]
    bad = [c for c in y_cols if cols[c]["dtype"] != "number"]
    if bad:
        raise ValueError(f"y 列必须为数值列：{bad} 不是数值")
    if not y_cols:
        raise ValueError("no_numeric_columns:该数据集无数值列，只能出 table")
    if chart_type == "radar" and len(y_cols) < 3:
        raise ValueError("radar 需要至少 3 个数值指标列")
    if chart_type == "scatter3d":
        # 真 3D 散点（echarts-gl grid3D）：X（首数值或类目）+ Y/Z 数值 + 第 4 维 visualMap
        numeric = [c["name"] for c in ds["columns"] if c["dtype"] == "number"]
        need = [c for c in ([x_col] + y_cols) if c in numeric]
        if len(set(need)) < 2 and len(numeric) < 3:
            raise ValueError("scatter3d 需要至少 3 个数值维度（x/y/z）")
        dims = ([x_col] if x_col in numeric else []) + [c for c in numeric if c != x_col][:3]
        dims = dims[:4]
        if len(dims) < 3:
            raise ValueError("scatter3d 需要至少 3 个数值维度（x/y/z）")
        xi = names.index(x_col)
        idx = [names.index(d) for d in dims]
        pts = [[r[i] if not _NUM_RE.match(r[i] or "") else float(r[i]) for i in idx]
               for r in ds["rows"] if all(_NUM_RE.match(r[i] or "") for i in idx[1:])]
        if len(pts) > 5000:   # 3D 点云降采样
            step = len(pts) // 5000 + 1
            pts = pts[::step]
        spec = {"type": "scatter3d", "title": title or f"{ds['name']} · 3D",
                "axes": dims[:3], "visual_dim": dims[3] if len(dims) > 3 else None,
                "points": pts,
                "provenance": {"dataset_id": ds["id"], "dataset_name": ds["name"],
                               "stored_rows": len(ds["rows"])}}
        return {"spec": spec}

    xi = names.index(x_col)
    labels = [r[xi] for r in ds["rows"]]
    series = [{"name": c, "data": [float(r[names.index(c)]) if _NUM_RE.match(r[names.index(c)] or "") else None
                                   for r in ds["rows"]]} for c in y_cols]
    if chart_type in ("line",) and len(labels) > 2000:   # 大数据降采样
        step = len(labels) // 2000 + 1
        labels, series = labels[::step], [{**s, "data": s["data"][::step]} for s in series]
    spec: dict[str, Any] = {"type": chart_type, "title": title or f"{ds['name']} · {x_col}",
                            "x": {"name": x_col, "data": labels},
                            "series": series, "provenance": {"dataset_id": ds["id"],
                                                             "dataset_name": ds["name"]}}
    if chart_type == "radar":
        spec["indicators"] = [{"name": c, "max": max((v for v in s["data"] if v is not None), default=1) * 1.1}
                              for c, s in zip(y_cols, series)]
    return {"spec": spec}


def _column_stats(ds: dict, x_col: str, y_col: str) -> dict:
    names = [c["name"] for c in ds["columns"]]
    xi, yi = names.index(x_col), names.index(y_col)
    pts = [(r[xi], float(r[yi])) for r in ds["rows"] if _NUM_RE.match(r[yi] or "")]
    if not pts:
        return {}
    lo = min(pts, key=lambda p: p[1])
    hi = max(pts, key=lambda p: p[1])
    first, last = pts[0], pts[-1]
    trend = "上升" if last[1] > first[1] else ("下降" if last[1] < first[1] else "持平")
    ratio = round(hi[1] / lo[1], 2) if lo[1] not in (0,) and lo[1] is not None and hi[1] * lo[1] > 0 else None
    return {"n": len(pts), "min": {"label": lo[0], "value": lo[1]},
            "max": {"label": hi[0], "value": hi[1]},
            "first": first, "last": last, "trend": trend, "ratio": ratio}


def make_caption(ds: dict, spec: dict, x_col: str, y_col: str) -> str:
    """学术图注：确定性统计模板 + 可选 LLM 润色（数字钉死占位符注入）。
    后验：caption 中出现的数字必须 ⊆ 统计值，否则回退模板句。"""
    st = _column_stats(ds, x_col, y_col)
    if not st:
        return f"Figure: {spec.get('title', '')}（数据集中该列无数值，无法统计）"
    ratio_txt = f"，最高/最低约 {st['ratio']} 倍" if st.get("ratio") else ""
    template = (f"{spec.get('title', '该图')}（n={st['n']}）：{y_col} 随 {x_col} 整体{st['trend']}"
                f"（首值 {st['first'][1]} → 末值 {st['last'][1]}）；"
                f"峰值出现在 {st['max']['label']}（{st['max']['value']}）"
                f"，谷值在 {st['min']['label']}（{st['min']['value']}）{ratio_txt}。")
    if not llm.available():
        return template
    try:
        res = llm.chat(
            [{"role": "system", "content":
                "你是学术图注润色器。只调整措辞为规范学术表达，所有数字必须原样保留，"
                "禁止新增任何数字或结论。只输出图注一句话。"},
             {"role": "user", "content": template}],
            temperature=0.2, max_tokens=200, module="viz_caption",
            audit_prompt=template[:120], prompt_version="1.0.0")
        text = (res.get("text") or "").strip()
        allowed = {str(abs(round(v, 4))) for v in
                   (st["first"][1], st["last"][1], st["max"]["value"], st["min"]["value"],
                    st["n"], *([st["ratio"]] if st.get("ratio") else []))}
        for num in re.findall(r"\d+\.?\d*", text):
            if num not in allowed and str(round(float(num), 4)) not in allowed:
                return template        # 出现统计外数字 → 回退确定性模板
        return text or template
    except Exception:  # noqa: BLE001 —— 润色失败回退模板
        return template
