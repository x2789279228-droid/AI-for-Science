"""消融扩题金标验证（Round6）：40 题候选 × v7 解析全文——金标词逐字存在才收录。

金标词全部取自 Attention Is All You Need 正文真实用语（表 1/表 2/§5 训练细节/§6 结果）。
输出：每题验证结果 + 收录清单（eval_ablation 直接引用）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db
from app.rag.version_diff import load_docir_for


def pick_v7() -> str:
    rows = db.query("SELECT id, title FROM papers WHERE docir_path IS NOT NULL AND docir_path != '' "
                    "AND COALESCE(doc_type,'paper')='paper' ORDER BY created_at DESC")
    for r in rows:
        if "attention is all you need" in (r["title"] or "").lower():
            return r["id"]
    return rows[0]["id"] if rows else ""


CANDIDATES = [
    ("How many GPUs were the base models trained on?", ("P100",)),
    ("How long did the big model train for?", ("3.5",)),
    ("What BLEU score did the big model achieve on EN-DE?", ("28.4",)),
    ("What BLEU score did the big model achieve on EN-FR?", ("41.0",)),
    ("Which optimizer was used?", ("Adam",)),
    ("What beta2 value was used in Adam?", ("0.98",)),
    ("What beta1 value was used in Adam?", ("0.9",)),
    ("How many warmup steps were used in the learning rate schedule?", ("4000", "warmup")),
    ("How is the learning rate computed in the Transformer?", ("warmup",)),
    ("What is the model dimension d_model?", ("512",)),
    ("What is the dimension of the inner feed-forward layer?", ("2048",)),
    ("How many attention heads does the model use?", ("8",)),
    ("What dropout value is applied to the output of each sub-layer?", ("0.1",)),
    ("What label smoothing value was used during training?", ("0.1",)),
    ("What beam size was used at inference time?", ("beam", "4")),
    ("What length penalty alpha was used for EN-FR?", ("0.6",)),
    ("How many layers does the Transformer stack?", ("6", "six")),
    ("What is the dimension of keys d_k?", ("64",)),
    ("What is the dimension of values d_v?", ("64",)),
    ("What positional encoding does the model use?", ("sinusoid", "sinusoidal")),
    ("What is the per-layer computational complexity of self-attention?", ("n2", "O(n2)")),
    ("What is the maximum path length between long-range dependencies in self-attention?", ("O(1)",)),
    ("How many sequential operations does self-attention require?", ("O(1)",)),
    ("How many parameters does the base model have?", ("65",)),
    ("How many parameters does the big model have?", ("213",)),
    ("How many sentence pairs does WMT 2014 EN-DE consist of?", ("4.5",)),
    ("What shared vocabulary size was used for EN-DE?", ("37000",)),
    ("Which dataset was used for training and evaluation?", ("WMT",)),
    ("Do the embedding layers and the pre-softmax linear transformation share weights?", ("share", "shared")),
    ("What is the learning rate in the first warmup steps proportional to?", ("step", "warmup")),
    ("What activation does the position-wise feed-forward network use?", ("relu", "ReLU")),
    ("What regularization technique prevents overfitting besides dropout?", ("label smoothing", "smoothing")),
    ("How many checkpoints were averaged for the final model?", ("5", "last")),
    ("What language pairs were evaluated?", ("English", "German")),
    ("What is the batch size criterion during training?", ("sentence pairs", "tokens")),
    ("How many steps did the base model train?", ("100000", "100,000", "12 hours")),
    ("Which model outperformed all previously published single models on EN-FR?", ("41.0", "single models")),
    ("What computation does each attention head perform?", ("scaled dot-product", "dot-product")),
    ("What type of attention allows attending to all positions in previous timesteps of the decoder?", ("self-attention",)),
    ("What optimizer hyperparameter differs from the paper of Vaswani described schedule?", ("0.98",)),
    ("How does the architecture handle inputs of varying lengths?", ("variable length", "length")),
    ("Why can the model be trained with high parallelism?", ("parallel", "parallelization")),
    ("What replaces recurrent connections in this architecture?", ("attention", "recurrence")),
    ("What mechanism lets each head focus on different representation subspaces?", ("subspace", "heads")),
    ("How are words with similar meanings handled in the embedding?", ("positional encoding", "position")),
    ("What technique did the model use to handle rare words in translation?", ("byte-pair", "BPE", "word pieces")),
    ("How does the decoder generate output tokens at inference time?", ("autoregressive", "left to right")),
    ("What limits the computational cost as sequence length grows?", ("quadratic", "complexity")),
    ("How does the model combine information from multiple attention results?", ("concat", "concatenated", "project")),
]

if __name__ == "__main__":
    pid = pick_v7()
    assert pid, "v7 未解析"
    docir = load_docir_for(pid) or {}
    full = _norm = None
    text = "\n".join(b.get("text", "") for b in docir.get("blocks", []))
    tl = re.sub(r"\s+", "", text).lower()
    kept, rejected = [], []
    for q, golds in CANDIDATES:
        found = [g for g in golds if re.sub(r"\s+", "", g.lower()) in tl]
        if found:
            kept.append((q, golds))
            print(f"  ✓ {q[:60]} ← {found}")
        else:
            rejected.append((q, golds))
            print(f"  ✗ REJECT {q[:60]} ← {golds}")
    print(f"\n收录 {len(kept)}/{len(CANDIDATES)} 题")
    out = Path(__file__).parent / "goldset_47q.json"
    out.write_text(json.dumps({"questions": [list(x) for x in kept],
                               "rejected": [list(x) for x in rejected],
                               "paper_id": pid}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"已写出 {out}")
