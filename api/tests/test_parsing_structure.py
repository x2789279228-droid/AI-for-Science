"""文本细粒度结构修复回归（P0-1/2/4/5/7 + P1-8/9/10/12 + P2-13/15）。

覆盖此前零测试的纯函数：backfill_sections / _find_refs_start / _parse_references /
_mark_appendix / quality.compute / heuristic_abstract / _build_toc / 跨页表合并 /
html colspan-rowspan / Crossref 兜底 / 标题 stem 回填。
"""
from __future__ import annotations

import uuid

from app import db
from app.parsing import pipeline, quality
from app.parsing.tables import html_table_to_rows
from app.rag import metadata


def _blk(i: int, text: str, page: int, y: float) -> dict:
    return {"id": f"b{i}", "type": "paragraph", "text": text, "latex": None,
            "page": page, "bbox": [0.1, y, 0.9, y + 0.05], "section_path": ""}


# ---------- P0-1：章节归属 + heading 分类 ----------

def test_backfill_not_runaway_on_title_miss():
    """标题块文本与 toc 归一化不同（双空格）→ 未命中不得猛进到页末章节（v7 错配回归形态）。"""
    blocks = [
        _blk(1, "Title of the Paper", 0, 0.05),
        _blk(2, "1  Introduction", 0, 0.30),        # 双空格：旧精确匹配必失配
        _blk(3, "Intro paragraph one.", 0, 0.40),
        _blk(4, "Intro paragraph two.", 0, 0.50),
    ]
    blocks_by_page = {0: blocks}
    toc = [
        {"level": 1, "title": "1 Introduction", "page": 0},
        {"level": 1, "title": "2 Model Architecture", "page": 0},   # 页内无对应块
    ]
    pipeline.backfill_sections(blocks, toc, blocks_by_page)
    paths = [b["section_path"] for b in blocks]
    assert "2 Model Architecture" not in paths, "未命中标题不得触发猛进"
    assert paths[2] == "1 Introduction" and paths[3] == "1 Introduction"
    assert blocks[1]["type"] == "heading", "归一化命中必须回填 heading"
    assert all(b.get("section_id") for b in blocks[2:]), "section_id 落块"


def test_backfill_numbered_block_vs_unnumbered_toc():
    """v7 实证形态：字号树 toc 无节编号（"Introduction"），块文本带编号
    （"1 Introduction"）——剥编号降级匹配必须命中，否则 heading/章节整页错配。"""
    blocks = [
        _blk(1, "Title of the Paper", 0, 0.05),
        _blk(2, "1 Introduction", 0, 0.30),
        _blk(3, "Intro paragraph.", 0, 0.45),
        _blk(4, "2 Model Architecture", 0, 0.70),
    ]
    toc = [
        {"level": 1, "title": "Introduction", "page": 0},
        {"level": 1, "title": "Model Architecture", "page": 0},
    ]
    pipeline.backfill_sections(blocks, toc, {0: blocks})
    assert blocks[1]["type"] == "heading" and blocks[3]["type"] == "heading"
    assert blocks[2]["section_path"] == "Introduction"
    assert blocks[3]["section_path"] == "Model Architecture"


def test_backfill_toc_ids_parent_chain():
    toc = [
        {"level": 1, "title": "Ch1", "page": 0},
        {"level": 2, "title": "Ch1.1", "page": 1},
        {"level": 3, "title": "Ch1.1.1", "page": 2},
        {"level": 2, "title": "Ch1.2", "page": 3},
    ]
    pipeline.backfill_sections([], toc, {})
    assert [t["id"] for t in toc] == ["s1", "s2", "s3", "s4"]
    assert toc[1]["parent_id"] == "s1" and toc[2]["parent_id"] == "s2"
    assert toc[3]["parent_id"] == "s1" and toc[0]["parent_id"] == ""


def test_build_toc_font_tree_three_levels():
    """字号分位三档：level3 档生效（书签路径不受影响）。
    span 占比按真实文档形态构造（6/10/8/60），使 3%/10%/22% 分位点分别落在
    三档标题字号上；body 块文本 >120 字符被过滤。"""

    def _one(text: str, size: float, n_lines: int) -> dict:
        return {"bbox": [0.1, 0.0, 0.9, 0.05],
                "lines": [{"spans": [{"size": size, "text": text}]} for _ in range(n_lines)],
                "text": text, "size": size}

    blocks_by_page = {0: [
        _one("Big Title", 24.0, 6),
        _one("Mid Head", 17.0, 10),
        _one("Small Head", 14.0, 8),
        _one("Body sentence.", 9.0, 60),
    ]}
    toc = pipeline._build_toc(None, blocks_by_page)
    levels = {t["title"]: t["level"] for t in toc}
    assert levels.get("Big Title") == 1 and levels.get("Mid Head") == 2
    assert levels.get("Small Head") == 3, "第三档字号必须产出 level3"


# ---------- P0-4：参考文献链 ----------

def test_find_refs_start_short_paper_no_halfpage_gate():
    """2 页短论文：References 在第 2 块——旧 pageno>n/0.5 约束必漏判，现应命中。"""
    blocks = [
        _blk(1, "Short Paper Title", 0, 0.05),
        _blk(2, "Body text about methods and experiments.", 0, 0.30),
        _blk(3, "References", 1, 0.10),
        _blk(4, "[1] Someone. 2020. A title.", 1, 0.20),
        _blk(5, "[2] Other. 2021. Another title.", 1, 0.40),
    ]
    assert pipeline._find_refs_start(blocks) == 2


def test_find_refs_start_chinese_heading_and_miss():
    blocks_cn = [
        _blk(1, "标题", 0, 0.05),
        _blk(2, "参考文献", 1, 0.10),
        _blk(3, "[1] 某作者. 2020. 某标题.", 1, 0.20),
        _blk(4, "[2] 另一作者. 2021. 另一标题.", 1, 0.40),
    ]
    assert pipeline._find_refs_start(blocks_cn) == 1, "词表必须覆盖「参考文献」"
    assert pipeline._find_refs_start([_blk(1, "No refs here", 0, 0.1)]) == -1


def test_parse_references_robust_title():
    refs = pipeline._parse_references(
        "[1] Ashish Vaswani, Noam Shazeer, Y. Le Cun. Learning Phrase Representations "
        "using RNN Encoder-Decoder for Statistical Machine Translation. arXiv preprint "
        "arXiv:1406.1078 (2014).\n"
        "[2] Someone Else. 2021. A short note.\n"
        "[3] Folded author line,\ncontinues here. The Actual Title of the Paper. Journal, 2019.")
    r1 = refs[0]
    assert r1["title"] != "Le" and len(r1["title"]) >= 8, "缩写名碎段不得污染 title"
    assert "Learning Phrase" in r1["title"]
    assert r1["year"] == 2014, "year 字段从条目抽取"
    assert refs[1]["title"], "短条目有 raw 兜底 title"
    r3 = refs[2]
    assert "Folded author line, continues here" in r3["raw"] and r3["year"] == 2019


# ---------- Phase 0：references 合并巨块修复 ----------

def test_parse_references_inline_markers_split():
    """整节被并成单块（MinerU 常见形态）：行内 `[n]` 也必须切分——BERT/GPT-2
    references=1 脏巨块的根因回归。"""
    merged_block = (
        "[1] Ashish Vaswani, Noam Shazeer. Attention Is All You Need. NeurIPS, 2017. "
        "[2] Jacob Devlin, Ming-Wei Chang. BERT: Pre-training of Deep Bidirectional "
        "Transformers for Language Understanding. NAACL, 2019. "
        "[3] Alec Radford, Karthik Narasimhan. Improving Language Understanding by "
        "Generative Pre-Training. OpenAI, 2018.")
    refs = pipeline._parse_references(merged_block)
    assert [r["index"] for r in refs] == [1, 2, 3], "行内标记必须逐条切开，不得折进第 1 条"
    assert all(len(r["raw"]) < 150 for r in refs), "单条不得残留其余条目内容"
    assert refs[0]["year"] == 2017 and refs[1]["year"] == 2019
    assert "Attention Is All You Need" in refs[0]["title"]


def test_parse_references_inline_citation_not_split():
    """编号回退（条目内引用 "[5] A cites [3] B"）：`[3]` 是引用文本不是新条目。"""
    refs = pipeline._parse_references(
        "[1] First Author. A Great Title. Journal of Things, 2020.\n"
        "[5] Second Author. Another Nice Title with substance. In [3] the method "
        "was first proposed. Conference, 2021.")
    assert len(refs) == 2, "回退编号并入当前条目，不新开"
    assert refs[1]["index"] == 5
    assert "In [3] the method" in refs[1]["raw"]


def test_parse_references_rich_fields():
    """authors/venue/title_norm 抽取 + anomaly 脏块暴露。"""
    text = (
        "[1] Ashish Vaswani, Noam Shazeer, Niki Parmar. Attention Is All You Need. "
        "Advances in Neural Information Processing Systems, 2017.\n"
        "[2] B Author. Short Title Here. Some Venue Name, 2019.")
    refs = pipeline._parse_references(text)
    r1 = refs[0]
    assert "Vaswani" in r1["authors"], "作者 = title 前段"
    assert "Neural Information Processing Systems" in r1["venue"], "载体 = 尾段清洗后"
    assert r1["title_norm"] == "attention is all you need"
    assert r1.get("anomaly") is False
    # 单条超长（>3×中位）打 anomaly 标记
    text2 = text + "\n[3] C Author. Fine Title. Venue, 2018. " + "filler. " * 200
    refs2 = pipeline._parse_references(text2)
    assert refs2[2].get("anomaly") is True, "巨块条目必须可被质量指标暴露"


def test_quality_ref_consistency_exposes_folding():
    """条数一致性以正文引用标记为锚：正文引到 [5] 但只解析出 1 条 → 0.2，不再满分。"""
    one_giant = [{"index": 1, "title": "x" * 50, "raw": "", "anomaly": True}]
    body_cites = _blk(1, "As shown in [3] and [5], the method works.", 0, 0.1)
    docir = {"meta": {"page_count": 2}, "toc": [], "blocks": [body_cites],
             "tables": [], "figures": [], "references": one_giant}
    q1 = quality.compute(docir)
    assert q1["ref_count_consistency"] == 0.2 and q1["ref_anomaly"] == 1
    docir["references"] = [{"index": i, "title": f"title number {i} here", "raw": "",
                            "anomaly": False} for i in (1, 2, 3, 4, 5)]
    q2 = quality.compute(docir)
    assert q2["ref_count_consistency"] == 1.0 and q2["ref_anomaly"] == 0


# ---------- P0-5：附录 ----------

def test_mark_appendix_prefix_and_blocks():
    toc = [{"level": 1, "title": "A Appendix", "page": 3},
           {"level": 1, "title": "B Extra", "page": 5}]
    meta: dict = {}
    blocks = [_blk(1, "body", 1, 0.3), _blk(2, "app text", 3, 0.5), _blk(3, "app text 2", 4, 0.3)]
    pipeline._mark_appendix(toc, meta, blocks)
    assert meta["appendix"]["start_page"] == 3
    assert all(t.get("appendix") for t in toc), "前缀编号（A Appendix）必须命中"
    assert blocks[1].get("appendix") is True and blocks[2].get("appendix") is True
    assert not blocks[0].get("appendix")
    meta2: dict = {}
    toc2 = [{"level": 1, "title": "Just Chapter", "page": 0}]
    pipeline._mark_appendix(toc2, meta2)
    assert "appendix" not in meta2


# ---------- P0-7：质量指标正确性 ----------

def test_quality_reflects_structure_correctness():
    toc = [{"level": 1, "title": "1 Introduction", "page": 0},
           {"level": 1, "title": "2 Model", "page": 1}]
    blocks_bad = [
        {**_blk(1, "1 Introduction", 0, 0.3), "section_path": "2 Model"},
        {**_blk(2, "intro text", 0, 0.5), "section_path": "2 Model"},
    ]
    q_bad = quality.compute({"meta": {"title": "p_abc123def", "page_count": 2},
                             "toc": toc, "blocks": blocks_bad,
                             "tables": [], "figures": [], "references": []}, None)
    assert q_bad["title_ok"] is False, "stem 退化标题必须被判 False"
    assert q_bad["heading_rate"] == 0.5, "缺失「2 Model」标题块 → 命中率如实 0.5"
    blocks_good = [
        {**_blk(1, "1 Introduction", 0, 0.3), "section_path": "1 Introduction"},
        {**_blk(2, "2 Model", 1, 0.2), "section_path": "2 Model"},
    ]
    q_good = quality.compute({"meta": {"title": "A Real Paper Title", "page_count": 2},
                              "toc": toc, "blocks": blocks_good,
                              "tables": [], "figures": [], "references": []}, None)
    assert q_good["title_ok"] is True
    assert q_good["overall"] > q_bad["overall"], "正确结构得分必须高于错配结构"


def test_quality_ref_rate_length_threshold():
    refs = [{"title": "Le"}, {"title": "A Real Title Here"}]
    q = quality.compute({"meta": {"title": "T", "page_count": 1}, "toc": [], "blocks": [],
                         "tables": [], "figures": [], "references": refs}, None)
    assert q["reference_parse_rate"] == 0.5, "长度<8 的碎段 title 不得计入解析成功"


# ---------- P1-9：heuristic_abstract ----------

def test_heuristic_abstract_emdash_inline():
    docir = {"blocks": [
        _blk(1, "Abstract—We propose the Transformer, the first sequence model based "
                "solely on attention, achieving state-of-the-art BLEU on WMT14 translation.", 0, 0.2),
        _blk(2, "1 Introduction", 0, 0.5),
    ]}
    ab = metadata.heuristic_abstract(docir)
    assert ab.startswith("We propose"), "IEEE 行内 em-dash 形式必须命中"


def test_heuristic_abstract_preface():
    docir = {"blocks": [
        {"id": "b1", "type": "paragraph", "text": "前言", "page": 0, "bbox": [0, 0.05, 1, 0.1]},
        {"id": "b2", "type": "paragraph", "text": "This book is about deep learning "
         "and its foundations, written for readers who want rigor.", "page": 0,
         "bbox": [0, 0.12, 1, 0.2]},
        {"id": "b3", "type": "paragraph", "text": "Chapter 1: Introduction to models.", "page": 0,
         "bbox": [0, 0.25, 1, 0.3]},
    ]}
    ab = metadata.heuristic_abstract(docir)
    assert "deep learning" in ab and "Chapter 1" not in ab


# ---------- P2-13：表格 colspan/rowspan + 跨页合并 ----------

def test_html_table_colspan_rowspan_grid():
    html = ("<table><tr><td>A</td><td>B</td></tr>"
            "<tr><td colspan=\"2\">Wide</td></tr>"
            "<tr><td rowspan=\"2\">Span</td><td>x1</td></tr>"
            "<tr><td>x2</td></tr></table>")
    headers, rows = html_table_to_rows(html)
    assert headers == ["A", "B"]
    assert rows[0] == ["Wide", "Wide"], "colspan 展开为占位重复"
    assert rows[1][0] == "Span" and rows[2][0] == "Span", "rowspan 后续行补占位"
    assert rows[1][1] == "x1" and rows[2][1] == "x2"


def test_merge_cross_page_tables():
    tables = [{"id": "t1", "page": 3, "bbox": [0.1, 0.3, 0.9, 0.92],
               "headers": ["Model", "BLEU"], "rows": [["A", "27.3"]]},
              {"id": "t2", "page": 4, "bbox": [0.1, 0.08, 0.9, 0.5],
               "headers": ["Model", "BLEU"], "rows": [["Model", "BLEU"], ["B", "26.1"]]}]
    pipeline._merge_cross_page_tables(tables)
    assert len(tables) == 1, "相邻页同表头近页界必须合并"
    assert tables[0]["rows"] == [["A", "27.3"], ["B", "26.1"]], "续页重复表头丢弃"
    assert tables[0]["id"] == "t1", "合并后重排 id"
    # 误合并防护：同表头但页不相邻
    far = [{"id": "t1", "page": 3, "bbox": [0.1, 0.3, 0.9, 0.92],
            "headers": ["Model", "BLEU"], "rows": [["A", "27.3"]]},
           {"id": "t2", "page": 9, "bbox": [0.1, 0.08, 0.9, 0.5],
            "headers": ["Model", "BLEU"], "rows": [["B", "26.1"]]}]
    pipeline._merge_cross_page_tables(far)
    assert len(far) == 2


# ---------- P0-3：Crossref 兜底 + 标题回填 ----------
# 注意：不得在运行时改 SCHOLAR_DATA_DIR——惰性读取该 env 的模块（如 sandbox figures
# 目录）会被重定向污染后续测试；conftest 已提供会话级密闭数据目录，直接插桩即可。

def test_crossref_lookup_and_cache(monkeypatch):
    pid = f"p_cr_{uuid.uuid4().hex[:8]}"
    with db.tx() as c:
        c.execute("INSERT INTO papers(id,title,file_path,file_hash,created_at) "
                  "VALUES(?,?,?,?,?)", (pid, "A Real Paper Title", "x.pdf",
                                        uuid.uuid4().hex, db.now()))
    calls = {"n": 0}

    class R:
        def raise_for_status(self): pass
        def json(self):
            calls["n"] += 1
            return {"message": {"items": [
                {"title": ["A Real Paper Title"], "DOI": "10.1234/real",
                 "container-title": ["Nature"], "issued": {"date-parts": [[2020]]}}]}}

    monkeypatch.setattr(metadata.httpx, "get", lambda *a, **k: R())
    res = metadata._fetch_crossref("A Real Paper Title", pid)
    assert res and res["doi"] == "10.1234/real" and res["venue"] == "Nature" and res["year"] == 2020
    metadata._fetch_crossref("A Real Paper Title", pid)
    assert calls["n"] == 1, "缓存命中不得重复外呼"
    monkeypatch.setattr(metadata.httpx, "get",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert metadata._fetch_crossref("Another Different Title", pid) is None


def test_title_backfill_from_arxiv(monkeypatch):
    pid = f"p_tb_{uuid.uuid4().hex[:8]}"
    with db.tx() as c:
        c.execute("INSERT INTO papers(id,title,file_path,file_hash,arxiv_id,created_at) "
                  "VALUES(?,?,?,?,?,?)", (pid, "p_abc123def456", "x.pdf",
                                          uuid.uuid4().hex, "1706.03762", db.now()))
    monkeypatch.setattr(metadata, "fetch_arxiv_meta", lambda base, timeout=8.0: {
        "title": "Attention Is All You Need", "authors": [], "abstract": "abs",
        "year": 2017, "doi": "", "journal_ref": "", "categories": []})
    docir = {"meta": {"title": "p_abc123def456", "arxiv_id": "1706.03762",
                      "file": "p_abc123def456.pdf"},
             "blocks": [_blk(1, "arXiv:1706.03762v7 10.1111/zz.yy", 0, 0.1)]}
    out = metadata.extract_and_store_metadata(pid, docir)
    assert out["title"] == "Attention Is All You Need", "stem 形标题必须被 arXiv title 回填"
    assert out["doi"].startswith("10."), "DOI 链（首页正则/合成/Crossref）必有产出"
    assert "arxiv_api" in out["meta_source"]
