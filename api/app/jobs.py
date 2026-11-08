"""后台任务队列（方案：解析走后台队列 + 进度条）。

MVP 用进程内线程池实现，接口与 APScheduler/Redis 队列可替换。
上传 → job(parse)：解析 DocIR → 结构化元数据 → 论文签名（向量+指纹）→
切块 → FTS 索引 → 语义去重预警（三态决策）。

取消：协作式——任务在阶段边界轮询 cancel_requested；MinerU 子进程由解析引擎
在 cancel_check 回调中 terminate（见 parsing/router.cancel_check 透传）。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil

from . import db
from .config import DOCIR_DIR, UPLOAD_DIR, settings
from .log import logger
from .parsing._types import ParseCancelled
from .parsing.router import parse_document, write_docir
from .rag import fingerprint, indexing
from .rag.chunking import chunk_docir
from .rag.metadata import extract_and_store_metadata

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")

# TOCTOU 防线一：上传插入 + 去重扫描串行化（并发上传 v7/v1 时后完成方在锁内重查必见先完成方）
_dedup_lock = threading.Lock()


def start() -> None:
    """应用启动时调用。"""
    # 重启自愈：把上次中断的 running/queued 重置
    with db.tx() as c:
        c.execute("UPDATE jobs SET status='error', message='server restarted' "
                  "WHERE status IN ('queued','running','cancel_requested')")


def submit_parse(paper_id: str) -> str:
    job_id = db.create_job("parse", paper_id=paper_id)
    _pool.submit(_run_parse, job_id, paper_id)
    return job_id


def cancel_job(job_id: str) -> dict | None:
    """请求取消：仅 queued/running 可取消（置 cancel_requested，协作式终止）。"""
    job = db.get_job(job_id)
    if not job:
        return None
    if job["status"] in ("queued", "running"):
        db.update_job(job_id, status="cancel_requested",
                      message="取消请求已受理，等待阶段边界终止…")
        return db.get_job(job_id)
    return job


def submit_version_diff(base_id: str, target_id: str) -> str:
    """DiffLens 版本差异任务（rag/version_diff.compute_diff → version_diffs 缓存）。"""
    job_id = db.create_job("version_diff", paper_id=target_id)
    _pool.submit(_run_version_diff, job_id, base_id, target_id)
    return job_id


def _run_version_diff(job_id: str, base_id: str, target_id: str) -> None:
    db.update_job(job_id, status="running", progress=0.05, message="加载两版 DocIR")
    try:
        from .parsing._types import ParseCancelled  # noqa: F401 —— 语义对齐（此任务无子进程）
        from .rag.version_diff import compute_diff, load_docir_for

        base_data = load_docir_for(base_id)
        target_data = load_docir_for(target_id)
        if not base_data or not target_data:
            missing = base_id if not base_data else target_id
            db.update_job(job_id, status="error", message=f"DocIR 不存在：{missing}")
            return
        db.update_job(job_id, progress=0.4, message="章节对齐与段落 diff")
        if _hit_cancel(job_id, "diff 对齐"):
            return
        payload = compute_diff(base_data, target_data)
        if _hit_cancel(job_id, "diff 摘要"):
            return
        db.save_version_diff(base_id, target_id, payload.get("stats", {}), payload)
        db.update_job(job_id, status="done", progress=1.0,
                      message=f"差异：{payload['stats'].get('sections_changed', 0)} 章节 / "
                              f"{payload['stats'].get('paras_changed', 0)} 段落 / "
                              f"{payload['stats'].get('numbers_changed', 0)} 数值 · 摘要 "
                              f"{payload['summary']['engine']}")
    except Exception as e:  # noqa: BLE001
        db.update_job(job_id, status="error", message=f"{type(e).__name__}: {e}")


def _hit_cancel(job_id: str, stage: str) -> bool:
    """阶段边界取消探测：命中即落 cancelled 终态并返回 True。"""
    if db.job_cancel_requested(job_id):
        db.update_job(job_id, status="cancelled", message=f"已在「{stage}」阶段取消")
        logger.info(f"job {job_id} cancelled at stage={stage}", module="job")
        return True
    return False


def _run_parse(job_id: str, paper_id: str) -> None:
    db.update_job(job_id, status="running", progress=0.02, message="开始解析")
    papers = db.query("SELECT * FROM papers WHERE id=?", (paper_id,))
    if not papers:
        db.update_job(job_id, status="error", message="paper not found")
        return
    paper = papers[0]
    proc = psutil.Process()
    mem_curve: list[list[float]] = []
    stop_sampler = threading.Event()

    def _sample_mem() -> None:
        t0 = time.time()
        while True:
            try:   # 先采一次（快解析也要有曲线起点），之后每 2s 一次
                mem_curve.append([round(time.time() - t0, 1),
                                  round(proc.memory_info().rss / 1048576, 1)])
            except Exception:  # noqa: BLE001, S110 —— 采样失败跳过
                pass
            if stop_sampler.wait(2.0):
                break

    def progress(frac: float, msg: str) -> None:
        db.update_job(job_id, progress=round(min(frac, 0.99), 3), message=msg)

    sampler = threading.Thread(target=_sample_mem, daemon=True)
    sampler.start()
    try:
        docir = parse_document(paper["file_path"], progress=progress, paper_id=paper_id,
                               cancel_check=lambda: db.job_cancel_requested(job_id))
        if _hit_cancel(job_id, "解析"):
            return
        peak_mb = max((m[1] for m in mem_curve), default=0.0)
        docir["meta"]["parse_memory"] = {"peak_mb": peak_mb, "samples": len(mem_curve)}
        write_docir(paper_id, docir)

        # 结构化元数据（分层：arXiv API → LLM → 启发式）：写回 docir.meta + papers 行。
        # 失败不阻断解析（闭环不中断），meta_source 如实标注来源。
        db.update_job(job_id, progress=0.86, message="抽取元数据（作者/摘要）")
        try:
            extract_and_store_metadata(paper_id, docir)
        except Exception as e:  # noqa: BLE001 —— 元数据失败不阻断
            logger.warning(f"extract_and_store_metadata 失败: {type(e).__name__}: {e}",
                           module="meta")
        write_docir(paper_id, docir)
        if _hit_cancel(job_id, "元数据抽取"):
            return

        # 论文级签名：title+abstract 向量（语义去重）+ 正文 MinHash（内容重合）+ family_id
        db.update_job(job_id, progress=0.90, message="计算论文指纹")
        _store_paper_signature(paper_id, docir)

        db.update_job(job_id, progress=0.92, message="切块与索引")
        index_docir = docir
        long_note = ""
        if docir["meta"].get("long_doc"):
            # 降级矩阵：长文档只对前 N 页做全文 chunk/索引，全书标题树已在 DocIR
            cap = settings.long_doc_index_pages
            index_docir = _index_view(docir, cap)
            long_note = f"（长文档：前 {cap} 页全文索引）"
        chunks = chunk_docir(index_docir)
        n = indexing.index_chunks(paper_id, chunks)
        vec_rows = db.query("SELECT COUNT(*) c FROM chunks WHERE paper_id=? AND vec IS NOT NULL",
                            (paper_id,))
        if indexing._backend_mode() != "off":
            vec_note = (f"，向量 {vec_rows[0]['c']}/{n}"
                        if vec_rows[0]["c"] else f"，向量 0/{n}（{indexing._EMBED_LAST_ERROR or '未知原因'}）")
        else:
            vec_note = ""
        if _hit_cancel(job_id, "切块索引"):
            return

        # references 落库
        with db.tx() as c:
            c.execute('DELETE FROM "references" WHERE paper_id=?', (paper_id,))
            for r in docir.get("references", []):
                c.execute('INSERT INTO "references"(id,paper_id,idx,raw,title,arxiv_id,doi)'
                          " VALUES(?,?,?,?,?,?,?)",
                          (db.new_id("r_"), paper_id, r["index"], r["raw"],
                           r.get("title", ""), r.get("arxiv_id", ""), r.get("doi", "")))

        with db.tx() as c:
            # arxiv_id 存基础 id（剥离 vN）；版本号单独入 version 列（去重文案/版本演进用）
            base_arxiv, ver_int = _split_arxiv(docir["meta"].get("arxiv_id", ""))
            ver = f"v{ver_int}" if ver_int else \
                _version_from_name(paper["meta_json"] and json.loads(paper["meta_json"] or "{}")
                                   .get("orig_filename", ""))
            c.execute("UPDATE papers SET title=?, arxiv_id=?, version=?, page_count=?, docir_path=?"
                      " WHERE id=?",
                      (docir["meta"].get("title", ""), base_arxiv, ver,
                       docir["meta"].get("page_count", 0),
                       str(DOCIR_DIR / f"{paper_id}.json"), paper_id))

        # 内容级去重先于置 done（官方用例 3.1：done 的瞬间 dedup_alert 必须已在；
        # P0-2 根因之二——旧顺序先 done 后去重，前端拿到 done 时预警尚不存在）
        try:
            post_parse_dedup(paper_id)
        except Exception as e:  # noqa: BLE001 —— 去重失败不影响 parse 成功，job message 如实标注
            logger.warning(f"post_parse_dedup 失败（不阻塞 parse）: {type(e).__name__}: {e}",
                           module="dedup")

        db.update_job(job_id, status="done", progress=1.0,
                      message=f"完成：{len(chunks)} chunks{vec_note} / refs {len(docir.get('references', []))}"
                              f" · {docir['meta'].get('parse_engine', '')}{long_note}")
    except ParseCancelled:
        db.update_job(job_id, status="cancelled", message="解析已取消")
    except Exception as e:  # noqa: BLE001
        db.update_job(job_id, status="error", message=f"{type(e).__name__}: {e}")
    finally:
        stop_sampler.set()


# ---------- 论文级签名（语义去重输入） ----------

def _store_paper_signature(paper_id: str, docir: dict) -> None:
    """title+abstract 论文级向量 + 正文 MinHash + family_id 落库。

    去重必须用「摘要+标题」而非整篇 embedding——整篇向量被正文稀释，
    同族不同版本反而相似度下降（方案 §1.7 明确踩坑）。
    正文指纹前先 boilerplate 滤除（页眉页脚/水印/References 是跨文档共噪声，
    会系统性抬高 MinHash——夹具跨 tag 误配 0.86~1.0 的实证根因之一）。
    向量带 (model, text_hash) 缓存：reparse 不重复调 embedding API。
    """
    meta = docir.get("meta", {})
    title = meta.get("title", "")
    abstract = meta.get("abstract", "")
    full_text = fingerprint.strip_boilerplate(
        [(b.get("text") or "") for b in docir.get("blocks", [])])
    if not abstract:  # 无摘要退化：首页文本头 1000 字（含作者行，仍优于整篇）
        head = " ".join((b.get("text") or "") for b in docir.get("blocks", [])
                        if b.get("page", 0) == 0)
        abstract = head[:1000]
    vec_blob = None
    if (title or abstract).strip() and indexing._backend_mode() != "off":
        vec_text = f"{title}\n{abstract}"
        cache_key = f"sig_vec_cache:{settings.embedding_model}:" \
                    f"{hashlib.sha1(vec_text.encode('utf-8')).hexdigest()[:16]}"
        old_meta = db.query("SELECT meta_json FROM papers WHERE id=?", (paper_id,))
        cached = {}
        if old_meta:
            try:
                cached = json.loads(old_meta[0]["meta_json"] or "{}")
            except ValueError:
                cached = {}
        blob_hex = cached.get(cache_key)
        if blob_hex:
            try:
                vec_blob = bytes.fromhex(blob_hex)
            except ValueError:
                vec_blob = None
        if vec_blob is None:
            vecs = indexing._embed_texts([vec_text])
            if vecs:
                vec_blob = indexing._pack(vecs[0])
                # 缓存只保留最近 4 个键（防 meta_json 无界增长）
                keys = [k for k in cached if k.startswith("sig_vec_cache:")]
                for k in keys[:-3]:
                    cached.pop(k, None)
                cached[cache_key] = vec_blob.hex()
                with db.tx() as c:
                    c.execute("UPDATE papers SET meta_json=? WHERE id=?",
                              (json.dumps(cached, ensure_ascii=False), paper_id))
    sig = fingerprint.minhash_sig(full_text)
    base_arxiv, _ = _split_arxiv(meta.get("arxiv_id", ""))
    fam = fingerprint.family_id(base_arxiv, title)
    with db.tx() as c:
        c.execute("UPDATE papers SET paper_vec=?, content_sig=?, family_id=? WHERE id=?",
                  (vec_blob, sig, fam, paper_id))


# ---------- 语义去重（官方用例 1） ----------

_ARXIV_FULL = re.compile(r"^(\d{4}\.\d{4,5})(?:v(\d+))?$")

# 综合分权重（9 层方案 L2-L6）：8 特征线性加权 + 缺失分量按剩余权重归一化——
# 缺失是「无证据」，不是 0 分。identifier 不进加权（走旗标/抬底逻辑）；llm 由灰区复核
# 单独处理；终值由 scripts/calibrate_dedup.py 的消融表与 LR 学习权重复核。
_WEIGHTS = {"vector": 0.30, "title": 0.20, "author": 0.10, "minhash": 0.20,
            "rerank": 0.10, "ngram": 0.05, "containment": 0.05, "year": 0.05}

_DOI_PREFIX = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def _norm_doi(doi: str | None) -> str:
    """DOI 归一：去 URL 前缀/doi: 头/空白，lower（标识符精确层的键）。"""
    return _DOI_PREFIX.sub("", (doi or "").strip()).lower()


def _index_view(docir: dict, cap: int) -> dict:
    """长文档索引视图：blocks/tables/**toc** 全部截断到前 cap 页（P0-3）。

    旧实现只过滤 blocks/tables——chunking 的章节概览块（summary）直接用 docir["toc"] 页码，
    后段章节会产生 page≥cap 的 summary chunk，官方用例 3.2b 的 max(chunk.page)<120 断言失败。
    """
    return {**docir,
            "blocks": [b for b in docir["blocks"] if b["page"] < cap],
            "tables": [t for t in docir["tables"] if t["page"] < cap],
            "toc": [t for t in docir.get("toc", []) if t.get("page", 0) < cap]}


def _split_arxiv(arxiv_id: str) -> tuple[str, int]:
    """arXiv id → (base, version_int)，无版本号 version=0。"""
    m = _ARXIV_FULL.match((arxiv_id or "").strip())
    if not m:
        return (arxiv_id or ""), 0
    return m.group(1), int(m.group(2) or 0)


def _version_from_name(name: str) -> str:
    """文件名抽版本：优先 arXiv id 后缀（1706.03762v7.pdf → v7），退化 vN（显式边界）。

    旧正则 `\bv(\\d+)\b` 两处不命中：digit→v 无词边界（…03762v7，官方 3.1 根因之一）、
    `_v1` 下划线后同样无词边界（a_v1.pdf 这类常见命名）→ 换显式前后断言。
    """
    m = re.search(r"(\d{4}\.\d{4,5})v(\d+)", name or "")
    if m:
        return f"v{m.group(2)}"
    m = re.search(r"(?<![0-9a-zA-Z])v(\d+)(?![0-9a-zA-Z])", name or "", re.IGNORECASE)
    return f"v{m.group(1)}" if m else ""


def _ver_int(paper_row) -> int:
    _base, ver = _split_arxiv(paper_row["arxiv_id"] or "")
    if ver:
        return ver
    return int(re.sub(r"\D", "", paper_row["version"] or "") or 0)


def _authors_of(paper_row) -> list:
    try:
        arr = json.loads(paper_row["authors_json"] or "[]")
        return arr if isinstance(arr, list) else []
    except Exception:  # noqa: BLE001
        return []


def _rerank_pair(row_a, row_b) -> float | None:
    """cross-encoder 精判（L6）：bge-reranker 对 (title+abstract) 成对打分（0~1）。
    复用 indexing.rerank_hits（截断/超时/失败返 None 齐备），零新增基础设施；
    无 key / 失败 → None（分量缺失走权重归一化，与 vector 同机制）。"""
    if not indexing.rerank_available():
        return None

    def _txt(r) -> str:
        return f"{r['title'] or ''}\n{(r['abstract'] or '')[:800]}".strip()

    q, d = _txt(row_a), _txt(row_b)
    if not q or not d:
        return None
    hits = indexing.rerank_hits(q, [{"text": d}], top_k=1)
    if not hits:
        return None
    try:
        return max(0.0, min(1.0, float(hits[0]["rerank"])))
    except (KeyError, TypeError, ValueError):
        return None


def _llm_judge_pair(row_a, row_b) -> dict | None:
    """LLM 判重（L7）：成对判断「同一论文的不同版本/重复入库」vs「仅主题相似」。
    结构化输出 DedupVerdict（same_family/confidence/reason）；端点不可用或校验
    失败 → None（调用方降级，不影响规则分）。prompt 版本化落 qa_logs 审计。"""
    from .llm import client
    from .llm.schemas import DedupVerdict

    if not client.available():
        return None

    def _fmt(r) -> str:
        names = ", ".join((a.get("name", "") if isinstance(a, dict) else str(a))
                          for a in _authors_of(r)) or "未知"
        return (f"标题：{r['title'] or '未知'}\n"
                f"摘要：{(r['abstract'] or '未知')[:800]}\n"
                f"作者：{names}\n年份：{r['pub_year'] or '未知'}\n"
                f"DOI：{r['doi'] or '未知'}\narXiv：{r['arxiv_id'] or '未知'}")

    messages = [
        {"role": "system", "content": (
            "你是学术文献判重助手。判断两篇论文是否为「同一篇论文的不同版本或重复入库」"
            "（same_family=true），而非仅主题相似的不同论文。判同族依据：标题实质相同、"
            "权威 id（DOI/arXiv）相同、作者团队一致、摘要核心工作一致。"
            "主题相近但研究工作不同 → same_family=false。只输出 JSON。")},
        {"role": "user", "content": f"论文 A：\n{_fmt(row_a)}\n\n论文 B：\n{_fmt(row_b)}"},
    ]
    return client.chat_json_validated(messages, DedupVerdict, module="dedup_judge",
                                      prompt_version="dedup-judge-v1", max_tokens=256)


def _score_pair(new, old, rerank_budget: list[int] | None = None,
                disable: set[str] | None = None) -> tuple[float, dict]:
    """两篇论文的同族综合分（0~1）+ 各分量 breakdown；数据缺失的分量按权重归一化。

    rerank_budget：形如 [剩余额度] 的可变计数（每次扫描共享，防 cross-encoder 放量）；
    disable：标定消融用——把指定分量视为缺失（不进 avail；确定性同源抬底不受影响）。
    """
    off = disable or set()
    base_new, _ = _split_arxiv(new["arxiv_id"] or "")
    base_old, _ = _split_arxiv(old["arxiv_id"] or "")
    same_family = bool(new["family_id"]) and new["family_id"] == old["family_id"]
    same_arxiv = bool(base_new) and base_new == base_old
    doi_new, doi_old = _norm_doi(new["doi"]), _norm_doi(old["doi"])
    same_doi = bool(doi_new) and doi_new == doi_old
    t_sim = fingerprint.title_sim(new["title"], old["title"])
    vec_cos = fingerprint.vec_cosine(new["paper_vec"], old["paper_vec"])

    # 快筛：任一命中才算候选（确定性同源 ×3 / 标题门 / 向量门）——与 blocking 层一致
    if not (same_family or same_arxiv or same_doi or t_sim >= 0.6 or vec_cos >= 0.85):
        return 0.0, {}

    avail: dict[str, float] = {}
    if "vector" not in off and new["paper_vec"] and old["paper_vec"]:
        avail["vector"] = vec_cos
    if "title" not in off:
        avail["title"] = t_sim
    authors_new, authors_old = _authors_of(new), _authors_of(old)
    if "author" not in off and authors_new and authors_old:
        avail["author"] = fingerprint.author_jaccard(authors_new, authors_old)
    if "minhash" not in off and new["content_sig"] and old["content_sig"]:
        avail["minhash"] = fingerprint.minhash_jaccard(new["content_sig"], old["content_sig"])
    if "containment" not in off and new["abstract"] and old["abstract"]:
        # 版本演进 v1⊂v7：Jaccard 系统性低估，containment（|A∩B|/min）更宽容；
        # 只用 DB 现有列（title+abstract），不读 DocIR。
        avail["containment"] = fingerprint.containment_from_sets(
            fingerprint.tokens(f"{new['title']} {new['abstract']}"),
            fingerprint.tokens(f"{old['title']} {old['abstract']}"))
    if "ngram" not in off and new["abstract"] and old["abstract"]:
        # 统计向量用 title+abstract；摘要缺失时不退化为纯标题 n-gram——那会与
        # title 分量同证据双计（标定集难负例实证：同题无摘要对综合分虚高 1.0）
        avail["ngram"] = fingerprint.char_ngram_cosine(
            f"{new['title']} {new['abstract']}", f"{old['title']} {old['abstract']}")
    if "rerank" not in off and rerank_budget is not None and rerank_budget[0] > 0:
        r = _rerank_pair(new, old)
        if r is not None:
            avail["rerank"] = r
            rerank_budget[0] -= 1
    if "year" not in off:
        year_new, year_old = int(new["pub_year"] or 0), int(old["pub_year"] or 0)
        if year_new and year_old:
            avail["year"] = math.exp(-abs(year_new - year_old) / 3.0)
    if not avail:
        return 0.0, {}
    wsum = sum(_WEIGHTS[k] for k in avail)
    score = sum(_WEIGHTS[k] * v for k, v in avail.items()) / wsum
    breakdown = {k: round(v, 4) for k, v in avail.items()}
    if same_arxiv or same_family or same_doi:
        breakdown["same_arxiv_id"] = same_arxiv
        breakdown["same_family"] = same_family
        breakdown["same_doi"] = same_doi
        if same_arxiv or same_doi:
            # 确定性同源是最强同族证据（方案 Step0 快速指纹的本意）：真 v1→v7
            # 正文差异本就大，嵌入/元数据降级场景文本分量会低估——id/DOI 同源直接
            # 抬到底线判定分（0.90），保证官方用例 3.1 在任何降级环境都命中。
            score = max(score, 0.90)
    return round(score, 4), breakdown


def _version_advice(ver_new: int, ver_old: int) -> str:
    if ver_new and ver_old and ver_new != ver_old:
        if ver_old > ver_new:
            return f"库中已有版本较新（v{ver_old} > v{ver_new}），建议保留已有版本，新上传仅作历史参照"
        return f"新上传版本较新（v{ver_new} > v{ver_old}），建议保留新版本并可归档旧版本"
    return "版本号未知或相同，请人工核对内容差异"


def _blocking_hit(new, row, band_keys: set[str], title_tokens: set[str]) -> bool:
    """blocking 层（L3）：LSH band 命中 ∪ 确定性同源（family/arXiv/DOI）∪
    标题 token 门 ∪ 向量门——任一命中才进入精判。全部用便宜键，贵的
    SequenceMatcher/minhash/cross-encoder 只花在候选上。"""
    base_new, _ = _split_arxiv(new["arxiv_id"] or "")
    base_old, _ = _split_arxiv(row["arxiv_id"] or "")
    if base_new and base_new == base_old:
        return True
    if new["family_id"] and new["family_id"] == row["family_id"]:
        return True
    doi_new, doi_old = _norm_doi(new["doi"]), _norm_doi(row["doi"])
    if doi_new and doi_new == doi_old:
        return True
    if band_keys:
        for k in fingerprint.lsh_bands(row["content_sig"]):
            if k in band_keys:
                return True
    tt = fingerprint.tokens(row["title"] or "")
    if title_tokens and tt and \
            len(title_tokens & tt) / max(1, len(title_tokens | tt)) >= 0.6:
        return True
    return fingerprint.vec_cosine(new["paper_vec"], row["paper_vec"]) >= 0.85


def _write_alert_pair(new, old_row, score: float, breakdown: dict,
                      extra_reason: str = "") -> dict:
    """写双向去重预警（判定路径统一出口）+ supersedes 指针 + 族重建。
    兼容旧字段 duplicate_of/title/existing_version/new_version/reason。"""
    ver_new, ver_old = _ver_int(new), _ver_int(old_row)
    comp_str = " · ".join(f"{k} {v:.2f}" for k, v in breakdown.items()
                          if isinstance(v, float))
    advice = _version_advice(ver_new, ver_old)
    reason = (f"综合相似度 {score:.0%}（{comp_str}）"
              + ("，且 arXiv id 同源" if breakdown.get("same_arxiv_id") else "")
              + ("，且 DOI 同源" if breakdown.get("same_doi") else "")
              + (f" · {extra_reason}" if extra_reason else "")
              + f" · {advice}")
    reverse_reason = (f"库中后上传了疑似同族版本（综合相似度 {score:.0%}）"
                      + (f" · {extra_reason}" if extra_reason else "")
                      + f" · {_version_advice(ver_old, ver_new)}")

    alert_id = db.create_dedup_alert(new["id"], old_row["id"], score, breakdown, reason)
    # 对称补警（TOCTOU 防线二）：先完成解析的一方也补一条指向后上传方的 pending，
    # 幂等（同对已存在即复用），双向都能看到预警并做出决策
    db.create_dedup_alert(old_row["id"], new["id"], score,
                          {**breakdown, "reverse": True}, reverse_reason)
    with db.tx() as c:
        c.execute("UPDATE papers SET supersedes=? WHERE id=?", (old_row["id"], new["id"]))
    rebuild_families()

    return {"duplicate_of": old_row["id"], "title": old_row["title"],
            "existing_version": f"v{ver_old}" if ver_old else (old_row["version"] or ""),
            "new_version": f"v{ver_new}" if ver_new else "",
            "same_arxiv_id": bool(breakdown.get("same_arxiv_id")),
            "score": score, "breakdown": breakdown, "alert_id": alert_id,
            "passed": score >= settings.dedup_threshold,
            "reason": reason}


def dedup_scan(paper_id: str) -> dict | None:
    """内容级语义查重（L3 blocking → 精判 → 三级候选选择 → 落 dedup_alerts）。

    必须在 _dedup_lock 内调用（与 upload_pdf 插入互斥，消除 TOCTOU 读后写竞态）。
    返回 dict（含 passed 标记）；无候选或灰区外未过阈返回 None。
    灰区近失对（阈值下方一个 dedup_gray_zone 带宽内）返回但不落警——
    由 post_parse_dedup 在锁外走 LLM 复核决定补警。
    """
    rows = db.query("SELECT * FROM papers WHERE id=?", (paper_id,))
    if not rows or not rows[0]["title"]:
        return None
    new = rows[0]
    ver_new = _ver_int(new)

    # blocking：一次取全库（元数据列本就要读），band keys 预建，候选外零特征成本
    band_keys = set(fingerprint.lsh_bands(new["content_sig"]))
    title_tokens = fingerprint.tokens(new["title"])
    budget = [settings.dedup_rerank_max]
    candidates: list[tuple[float, object, dict]] = []
    best_miss: tuple[float, object, dict] | None = None
    th = settings.dedup_threshold
    for row in db.query("SELECT * FROM papers WHERE id != ?", (paper_id,)):
        if not _blocking_hit(new, row, band_keys, title_tokens):
            continue
        score, breakdown = _score_pair(new, row, rerank_budget=budget)
        if score >= th:
            candidates.append((score, row, breakdown))
        elif score >= th - settings.dedup_gray_zone and \
                (best_miss is None or score > best_miss[0]):
            best_miss = (score, row, breakdown)
    if not candidates:
        if best_miss is None:
            return None
        score, old, breakdown = best_miss
        return {"duplicate_of": old["id"], "title": old["title"],
                "existing_version": old["version"] or "",
                "new_version": f"v{ver_new}" if ver_new else "",
                "same_arxiv_id": bool(breakdown.get("same_arxiv_id")),
                "score": score, "breakdown": breakdown, "alert_id": None,
                "passed": False, "reason": ""}
    # 候选选择三级优先（修跨族挤占）：同族(family_id) → 同 arXiv 基/DOI 确定性同源
    # → 其余。前两级是「版本治理可执行」的正主（预警要指向可保留的那个版本）；
    # 兜底池保留 newer 语义：库中已有更新版本时只在更新者中取「版本最新、分数最高」。
    fam_new = new["family_id"] or ""
    base_new, _ = _split_arxiv(new["arxiv_id"] or "")
    doi_new = _norm_doi(new["doi"])

    def _same_root(c: tuple[float, object, dict]) -> bool:
        _s, r, _b = c
        if fam_new and r["family_id"] == fam_new:
            return True
        rbase, _ = _split_arxiv(r["arxiv_id"] or "")
        if base_new and rbase == base_new:
            return True
        return bool(doi_new) and _norm_doi(r["doi"]) == doi_new

    same_root = [c for c in candidates if _same_root(c)]
    pool = same_root or candidates
    if not same_root:
        newer = [c for c in pool if _ver_int(c[1]) >= ver_new > 0]
        pool = newer or pool
    score, old, breakdown = max(pool, key=lambda c: (_ver_int(c[1]), c[0]))
    return _write_alert_pair(new, old, score, breakdown)


def _refine_with_judge(paper_id: str, result: dict) -> dict | None:
    """L7 灰区 LLM 复核（锁外执行，不拉长 _dedup_lock 持有）。默认开，上限
    dedup_llm_judge_max 次/扫描（当前实现每篇只复核 top1 对，天然 ≤1 次）：
    - 近失对（passed=False 且 ≥阈值-灰区）：判同族且 conf≥0.8 → 补警抬至阈值；
    - 过阈但正文指纹弱（minhash<0.5）：复核防「特征分虚高」；
      判异族且无确定性同源 → 双向 pending 撤销；判同族 → 记 llm 分量+理由；
    - same_arxiv/same_doi/same_family 抬底的对不受 veto（确定性证据优先，仅附注意见）。
    """
    if not settings.dedup_llm_judge or not result:
        return result
    th = settings.dedup_threshold
    breakdown = result.get("breakdown", {})
    score = float(result.get("score", 0.0))
    floor_hit = bool(breakdown.get("same_arxiv_id") or breakdown.get("same_doi")
                     or breakdown.get("same_family"))
    gray_low = not result.get("passed") and score >= th - settings.dedup_gray_zone
    weak = bool(result.get("passed")) and breakdown.get("minhash", 1.0) < 0.5
    if not (gray_low or weak):
        return result
    rows_a = db.query("SELECT * FROM papers WHERE id=?", (paper_id,))
    rows_b = db.query("SELECT * FROM papers WHERE id=?", (result["duplicate_of"],))
    if not rows_a or not rows_b:
        return result
    verdict = _llm_judge_pair(rows_a[0], rows_b[0])
    if verdict is None:   # 端点不可用/输出不合法：维持规则分结论（降级语义）
        return result
    conf = float(verdict.get("confidence", 0.5))
    note = (f"LLM 复核 conf {conf:.0%}："
            f"{'同族' if verdict['same_family'] else '不同论文'}——{verdict.get('reason', '')}")
    # create_dedup_alert 幂等复用不会更新字段，先清双向 pending 再按结论重写
    db.delete_pending_alert_pair(paper_id, result["duplicate_of"])
    if verdict["same_family"]:
        new_bd = {**breakdown, "llm": round(conf, 4)}
        new_score = max(score, th) if (gray_low and conf >= 0.8) else score
        logger.info(f"LLM 判重补证 {paper_id} ↔ {result['duplicate_of']} "
                    f"score→{new_score:.2f}", module="dedup")
        return _write_alert_pair(rows_a[0], rows_b[0], new_score, new_bd, extra_reason=note)
    if floor_hit:   # 确定性同源优先：保留预警，附 LLM 存疑意见，人工最终裁决
        logger.info(f"LLM 判重存疑但 id 同源，保留 {paper_id} ↔ {result['duplicate_of']}",
                    module="dedup")
        return _write_alert_pair(rows_a[0], rows_b[0], score, breakdown, extra_reason=note)
    logger.info(f"LLM 判重否决 {paper_id} ↔ {result['duplicate_of']}，预警撤销",
                module="dedup")
    return None


def post_parse_dedup(paper_id: str) -> dict | None:
    """解析完成后内容级查重（官方用例 1：先 v7 后 v1）。
    锁内扫描落警；LLM 灰区复核在锁外（防 API 延迟拉长 TOCTOU 串行段）。"""
    with _dedup_lock:
        result = dedup_scan(paper_id)
        rebuild_families()
    if result:
        result = _refine_with_judge(paper_id, result)
    return result


# ---------- 族聚类（L8）：union-find 连通分量 = 论文族 ----------

def rebuild_families() -> dict:
    """全库族重建：边 = 确定性同源（arXiv 基/DOI 相同）∪ 人工已决策对
    （covered/kept/coexisted）。pending 分数对**不入边**——防一处错配经闭包传播
    （标准做法明示的局限，用「人工确认后才成簇」化解）。根选择：族内有 arXiv 基者
    取字典序最小（版本链的天然锚），否则按最早成员的标题重算 t_hash 键。仅写变化行。"""
    rows = db.query("SELECT id,title,arxiv_id,doi,family_id,created_at FROM papers")
    if not rows:
        return {"papers": 0, "changed": 0, "families": 0}
    parent: dict[str, str] = {r["id"]: r["id"] for r in rows}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    base_of: dict[str, str] = {}
    doi_of: dict[str, str] = {}
    for r in rows:
        b, _ = _split_arxiv(r["arxiv_id"] or "")
        if b:
            if b in base_of:
                union(base_of[b], r["id"])
            else:
                base_of[b] = r["id"]
        d = _norm_doi(r["doi"])
        if d:
            if d in doi_of:
                union(doi_of[d], r["id"])
            else:
                doi_of[d] = r["id"]
    for a in db.query("SELECT new_paper_id, existing_paper_id FROM dedup_alerts "
                      "WHERE status IN ('covered','kept','coexisted')"):
        if a["new_paper_id"] in parent and a["existing_paper_id"] in parent:
            union(a["new_paper_id"], a["existing_paper_id"])

    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r)
    changed = 0
    with db.tx() as c:
        for members in groups.values():
            bases = sorted({_split_arxiv(m["arxiv_id"] or "")[0] for m in members
                            if _split_arxiv(m["arxiv_id"] or "")[0]})
            root = bases[0] if bases else ""
            if not root:
                earliest = min(members, key=lambda m: (m["created_at"], m["id"]))
                root = fingerprint.family_id("", earliest["title"] or "")
            if not root:
                continue
            for m in members:
                if m["family_id"] != root:
                    c.execute("UPDATE papers SET family_id=? WHERE id=?", (root, m["id"]))
                    changed += 1
    return {"papers": len(rows), "changed": changed, "families": len(groups)}


def upload_pdf(filename: str, content: bytes) -> dict:
    """保存文件 → 查重 → 建 paper 记录 → 提交解析任务。

    hash 查重先于写文件（消灭孤儿文件）；插入+复用在 _dedup_lock 内串行，
    file_hash 唯一索引作并发最后防线。
    """
    import hashlib

    h = hashlib.sha256(content).hexdigest()
    with _dedup_lock:
        exact = db.query("SELECT id,title FROM papers WHERE file_hash=?", (h,))
        if exact:  # 文件级完全相同 → 直接复用
            return {"paper_id": exact[0]["id"], "dedup": "exact_file",
                    "existing_title": exact[0]["title"], "job_id": None}

        paper_id = db.new_id("p_")
        dest = UPLOAD_DIR / f"{paper_id}.pdf"
        dest.write_bytes(content)
        try:
            with db.tx() as c:
                c.execute("INSERT INTO papers(id,title,file_path,file_hash,created_at,meta_json)"
                          " VALUES(?,?,?,?,?,?)",
                          (paper_id, filename.rsplit(".", 1)[0], str(dest), h, db.now(),
                           json.dumps({"orig_filename": filename}, ensure_ascii=False)))
        except Exception:
            # 唯一索引兜底（跨进程并发同 hash）：复用既有记录并清理本进程孤儿文件
            dest.unlink(missing_ok=True)
            exact = db.query("SELECT id,title FROM papers WHERE file_hash=?", (h,))
            if exact:
                return {"paper_id": exact[0]["id"], "dedup": "exact_file",
                        "existing_title": exact[0]["title"], "job_id": None}
            raise
        job_id = submit_parse(paper_id)
    return {"paper_id": paper_id, "dedup": None, "job_id": job_id}


# ---------- 去重决策（覆盖 / 保留 / 并存，软归档语义） ----------

_DECISIONS = ("cover", "keep", "coexist")


def apply_dedup_decision(new_paper_id: str, target_id: str, decision: str) -> dict:
    """三态决策落库（软归档：不物理删除，diff/审计链路始终可用）。

    - cover  覆盖：新版本 canonical，旧版本 archived（superseded_by 指向新）
    - keep   保留既有：旧版本 canonical，新上传 archived（对应「仅归档 v1」）
    - coexist 并存：双方 active，同族关联
    幂等：重复调用同一决策无副作用；决策后双向 pending alert 落终态。
    """
    if decision not in _DECISIONS:
        raise ValueError(f"unknown decision: {decision}")
    rows_new = db.query("SELECT * FROM papers WHERE id=?", (new_paper_id,))
    rows_old = db.query("SELECT * FROM papers WHERE id=?", (target_id,))
    if not rows_new or not rows_old:
        raise LookupError("paper not found")
    new, old = rows_new[0], rows_old[0]
    # 同族校验：family/arxiv/标题相似 任一成立才允许决策（防误传不相干的 id）
    if new["id"] != old["id"] and not _score_pair(new, old)[0]:
        t_sim = fingerprint.title_sim(new["title"], old["title"])
        if t_sim < 0.6:
            raise ValueError("papers are not the same family; refuse decision")

    # family 归一：双方取同族键（旧版 family 为空时用新版回填）
    fam = new["family_id"] or old["family_id"]
    with db.tx() as c:
        if decision == "cover":
            c.execute("UPDATE papers SET status='active', supersedes=?, family_id=? WHERE id=?",
                      (old["id"], fam, new["id"]))
            c.execute("UPDATE papers SET status='archived', superseded_by=? WHERE id=?",
                      (new["id"], old["id"]))
        elif decision == "keep":
            c.execute("UPDATE papers SET status='archived', superseded_by=?, family_id=? "
                      "WHERE id=?", (old["id"], fam, new["id"]))
            c.execute("UPDATE papers SET family_id=? WHERE id=?", (fam, old["id"]))
        else:  # coexist
            c.execute("UPDATE papers SET status='active', family_id=? WHERE id=?",
                      (fam, new["id"]))
            c.execute("UPDATE papers SET family_id=? WHERE id=?", (fam, old["id"]))
        c.execute("UPDATE dedup_alerts SET status=?, decided_at=? "
                  "WHERE status='pending' AND new_paper_id=? AND existing_paper_id=?",
                  (decision, db.now(), new["id"], old["id"]))
        c.execute("UPDATE dedup_alerts SET status=?, decided_at=? "
                  "WHERE status='pending' AND new_paper_id=? AND existing_paper_id=?",
                  (decision, db.now(), old["id"], new["id"]))
    rebuild_families()   # 已决策对成为族边（L8）：决策即成簇，闭包立即生效
    return {"decision": decision, "new": dict(db.query(
        "SELECT id,title,status,supersedes,superseded_by,family_id FROM papers WHERE id=?",
        (new["id"],))[0]),
        "target": dict(db.query(
            "SELECT id,title,status,supersedes,superseded_by,family_id FROM papers WHERE id=?",
            (old["id"],))[0])}


def delete_paper(paper_id: str) -> dict:
    """硬删除（CRUD）：文件 + DocIR + chunks/FTS + references + 双向 alert +
    相关 diff + jobs + translations 级联清理；archived 同样可删。"""
    rows = db.query("SELECT * FROM papers WHERE id=?", (paper_id,))
    if not rows:
        raise LookupError("paper not found")
    paper = rows[0]
    with db.tx() as c:
        c.execute("DELETE FROM chunks WHERE paper_id=?", (paper_id,))
        c.execute("DELETE FROM chunks_fts WHERE paper_id=?", (paper_id,))
        c.execute('DELETE FROM "references" WHERE paper_id=?', (paper_id,))
        c.execute("DELETE FROM dedup_alerts WHERE new_paper_id=? OR existing_paper_id=?",
                  (paper_id, paper_id))
        c.execute("DELETE FROM version_diffs WHERE base_id=? OR target_id=?",
                  (paper_id, paper_id))
        c.execute("DELETE FROM jobs WHERE paper_id=?", (paper_id,))
        c.execute("DELETE FROM translations WHERE paper_id=?", (paper_id,))
        c.execute("UPDATE papers SET supersedes='' WHERE supersedes=?", (paper_id,))
        c.execute("UPDATE papers SET superseded_by='' WHERE superseded_by=?", (paper_id,))
        c.execute("DELETE FROM papers WHERE id=?", (paper_id,))
    for path in (paper["file_path"], paper["docir_path"]):
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                logger.warning(f"删除文件失败（记录已清）: {path}", module="crud")
    return {"deleted": paper_id, "title": paper["title"]}
