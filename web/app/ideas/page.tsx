"use client";

/**
 * 研究缝隙（4.6 招牌创新）：Future Work 去重过滤。
 * 左栏「已失效 / 有进展」（solved/partial，附被谁解决的证据链）；
 * 右栏「真正值得做的方向」（open，按库内被引数升序 = 越没人做越靠前）。
 * 无库内后续论文引用的 idea 直接 open（没人跟进 = 缝隙还在，逻辑必然）。
 */
import { useEffect, useState } from "react";
import Link from "next/link";
import { IdeaItem, listPapers, streamIdeas } from "@/lib/api";

export default function IdeasPage() {
  const [paperIds, setPaperIds] = useState<string[]>([]);
  const [titles, setTitles] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");
  const [ideas, setIdeas] = useState<{ solved: IdeaItem[]; open: IdeaItem[]; total: number } | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    listPapers().then((ps) => {
      setPaperIds(ps.map((p) => p.id));
      setTitles(Object.fromEntries(ps.map((p) => [p.id, p.title])));
    });
  }, []);

  const run = () => {
    if (running || paperIds.length === 0) return;
    setRunning(true);
    setIdeas(null);
    setMsg("扫描 Conclusion/Limitation 章节，抽取作者明确提出的未来工作…");
    streamIdeas(paperIds, {
      status: (d) => setMsg(d.note),
      final: (d) => { setIdeas(d); setRunning(false); },
      error: (d) => { setMsg(`出错：${d.message}`); setRunning(false); },
      done: () => { },
    }).catch((e) => { setMsg(`请求失败：${e}`); setRunning(false); });
  };

  const titleOf = (pid: string) => titles[pid] ?? pid.slice(0, 10);

  return (
    <div className="page">
      <div className="page-inner">
        <h1>研究缝隙 <span className="chip accent">Future Work 去重过滤 · 招牌创新</span></h1>
        <div className="card">
          <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", alignItems: "center" }}>
            <span className="chip">{paperIds.length} 篇库内论文参与分析</span>
            <button className="btn primary" onClick={run} disabled={running || paperIds.length === 0}>
              {running ? "分析中…" : "开始分析"}
            </button>
            {msg && <span className="kv">{msg}</span>}
          </div>
          <p className="kv" style={{ marginTop: "var(--space-2)" }}>
            流水：尾章抽取想法（规则 + LLM 概括）→ 反向引用找「后续论文」→ 在后续论文全文检索该想法 →
            逐条判定 solved/partial/open（判定带证据链；无后续引用直接 open）。
          </p>
        </div>

        {ideas && (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-4)" }}>
            <div className="card">
              <h2>已失效 / 有进展（{ideas.solved.length}）</h2>
              {ideas.solved.map((x) => (
                <div key={x.id} className="kv" style={{ border: "1px solid var(--line)",
                  borderRadius: 8, padding: "var(--space-3)", marginBottom: 10 }}>
                  <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                    <span className={`chip ${x.status === "solved" ? "" : "warn"}`} style={{ fontSize: 10 }}>
                      {x.status === "solved" ? "已实现" : "部分进展"}
                    </span>
                    <span className="chip" style={{ fontSize: 10 }}>{titleOf(x.source_paper)}</span>
                    {x.anchor?.page && (
                      <Link className="chip" style={{ fontSize: 10 }}
                        href={`/reader/${x.source_paper}?page=${x.anchor.page}`}>
                        ↗ 原文 p{x.anchor.page}
                      </Link>
                    )}
                  </div>
                  <p style={{ margin: "8px 0 4px", fontSize: 13 }}>{x.idea_text}</p>
                  {x.reason && <p style={{ fontSize: 11, opacity: 0.7 }}>{x.reason}</p>}
                  {x.evidence?.length > 0 && (
                    <p style={{ fontSize: 11, opacity: 0.8, marginTop: 4 }}>
                      被谁解决：<b>{titleOf(x.evidence[0].paper_id)}</b> p{x.evidence[0].page} —
                      「{x.evidence[0].text.slice(0, 80)}」
                      <Link className="chip" style={{ fontSize: 10, marginLeft: 6 }}
                        href={`/reader/${x.evidence[0].paper_id}?page=${x.evidence[0].page}`}>查证 →</Link>
                    </p>
                  )}
                </div>
              ))}
              {ideas.solved.length === 0 && <p className="kv">暂无（库内后续文献尚未覆盖这些方向）。</p>}
            </div>

            <div className="card">
              <h2>真正值得做的方向（{ideas.open.length} · 按被引数升序）</h2>
              {ideas.open.map((x) => (
                <div key={x.id} className="kv" style={{ border: "1px solid var(--accent)",
                  borderRadius: 8, padding: "var(--space-3)", marginBottom: 10 }}>
                  <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                    <span className="chip ok" style={{ fontSize: 10 }}>open · 缝隙</span>
                    <span className="chip" style={{ fontSize: 10 }}>{titleOf(x.source_paper)}</span>
                    {x.anchor?.page && (
                      <Link className="chip" style={{ fontSize: 10 }}
                        href={`/reader/${x.source_paper}?page=${x.anchor.page}`}>
                        ↗ 原文 p{x.anchor.page}
                      </Link>
                    )}
                  </div>
                  <p style={{ margin: "8px 0 4px", fontSize: 13 }}>{x.idea_text}</p>
                  <p style={{ fontSize: 11, opacity: 0.8 }}>
                    库内被引 {x.cited_count} 篇 · {x.reason || "无后续论文跟进 = 缝隙仍在"}
                  </p>
                </div>
              ))}
              {ideas.open.length === 0 && <p className="kv">暂无。</p>}
            </div>
          </div>
        )}
        {!ideas && !running && (
          <div className="card"><p className="kv">点击「开始分析」运行。演示语料建议 3-4 篇同领域论文（如 Transformer + 后续改进）。</p></div>
        )}
      </div>
    </div>
  );
}
