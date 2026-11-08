"""block 级翻译服务（4.7 地基；4.4 的 translate_block 工具薄封装复用本模块）。

三道保障（评分表"全文翻译"设计点逐条落地）：
- 占位符保护：行内公式 $..$、`代码`、[n] 引用标记 → ⟦n⟧ 序号占位；译文回填后校验
  占位符完整在场，缺失/被翻译 → format_ok=False 并回退原文（绝不交付烂译文）
- 术语表优先：terminology 表命中术语先行占位（映射直接指向术语译法）→ 全文译法一致
- 缓存续译：translations 表按 (paper_id, block_id, lang) + src_hash 命中即跳过 LLM，
  翻到一半中断后重试秒级续跑
并发限流：ThreadPool(2) + 调用失败单次退避重试（方舟 429 实测经验）。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db
from .config import DOCIR_DIR
from .llm import client as llm
from .llm.prompts import resolve as _resolve
from .log import logger

# 公式（$$..$$ 优先于 $..$）/ 行内代码 / [n] 引用标记（学术论文数字引用）
_TOKEN_RES = [
    re.compile(r"\$\$.+?\$\$", re.DOTALL),
    re.compile(r"\$[^$\n]+?\$"),
    re.compile(r"`[^`\n]+?`"),
    re.compile(r"\[\d{1,3}(?:,\s*\d{1,3})*\]"),
]
_PLACE_RE = re.compile(r"⟦(\d{1,4})⟧")

_TRANSLATE_WORKERS = 2
_translate_lock = threading.Lock()


# ---------- 占位符保护 ----------

def protect(text: str, terms: list[dict] | None = None) -> tuple[str, dict[int, str]]:
    """原文 → 含 ⟦n⟧ 占位符的文本 + 序号→回填内容映射。

    terms：术语表命中项 [{"term","translation"}]，先行占位（回填内容=术语译法）；
    缺省自动查术语表。
    """
    if terms is None:
        terms = hit_terms(text)
    mapping: dict[int, str] = {}
    out = text
    counter = 0

    def _sub(m: re.Match) -> str:
        nonlocal counter
        counter += 1
        mapping[counter] = m.group(0)
        return f"⟦{counter}⟧"

    for t in terms or []:
        term = t.get("term", "")
        trans = t.get("translation", "")
        if not term or not trans:
            continue
        pat = re.compile(re.escape(term), re.IGNORECASE)
        while True:
            m = pat.search(out)
            if not m or "⟦" in m.group(0):
                break
            counter += 1
            mapping[counter] = trans
            out = out[:m.start()] + f"⟦{counter}⟧" + out[m.end():]
    for pattern in _TOKEN_RES:
        out = pattern.sub(_sub, out)
    return out, mapping


def restore(translated: str, mapping: dict[int, str]) -> tuple[str, bool]:
    """译文回填占位符；任一占位符缺失 → (原文回退, format_ok=False) 由调用方处理。"""
    found = {int(n) for n in _PLACE_RE.findall(translated)}
    ok = found == set(mapping.keys())
    if not ok:
        return "", False

    def _sub(m: re.Match) -> str:
        return mapping.get(int(m.group(1)), m.group(0))

    return _PLACE_RE.sub(_sub, translated), True


def src_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------- 术语表 ----------

def hit_terms(text: str) -> list[dict]:
    """返回 text 命中的术语表条目。"""
    terms = db.query("SELECT term, translation FROM terminology ORDER BY LENGTH(term) DESC")
    low = text.lower()
    return [dict(t) for t in terms if t["term"].lower() in low]


# ---------- LLM 翻译单块 ----------

def _llm_translate(protected: str, terms: list[dict]) -> str:
    system, pv = _resolve("translate", "system_translate",
                          "你是学术文献翻译器。只译自然语言，⟦n⟧ 占位符原样保留；术语表优先；只输出译文。")
    term_txt = json.dumps([{ "term": t["term"], "translation": t["translation"]} for t in terms],
                          ensure_ascii=False) if terms else "（无）"
    last_err = ""
    for attempt in range(2):  # 单次退避重试（方舟限流实测）
        res = llm.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content":
                 f"术语对照表：{term_txt}\n\n待翻译段落：\n{protected}"}],
            temperature=0.1, max_tokens=1600, module="translate",
            audit_prompt=protected[:200], prompt_version=pv,
        )
        text = res.get("text", "").strip()
        if text:
            return text
        last_err = res.get("error", "empty")
        if attempt == 0:
            time.sleep(2.0)
    raise RuntimeError(f"translate_llm_failed:{last_err}")


# ---------- 缓存 ----------

def _cached(paper_id: str, block_id: str, lang: str, hash_: str) -> dict | None:
    rows = db.query(
        "SELECT text, format_ok, src_hash FROM translations "
        "WHERE paper_id=? AND block_id=? AND lang=?",
        (paper_id, block_id, lang))
    if rows and rows[0]["src_hash"] == hash_:   # 原文变了 → 旧译文失效，重译
        return dict(rows[0])
    return None


def _store(paper_id: str, block_id: str, lang: str, text: str, hash_: str, ok: bool) -> None:
    with _translate_lock, db.tx() as c:
        c.execute(
            "INSERT INTO translations(paper_id,block_id,lang,text,src_hash,format_ok,created_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(paper_id,block_id,lang) "
            "DO UPDATE SET text=excluded.text, src_hash=excluded.src_hash, "
            "format_ok=excluded.format_ok, created_at=excluded.created_at",
            (paper_id, block_id, lang, text, hash_, 1 if ok else 0, time.time()))


# ---------- DocIR 读取 ----------

def load_blocks(paper_id: str) -> dict:
    path = Path(DOCIR_DIR) / f"{paper_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"docir_missing:{paper_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def select_block_ids(docir: dict, *, page: int | None = None,
                     section: str | None = None, block_ids: list[str] | None = None) -> list[str]:
    """选择需翻译的块：自然语言块（paragraph/heading）；按页 / 按章 / 显式清单。"""
    out = []
    for b in docir.get("blocks", []):
        if b.get("type") not in ("paragraph", "heading") or not (b.get("text") or "").strip():
            continue
        if block_ids is not None and b["id"] not in block_ids:
            continue
        if page is not None and b.get("page") != page:
            continue
        if section is not None and not (b.get("section_path") or "").startswith(section):
            continue
        out.append(b["id"])
    return out


def _block_text(docir: dict, block_id: str) -> str:
    for b in docir.get("blocks", []):
        if b["id"] == block_id:
            return b.get("text") or ""
    return ""


# ---------- 批量入口 ----------

def translate_blocks(paper_id: str, block_ids: list[str], *, lang: str = "zh",
                     progress=None, should_stop=None) -> dict:
    """批量翻译（缓存优先，未命中走限流并发）。progress(done,total,block_id,text,ok)。

    should_stop() 返回 True 时提前停止（已完成部分已落缓存 → 续译天然成立）。
    """
    docir = load_blocks(paper_id)
    total = len(block_ids)
    stats = {"done": 0, "cached": 0, "failed": 0, "format_bad": 0}
    if total == 0:
        return stats

    todo: list[str] = []
    for bid in block_ids:   # 先收割缓存（秒级续译）
        text = _block_text(docir, bid)
        if not text:
            continue
        hit = _cached(paper_id, bid, lang, src_hash(text))
        if hit:
            stats["done"] += 1
            stats["cached"] += 1
            if progress:
                progress(stats["done"], total, bid, hit["text"], bool(hit["format_ok"]))
        else:
            todo.append(bid)

    def _one(bid: str) -> None:
        text = _block_text(docir, bid)
        terms = hit_terms(text)
        protected, mapping = protect(text, terms)
        try:
            raw = _llm_translate(protected, terms)
            restored, ok = restore(raw, mapping)
            if not ok:
                restored, ok = text, False   # 占位符被翻译/丢失 → 回退原文，绝不交付烂译文
                with _translate_lock:
                    stats["format_bad"] += 1
            _store(paper_id, bid, lang, restored, src_hash(text), ok)
            with _translate_lock:
                stats["done"] += 1
                done, ok_flag = stats["done"], ok
            if progress:
                progress(done, total, bid, restored, ok_flag)
        except Exception as e:  # noqa: BLE001 —— 单块失败不断链，如实计数
            with _translate_lock:
                stats["failed"] += 1
                done = stats["done"]
            logger.warning(f"translate block 失败: {type(e).__name__}: {e}", module="translate")
            if progress:
                progress(done, total, bid, "", False)

    if todo:
        with ThreadPoolExecutor(max_workers=_TRANSLATE_WORKERS) as pool:
            futures = []
            for i, bid in enumerate(todo):
                if should_stop is not None and should_stop():
                    break
                # 提交节奏限流：最多同时 2 个在飞（ThreadPool 已限），批内顺序提交
                futures.append(pool.submit(_one, bid))
                if i and i % 16 == 0:
                    time.sleep(0.3)   # 批间隔，避开连发限流
            for f in futures:
                f.result()
    return stats


def translate_selection(text: str, context: str = "") -> dict:
    """划词翻译：上下文感知短译 + 术语命中提示。"""
    terms = hit_terms(text)
    protected, mapping = protect(text, terms)
    system, pv = _resolve("translate", "system_translate",
                          "你是学术翻译器。只译自然语言，⟦n⟧ 占位符原样保留；只输出译文。")
    res = llm.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content":
             (f"上下文（仅辅助理解，不翻译）：{context[:800]}\n\n" if context else "")
             + f"翻译以下选中文本：\n{protected}"}],
        temperature=0.1, max_tokens=400, module="translate_selection",
        audit_prompt=text[:200], prompt_version=pv,
    )
    raw = res.get("text", "").strip()
    if not raw:
        return {"translation": "", "term_hit": terms[0] if terms else None, "ok": False}
    out, ok = restore(raw, mapping)
    if not ok:
        return {"translation": text, "term_hit": terms[0] if terms else None,
                "ok": False, "reason": "placeholder_lost"}
    return {"translation": out, "term_hit": terms[0] if terms else None, "ok": True}


def cached_translations(paper_id: str, lang: str = "zh") -> dict[str, dict]:
    rows = db.query(
        "SELECT block_id, text, format_ok FROM translations WHERE paper_id=? AND lang=?",
        (paper_id, lang))
    return {r["block_id"]: {"text": r["text"], "format_ok": bool(r["format_ok"])} for r in rows}
