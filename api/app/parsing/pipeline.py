"""DocIR 解析管道（模块一·快速路径）。

PyMuPDF 提取 → 双栏阅读顺序重排（x 坐标聚类）→ 标题树（字号层级）
→ 表格（find_tables）→ 图片+caption 关联 → References 锚点切段逐条解析。
输出统一 DocIR JSON（方案 §4.1），溯源锚点 (page, bbox) 一等公民。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from ._types import CancelCheck, ParseCancelled
from .tables import table_to_md as _table_to_md  # 共享表格工具（tools/vlm/mineru 引擎复用）

# ---------- 工具 ----------

_REF_HEAD = re.compile(r"^\s*(references|bibliography|参考文献)\s*$", re.IGNORECASE)
_REF_ITEM_LINE = re.compile(r"^\[\d{1,3}\]")
# Phase 0 修复：`[n]` 标记在条目内任意位置出现也应切分——MinerU/PDF 引擎常把
# References 整节并成单个段落块，行内 `[2]`、`[3]` 不在行首，旧行首状态机把全部
# 内容折进第 1 条（BERT/PreLN/GPT-2 references=1 脏巨块实证根因）。
_REF_MARKER = re.compile(r"\[(\d{1,3})\]")
# 字母键引用风格：[ACDE12]/[HCC+18]（Scaling Laws 等采用；MinerU 常把含 ^{+} 的键
# 误转成 LaTeX 公式，字面形态只余一部分——字面键 + 作者-年份头双通道切分兜底）
_REF_ALPHA_KEY = re.compile(r"\[[A-Za-z][A-Za-z+&]{1,9}\d{2,4}\]")
_AUTHOR_YEAR_HEAD = re.compile(
    r"(?P<name>[A-Z][\w\-'’]*(?:[,\s]+(?:and\s+)?[A-Z][\w\-'’]*){0,9})[.,]?\s*"
    r"\(?((19|20)\d{2}[a-z]?)\)?\.")
# 作者-年份头守卫：载体短语开头（"In Proceedings…, 2018."）不是作者头，误切会
# 把上一条的 venue 尾巴切成新条目
_VENUE_OPENERS = {"in", "proceedings", "advances", "journal", "conference", "international",
                  "annual", "ieee", "acm", "nature", "science", "arxiv", "transactions",
                  "computational", "physical", "review", "reviews", "neurips", "icml",
                  "iclr", "cvpr", "acl", "emnlp", "naacl", "foundations", "trends"}
_REF_ANOMALY_RATIO = 4.0  # 单条长度 > max(4×中位, 400字) → anomaly（脏块暴露，降噪声）
_ARXIV_ID = re.compile(r"arxiv[:\s]*(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
_DOI = re.compile(r"10\.\d{4,9}/[\w.\-/:]+", re.IGNORECASE)
_APPENDIX_HEAD = re.compile(r"^(?:[a-zA-Z]\s+)?(appendices|appendix|附录|附錄)\b", re.IGNORECASE)
_BACKMATTER_HEAD = re.compile(r"^(index|notation|glossary|acknowledg|author index|subject index)", re.IGNORECASE)
_TABLE_CAPTION = re.compile(r"(?:table|表)\s*(\d+)", re.IGNORECASE)


def _norm_title(t: str) -> str:
    """标题归一（空白折叠 + casefold）：跨引擎 toc 标题与块文本比较的统一口径。"""
    return re.sub(r"\s+", " ", (t or "")).strip().casefold()


_LEAD_NUM = re.compile(r"^(?:\d+(?:\.\d+)*|[ivxlcdm]+\.)\s+", re.IGNORECASE)


def _title_match(block_text: str, toc_title: str) -> bool:
    """标题匹配降级链：归一化全等 → 剥前导节编号相等。
    v7 实证：字号树 toc 标题常无节编号（"Introduction"），块文本带编号
    （"1 Introduction"）——剥编号兜底后 heading 命中率从 0.09 回升。"""
    na, nb = _norm_title(block_text), _norm_title(toc_title)
    if na == nb:
        return True
    return bool(na) and _LEAD_NUM.sub("", na, count=1) == _LEAD_NUM.sub("", nb, count=1)


def _mark_appendix(toc: list[dict], meta: dict,
                   blocks: list[dict] | None = None) -> None:
    """附录结构化：Appendix/附录（含 "A Appendix"/"Appendix A" 前缀编号形态）起的
    toc 条目打标 + meta.appendix 汇总；blocks 提供时给起始页之后的块打标。"""
    idx = next((i for i, t in enumerate(toc)
                if _APPENDIX_HEAD.match(_norm_title((t.get("title") or "").strip()))), None)
    if idx is None:
        return
    appendix_toc = toc[idx:]
    for t in appendix_toc:
        t["appendix"] = True
    start = appendix_toc[0].get("page")
    meta["appendix"] = {"start_page": start,
                        "sections": [t.get("title", "") for t in appendix_toc]}
    if blocks is not None and start is not None:
        for b in blocks:
            if b.get("page", 0) >= start:
                b["appendix"] = True


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _has_text_layer(doc: fitz.Document) -> bool:
    """文本层检测：前 5 页平均字符密度过低 → 视为扫描件（走困难路径，MVP 先标记）。"""
    if doc.page_count == 0:
        return False
    sample = min(5, doc.page_count)
    chars = sum(len(doc[i].get_text("text").strip()) for i in range(sample))
    return chars / sample >= 80  # 每页至少 ~80 字符


# ---------- 阅读顺序（双栏重排） ----------

def _pymupdf_page_blocks(page: fitz.Page) -> list[dict]:
    """PyMuPDF 页 → 内部块列表（归一化 bbox/lines/text/size）。
    parse_pdf 主循环与标题/作者首页启发式共用（longdoc 初始化复用）。"""
    pw, ph = page.rect.width, page.rect.height
    out: list[dict] = []
    for blk in page.get_text("dict").get("blocks", []):
        if blk["type"] != 0:  # 0=text, 1=image
            continue
        x0, y0, x1, y1 = blk["bbox"]
        lines = blk.get("lines", [])
        text = "\n".join("".join(s["text"] for s in ln["spans"]) for ln in lines).strip()
        if not text:
            continue
        out.append({"bbox": [round(x0 / pw, 4), round(y0 / ph, 4),
                             round(x1 / pw, 4), round(y1 / ph, 4)],
                    "lines": lines, "text": text,
                    "size": max((s["size"] for ln in lines for s in ln["spans"]), default=10.0)})
    return out

def _raw_order_perm(orig: list[dict], reordered: list[dict]) -> list[int] | None:
    """原始序 → 重排序的置换（perm[k] = 原始序第 k 块在重排后数组中的索引）。

    恒等（未发生重排）或块数不足时返回 None——meta.raw_orders 只记真实重排页。
    """
    if len(orig) != len(reordered) or len(orig) < 2:
        return None
    pos = {id(b): i for i, b in enumerate(reordered)}
    perm = [pos[id(b)] for b in orig]
    return perm if perm != list(range(len(perm))) else None


def _reading_order(blocks: list[dict]) -> list[dict]:
    """阅读顺序重排 v2（归一化 bbox，页宽=1）。

    规则：跨中线的宽块视为分隔符（标题/通栏摘要/表格），flush 已积攒的
    左右栏队列后插入；非跨栏块按中心 x 分入左/右队列，flush 时左列全出
    再出右列（双栏先左后右）。竖排页边横幅（arXiv 侧边条）为 furniture 置底。
    """
    if not blocks:
        return blocks
    mid = 0.5
    def crosses(b: dict) -> bool:
        x0, _, x1, _ = b["bbox"]
        return x0 < mid - 0.04 and x1 > mid + 0.04
    furniture = [b for b in blocks if (b["bbox"][2] - b["bbox"][0]) < 0.08
                 and (b["bbox"][3] - b["bbox"][1]) > 0.25]
    body = [b for b in blocks if b not in furniture]

    left: list[dict] = []
    right: list[dict] = []
    out: list[dict] = []
    for b in sorted(body, key=lambda b: b["bbox"][1]):
        if crosses(b):
            out += sorted(left, key=lambda x: (x["bbox"][1], x["bbox"][0]))
            out += sorted(right, key=lambda x: (x["bbox"][1], x["bbox"][0]))
            left, right = [], []
            out.append(b)
        elif (b["bbox"][0] + b["bbox"][2]) / 2 < mid:
            left.append(b)
        else:
            right.append(b)
    out += sorted(left, key=lambda x: (x["bbox"][1], x["bbox"][0]))
    out += sorted(right, key=lambda x: (x["bbox"][1], x["bbox"][0]))
    out += sorted(furniture, key=lambda x: (x["bbox"][1], x["bbox"][0]))
    return out


# ---------- 标题层级 ----------

def _build_toc(doc: fitz.Document, blocks_by_page: dict[int, list[dict]]) -> list[dict]:
    """优先 PDF 书签；否则按字号层级推断标题块。toc.page 与 blocks 一致为 0 基。"""
    toc = []
    try:
        for lvl, title, page in doc.get_toc():   # get_toc 页码 1 基 → 统一转 0 基
            toc.append({"level": min(int(lvl), 3), "title": title.strip(),
                        "page": max(int(page), 1) - 1})
    except Exception:  # noqa: BLE001, S110 —— 书签解析失败由字号树兜底
        pass
    if toc:
        return toc
    sizes: list[float] = []
    for pageno, blocks in blocks_by_page.items():
        for b in blocks:
            for line in b.get("lines", []):
                for span in line["spans"]:
                    if len(span["text"].strip()) > 2:
                        sizes.append(round(span["size"], 1))
    if not sizes:
        return []
    sizes.sort(reverse=True)
    big = sizes[max(int(len(sizes) * 0.03), 0)]     # 前 3% 字号 → level1
    mid = sizes[max(int(len(sizes) * 0.10), 1)]     # 前 10% → level2
    small = sizes[max(int(len(sizes) * 0.22), 2)]   # 前 22% → level3（细粒度 P1：三级标题树）
    seen: set[str] = set()
    for pageno in sorted(blocks_by_page):
        for b in blocks_by_page[pageno]:
            bsize = max((s["size"] for ln in b.get("lines", []) for s in ln["spans"]), default=0)
            text = b.get("text", "").strip()
            if not text or len(text) > 120:
                continue
            lvl = 1 if bsize >= big else 2 if bsize >= mid else 3 if bsize >= small else 0
            if lvl and _norm_title(text) not in seen and not text.endswith((".", "。", ";", "；")):
                seen.add(_norm_title(text))
                toc.append({"level": lvl, "title": text, "page": pageno})
    return toc


# ---------- References 解析 ----------

def _authoryear_cuts(seg: str) -> list[int]:
    """段内"作者-年份头"位置（author-year 风格新条目候选起点）。

    用于字母键被 MinerU LaTeX 化后整节粘连的形态（Scaling Laws 实证）：
    字面 `[ABC+18]` 键 + "Name Name… 2018." 作者头 + LaTeX 化键闭合 `]` 后跟
    人名 三通道定位；venue 词开头/超长人名串一律不切；作者头前置必须为句点/`]`
    （上一条目结束符）或段首——人名串跨行断开（"Jakob\nUszkoreit"）经空白折叠后
    与句中无异，空格前置放行会把单条目从人名中间劈开（BERT 实证 73>58 过切根因）。
    LaTeX 化键形态（ACM 风格，年份在键内，正文无 author-year 头）：键被 MinerU
    转成 `\\mathrm{HCC^{+}}1 8`，仅 `]` 后跟大写人名可定位条目起点。"""
    cuts = {m.start() for m in _REF_ALPHA_KEY.finditer(seg)}
    literal_ends = {m.end() for m in _REF_ALPHA_KEY.finditer(seg)}
    for m in _AUTHOR_YEAR_HEAD.finditer(seg):
        name = m.group("name").strip()
        first = re.split(r"[\s,]+", name)[0].lower()
        if first in _VENUE_OPENERS or len(name) > 90:
            continue
        if m.start() > 0 and seg[m.start() - 1] not in ".]":
            continue
        cuts.add(m.start())
    for m in re.finditer(r"\](?=[A-Z][a-z])", seg):
        j = m.end()
        if j in literal_ends:      # 字面键自己的 `]`：交给键起点切分，避免键被劈成孤儿段
            continue
        if ", " in seg[j:j + 70] and "." in seg[j:j + 120]:
            cuts.add(j)
    # Round5 第四通道：**年份在尾**的 author-year 形态（ICLR/CoRR 列表式，Reformer
    # 实证）——条目以 "标题. venue, (19|20)xx. URL http…." 结束，下一条以
    # "First Last, First Last, …" 大写人名串开头。上一通道要求人名后跟年份，此形态
    # 年份在条目尾必漏切（Reformer 24/≈50）。
    # 形态签名门：段内含 "URL http"/"CoRR, abs/" 才启用（该样式每条 arXiv 条目带
    # URL 行）——教材/会议风格（UDL 短条目、数字编号论文的无编号残段）不含此签名，
    # 首轮无门实测 UDL 245→111、Attention/PreLN 副本 +25~+54 过切。
    # 其余守卫：头两词均首字母大写（人名串形态）；首词非 venue/句首停用词；60 字符内
    # 有作者逗号；与上一保留切点间距 ≥120（条目长度下限）。
    if re.search(r"URL\s*https?://|CoRR,\s*abs/", seg) or re.search(
            r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)\.\s+[A-Z][a-z]+,\s*[A-Z]\.", seg):
        # 形态签名门（满足其一）：URL/CoRR 列表式（Reformer），或 APA 书目式
        # "(2016b). Surname, I." —— UDL 教材书目（Round5：旧存储的 245 条实为
        # 序言散文假阳性，真 Bibliography 区域切分后仅 59 条即缺此头型）
        _HEAD_WORDS = {"the", "a", "an", "we", "our", "this", "these", "it", "for", "as",
                       "at", "to", "of", "on", "in", "see", "note", "table", "figure"}
        cand: list[int] = []
        for m in re.finditer(r"(?:19|20)\d{2}[a-z]?[\).]?\.\s*"
                             r"(?:(?:URL|https?://|doi:|arxiv)[:\s]?\S+\s*)?\s*", seg):
            j = m.end()
            head = seg[j:j + 60]
            # 头型二选一：Given Family, Given …（Cap cap）或 Surname, I.（APA）
            if not (re.match(r"[A-Z][a-z][\w\-'’]*\s+[A-Z][a-z][\w\-'’]*", head)
                    or re.match(r"[A-Z][a-z][\w\-'’]+,\s*[A-Z]\.\s", head)):
                continue
            first = re.split(r"[\s,]+", head)[0].lower()
            if first in _VENUE_OPENERS or first in _HEAD_WORDS:
                continue
            if ", " not in head:
                continue
            cand.append(j)
        last_kept = -10_000
        for j in sorted(cand):
            if j - last_kept < 120:
                continue
            cuts.add(j)
            last_kept = j
    return sorted(c for c in cuts if c > 0)


def _split_ref_items(ref_text: str) -> list[tuple[int, str]]:
    """条目切分：`[n]` 标记（行首或行内皆可）即新条目起点，编号须递增。
    编号回退（如 "[5] A cited [3] B" 中的 `[3]`）视为条目内引用文本并入当前条目。
    无 `[n]` 编号（author-year/字母键风格）走块边界 + 作者-年份头切分兜底。"""
    parts = _REF_MARKER.split(ref_text)
    items: list[tuple[int, str]] = []
    for i in range(1, len(parts) - 1, 2):
        idx, seg = int(parts[i]), re.sub(r"\s+", " ", parts[i + 1]).strip()
        if not seg:
            continue
        if items and idx <= items[-1][0]:
            prev_idx, prev = items[-1]
            items[-1] = (prev_idx, f"{prev} [{idx}] {seg}".strip())
            continue
        items.append((idx, seg))
    if not items:
        # author-year 风格：块边界（空行）切段 → ① 续段（小写/"and"/短块）并回
        # 上一条——先合并再过滤，否则 <30 字符的尾段碎片会整段丢失（UDL 实证）；
        # ② 段内作者-年份头再切分（LaTeX 化字母键粘连形态）
        segs: list[str] = []
        for s in (re.sub(r"\s+", " ", seg.strip())
                  for seg in re.split(r"\n\s*\n", ref_text)):
            if not s:
                continue
            if segs and (s[:1].islower() or s[:4].lower() == "and " or len(s) < 25):
                segs[-1] += " " + s
            else:
                segs.append(s)
        pieces: list[str] = []
        for s in segs:
            cuts = [0] + _authoryear_cuts(s)
            for a, b in zip(cuts, cuts[1:] + [len(s)]):
                piece = s[a:b].strip(" ,;")
                if len(piece) >= 25:
                    pieces.append(piece)
        items = list(enumerate(pieces, 1))
    return items


def _strip_meta_tail(text: str) -> str:
    """条目尾段清洗：去 arXiv 标识/DOI/年份等元数据残片，剩载体名。"""
    text = re.sub(r"arxiv[:\s]*[\dxv\.\-]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"10\.\d{4,9}/[\w.\-/:]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\(?((19|20)\d{2})\)?", " ", text)
    return re.sub(r"\s+", " ", text).strip(" ,.;:")


def _ref_block_texts(blocks: list[dict], start: int) -> list[str]:
    """References 标题后的正文块文本序列（三引擎共享）。

    Phase 0 关键点：① 块间由调用方以空行拼接——author-year 风格（BERT/ACL 系）
    条目无 `[n]` 编号，一个段落块即一条条目，块边界=条目边界，旧 `"\n"` 拼接
    使空行兜底失效是"整节 1 条巨块"的另一半根因；② 到 Appendix 边界截断，
    防附录正文混入条目；③ 跳过空块（页眉脚残片）。"""
    out: list[str] = []
    for b in blocks[start + 1:]:
        t = (b.get("text") or "").strip()
        if b.get("type") == "heading" and (_APPENDIX_HEAD.match(t) or _BACKMATTER_HEAD.match(t)):
            break
        if t:
            out.append(t)
    return out


def _parse_references(ref_text: str) -> list[dict]:
    """References 切分 + 字段抽取。

    Phase 0 升级：① `[n]` 任意位置切分（合并巨块根因修复）；② 单条 > 3×中位长
    打 anomaly 标记（脏块可被质量指标暴露，不再假阳性满分）；③ 抽 authors
    （title 前段作者跨度）/venue（尾段载体名清洗）/title_norm（归一标题，供
    图谱匹配与写作 References 白名单去重）。year 沿用原年份正则。
    """
    items = _split_ref_items(ref_text)
    lengths = sorted(len(s) for _, s in items)
    med = lengths[len(lengths) // 2] if lengths else 0

    out = []
    for idx, raw in items:
        raw = re.sub(r"\s+", " ", raw).strip()
        m = _ARXIV_ID.search(raw)
        doi = _DOI.search(raw)
        year_m = re.search(r"(19|20)\d{2}", raw)
        # 标题稳健化（P0）：". " 切段后长度<4 的碎段（缩写名 "Y."/"Le" 类）并回前段，
        # 去掉首段（作者）与尾段（年份/载体）后取最长中段；不足 8 字回退 raw 前 120 字
        segs = [s.strip() for s in raw.split(". ") if s.strip()]
        merged: list[str] = []
        for s in segs:
            if len(s) < 4 and merged:
                merged[-1] += ". " + s
            else:
                merged.append(s)
        title = ""
        if len(merged) >= 3:
            title = max(merged[1:-1], key=len)
        elif len(merged) == 2:
            title = merged[1]
        if len(title) < 8:
            title = raw[:120]
        # 作者 = title 前段（学术引用惯例作者在首位）；超长截断标注 et al.
        authors = merged[0].strip(" ,;") if len(merged) >= 2 else ""
        if len(authors) > 180:
            authors = authors[:180].rstrip(" ,;") + " et al."
        # 载体 = 尾段去元数据后（期刊/会议名线索）；清洗后仍超长视为未抽出
        venue = _strip_meta_tail(merged[-1]) if len(merged) >= 3 else ""
        if not (8 <= len(venue) <= 120):
            venue = ""
        title_norm = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", title.lower())).strip()
        out.append({"index": idx, "raw": raw[:500],
                    "title": title[:200],
                    "title_norm": title_norm[:200],
                    "authors": authors[:200],
                    "venue": venue,
                    "arxiv_id": (m.group(1) + (m.group(2) or "")) if m else "",
                    "doi": doi.group(0) if doi else "",
                    "year": int(year_m.group(0)) if year_m else 0,
                    "anomaly": bool(len(raw) > max(_REF_ANOMALY_RATIO * med, 400))})
    return out


# ---------- 主流程 ----------

def parse_pdf(pdf_path: str | Path, progress=None,
              pages: tuple[int, int] | None = None,
              cancel_check: CancelCheck | None = None) -> dict:
    """PDF → DocIR JSON。progress: 可选回调 (frac 0~1, msg)。

    pages: (start, end) 0 基页码范围（长文档分批解析用）；None = 全文。
    块的 page/bbox 始终为文档绝对坐标。
    cancel_check: 协作式取消，逐页边界轮询，命中抛 ParseCancelled。
    """
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)
    p_start, p_end = (pages or (0, doc.page_count))
    p_end = min(p_end, doc.page_count)

    def report(frac: float, msg: str) -> None:
        if progress:
            progress(frac, msg)

    if not _has_text_layer(doc):
        report(0.0, "no_text_layer")   # 扫描件标记：后续由 Qwen-VL 困难路径接管（MVP 暂降级提示）

    meta: dict[str, Any] = {}
    blocks: list[dict] = []
    tables: list[dict] = []
    figures: list[dict] = []
    blocks_by_page: dict[int, list[dict]] = {}
    demo_raw_x0: list[float] = []   # 第 2 页原始块序（4.3 阅读顺序红绿开关素材）
    raw_orders: dict[str, list[int]] = {}   # 模块三 E：按页重排置换（仅记发生重排的页）

    n = doc.page_count
    bid = 0
    for pageno in range(p_start, p_end):
        if cancel_check is not None and cancel_check():
            raise ParseCancelled(f"PyMuPDF 解析已取消（第 {pageno} 页边界）")
        page = doc[pageno]
        pw, ph = page.rect.width, page.rect.height
        raw = page.get_text("dict")
        pblocks = _pymupdf_page_blocks(page)
        if pageno == 1:   # demo 素材（4.3 阅读顺序红绿开关）：第 2 页重排前的原始 x0 序
            demo_raw_x0 = [b["bbox"][0] for b in pblocks]
        reordered = _reading_order(pblocks)
        # 模块三 E：记录真实重排置换——raw_orders["<页>"][k] = 原始序第 k 块在重排后
        # 数组中的索引；仅记 perm ≠ 恒等（真正发生重排）的页，前端据此还原「原始序
        # （解析记录）」，无记录页由前端 x0 启发模拟并如实标「模拟」
        perm = _raw_order_perm(pblocks, reordered)
        if perm is not None:
            raw_orders[str(pageno)] = perm
        pblocks = reordered
        blocks_by_page[pageno] = pblocks

        for b in pblocks:
            bid += 1
            # type 统一由 backfill_sections 归一化回填（旧行内 heading 集合推导是恒空死代码）
            blocks.append({"id": f"b{bid}", "type": "paragraph", "text": b["text"],
                           "latex": None, "page": pageno, "bbox": b["bbox"],
                           "section_path": ""})

        # 表格：GFM markdown + headers/rows 拆分（P1：三线表渲染需要结构化字段）
        try:
            for t in (page.find_tables() or []):
                cells = t.extract()
                md = _table_to_md(cells)
                if not md:
                    continue
                entry: dict = {"id": f"t{len(tables)+1}", "page": pageno,
                               "bbox": [round(v / pw, 4) if i % 2 == 0 else round(v / ph, 4)
                                        for i, v in enumerate(t.bbox)],
                               "markdown": md}
                try:   # 表头识别：header.external=False 时 extract() 首行即表头
                    hdr = t.header
                    names = list(getattr(hdr, "names", None) or [])
                    if names and not getattr(hdr, "external", False):
                        entry["headers"] = [str(h).strip() if h is not None else ""
                                            for h in names]
                        entry["rows"] = [["" if c is None else str(c).strip() for c in r]
                                         for r in cells[1:]]
                except Exception:  # noqa: BLE001, S110 —— 表头探测失败仅少结构化字段
                    pass
                # 表标题/编号（细粒度 P1）：表上方 0.06 页高内含 "Table N/表 N" 的最近块
                for pb in pblocks:
                    gap = (t.bbox[1] / ph) - pb["bbox"][3]
                    mcap = _TABLE_CAPTION.search(pb["text"])
                    if 0 <= gap < 0.06 and mcap:
                        entry["title"] = pb["text"][:200]
                        entry["number"] = int(mcap.group(1))
                        break
                tables.append(entry)
        except Exception:  # noqa: BLE001, S110 —— 单表 HTML 解析失败跳过该表
            pass

        # 图片 + caption 关联（图块下方最近文本块）；P1：裁剪图落盘（快速通道也有真图）
        for blk in raw.get("blocks", []):
            if blk["type"] != 1:
                continue
            x0, y0, x1, y1 = blk["bbox"]
            if (x1 - x0) * (y1 - y0) < pw * ph * 0.005:
                continue
            cap = ""
            for b in pblocks:
                if b["bbox"][1] >= y1 / ph and b["bbox"][1] - y1 / ph < 0.06:
                    cap = b["text"][:200]
                    break
            entry = {"id": f"f{len(figures)+1}", "page": pageno, "caption": cap,
                     "bbox": [round(x0 / pw, 4), round(y0 / ph, 4),
                              round(x1 / pw, 4), round(y1 / ph, 4)]}
            try:   # 裁剪渲染（150 DPI，与 VLM 通道一致）；落 data/figures（figure 端点可达）
                fig_dir = _figures_dir()
                fname = f"{pdf_path.stem}_p{pageno}_{entry['id']}.png"
                fpath = fig_dir / fname
                if not fpath.exists():   # 重解析幂等：同名覆盖语义，存在即跳过
                    page.get_pixmap(clip=fitz.Rect(x0, y0, x1, y1),
                                    dpi=150).save(str(fpath))
                entry["image_path"] = str(fpath)
            except Exception:  # noqa: BLE001, S110 —— 裁图失败保留 caption（不阻断）
                pass
            figures.append(entry)
        span = max(p_end - p_start, 1)
        report(0.05 + 0.75 * (pageno + 1 - p_start) / span, f"解析第 {pageno+1}/{n} 页")

    toc = _build_toc(doc, blocks_by_page)
    backfill_sections(blocks, toc, blocks_by_page)

    meta = _extract_meta(doc, blocks, blocks_by_page.get(0, []))
    _mark_appendix(toc, meta, blocks)
    if demo_raw_x0:
        meta["demo_raw_order"] = {"page": 2, "x0_order": demo_raw_x0}
    if raw_orders:
        meta["raw_orders"] = raw_orders
    _merge_cross_page_tables(tables)
    ri = _find_refs_start(blocks)
    docir: dict[str, Any] = {
        "paper_id": meta.get("arxiv_id") or pdf_path.stem,
        "meta": {**meta, "page_count": n, "file": pdf_path.name},
        "toc": toc,
        "blocks": blocks,
        "tables": tables,
        "figures": figures,
        "references": _parse_references(
            "\n\n".join(_ref_block_texts(blocks, ri))) if ri >= 0 else [],
    }
    report(0.85, f"解析完成：{len(blocks)} blocks / {len(tables)} tables / {len(figures)} figures")
    return docir


def _page_y_of_title(blocks_by_page: dict[int, list[dict]], title: str, page: int) -> float | None:
    """标题块在页内的 y 坐标（归一化 + 剥编号降级匹配）；未命中返回 None——调用方
    不得当 0 处理（旧实现返回 0.0 使推进条件恒真，整页被标成页末章节，v7 实证根因）。"""
    for b in blocks_by_page.get(page, []):
        if _title_match(b.get("text", ""), title):
            return b["bbox"][1]
    return None


def _ensure_toc_ids(toc: list[dict]) -> None:
    """toc 稳定 id（s1…）与 parent_id（level 栈推导）：三引擎共享，幂等。"""
    stack: list[str] = []
    for i, t in enumerate(toc):
        if not t.get("id"):
            t["id"] = f"s{i + 1}"
        lvl = max(1, min(int(t.get("level", 1)), 6))
        del stack[lvl - 1:]
        t["parent_id"] = stack[-1] if stack else ""
        stack.append(t["id"])


def backfill_sections(blocks: list[dict], toc: list[dict],
                      blocks_by_page: dict[int, list[dict]]) -> None:
    """按 toc（页面+标题序）为每个 block 推断所属章节（各解析引擎共用）。

    同页推进必须以"标题块实际命中 y"为准——未命中不推进（宁可保持上一节，
    不可猛进到页末章节）。顺带：type=heading 归一化回填、section_id 落块。
    """
    _ensure_toc_ids(toc)
    sections = sorted(toc, key=lambda t: (t["page"], -t.get("level", 1)))
    si = -1
    for blk in blocks:
        while si + 1 < len(sections) and (
            sections[si + 1]["page"] < blk["page"]
            or (sections[si + 1]["page"] == blk["page"]
                and (y := _page_y_of_title(blocks_by_page, sections[si + 1]["title"],
                                           blk["page"])) is not None
                and y <= blk["bbox"][1])
        ):
            si += 1
        if si >= 0:
            sec = sections[si]
            if _title_match(blk.get("text", ""), sec["title"]):
                blk["type"] = "heading"
            blk["section_path"] = sec["title"]
            blk["section_id"] = sec.get("id", "")


def _find_refs_start(blocks: list[dict]) -> int:
    """定位参考文献起始块下标；无参考文献返回 -1。

    候选 = References/Bibliography/参考文献 类独立标题块（块首行匹配）；
    后验密度校验 = 其后 3 块中 ≥2 块以 [n] 开头；无后验通过时取最后候选
    （宁多勿漏，解析器自带兜底）。替代旧 `pageno > n*0.5`——2 页短论文不再漏判。
    """
    cands = [i for i, b in enumerate(blocks)
             if _REF_HEAD.match(b.get("text", "").split("\n", 1)[0].strip())]
    if not cands:
        return -1
    best = -1
    for i in cands:
        nxt = [blocks[j].get("text", "") for j in range(i + 1, min(i + 4, len(blocks)))]
        if sum(1 for t in nxt if _REF_ITEM_LINE.match(t.strip())) >= 2:
            best = i
    return best if best >= 0 else cands[-1]


def _merge_cross_page_tables(tables: list[dict]) -> None:
    """跨页续表合并（细粒度 P2）：相邻页 + 同列数 + 归一表头相同 + 前表近页底/
    后表近页顶，四条件才合并（续页重复表头丢弃）；就地修改并重排 id。"""
    i = 0
    while i < len(tables) - 1:
        a, b = tables[i], tables[i + 1]
        same_shape = (a.get("headers") and b.get("headers")
                      and len(a["headers"]) == len(b["headers"])
                      and [_norm_title(x) for x in a["headers"]] == [_norm_title(x) for x in b["headers"]])
        contiguous = b["page"] == a["page"] + 1
        positional = bool(a.get("bbox")) and bool(b.get("bbox")) \
            and a["bbox"][3] > 0.85 and b["bbox"][1] < 0.15
        if same_shape and contiguous and positional:
            # 续页首行若仍是表头（归一相同）则丢弃
            b_rows = b.get("rows", [])
            if b_rows and [_norm_title(x) for x in b_rows[0]] == [_norm_title(x) for x in a["headers"]]:
                b_rows = b_rows[1:]
            a["rows"] = a.get("rows", []) + b_rows
            if a.get("headers"):
                a["markdown"] = _table_to_md([a["headers"]] + a["rows"])
            tables.pop(i + 1)
            continue   # 第三页可能仍是续表
        i += 1
    for k, t in enumerate(tables, 1):
        t["id"] = f"t{k}"


def page_char_counts(doc: fitz.Document) -> list[int]:
    """每页文本字符数（VLM 逐页救援的空白页探测用）。"""
    return [len(p.get_text("text").strip()) for p in doc]


def _figures_dir():
    """裁剪图落盘目录 data/figures（figure 端点守卫：必须在数据目录内）。"""
    from ..config import DOCIR_DIR

    d = DOCIR_DIR.parent / "figures"
    d.mkdir(parents=True, exist_ok=True)
    return d


_ARXIV_BANNER = re.compile(r"arxiv:\d{4}\.\d{4,5}", re.IGNORECASE)


def _extract_meta(doc: fitz.Document, blocks: list[dict], first_page_internal: list[dict]) -> dict:
    # arXiv id 可能在任意块（含被排到尾部的侧边横幅），全首页搜
    text = " ".join(b["text"] for b in blocks[:10]) + " " + \
           " ".join(b["text"] for b in first_page_internal)
    m = _ARXIV_ID.search(text)
    # 标题 = 首页上部（y<0.45）字号最大的非横幅块
    cands = [b for b in first_page_internal
             if b["bbox"][1] < 0.45 and not _ARXIV_BANNER.search(b["text"])
             and (b["bbox"][2] - b["bbox"][0]) > 0.15]
    title = ""
    if cands:
        best = max(cands, key=lambda b: b["size"])
        t = re.sub(r"\s+", " ", best["text"]).strip()
        if 8 <= len(t) <= 200 and not t[0].islower() and t.count("@") < 2:
            title = t
    authors = _author_candidates(first_page_internal)
    return {"title": title or (doc.metadata.get("title") or ""),
            "authors": authors, "arxiv_id": m.group(1) if m else "",
            "pub_date": "", "venue": ""}


def _author_candidates(first_page_internal: list[dict]) -> list[dict]:
    """作者启发式（细粒度 P1，离线兜底）：标题块与 Abstract/Introduction 之间的
    姓名行（逗号/and 分隔、无数字、无邮箱）。精度有限——arXiv/LLM 权威层存在时
    会被 _merge_authors 整体覆盖，仅作离线预填。"""
    if not first_page_internal:
        return []
    cands = [b for b in first_page_internal
             if b["bbox"][1] < 0.45 and not _ARXIV_BANNER.search(b["text"])
             and (b["bbox"][2] - b["bbox"][0]) > 0.15]
    title_y = max(cands, key=lambda b: b["size"])["bbox"][1] if cands else 0.0
    stop_y = None
    for b in first_page_internal:
        if b["bbox"][1] > title_y and \
                re.match(r"^(abstract|摘要|introduction)", _norm_title(b["text"][:40])):
            stop_y = b["bbox"][1]
            break
    out: list[dict] = []
    for b in first_page_internal:
        y = b["bbox"][1]
        if y <= title_y or y >= (stop_y if stop_y is not None else title_y + 0.22):
            continue
        line = re.sub(r"\s+", " ", b["text"]).strip()
        if not line or len(line) > 200 or "@" in line or line.count(",") > 8:
            continue
        names = [n.strip(" *†‡§¶•·-–—.\u2217\u204e") for n in re.split(r",|;| and ", line) if n.strip()]
        names = [n for n in names if 1 <= len(n.split()) <= 5 and not re.search(r"\d", n)]
        for n in names:
            out.append({"name": n.title() if n.islower() else n, "affiliation": ""})
        if len(out) >= 20:
            break
    return out
