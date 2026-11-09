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

from . import db
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

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ZH2EN_BUILTIN = ("You are an academic translation engine (Chinese -> English). Translate "
                  "natural language only, keep ⟦n⟧ placeholders verbatim, follow the "
                  "terminology table, never add/omit claims. Output the translation only.")


def detect_lang(text: str) -> str:
    """粗粒度语言检测：CJK 字符占比 >8% 判 zh，否则 en（仅用于方向校验，非语言学断言）。"""
    if not text:
        return "en"
    cjk = len(_CJK_RE.findall(text))
    return "zh" if cjk / max(len(text), 1) > 0.08 else "en"


def cache_key(lang: str, mode: str) -> str:
    """复合缓存键 target_lang|mode。legacy 行（"zh"）读取时按 zh|academic 兼容命中，
    写入恒为复合键——UNIQUE(paper_id, block_id, lang) 天然隔离不同方向/档位。"""
    return f"{lang}|{mode}"


def _system_for(target_lang: str, mode: str) -> tuple[str, str]:
    """按方向选槽：en→zh 用 system_translate，zh→en 用 system_translate_zh2en；
    literal 档位在两个方向上追加直译指令（同一槽位内分化，避免槽位爆炸）。"""
    if target_lang == "en":
        system, pv = _resolve("translate", "system_translate_zh2en", _ZH2EN_BUILTIN)
    else:
        system, pv = _resolve("translate", "system_translate",
                              "你是学术文献翻译器。只译自然语言，⟦n⟧ 占位符原样保留；术语表优先；只输出译文。")
    if mode == "literal":
        system += "\n5. 直译档位：优先字面对应与句序，仅为保证目标语可读性作最小调整，禁止润色、扩写或意译。"
    return system, pv


def _llm_translate(protected: str, terms: list[dict],
                   target_lang: str = "zh", mode: str = "academic") -> str:
    system, pv = _system_for(target_lang, mode)
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

def _cached(paper_id: str, block_id: str, lang: str, hash_: str,
            legacy: str | None = None) -> dict | None:
    """缓存命中（src_hash 校验原文未变）。legacy：兼容旧版裸 "zh" 行——升级到复合键
    后旧译文仍可命中，不为同一内容重复花 LLM 费用。"""
    rows = db.query(
        "SELECT text, format_ok, src_hash FROM translations "
        "WHERE paper_id=? AND block_id=? AND lang=?",
        (paper_id, block_id, lang))
    if not rows and legacy:
        rows = db.query(
            "SELECT text, format_ok, src_hash FROM translations "
            "WHERE paper_id=? AND block_id=? AND lang=?",
            (paper_id, block_id, legacy))
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
    """DocIR 读取（与 docir 端点同源：DB docir_path 优先、数据目录回退——统一 404 判据）。"""
    from .rag.version_diff import load_docir_for

    data = load_docir_for(paper_id)
    if data is None:
        raise FileNotFoundError(f"docir_missing:{paper_id}")
    return data


def select_block_ids(docir: dict, *, page: int | None = None,
                     section: str | None = None, block_ids: list[str] | None = None,
                     include_extras: bool = False) -> list[str]:
    """选择需翻译的块：自然语言块（paragraph/heading）；按页 / 按章 / 显式清单。

    include_extras（模块三 P1）：纳入表格（markdown）与图注（caption）——伪块 id
    即 DocIR 的 t#/f#，译文缓存/读取与普通块同一套（translations.block_id）。"""
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
    if include_extras:
        want = set(block_ids) if block_ids is not None else None
        for t in docir.get("tables", []):
            if not (t.get("markdown") or "").strip():
                continue
            if want is not None and t["id"] not in want:
                continue
            if page is not None and t.get("page") != page:
                continue
            out.append(t["id"])
        for f in docir.get("figures", []):
            if not (f.get("caption") or "").strip():
                continue
            if want is not None and f["id"] not in want:
                continue
            if page is not None and f.get("page") != page:
                continue
            out.append(f["id"])
    return out


def _block_text(docir: dict, block_id: str) -> str:
    for b in docir.get("blocks", []):
        if b["id"] == block_id:
            return b.get("text") or ""
    for t in docir.get("tables", []):
        if t["id"] == block_id:
            return t.get("markdown") or ""
    for f in docir.get("figures", []):
        if f["id"] == block_id:
            return (f.get("caption") or "").strip()
    return ""


# ---------- 批量入口 ----------

def translate_blocks(paper_id: str, block_ids: list[str], *, lang: str = "zh",
                     mode: str = "academic", progress=None, should_stop=None) -> dict:
    """批量翻译（缓存优先，未命中走限流并发）。progress(done,total,block_id,text,ok)。

    lang/mode 共同构成缓存键（cache_key）；zh|academic 读侧兼容 legacy "zh" 行。
    should_stop() 返回 True 时提前停止（已完成部分已落缓存 → 续译天然成立）。
    """
    docir = load_blocks(paper_id)
    key = cache_key(lang, mode)
    legacy = "zh" if key == "zh|academic" else None
    total = len(block_ids)
    stats = {"done": 0, "cached": 0, "failed": 0, "format_bad": 0}
    if total == 0:
        return stats

    todo: list[str] = []
    for bid in block_ids:   # 先收割缓存（秒级续译）
        text = _block_text(docir, bid)
        if not text:
            continue
        hit = _cached(paper_id, bid, key, src_hash(text), legacy=legacy)
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
            raw = _llm_translate(protected, terms, target_lang=lang, mode=mode)
            restored, ok = restore(raw, mapping)
            if not ok:
                restored, ok = text, False   # 占位符被翻译/丢失 → 回退原文，绝不交付烂译文
                with _translate_lock:
                    stats["format_bad"] += 1
            _store(paper_id, bid, key, restored, src_hash(text), ok)
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


def translate_selection(text: str, context: str = "", *, target_lang: str = "zh",
                        mode: str = "academic") -> dict:
    """划词翻译：上下文感知短译 + 术语命中提示。target_lang=en → 中→英学术短译。"""
    terms = hit_terms(text)
    protected, mapping = protect(text, terms)
    system, pv = _system_for(target_lang, mode)
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


def cached_translations(paper_id: str, lang: str = "zh", mode: str = "academic") -> dict[str, dict]:
    """缓存译文读取：复合键行优先，zh|academic 无命中时回退 legacy "zh" 行（旧数据
    兼容）；两者都存在时复合键覆盖（同一块以新键重译过则以新译文为准）。"""
    key = cache_key(lang, mode)
    out: dict[str, dict] = {}
    if key == "zh|academic":
        for r in db.query(
                "SELECT block_id, text, format_ok FROM translations WHERE paper_id=? AND lang=?",
                (paper_id, "zh")):
            out[r["block_id"]] = {"text": r["text"], "format_ok": bool(r["format_ok"])}
    for r in db.query(
            "SELECT block_id, text, format_ok FROM translations WHERE paper_id=? AND lang=?",
            (paper_id, key)):
        out[r["block_id"]] = {"text": r["text"], "format_ok": bool(r["format_ok"])}
    return out
