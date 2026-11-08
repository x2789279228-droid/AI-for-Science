"""正文指纹与相似度（模块一 P0：语义级去重的快筛/精判工具层）。

设计按方案 §1.2「三步判定」+ 9 层升级（从字面到推理）：
- 5-shingle MinHash（128 维，float32 打包进 papers.content_sig BLOB）→ 正文重合度；
- b-band LSH（32×4，命中阈值≈0.5）→ blocking 候选生成（亚线性，召回有安全垫）；
- 标题 token Jaccard / difflib ratio 双条件 → 标题相似（修单尺虚高）；
- containment / 字符 3-gram 余弦 → 版本演进与统计向量特征；
- family_id：arXiv 基 id（剥离 vN）优先，否则标题归一（同族版本链的锚）。
纯 numpy + 标准库，确定性可复现（hash 用 sha1 而非内置 hash——后者进程间加盐不稳定）。
"""
from __future__ import annotations

import hashlib
import re
import struct
import unicodedata
from difflib import SequenceMatcher

import numpy as np

PERMS = 128          # MinHash 置换函数个数（128×f4 = 512B/篇）
SHINGLE = 5          # 5-gram 字符 shingle（中英混排均可）
_MAX_HEAD = 20000    # 指纹取样上限字符（正文过长时取头部——重合检测对头部已足够敏感）
_MAX_HASHES = 512    # 参与 MinHash 的 shingle 抽样上限（长文档防慢）

LSH_BANDS = 32       # b-band LSH：32 band × 4 row，命中阈值≈(1/32)^(1/4)≈0.42
LSH_ROWS = 4         # 取向召回：判定阈 0.85 分量里 minhash 0.5~0.75 的对也要能被 blocking 捕获，
                     # 而 32×4 在 s=0.55 时召回≈0.93；低相似对误入候选的成本仅为一次快筛

_MERSenne = (1 << 61) - 1  # 大素数模（避免溢出碰撞）

_ARXIV_LINE = re.compile(r"^arxiv:\d{4}\.\d{4,5}(v\d+)?[.]?$", re.IGNORECASE)
_PAGE_NO = re.compile(r"^\d{1,4}$")
_REF_HEADING = re.compile(r"^(references|参考文献)$", re.IGNORECASE)


def _shingles(text: str, k: int = SHINGLE) -> list[bytes]:
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if len(t) <= k:
        return [t.encode("utf-8")] if t else []
    shingles = [t[i:i + k].encode("utf-8") for i in range(len(t) - k + 1)]
    if len(shingles) > _MAX_HASHES:  # 均匀抽样，保持签名对全文代表性
        step = len(shingles) / _MAX_HASHES
        shingles = [shingles[int(i * step)] for i in range(_MAX_HASHES)]
    return shingles


def _hash64(data: bytes) -> int:
    return int.from_bytes(hashlib.sha1(data).digest()[:8], "big")


# ---------- boilerplate 滤除（文本指纹标准前置：页眉页脚/水印/参考文献强干扰） ----------

def strip_boilerplate(blocks: list[str] | str, min_repeat: int = 3) -> str:
    """清洗正文用于指纹：文档内重复≥min_repeat 次的块（页眉/页脚）、纯页码行、
    arXiv 水印行删除；References 起截断（引用列表对判重是噪声且常跨版本不变）。
    输入 DocIR blocks 文本列表（或已拼接文本按行拆）。返回拼接后文本。
    """
    if isinstance(blocks, str):
        blocks = blocks.splitlines()

    def norm(b: str) -> str:
        return re.sub(r"\s+", " ", (b or "")).strip()

    freq: dict[str, int] = {}
    for b in blocks:
        key = norm(b)
        if key:
            freq[key] = freq.get(key, 0) + 1
    out: list[str] = []
    in_refs = False
    for b in blocks:
        key = norm(b)
        if not in_refs and _REF_HEADING.match(key):
            in_refs = True
            continue
        if in_refs or len(key) < 2:
            continue
        if _PAGE_NO.match(key) or _ARXIV_LINE.match(key):
            continue
        if freq[key] >= min_repeat:
            continue
        out.append(key)
    return " ".join(out)


def minhash_sig(text: str) -> bytes:
    """正文 → 128 维 MinHash 签名（float32 LE BLOB，存 papers.content_sig）。"""
    shingles = _shingles((text or "")[:_MAX_HEAD])
    if not shingles:
        return b""
    hs = np.fromiter((_hash64(s) for s in shingles), dtype=np.uint64,
                     count=len(shingles)).astype(np.uint64)
    # 置换族：a_i * x + b_i (mod 大素数)，a/b 由计数序列派生（确定性）
    idx = np.arange(PERMS, dtype=np.uint64)
    a = (idx * 2 + 1) * 2654435761 % _MERSenne
    b = (idx + 1) * 40503 % _MERSenne
    sig = np.full(PERMS, np.uint64(_MERSenne), dtype=np.uint64)
    for i in range(PERMS):
        sig[i] = np.min((a[i] * hs + b[i]) % np.uint64(_MERSenne))
    return struct.pack(f"<{PERMS}f", *(v / _MERSenne for v in sig.astype(np.float64)))


def unpack_sig(blob: bytes | None) -> np.ndarray | None:
    if not blob:
        return None
    try:
        return np.frombuffer(blob, dtype="<f4")
    except Exception:  # noqa: BLE001 —— 脏数据按无指纹处理
        return None


def minhash_jaccard(sig_a: bytes | None, sig_b: bytes | None) -> float:
    """两签名 MinHash Jaccard 估计；任一缺失返回 0。"""
    va, vb = unpack_sig(sig_a), unpack_sig(sig_b)
    if va is None or vb is None:
        return 0.0
    return float(np.mean(va == vb))


def lsh_bands(sig: bytes | None, bands: int = LSH_BANDS, rows: int = LSH_ROWS) -> list[str]:
    """MinHash 签名 → b-band LSH band keys（blocking）。
    任一 band key 相同 → 候选对；签名缺失/长度不足返回 []。"""
    va = unpack_sig(sig)
    if va is None or va.size < bands * rows:
        return []
    return [hashlib.sha1(va[i * rows:(i + 1) * rows].tobytes()).hexdigest()
            for i in range(bands)]


def containment_from_sets(tokens_a: set[str], tokens_b: set[str]) -> float:
    """|A∩B| / min(|A|,|B|)——版本演进（v1→v7 增写内容）比 Jaccard 更宽容。"""
    if not tokens_a or not tokens_b:
        return 0.0
    inter = len(tokens_a & tokens_b)
    return inter / max(1, min(len(tokens_a), len(tokens_b)))


def tokens(text: str) -> set[str]:
    """词级 token 集（英文词 + 单个汉字）——标题门控与 containment 共用。"""
    return set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", (text or "").lower()))


def title_token_jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def title_norm_v2(t: str, keep_subtitle: bool = False) -> str:
    """标题归一 v2：NFKC 折叠（全角/数学字母→ASCII）→ lower → 去标点 →（默认）
    副标题截断（首个冒号/破折号处，截断后过短则保留全长）——OCR/导出器差异
    不再制造假差异，而仅尾字符不同的短标题不再被字符比虚高抬分。"""
    s = unicodedata.normalize("NFKC", (t or "")).lower()
    if not keep_subtitle:
        head = re.split(r"[:：]|[—–]| [-] ", s, maxsplit=1)[0]
        if len(re.findall(r"[a-z0-9\u4e00-\u9fff]", head)) >= 8:
            s = head
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s)


def title_sim(a: str, b: str) -> float:
    """标题相似 = token Jaccard 与归一串 SequenceMatcher 的**双条件**合成：
    - 双 ≥0.75：同题确证，取 max；
    - 任一 ≤0.35：有一把尺子明确低，取 min（惩罚单尺虚高，如仅尾字符不同的短标题）；
    - 中间带：取平均。
    归一串用 title_norm_v2（旧实现剥符号后 `sd1`/`sd3` 仅差一字符 ratio≈0.95 直接过门）。
    """
    if not a or not b:
        return 0.0
    ta, tb = tokens(a), tokens(b)
    jac = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    na, nb = title_norm_v2(a), title_norm_v2(b)
    ratio = SequenceMatcher(None, na, nb).ratio() if na and nb else 0.0
    if jac >= 0.75 and ratio >= 0.75:
        return round(max(jac, ratio), 4)
    if jac <= 0.35 or ratio <= 0.35:
        return round(min(jac, ratio), 4)
    return round((jac + ratio) / 2, 4)


def author_jaccard(authors_a: list | str, authors_b: list | str) -> float:
    """作者重合（L2 增强）：姓氏必命中 + 名全称或首字母缩写兼容（"Z. Li"≈"Zhang Li"）
    的贪心配对比例；分母取较小名单长度（名单缺失侧不罚，与 containment 同哲学）。
    姓氏判定：含 CJK 取首 token（姓前名后），西文取末 token（名前姓后），
    显式 "Surname, Given" 逗号格式取逗号前。"""
    def parse(lst: list | str) -> list[tuple[str, set[str]]]:
        out: list[tuple[str, set[str]]] = []
        for it in (lst if isinstance(lst, list) else []):
            name = (it.get("name", "") if isinstance(it, dict) else str(it))
            name = unicodedata.normalize("NFKC", name).lower()
            if "," in name:  # "Vaswani, Ashish" → 姓=vaswani
                sur, givens = name.split(",", 1)
                parts = re.findall(r"[a-z\u4e00-\u9fff]+", sur)
                given_parts = re.findall(r"[a-z\u4e00-\u9fff]+", givens)
            else:
                parts = re.findall(r"[a-z\u4e00-\u9fff]+", name)
                if len(parts) > 1:
                    if re.search(r"[\u4e00-\u9fff]", name):
                        sur, given_parts = parts[0], parts[1:]   # 姓前名后：首 token 即姓
                    else:
                        sur, given_parts = parts[-1], parts[:-1]
                else:
                    sur, given_parts = (parts[0] if parts else ""), []
            if sur:
                out.append((sur, set(given_parts)))
        return out

    def initials(gs: set[str]) -> set[str]:
        return {g[0] for g in gs if g}

    A, B = parse(authors_a), parse(authors_b)
    if not A or not B:
        return 0.0
    matched = 0
    used: set[int] = set()
    for sur, givens in A:
        for j, (sur2, givens2) in enumerate(B):
            if j in used or sur != sur2:
                continue
            i1, i2 = initials(givens), initials(givens2)
            if not givens or not givens2 or givens == givens2 or (i1 and i1 == i2):
                used.add(j)
                matched += 1
                break
    return matched / max(1, min(len(A), len(B)))


def char_ngram_cosine(a: str, b: str, n: int = 3, dim: int = 4096) -> float:
    """哈希式字符 n-gram 余弦（统计向量层）：免分词、对 OCR 噪声稳健、确定性。
    sha1 桶化到 dim 维 + L2 归一后点积；空文本返回 0。只对候选对现算，无存储。"""
    def vec(t: str) -> np.ndarray:
        t = re.sub(r"\s+", " ", (t or "").lower()).strip()
        v = np.zeros(dim, dtype=np.float32)
        if len(t) < n:
            if t:
                v[_bucket(t, dim)] = 1.0
            return v
        for i in range(len(t) - n + 1):
            v[_bucket(t[i:i + n], dim)] += 1.0
        return v

    def _bucket(g: str, dim: int) -> int:
        return int.from_bytes(hashlib.sha1(g.encode("utf-8")).digest()[:4], "big") % dim

    va, vb = vec(a), vec(b)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return float(np.dot(va, vb) / denom) if denom else 0.0


def vec_cosine(vec_a: bytes | None, vec_b: bytes | None) -> float:
    va, vb = unpack_sig(vec_a), unpack_sig(vec_b)
    if va is None or vb is None or va.shape != vb.shape:
        return 0.0
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return float(np.dot(va, vb) / denom) if denom else 0.0


def family_id(arxiv_base: str, title: str) -> str:
    """同族键：arXiv 基 id 优先（1706.03762），否则标题归一哈希。
    注意：归一沿用旧规则（identity key 稳定性优先）——换 title_norm_v2 会让
    旧库 t_hash 族键悄悄失配，故 title_sim 的改进不外溢到此处。"""
    base = (arxiv_base or "").strip()
    if re.match(r"^\d{4}\.\d{4,5}$", base):
        return base
    if base:
        return base
    norm = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (title or "").lower())
    return f"t_{hashlib.sha1(norm.encode('utf-8')).hexdigest()[:12]}" if len(norm) >= 8 else ""
