"""去重阈值校准（docs/05 D4 既定动作 + 9 层方案升级）。

标注集四部分：
1. 合成正例：同题不同版本（改写数字/加句/增写一章）= 正例（可复现、量大）；
2. 合成负例：异题论文（悬殊 topic）；
3. **难负例**（9 层方案新增，标定集此前缺口的补齐）：
   a. 同模板标题仅尾词异（复刻跨夹具碰撞形态，实测旧实现 0.91~0.94 的 FP 源）；
   b. 综述 vs 具体论文（同域不同工作）；
   c. 同题不同作者不同文（同题是判重最强文本证据，必须能被内容特征否决）；
4. 真素材对：downloads/1706.03762/ 下 v7 vs v1（真解析后比对），存在时并入。

输出：阈值扫 P/R/F1 表 + 推荐档；同族对分数与分量；负例最高分；
分量消融表（逐一 disable 后 F1 变化）；LR 学习权重 vs 手写权重对照。
用法：cd api && uv run python scripts/calibrate_dedup.py [--thresholds 0.80 0.85 0.90 0.92 0.95]
只读已解析库 + 新建密闭数据目录，不改线上状态。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 密闭 env（对齐 pytest 头部约定），嵌入/rerank 关（指纹/标题分量足够校准）
os.environ.setdefault("MINERU_ENABLED", "off")
os.environ.setdefault("EMBEDDING_BACKEND", "off")
os.environ.setdefault("SCHOLAR_DATA_DIR",
                      str(Path(__file__).parent.parent / "tests" / f"_cal_{int(__import__('time').time())}"))

import numpy as np

from app import db, jobs
from app.parsing import pipeline
from tests.fixtures_pdf import make_paper_pdf


def _ingest(pdf_path: Path) -> dict:
    """解析入库（不走线程池，同步快）+ 元数据抽取 + 论文签名——与真实 parse 管线同序，
    作者/摘要/年份进入特征（标定集此前跳过元数据，难负例 c 的作者/摘要特征失效）。"""
    docir = pipeline.parse_pdf(pdf_path)
    paper_id = f"cal_{pdf_path.stem[:16]}_{db.new_id()[:6]}"
    from app.config import DOCIR_DIR
    DOCIR_DIR.mkdir(parents=True, exist_ok=True)
    (DOCIR_DIR / f"{paper_id}.json").write_text(json.dumps(docir, ensure_ascii=False),
                                                encoding="utf-8")
    meta = docir["meta"]
    base, _ver = jobs._split_arxiv(meta.get("arxiv_id", ""))
    with db.tx() as c:
        c.execute("INSERT INTO papers(id,title,file_path,file_hash,arxiv_id,page_count,"
                  "docir_path,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (paper_id, meta.get("title", ""), str(pdf_path),
                   __import__("hashlib").sha256(pdf_path.read_bytes()).hexdigest(),
                   base, meta.get("page_count", 0), str(DOCIR_DIR / f"{paper_id}.json"), db.now()))
    try:
        from app.rag.metadata import extract_and_store_metadata
        extract_and_store_metadata(paper_id, docir)
    except Exception as e:  # noqa: BLE001 —— 元数据失败不阻断（与管线同语义）
        print(f"[cal] 元数据抽取失败（{type(e).__name__}: {e}），该篇无摘要/作者特征")
    jobs._store_paper_signature(paper_id, docir)
    return dict(db.query("SELECT * FROM papers WHERE id=?", (paper_id,))[0])


def _mk(tmp: Path, name: str, title: str, facts: list[str], base: str,
        ver: int = 1) -> dict:
    p = tmp / f"{name}.pdf"
    make_paper_pdf(p, title, body_facts=facts,
                   refs=[f"Ref{i} et al. 20{i}. arXiv:20{i:02d}.0000{i}" for i in range(1, 3)],
                   arxiv_line=f"arXiv:{base}v{ver}")
    return _ingest(p)


def build_dataset() -> list[tuple[dict, dict, int]]:
    """返回 [(rowA, rowB, label)]，label 1=同族正例 0=异族负例（含难负例）。"""
    tmp = Path(os.environ["SCHOLAR_DATA_DIR"]) / "cal"
    tmp.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[dict, dict, int]] = []

    # ---- 正例族 A（3 篇同题不同版：改数字 / 增写句 / 增写一章=containment 场景）----
    fam = tmp / "fam"
    fam.mkdir(exist_ok=True)
    variants = [
        ("Semantic相似度校准论文 A", 1, "Trains for 100K steps, gets 26.1 BLEU on WMT14."),
        ("Semantic相似度校准论文 A", 7, "Trains for 300K steps, gets 27.3 BLEU on WMT14."),
        ("Semantic相似度校准论文 A", 8,
         "Trains for 300K steps, gets 27.3 BLEU. We add a new ablation on positional encoding."),
    ]
    fam_rows = []
    for i, (title, ver, fact) in enumerate(variants):
        fam_rows.append(_mk(fam, f"famA_v{ver}_{i}", title, [fact], "1706.03762", ver))
    # v8 相对 v1 大幅增写（containment 场景）：Jaccard 低估、containment 应补上
    fam_rows.append(_mk(fam, "famA_v9", "Semantic相似度校准论文 A",
                        [variants[2][2],
                         ("Chapter on rotary embeddings added in the journal version with "
                          "extensive experiments on long context benchmarks and scaling laws.")],
                        "1706.03762", 9))
    for i in range(len(fam_rows)):
        for j in range(i + 1, len(fam_rows)):
            rows.append((fam_rows[i], fam_rows[j], 1))

    # ---- 难负例 a：同模板标题仅尾词异（跨夹具碰撞形态）----
    hn = tmp / "hardneg"
    hn.mkdir(exist_ok=True)
    hn_a = []
    hn_bases = {"Alpha": "2001.00011", "Beta": "2001.00012", "Gamma": "2001.00013"}
    for suffix, hbase in hn_bases.items():
        hn_a.append(_mk(hn, f"hn_a_{suffix}",
                        f"Distributed Training Systems A Comparative {suffix}",
                        [f"The {suffix} system evaluates allreduce throughput on 64 GPUs."],
                        hbase))
    for i in range(len(hn_a)):
        for j in range(i + 1, len(hn_a)):
            rows.append((hn_a[i], hn_a[j], 0))

    # ---- 难负例 b：综述 vs 具体论文（同域不同工作）----
    surv = _mk(hn, "hn_b_survey", "A Survey of Graph Neural Networks",
               ["We survey message passing architectures across 200 papers."],
               "2002.00001")
    spec = _mk(hn, "hn_b_specific", "Graph Neural Networks Survey",
               ["We survey concrete methodological aspects of message passing layers."],
               "2002.00002")
    rows.append((surv, spec, 0))

    # ---- 难负例 c：同题不同作者不同文（同题必须能被内容特征否决）----
    same1 = _mk(hn, "hn_c_1", "On the Effectiveness of Weight Decay",
                ["Group A studies AdamW style decoupled decay on vision transformers."],
                "2003.00001")
    same2 = _mk(hn, "hn_c_2", "On the Effectiveness of Weight Decay",
                ["Group B analyzes Bayesian L2 regularizers for recurrent models."],
                "2003.00002")
    rows.append((same1, same2, 0))

    # ---- 悬殊异题负例（8 篇互不相关 + 跨族）----
    neg = tmp / "neg"
    neg.mkdir(exist_ok=True)
    topics = [
        ("Bayesian Optimization Handbook", "Gaussian process surrogates guide acquisition."),
        ("Reinforcement Learning Robotics", "Policy gradient methods solve manipulation."),
        ("Speech Recognition Frontend", "Conformer blocks improve phoneme error rate."),
        ("Protein Structure Prediction", "Equivariant attention folds amino chains."),
        ("Quantum Error Correction", "Surface codes suppress logical failure rates."),
        ("Autonomous Driving Planning", "Behavior cloning lags closed-loop margins."),
        ("Cryptographic Protocols Survey", "Zero-knowledge proofs scale with SNARKs."),
        ("Neural Radiance Caching", "Screen space buffers guide radiance regression."),
    ]
    neg_rows = []
    for i, (title, fact) in enumerate(topics):
        neg_rows.append(_mk(neg, f"neg_{i}", title, [fact], f"20{i:02d}.12345"))
    for i in range(len(neg_rows)):
        for j in range(i + 1, len(neg_rows)):
            rows.append((neg_rows[i], neg_rows[j], 0))
    for f in fam_rows + hn_a + [surv, spec, same1, same2]:
        for n in neg_rows[:4]:
            rows.append((f, n, 0))

    # ---- 真素材正例（v7 vs v1 真解析）——存在时并入 ----
    dl = Path(__file__).resolve().parents[2] / "downloads" / "1706.03762"
    v7, v1 = dl / "1706.03762v7.pdf", dl / "1706.03762v1.pdf"
    if v7.exists() and v1.exists():
        rows.append((_ingest(v7), _ingest(v1), 1))
        print(f"[cal] 真素材对并入：{v7.name} vs {v1.name}")
    return rows


def _features(a: dict, b: dict, disable: set[str] | None = None) -> tuple[float, dict]:
    s, bd = jobs._score_pair(a, b, rerank_budget=[0], disable=disable)
    return s, bd


def _prf(scores: list[tuple[float, int]], th: float) -> tuple[float, float, float, int, int]:
    tp = sum(1 for s, y in scores if s >= th and y == 1)
    fp = sum(1 for s, y in scores if s >= th and y == 0)
    fn = sum(1 for s, y in scores if s < th and y == 1)
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f1, fp, fn


def _ablation(data: list[tuple[dict, dict, int]], th: float) -> None:
    """分量消融：逐一 disable 后的 F1 变化（离线无 embedding/rerank/LLM 的分量
    天然缺失，消融差如实为 0——线上有凭据时该表才有区分度，此处演示方法论）。"""
    print(f"\n[cal] 分量消融（阈值 {th:.2f}，逐一 disable 后 F1）：")
    base_f1 = _prf([(_features(a, b)[0], y) for a, b, y in data], th)[2]
    print(f"    全特征           F1={base_f1:.3f}")
    all_bd = [_features(a, b)[1] for a, b, _ in data]
    for feat in jobs._WEIGHTS:
        f1 = _prf([(_features(a, b, disable={feat})[0], y) for a, b, y in data], th)[2]
        delta = f1 - base_f1
        if abs(delta) > 1e-9:
            note = ""
        elif not any(feat in bd for bd in all_bd):
            note = "（本环境缺失，线上启用后此处才见区分度）"
        else:
            note = "（冗余：其余特征已足以区分本标注集）"
        print(f"    -{feat:<12} F1={f1:.3f}（Δ{delta:+.3f}）{note}")


def _fit_lr(X: np.ndarray, y: np.ndarray, l2: float = 1e-2,
            iters: int = 100) -> np.ndarray:
    """numpy IRLS 逻辑回归（无 sklearn 依赖）：小样本 + L2 正则。
    X 为已含截距列的设计矩阵，返回 [截距, 系数...]。学习权重可解释
    （每维证据贡献），与手写权重对照写入答辩材料。"""
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(X @ w, -30, 30)))
        grad = X.T @ (y - p) - l2 * w
        Wd = p * (1 - p) + 1e-9
        hess = (X * Wd[:, None]).T @ X + l2 * np.eye(X.shape[1])
        try:
            step = np.linalg.solve(hess, grad)
        except np.linalg.LinAlgError:
            break
        w += step
        if float(np.max(np.abs(step))) < 1e-8:
            break
    return w


def _lr_report(data: list[tuple[dict, dict, int]]) -> None:
    """LR 学习权重 vs 手写权重对照。特征向量用 breakdown 原始分量（缺失→0 +
    可用性掩码分离），不含确定性同源抬底——LR 学「证据权重」而非「抬底规则」。
    全程缺失的特征（离线无 embedding/rerank）从设计中剔除，避免退化列。"""
    feats = list(jobs._WEIGHTS)
    X, mask, y = [], [], []
    for a, b, label in data:
        _s, bd = _features(a, b)
        X.append([float(bd.get(k, 0.0)) for k in feats])
        mask.append([1.0 if k in bd else 0.0 for k in feats])
        y.append(float(label))
    X, mask, y = np.array(X), np.array(mask), np.array(y)
    keep = [i for i in range(len(feats)) if mask[:, i].any()]
    pruned = [feats[i] for i in range(len(feats)) if i not in keep]
    X1 = np.hstack([np.ones((len(X), 1)), (X * mask)[:, keep]])
    w = _fit_lr(X1, y)
    print("\n[cal] LR 学习权重 vs 手写权重（特征缺失置 0 并以掩码告知模型；"
          f"全程缺失已剔除：{pruned or '无'}）：")
    print(f"    {'特征':<12} {'手写':>6} {'LR':>7}")
    print(f"    {'截距':<12} {'--':>6} {w[0]:>7.2f}")
    for j, i in enumerate(keep):
        print(f"    {feats[i]:<12} {jobs._WEIGHTS[feats[i]]:>6.2f} {w[j + 1]:>7.2f}")
    acc = float(np.mean(((1 / (1 + np.exp(-X1 @ w))) >= 0.5) == (y == 1)))
    print(f"    训练集准确率 {acc:.2f}（样本 {len(y)}；正例多为同源抬底，小样本下 "
          f"LR 结论仅示意，线上阈值以手写权重 + P/R/F1 扫描为准）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresholds", type=float, nargs="+",
                    default=[0.80, 0.85, 0.90, 0.92, 0.95])
    args = ap.parse_args()

    db.init_db()
    data = build_dataset()
    print(f"\n[cal] 标注集：{len(data)} 对（正 {sum(1 for *_, y in data if y)} / "
          f"负 {sum(1 for *_, y in data if not y)}，其中难负例 5 对）\n")

    scores = [(_features(a, b)[0], y) for a, b, y in data]
    print(f"{'阈值':>6} | {'P':>6} {'R':>6} {'F1':>6} | FP/FN")
    print("-" * 44)
    best = None
    for th in args.thresholds:
        p, r, f1, fp, fn = _prf(scores, th)
        print(f"{th:>6.2f} | {p:>6.2f} {r:>6.2f} {f1:>6.2f} | {fp}/{fn}")
        if best is None or f1 > best[1]:
            best = (th, f1)
    from app.config import settings
    print(f"\n[cal] 推荐 dedup_threshold = {best[0]:.2f}（F1={best[1]:.2f}）；"
          f"当前 settings.dedup_threshold={settings.dedup_threshold}")
    print("[cal] 同族对分数与分量：")
    for a, b, y in data:
        if y != 1:
            continue
        s, bd = _features(a, b)
        comp = " · ".join(f"{k}={v}" for k, v in bd.items())
        print(f"    {s:.3f} [{comp}]")
    hn_scores = [(_features(a, b)[0], (a['title'][:20], b['title'][:20]))
                 for a, b, y in data if y == 0]
    hn_scores.sort(reverse=True)
    print("[cal] 负例分数 Top5（应低于阈值；难负例在前列是设计意图的试金石）：")
    for s, names in hn_scores[:5]:
        print(f"    {s:.3f} {names[0]} <-> {names[1]}")
    _ablation(data, best[0])
    _lr_report(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
