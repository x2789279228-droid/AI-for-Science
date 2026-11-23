#!/usr/bin/env python
"""纵向长程对话 A/B 评测（模块七 r9）：有记忆感知 vs 无记忆感知。

设计：
- 同一记忆池（seed 演示库四条故事线）+ 同一篇论文（检索底座），同一探针问题集；
- 新系统口径：POST /api/chat {use_memory: true} —— 简报 + 记忆时间线注入 + [M#]；
- 旧系统口径：{use_memory: false} —— 整条记忆链关闭（不读不写，r9 公平性修复）；
- 每题真实调用 doubao 生成（流式 SSE 收集 final.answer），追问轮复用其依赖题的会话；
- 三重评分：锚点规则分（客观）/ 陈旧陷阱泄漏 / LLM judge 0-3 分（附 judge 依据）。

用法（先启动 seed 实例后端）：
  SCHOLAR_DATA_DIR=data/seed_mem python -m uvicorn app.main:app --port 8101
  python api/scripts/eval_ab_longterm.py --base-url http://localhost:8101 \
      --gold api/scripts/ab_probe_questions.json --out docs/acceptance/round9-20261121-r9
产出：ab_answers/*.txt（原始回答存档）+ ab_eval.json + ab_report.md。数字真实。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

TOP_K = 5


def chat_once(base_url: str, paper_id: str, question: str, conversation_id: str,
              use_memory: bool, timeout_s: float = 180.0) -> dict:
    """真实调用 /api/chat（流式 SSE），返回 {answer, m_citations, elapsed_s}。"""
    t0 = time.time()
    answer_parts: list[str] = []
    final_answer = ""
    m_refs = 0
    with httpx.Client(timeout=timeout_s) as client, client.stream(
            "POST", f"{base_url}/api/chat",
            json={"question": question, "paper_ids": [paper_id],
                  "conversation_id": conversation_id,
                  "agent": "auto", "doc_mode": "paper",
                  "thinking": False, "use_memory": use_memory}) as resp:
            event = ""
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    event = line[7:].strip()
                elif line.startswith("data: ") and event:
                    try:
                        data = json.loads(line[6:])
                    except ValueError:
                        data = {}
                    if event == "delta":
                        answer_parts.append(str(data.get("text", "")))
                    elif event == "final":
                        final_answer = str(data.get("answer", ""))
                        m_refs = len(data.get("memory_anchors") or {})
                    elif event == "done":
                        break
    answer = final_answer if final_answer else "".join(answer_parts)
    return {"answer": answer, "m_citations": m_refs,
            "elapsed_s": round(time.time() - t0, 1)}


def score_answer(q: dict, answer: str) -> dict:
    """客观规则分：锚点命中 + 陈旧陷阱泄漏。"""
    anchors = {a: (a in answer) for a in q.get("anchors", [])}
    leaks = [t for t in q.get("stale_traps", []) if t in answer]
    hits = list(anchors.values())
    return {"anchors": anchors,
            "anchor_rate": round(sum(hits) / len(hits), 4) if hits else None,
            "stale_leak": bool(leaks), "stale_terms": leaks}


def judge_answer(question: str, gold_points: list[str], answer: str,
                 stale_note: str) -> dict:
    """LLM judge 0-3 分（真实 doubao）。judge 失败如实返回 None。"""
    from app.config import settings
    from app.llm import client as llm
    _ = settings
    if not llm.available():
        return {"score": None, "stale_repeated": None,
                "reason": "LLM 不可用，judge 未评测（诚实标注）"}
    prompt = (
        "你是严格评测员。给定「用户问题」「标准要点」「系统回答」，评判回答质量。\n"
        "评分 0-3：0=回答与标准要点矛盾或编造事实；1=未命中任何要点且泛泛而谈；"
        "2=命中部分要点且无硬伤；3=命中全部要点且方向正确。\n"
        "另判定 stale_repeated：回答是否把已被推翻/取代的旧结论当作当前有效结论复述"
        f"（{stale_note or '对照标准要点判断'}）。\n"
        '只输出 JSON：{"score": 0-3, "stale_repeated": true|false, "reason": "≤60字"}')
    try:
        res = llm.chat(
            [{"role": "system", "content": prompt},
             {"role": "user", "content":
              f"<question>{question}</question>\n"
              f"<gold_points>\n" + "\n".join(f"- {g}" for g in gold_points) +
              "\n</gold_points>\n<answer>\n" + answer[:2500] + "\n</answer>"}],
            json_mode=True, max_tokens=200, module="ab_judge",
            audit_prompt=question[:100], prompt_version="ab-r9")
    except Exception as e:  # noqa: BLE001 —— judge 失败如实标注
        return {"score": None, "stale_repeated": None, "reason": f"judge 异常: {e}"}
    if not res.ok:
        return {"score": None, "stale_repeated": None, "reason": "judge 返回空"}
    import re

    m = re.search(r"\{.*\}", res.get("text") or "", re.DOTALL)
    try:
        data = json.loads(m.group(0)) if m else {}
    except (ValueError, TypeError):
        return {"score": None, "stale_repeated": None, "reason": "judge 输出不可解析"}
    score = data.get("score")
    return {"score": score if isinstance(score, int) and 0 <= score <= 3 else None,
            "stale_repeated": bool(data.get("stale_repeated")),
            "reason": str(data.get("reason") or "")[:120]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://localhost:8101")
    ap.add_argument("--data-dir", default="data/seed_mem")
    ap.add_argument("--gold", default="scripts/ab_probe_questions.json")
    ap.add_argument("--out", default="../docs/acceptance/round9-20261121-r9")
    args = ap.parse_args()

    _boot(Path(args.data_dir))
    from app.db import query as db_query
    from app.db import tx

    # 基线重置：删除评测期（ab_* 会话）轮末抽取写入的记忆——mem 口径每轮问答会
    # 经抽取钩子产生新事件，其 recency（ts=now）会压倒 30 天前的事故线事件，
    # 造成跨轮不可复现（r9 第五轮实测）。重置后每轮从相同的故事线基线出发；
    # 运行期内的动态写入保留（真实长程行为，报告中如实说明）。
    with tx() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM memory_events WHERE conversation_id LIKE 'ab|_%' ESCAPE '|'")]
        for i in ids:
            c.execute("DELETE FROM memory_edges WHERE src=? OR dst=?", (i, i))
        c.execute("DELETE FROM memory_events WHERE conversation_id LIKE 'ab|_%' ESCAPE '|'")
    print(f"[ab] 基线重置：清除评测期残留 {len(ids)} 条")

    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    prow = db_query("SELECT id FROM papers WHERE status='active' LIMIT 1")
    paper_id = prow[0]["id"] if prow else ""
    if not paper_id:
        print("[ab] seed 库无论文——先运行 prepare_ab_env.py")
        return 2

    out_dir = Path(args.out)
    ans_dir = out_dir / "ab_answers"
    ans_dir.mkdir(parents=True, exist_ok=True)

    order = [q for q in gold["questions"] if not q.get("depends_on")]
    order += [q for q in gold["questions"] if q.get("depends_on")]

    conv_of: dict[tuple[str, str], str] = {}
    results: list[dict] = []
    for q in order:
        dep = q.get("depends_on")
        for mode in ("mem", "base"):
            conv_key = (mode, dep or q["id"])
            conv_id = conv_of.get(conv_key) or f"ab_{mode}_{uuid4hex()[:8]}"
            conv_of[(mode, q["id"])] = conv_id
            use_memory = mode == "mem"
            t0 = time.time()
            try:
                out = chat_once(args.base_url, paper_id, q["question"], conv_id, use_memory)
            except Exception as e:  # noqa: BLE001 —— 单题失败不中断整体
                results.append({"qid": q["id"], "mode": mode, "error": str(e)[:200]})
                print(f"[ab] {q['id']}/{mode} 失败: {e}")
                continue
            (ans_dir / f"{mode}_{q['id']}.txt").write_text(out["answer"], encoding="utf-8")
            rule = score_answer(q, out["answer"])
            judge = judge_answer(q["question"], q["gold_points"], out["answer"],
                                 q.get("stale_note", ""))
            results.append({
                "qid": q["id"], "mode": mode, "question": q["question"],
                "conversation_id": conv_id,
                "elapsed_s": out["elapsed_s"], "gen_elapsed_s": out["elapsed_s"],
                "t_start": round(t0, 1),
                "anchor_rate": rule["anchor_rate"], "anchors": rule["anchors"],
                "stale_leak": rule["stale_leak"], "stale_terms": rule["stale_terms"],
                "judge_score": judge["score"], "judge_stale_repeated": judge["stale_repeated"],
                "judge_reason": judge["reason"],
                "m_citations": out["m_citations"],
                "answer_chars": len(out["answer"]),
            })
            print(f"[ab] {q['id']}/{mode}: anchor={rule['anchor_rate']} "
                  f"stale={rule['stale_leak']} judge={judge['score']} "
                  f"M#={out['m_citations']} ({out['elapsed_s']}s)")

    summary = _summarize(results)
    report = {"meta": {"base_url": args.base_url, "paper_id": paper_id,
                       "generated_at": datetime.now(UTC).isoformat(),
                       "honest": "模拟语料 + 单 judge + 小样本（n=4 探针×2 口径），结果不外推",
                       "judge_model": "doubao-seed-2.1-lite"},
              "summary": summary, "results": results}
    (out_dir / "ab_eval.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    md = _render_md(report)
    (out_dir / "ab_report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


def uuid4hex() -> str:
    import uuid
    return uuid.uuid4().hex


def _summarize(results: list[dict]) -> dict:
    """按口径汇总：锚点率 / judge 均分 / 陈旧泄漏数 / M# 引用。"""
    out: dict[str, dict] = {}
    for mode in ("mem", "base"):
        rows = [r for r in results if r.get("mode") == mode and "error" not in r]
        judged = [r for r in rows if r.get("judge_score") is not None]
        out[mode] = {
            "n": len(rows),
            "anchor_rate_avg": round(sum(r["anchor_rate"] for r in rows
                                         if r.get("anchor_rate") is not None)
                                     / max(1, sum(1 for r in rows
                                                  if r.get("anchor_rate") is not None)), 4),
            "judge_score_avg": round(sum(r["judge_score"] for r in judged)
                                     / len(judged), 3) if judged else None,
            "stale_leak_count": sum(1 for r in rows if r.get("stale_leak")),
            "judge_stale_repeated_count": sum(1 for r in rows
                                              if r.get("judge_stale_repeated")),
            "m_citations_total": sum(r.get("m_citations", 0) for r in rows),
            "avg_elapsed_s": round(sum(r.get("elapsed_s", 0) for r in rows)
                                   / max(1, len(rows)), 1),
        }
    return out


def _render_md(r: dict) -> str:
    s = r["summary"]
    mem, base = s.get("mem", {}), s.get("base", {})
    lines = [
        "# r9 · 纵向长程对话 A/B 对比：有记忆感知 vs 无记忆感知", "",
        (f"> 语料：30 会话记忆故事线 + 1 篇真实论文（{r['meta']['paper_id']}）· "
         f"判定模型 {r['meta']['judge_model']} · {r['meta']['honest']}"), "",
        "## 逐题明细", "",
        "| 探针 | 口径 | 锚点率 | 陈旧泄漏 | judge | M# 引用 | 用时 |",
        "|---|---|---|---|---|---|---|",
    ]
    for res in r["results"]:
        if "error" in res:
            lines.append(f"| {res['qid']} | {res['mode']} | 失败: {res['error'][:40]} | - | - | - | - |")
            continue
        lines.append(
            f"| {res['qid']} | {'有记忆' if res['mode'] == 'mem' else '无记忆'} "
            f"| {_fmt_rate(res.get('anchor_rate'))} "
            f"| {'是(' + ','.join(res.get('stale_terms', [])) + ')' if res.get('stale_leak') else '否'} "
            f"| {res.get('judge_score') if res.get('judge_score') is not None else '未评'} "
            f"| {res.get('m_citations', 0)} | {res.get('elapsed_s', '—')}s |")
    lines += [
        "", "## 口径汇总", "",
        "| 指标 | 有记忆感知（新系统） | 无记忆感知（旧系统） |", "|---|---|---|",
        f"| 锚点命中率（均值） | {mem.get('anchor_rate_avg')} | {base.get('anchor_rate_avg')} |",
        f"| judge 均分（0-3） | {mem.get('judge_score_avg')} | {base.get('judge_score_avg')} |",
        f"| 陈旧泄漏题数（/4） | {mem.get('stale_leak_count')} | {base.get('stale_leak_count')} |",
        (f"| judge 判定陈旧复述题数 | {mem.get('judge_stale_repeated_count')} "
         f"| {base.get('judge_stale_repeated_count')} |"),
        f"| [M#] 记忆引用次数 | {mem.get('m_citations_total')} | {base.get('m_citations_total')}（机制不存在） |",
        f"| 平均端到端用时 | {mem.get('avg_elapsed_s')}s | {base.get('avg_elapsed_s')}s |",
        "", "原始回答存档：`ab_answers/`。数字真实，样本小不外推（n=4 探针 × 2 口径）。",
    ]
    return "\n".join(lines)


def _fmt_rate(v) -> str:
    return f"{v:.0%}" if isinstance(v, (int, float)) else "—"


def _boot(seed_data_dir: Path) -> None:
    import os
    os.environ["SCHOLAR_DATA_DIR"] = str(seed_data_dir)
    for m in [k for k in list(sys.modules) if k.startswith("app")]:
        del sys.modules[m]


if __name__ == "__main__":
    sys.exit(main())
