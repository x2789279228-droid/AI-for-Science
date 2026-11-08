"use client";

/**
 * 引用图谱（4.6，创新分最高项）：AntV G6 交互图谱。
 * - 节点大小 = PageRank、颜色 = Louvain 社区、边粗细 = 引用含金量（加权）
 * - 基石论文 🪨 / 边缘衍生 🌿 角色徽章（规则可解释，答辩可讲）
 * - 点击节点 → 侧栏详情（指标/角色/实质性引用句）
 * - 按社区生成演进综述（每句带 [E#] 锚点，经 ClaimTrace 逐论断校验）
 */
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Graph } from "@antv/g6";
import {
  FullGraph, GraphNode, fetchGraph, streamCommunitySummary, streamIdeas,
} from "@/lib/api";
import { IdeaItem } from "@/lib/api";

const PALETTE = ["#5B8FF9", "#61DDAA", "#F6BD16", "#7262fd", "#78D3F8", "#9661BC", "#F6903D", "#008685"];

const ROLE_BADGE: Record<string, string> = { cornerstone: "🪨 基石", peripheral: "🌿 边缘衍生" };

/** [E#] → 可点击 chip（悬停显示对应锚点信息）。 */
function EvidenceText({ text, anchors }: { text: string; anchors: Record<string, unknown> }) {
  const parts = text.split(/(\[E\d+\])/g);
  return (
    <p style={{ fontSize: 13, lineHeight: 1.8 }}>
      {parts.map((p, i) => {
        const m = p.match(/^\[E(\d+)\]$/);
        if (m) {
          const a = anchors[p] as { page?: number; section_path?: string } | undefined;
          return (
            <span key={i} className="chip anchor-chip" style={{ fontSize: 11 }}
              title={a ? `p${a.page} · §${a.section_path ?? ""}` : "锚点信息"}>
              [E{m[1]}]
            </span>
          );
        }
        return <span key={i}>{p}</span>;
      })}
    </p>
  );
}

export default function GraphPage() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const graphRef = useRef<Graph | null>(null);
  const [graph, setGraph] = useState<FullGraph | null>(null);
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [selectedEdges, setSelectedEdges] = useState<FullGraph["edges"]>([]);
  const [err, setErr] = useState("");

  // 社区综述
  const [commId, setCommId] = useState<number>(0);
  const [summary, setSummary] = useState("");
  const [claims, setClaims] = useState<Record<string, unknown>[]>([]);
  const [anchors, setAnchors] = useState<Record<string, unknown>>({});
  const [summarizing, setSummarizing] = useState(false);
  const [guardrails, setGuardrails] = useState<string[]>([]);

  // Future Work
  const [ideas, setIdeas] = useState<{ solved: IdeaItem[]; open: IdeaItem[] } | null>(null);
  const [ideasMsg, setIdeasMsg] = useState("");

  useEffect(() => {
    fetchGraph().then((g) => {
      setGraph(g);
      if (g.communities.length) setCommId(g.communities[0].id);
    }).catch((e) => setErr(String(e)));
  }, []);

  useEffect(() => {
    if (!graph || !containerRef.current) return;
    // G6 v5 约定：业务字段放 data 挂载点，style 回调从 d.data 读取
    const g6 = new Graph({
      container: containerRef.current,
      autoFit: "view",
      padding: 20,
      data: {
        nodes: graph.nodes.map((n) => ({ id: n.paper_id, data: { ...n } })),
        edges: graph.edges.map((e, i) => ({
          id: `e${i}`, source: e.source, target: e.target,
          data: { weight: e.weight, examples: e.examples },
        })),
      },
      node: {
        style: {
          size: (d) => {
            const n = (d as { data?: GraphNode }).data;
            return 14 + Math.min(34, (n?.pagerank ?? 0) * 220);
          },
          fill: (d) => PALETTE[((d as { data?: GraphNode }).data?.community ?? 0) % PALETTE.length],
          labelText: (d) => String((d as { data?: GraphNode }).data?.title ?? "").slice(0, 16),
          labelPlacement: "bottom",
          labelFontSize: 11,
          stroke: (d) => {
            const role = (d as { data?: GraphNode }).data?.role;
            return role === "cornerstone" ? "#cc0000" : role === "peripheral" ? "#00aa77" : "#ffffff";
          },
          lineWidth: (d) => ((d as { data?: GraphNode }).data?.role ? 2.5 : 1),
        },
      },
      edge: {
        style: {
          stroke: (d) => {
            const w = (d as { data?: { weight?: number } }).data?.weight ?? 0;
            return w >= 1.0 ? "#2563eb" : w > 0 ? "#94a3b8" : "#d4d4d8";
          },
          lineWidth: (d) => {
            const w = (d as { data?: { weight?: number } }).data?.weight ?? 0;
            return 1 + Math.min(5, w * 2);
          },
          endArrow: true,
        },
      },
      layout: { type: "d3-force", linkDistance: 130, nodeStrength: -400 },
      behaviors: ["drag-canvas", "zoom-canvas", "drag-element"],
    });
    graphRef.current = g6;
    g6.render().then(() => {
      // G6 v5 IEvent 目标节点 id 在 evt.target.id（类型宽松处，见 G6 5.x 文档事件示例）
      g6.on("node:click", (evt: any) => {
        const id = evt?.target?.id as string | undefined;
        if (!id) return;
        const n = graph.nodes.find((x) => x.paper_id === id);
        setSelected(n ?? null);
        setSelectedEdges(graph.edges.filter((e) => e.source === id || e.target === id));
      });
      g6.on("canvas:click", () => setSelected(null));
    }).catch((e) => setErr(`G6 渲染失败：${e}`));
    return () => { g6.destroy(); graphRef.current = null; };
  }, [graph]);

  const runSummary = () => {
    if (summarizing || !graph) return;
    setSummarizing(true);
    setSummary("");
    setClaims([]);
    setGuardrails([]);
    const paperIds = graph.nodes.map((n) => n.paper_id);
    streamCommunitySummary(paperIds, commId, {
      delta: (d) => setSummary((prev) => prev + d.text),
      final: (d) => {
        setSummary(d.summary);
        setClaims(d.claims ?? []);
        setAnchors(d.anchors ?? {});
        setGuardrails(d.guardrails ?? []);
        setSummarizing(false);
      },
      error: (d) => { setSummary(`（出错：${d.message}）`); setSummarizing(false); },
      done: () => setSummarizing(false),
    }).catch((e) => { setSummary(`（请求失败：${e}）`); setSummarizing(false); });
  };

  const runIdeas = () => {
    if (!graph || ideasMsg) return;
    setIdeasMsg("扫描 Conclusion/Limitation…");
    streamIdeas(graph.nodes.map((n) => n.paper_id), {
      status: (d) => setIdeasMsg(d.note),
      final: (d) => { setIdeas({ solved: d.solved, open: d.open }); setIdeasMsg(`共 ${d.total} 条想法`); },
      error: (d) => setIdeasMsg(`出错：${d.message}`),
      done: () => { },
    }).catch((e) => setIdeasMsg(`请求失败：${e}`));
  };

  return (
    <div className="page">
      <div className="page-inner">
        <h1>引用图谱 <span className="chip accent">PageRank · Louvain 社区 · 引用含金量</span></h1>
        {err && <div className="card"><p className="kv">{err}</p></div>}
        {graph && (
          <>
            <div className="card">
              <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", alignItems: "center" }}>
                <span className="chip">{graph.nodes.length} 节点 · {graph.edge_count} 边</span>
                <span className="chip">外部引用 {graph.external_count} 条</span>
                <span className="chip">backend {graph.backend}</span>
                {graph.modularity != null && (
                  <span className="chip">模块度 Q={graph.modularity}</span>
                )}
                {graph.insights?.cornerstone?.length > 0 && (
                  <span className="chip ok">🪨 基石 ×{graph.insights.cornerstone.length}</span>
                )}
                {graph.insights?.peripheral?.length > 0 && (
                  <span className="chip">🌿 边缘衍生 ×{graph.insights.peripheral.length}</span>
                )}
                {graph.insights?.class_stat && (
                  <span className="chip" title={`实质/背景/凑数 · 共 ${graph.insights.mention_count} 处引用标记`}>
                    含金量：实质 {graph.insights.class_stat.substantive ?? 0} ·
                    背景 {graph.insights.class_stat.background ?? 0} ·
                    凑数 {graph.insights.class_stat.perfunctory ?? 0}
                  </span>
                )}
                <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center" }}>
                  <select value={commId} onChange={(e) => setCommId(Number(e.target.value))}
                    style={{ border: "1px solid var(--line-strong)", borderRadius: 6, padding: "4px 8px" }}>
                    {graph.communities.map((c) => (
                      <option key={c.id} value={c.id}>社区 {c.id}（{c.size} 篇）</option>
                    ))}
                  </select>
                  <button className="btn primary" onClick={runSummary} disabled={summarizing}>
                    {summarizing ? "综述生成中…" : "生成社区综述"}
                  </button>
                  <button className="btn ghost" onClick={runIdeas}>研究缝隙分析</button>
                </span>
              </div>
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: "var(--space-4)", alignItems: "start" }}>
              <div className="card" style={{ height: 480, position: "relative" }}>
                <div ref={containerRef} style={{ width: "100%", height: "100%" }} />
                <div style={{ position: "absolute", right: 12, bottom: 10, fontSize: 11, opacity: 0.6 }}>
                  节点大小=PageRank · 颜色=社区 · 边粗细=含金量（红边=基石 / 绿边=边缘衍生）
                </div>
              </div>

              <div className="card">
                {selected ? (
                  <>
                    <h2 style={{ marginTop: 0 }}>{selected.title}</h2>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
                      {selected.role && (
                        <span className={`chip ${selected.role === "cornerstone" ? "ok" : ""}`}>
                          {ROLE_BADGE[selected.role] ?? selected.role}
                        </span>
                      )}
                      {selected.year && <span className="chip">{selected.year} 年</span>}
                      <span className="chip">社区 {selected.community}</span>
                      <Link className="chip accent" href={`/reader/${selected.paper_id}`}>打开阅读器 →</Link>
                    </div>
                    <div className="kv" style={{ lineHeight: 2 }}>
                      PageRank <b>{selected.pagerank}</b> · 被引 <b>{selected.in_degree}</b> ·
                      引出 <b>{selected.out_degree}</b> · 介数 <b>{selected.betweenness}</b>
                      {selected.time_rank != null && <> · 时间序位 <b>{selected.time_rank}</b></>}
                    </div>
                    <h2 style={{ fontSize: 13 }}>引用明细（实质性引用句）</h2>
                    {selectedEdges.flatMap((e) => e.examples ?? []).length === 0 && (
                      <p className="kv">无实质性引用句记录（边或为背景/凑数引用）。</p>
                    )}
                    {selectedEdges.flatMap((e) => e.examples ?? []).slice(0, 4).map((x, i) => (
                      <p key={i} className="kv" style={{ borderBottom: "1px dashed var(--line)",
                        padding: "4px 0", fontSize: 12 }}>
                        「{x.sentence.slice(0, 80)}」<span className="chip ok" style={{ fontSize: 10 }}>实质</span>
                      </p>
                    ))}
                  </>
                ) : (
                  <p className="kv">点击节点查看详情（年份 / 指标 / 角色 / 实质性引用句）。</p>
                )}
              </div>
            </div>

            {summary && (
              <div className="card">
                <h2>社区 {commId} 演进综述 <span className="chip accent">逐论断经 ClaimTrace 校验</span></h2>
                <EvidenceText text={summary} anchors={anchors} />
                {guardrails.length > 0 && (
                  <p className="kv" style={{ color: "var(--warn)" }}>guardrails: {guardrails.join(" · ")}</p>
                )}
                {claims.length > 0 && (
                  <div style={{ marginTop: 8 }}>
                    <b className="kv">论断校验（{claims.length} 条）</b>
                    {claims.map((c, i) => (
                      <p key={i} className="kv" style={{ fontSize: 12, borderBottom: "1px dashed var(--line)", padding: "4px 0" }}>
                        <span className={`chip ${c.status === "supported" ? "ok" : c.status === "unsupported" ? "warn" : ""}`}
                          style={{ fontSize: 10, marginRight: 8 }}>
                          {c.status === "supported" ? "✓" : c.status === "unsupported" ? "✗" : "?"} {String(c.status)}
                        </span>
                        {String(c.text ?? "").slice(0, 80)}
                      </p>
                    ))}
                  </div>
                )}
              </div>
            )}

            {ideas && (
              <div className="card">
                <h2>Future Work 去重过滤 <span className="chip accent">招牌创新：已失效 vs 值得做</span></h2>
                <p className="kv">{ideasMsg}</p>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-4)" }}>
                  <div>
                    <h3 style={{ margin: "6px 0" }}>已失效 / 有进展（{ideas.solved.length}）</h3>
                    {ideas.solved.map((x) => (
                      <div key={x.id} className="kv" style={{ border: "1px solid var(--line)",
                        borderRadius: 8, padding: 8, marginBottom: 8 }}>
                        <span className="chip" style={{ fontSize: 10 }}>{x.status}</span> {x.idea_text}
                        {x.evidence?.length > 0 && (
                          <p style={{ fontSize: 11, opacity: 0.7, margin: "4px 0 0" }}>
                            依据：{x.evidence[0]?.paper_id?.slice(0, 10)} p{x.evidence[0]?.page}
                            「{x.evidence[0]?.text.slice(0, 60)}」
                          </p>
                        )}
                      </div>
                    ))}
                    {ideas.solved.length === 0 && <p className="kv">暂无。</p>}
                  </div>
                  <div>
                    <h3 style={{ margin: "6px 0" }}>真正值得做的方向（{ideas.open.length}，按被引数升序）</h3>
                    {ideas.open.map((x) => (
                      <div key={x.id} className="kv" style={{ border: "1px solid var(--accent)",
                        borderRadius: 8, padding: 8, marginBottom: 8 }}>
                        <span className="chip ok" style={{ fontSize: 10 }}>open · 缝隙</span>{" "}
                        {x.idea_text}
                        <p style={{ fontSize: 11, opacity: 0.7, margin: "4px 0 0" }}>
                          来源 {x.source_paper.slice(0, 10)}
                          {x.anchor?.page ? ` p${x.anchor.page}` : ""} ·
                          库内被引 {x.cited_count} 篇 · {x.reason || "无后续跟进"}
                        </p>
                      </div>
                    ))}
                    {ideas.open.length === 0 && <p className="kv">暂无。</p>}
                  </div>
                </div>
              </div>
            )}
          </>
        )}
        {!graph && !err && <div className="card"><p className="kv">加载图谱中…</p></div>}
      </div>
    </div>
  );
}
