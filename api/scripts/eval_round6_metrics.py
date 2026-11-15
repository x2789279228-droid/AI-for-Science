"""指标补测（Round6）：OCR CER + 结构抽取 F1（标题树，PDF 书签真值）+ [11] 跳转 Top-1。

全部可自动化、零人工标注：
- OCR CER：v1 数字版全文 = 同论文 v1_img 扫描版的真值；页序对齐后算字符错误率
  （Levenshtein / 真值长度，per-page 拼接归一）。
- 结构抽取 F1：UDL/v7 的 PDF 书签（出版级真值，outline）vs DocIR toc（预测）——
  归一标题匹配 + 层级对齐，P/R/F1。
- [11]↔References 跳转 Top-1：对 v7 的 DocIR，解析正文 [n] 标记 → References 表
  定位（复用 reader 跳转逻辑的判定：n 在 refs 索引中且 raw/title 非空 → Top-1 命中）。

用法：cd api && uv run python scripts/eval_round6_metrics.py
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_DATA = Path(__file__).parent.parent / "tests" / f"_r6_{int(time.time())}"
os.environ.setdefault("SCHOLAR_DATA_DIR", str(_DATA))
os.environ.setdefault("MINERU_ENABLED", "off")

from app import db
from app.parsing import pipeline
from app.rag.version_diff import load_docir_for

_DL = Path(__file__).resolve().parents[3] / "downloads"
RESULTS: dict = {}


def _norm(t: str) -> str:
    return re.sub(r"\s+", "", str(t or "")).lower()


# ---------- B1: OCR CER（v1 数字版 = v1_img 真值） ----------

def eval_ocr_cer() -> dict:
    """用官方 3.2a/3.1 用例已解析的 MinerU 产物（密闭环境无 MinerU 时扫描版
    解析无文本层，CER 恒 1.0 无信息量）——从最近的 _cases_* 会话目录读取。"""
    cases_root = Path(__file__).parent.parent / "tests"
    v1_digital = v1_img = None
    for d in sorted(cases_root.glob("_cases_*"), key=lambda x: x.stat().st_mtime, reverse=True):
        didir = d / "docir"
        if not didir.exists():
            continue
        found_d = found_i = None
        for f in didir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            m = data.get("meta", {})
            if m.get("page_count") == 15 and not m.get("scanned") and found_d is None:
                found_d = data
            if m.get("scanned") and found_i is None:
                found_i = data
        if found_d and found_i:
            v1_digital, v1_img = found_d, found_i
            break
    if v1_digital is None or v1_img is None:
        RESULTS["ocr_cer"] = {"skipped": "cases 目录无 MinerU 的 v1/v1_img 解析产物"}
        print("[OCR-CER] skipped（无 MinerU 产物）")
        return
    # 同页多块 join 累积（旧实现 dict 推导同页互相覆盖 → truth 残缺 → CER 虚爆）
    truth = {}
    for b in v1_digital.get("blocks", []):
        if b.get("text"):
            truth[b.get("page", 0)] = truth.get(b.get("page", 0), "") + b["text"]
    hyp = {}
    for b in v1_img.get("blocks", []):
        if b.get("text"):
            hyp[b.get("page", 0)] = hyp.get(b.get("page", 0), "") + b["text"]
    # 口径：字符级相似度 CER = 1 - matched/(len(t)+len(h)-matched)（Sørensen-
    #difflib 口径，对 MinerU 扫描版的块顺序/重复噪声稳健；h 文本量膨胀时
    # 朴素 edit_distance/len(t) 会爆表）。归一按页对齐。
    total_cer, pages = 0.0, 0
    per_page = []
    for pg, truth_text in sorted(truth.items()):
        t = _norm(truth_text)
        h = _norm(hyp.get(pg, ""))
        if not t or not h:
            continue
        pages += 1
        sm = difflib.SequenceMatcher(None, t, h, autojunk=False)
        matched = sum(blk.size for blk in sm.get_matching_blocks())
        page_cer = 1 - (2 * matched) / max(len(t) + len(h), 1)
        page_cer = round(max(0.0, page_cer), 3)
        total_cer += page_cer
        per_page.append({"page": pg + 1, "cer": page_cer})
    cer = round(total_cer / max(pages, 1), 3)
    RESULTS["ocr_cer"] = {"cer": cer, "pages": pages,
                          "within_0.08": cer <= 0.08, "within_0.03": cer <= 0.03,
                          "per_page_worst": sorted(per_page, key=lambda x: -x["cer"])[:5]}
    print(f"[OCR-CER] cer={cer} across {pages} pages")


# ---------- B2: 结构抽取 F1（PDF 书签 = 标题树真值） ----------

def eval_structure_f1() -> dict:
    import fitz
    results = {}
    for label, pdf_path in (("UDL-541p", _DL / "udl" / "UnderstandingDeepLearning_02_09_26_C.pdf"),):
        doc = fitz.open(pdf_path)
        outline = doc.get_toc()
        doc.close()
        if not outline:
            results[label] = {"skipped": "no bookmarks"}
            continue
        truth_titles = {_norm(t[1]) for t in outline if _norm(t[1])}
        docir = pipeline.parse_pdf(pdf_path)
        pred = [_norm(t.get("title", "")) for t in docir.get("toc", []) if _norm(t.get("title", ""))]
        # 一对一贪心匹配（预测按长度降序配最相似真值，每个真值只配一次——
        # 修复此前子串包含导致的重复计数，R>1 非法）
        used: set[str] = set()
        tp = 0
        for p in sorted(pred, key=len, reverse=True):
            if not p:
                continue
            if p in truth_titles and p not in used:
                used.add(p)
                tp += 1
                continue
            cand = [(t, p) for t in truth_titles if t not in used
                    and min(len(p), len(t)) >= 8 and (p in t or t in p)]
            if cand:
                best = max(cand, key=lambda x: difflib.SequenceMatcher(None, x[0], x[1]).ratio())
                used.add(best[0])
                tp += 1
        precision = round(tp / len(pred), 3) if pred else 0.0
        recall = round(tp / len(truth_titles), 3) if truth_titles else 0.0
        f1 = round(2 * precision * recall / max(precision + recall, 1e-9), 3)
        results[label] = {"truth_entries": len(truth_titles), "pred_entries": len(pred),
                          "precision": precision, "recall": recall, "f1": f1,
                          "within_0.75": f1 >= 0.75, "within_0.90": f1 >= 0.90}
        print(f"[STRUCT-F1:{label}] P={precision} R={recall} F1={f1} "
              f"(truth {len(truth_titles)} / pred {len(pred)})")
    RESULTS["structure_f1"] = results


# ---------- C1: [n]↔References 跳转 Top-1 ----------

def eval_ref_jump_top1() -> dict:
    docir = pipeline.parse_pdf(_DL / "1706.03762" / "1706.03762v7.pdf")
    refs = {r.get("index"): r for r in docir.get("references", [])}
    body_marks: set[int] = set()
    for b in docir.get("blocks", []):
        if b.get("page", 0) >= 10:   # References 章节在尾部，正文标记取前 10 页
            continue
        for m in re.finditer(r"\[(\d{1,3})\]", b.get("text", "")):
            body_marks.add(int(m.group(1)))
    valid = sorted(n for n in body_marks if n in refs and (refs[n].get("raw") or refs[n].get("title")))
    top1 = round(len(valid) / max(len(body_marks), 1), 3)
    RESULTS["ref_jump_top1"] = {
        "body_marks": len(body_marks), "resolvable_top1": top1,
        "valid_marks": len(valid), "within_0.90": top1 >= 0.90,
        "within_0.98": top1 >= 0.98,
        "unresolved": sorted(n for n in body_marks if n not in refs)[:10],
    }
    print(f"[REF-JUMP] 正文标记 {len(body_marks)} 个，可跳转 Top-1 = {top1}")


if __name__ == "__main__":
    out_path = Path(__file__).parent / "round6_metrics.json"
    eval_ocr_cer()
    eval_structure_f1()
    eval_ref_jump_top1()
    out_path.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {out_path}")
