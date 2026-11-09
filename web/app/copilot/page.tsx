"use client";

/**
 * 学术写作与数据可视化 Copilot（模块六）：
 * ① 写作：idea + 选中论文 → 无幻觉论文框架（固定章节 + References 白名单，
 *    正文 [R#] 与 References 由代码双向一致化，白名单外引用自动剔除并记 guardrails）
 * ② 数据可视化：CSV 上传（5MB/10万行护栏）→ table/line/bar/3D-radar（ECharts）
 *    + 学术 Figure Caption（数字全部可回溯数据集）
 * ③ 学术翻译：中文摘要/全文 → 英文学术语体（复用模块三双向管线）
 * ④ 拓扑图脚本：DiagramSpec → Mermaid/Graphviz DOT/TikZ 源码 + matplotlib PNG
 */
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import type { EChartsType } from "echarts";
import {
  ChartOut, DatasetMeta, DiagramOut, WritingOut, buildChart, datasetApi,
  generateDiagram, listPapers, streamWriting, zh2enTranslate,
} from "@/lib/api";
import type { Paper } from "@/lib/api";

type Tab = "write" | "viz" | "translate" | "diagram";

/** ECharts 客户端渲染（dynamic 导入防 SSR；type=table 由表格渲染；scatter3d 走 echarts-gl）。 */
function EChart({ spec }: { spec: ChartOut["spec"] }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const instRef = useRef<EChartsType | null>(null);
  useEffect(() => {
    let alive = true;
    (async () => {
      const mod = await import("echarts");
      const echarts = mod.default ?? mod;
      if (!alive || !ref.current) return;
      instRef.current?.dispose();
      const chart = echarts.init(ref.current);
      instRef.current = chart;
      const common = { tooltip: {}, legend: { top: 4 } };
      if (spec.type === "line" || spec.type === "bar") {
        chart.setOption({
          ...common,
          xAxis: { type: "category", name: spec.x?.name, data: spec.x?.data },
          yAxis: { type: "value" },
          series: (spec.series ?? []).map((s) => ({
            name: s.name, type: spec.type === "line" ? "line" : "bar",
            data: s.data, smooth: true,
          })),
        });
      } else if (spec.type === "radar") {
        chart.setOption({
          ...common,
          radar: { indicator: spec.indicators ?? [] },
          series: [{ type: "radar", data: (spec.series ?? []).map((s) => ({ name: s.name, value: s.data })) }],
        });
      } else if (spec.type === "scatter3d") {
        await import("echarts-gl");   // 真 3D：grid3D + scatter3D（注册后全局生效）
        const s3 = spec as unknown as { axes: string[]; visual_dim: string | null;
                                        points: (number | string)[][] };
        chart.setOption({
          tooltip: {},
          visualMap: s3.visual_dim
            ? { show: true, dimension: 3, inRange: { color: ["#5B8FF9", "#61DDAA", "#F6BD16", "#E5484D"] } }
            : undefined,
          xAxis3D: { type: "value", name: s3.axes[0] },
          yAxis3D: { type: "value", name: s3.axes[1] },
          zAxis3D: { type: "value", name: s3.axes[2] },
          grid3D: { boxWidth: 110, boxDepth: 110, viewControl: { autoRotate: false } },
          series: [{ type: "scatter3D", data: s3.points, symbolSize: 6 }],
        });
      }
    })();
    return () => { alive = false; instRef.current?.dispose(); instRef.current = null; };
  }, [spec]);
  return <div ref={ref} style={{ width: "100%", height: 360 }} />;
}

/** mermaid 源码预览（render 通过才显示，语法校验可视化）。 */
function MermaidPreview({ code }: { code: string }) {
  const [svg, setSvg] = useState("");
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({ startOnLoad: false, theme: "neutral" });
        const { svg } = await mermaid.render(`mmd-${Date.now()}`, code);
        if (alive) { setSvg(svg); setErr(""); }
      } catch (e) {
        if (alive) { setSvg(""); setErr(String(e)); }
      }
    })();
    return () => { alive = false; };
  }, [code]);
  if (err) return <p className="kv" style={{ color: "var(--warn)" }}>mermaid 校验失败：{err.slice(0, 120)}</p>;
  return svg ? <div dangerouslySetInnerHTML={{ __html: svg }} /> : <p className="kv">渲染中…</p>;
}

export default function CopilotPage() {
  const [tab, setTab] = useState<Tab>("write");
  const [papers, setPapers] = useState<Paper[]>([]);

  useEffect(() => { listPapers().then(setPapers).catch(() => {}); }, []);

  // ---- ① 写作框架 ----
  const [idea, setIdea] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [writing, setWriting] = useState<WritingOut | null>(null);
  const [writeNote, setWriteNote] = useState("");
  const [busy, setBusy] = useState(false);

  const runWriting = () => {
    if (!idea.trim() || !picked.length || busy) return;
    setBusy(true);
    setWriting(null);
    setWriteNote("构建 References 白名单…");
    streamWriting(idea.trim(), picked, {
      status: (d) => setWriteNote(d.note),
      final: (d) => { setWriting(d); setWriteNote(`完成：白名单 ${d.references.whitelist_size} 条 · 正文引用 ${d.references.used.length} 条`); setBusy(false); },
      error: (d) => { setWriteNote(`出错：${d.message}`); setBusy(false); },
    }).catch((e) => { setWriteNote(`请求失败：${e}`); setBusy(false); });
  };

  const exportWriting = (fmt: "md" | "html") => {
    if (!writing) return;
    const content = fmt === "md" ? writing.content_md
      : `<!doctype html><meta charset="utf-8"><body style="font-family:sans-serif;max-width:820px;margin:auto;line-height:1.8"><pre style="white-space:pre-wrap">${writing.content_md.replace(/</g, "&lt;")}</pre></body>`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([content], { type: `text/${fmt};charset=utf-8` }));
    a.download = `draft.${fmt}`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  // ---- ② 数据可视化 ----
  const [datasets, setDatasets] = useState<DatasetMeta[]>([]);
  const [dsId, setDsId] = useState("");
  const [chart, setChart] = useState<ChartOut | null>(null);
  const [chartType, setChartType] = useState<"table" | "line" | "bar" | "radar" | "scatter3d">("bar");
  const [xCol, setXCol] = useState("");
  const [yCols, setYCols] = useState<string[]>([]);
  const [vizNote, setVizNote] = useState("");

  const loadDatasets = () => datasetApi.list().then(setDatasets).catch(() => {});
  useEffect(() => { if (tab === "viz" && !datasets.length) void loadDatasets(); }, [tab]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const ds = datasets.find((d) => d.id === dsId);
    if (ds) {
      setXCol(ds.columns[0]?.name ?? "");
      setYCols(ds.columns.filter((c) => c.dtype === "number").map((c) => c.name));
    }
  }, [dsId, datasets]);

  const uploadCsv = async (f: File | null) => {
    if (!f) return;
    setVizNote("上传并校验中…");
    try {
      const out = await datasetApi.upload(f);
      setVizNote(`已上传 ${out.name}（${out.row_count} 行${out.note ? " · " + out.note : ""}）`);
      await loadDatasets();
      setDsId(out.id);
    } catch (e) { setVizNote(`上传失败：${e}`); }
  };

  const runChart = async () => {
    if (!dsId) return;
    setVizNote("生成中…");
    try {
      const out = await buildChart({ dataset_id: dsId, chart_type: chartType,
        x: xCol || undefined, y: yCols });
      setChart(out);
      setVizNote("");
    } catch (e) { setVizNote(`出错：${e}`); }
  };

  // ---- ③ 学术翻译 ----
  const [zhText, setZhText] = useState("");
  const [enOut, setEnOut] = useState("");
  const [trNote, setTrNote] = useState("");

  const runTranslate = async () => {
    if (!zhText.trim()) return;
    setTrNote("翻译中（中→英 · 学术语体 · 公式/引用保留）…");
    setEnOut("");
    try {
      const out = await zh2enTranslate(zhText.trim());
      setEnOut(out.translation);
      setTrNote(out.ok ? "完成" : `未能交付：${out.reason ?? "翻译不可用"}`);
    } catch (e) { setTrNote(`出错：${e}`); }
  };

  // ---- ④ 拓扑图 ----
  const [archDesc, setArchDesc] = useState("");
  const [diagram, setDiagram] = useState<DiagramOut | null>(null);
  const [dgNote, setDgNote] = useState("");

  const runDiagram = async () => {
    if (!archDesc.trim()) return;
    setDgNote("解析架构描述…");
    try {
      const out = await generateDiagram(archDesc.trim());
      setDiagram(out);
      setDgNote(`完成：${out.spec.nodes.length} 节点 · ${out.spec.edges.length} 边`);
    } catch (e) { setDgNote(`出错：${e}`); }
  };

  const copySrc = (s: string) => { navigator.clipboard?.writeText(s); };

  return (
    <div className="page">
      <div className="page-inner">
        <h1>Copilot <span className="chip accent">无幻觉写作 · 数据可视化 · 学术翻译</span></h1>

        <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", marginBottom: "var(--space-3)" }}>
          {([["write", "① 论文框架"], ["viz", "② CSV 可视化"],
             ["translate", "③ 中→英学术翻译"], ["diagram", "④ 拓扑图脚本"]] as [Tab, string][]).map(([t, label]) => (
            <button key={t} className={`chip ${tab === t ? "accent" : ""}`}
              style={{ cursor: "pointer" }} onClick={() => setTab(t)}>{label}</button>
          ))}
        </div>

        {tab === "write" && (
          <>
            <div className="card">
              <h2 style={{ marginTop: 0 }}>无幻觉论文框架</h2>
              <p className="kv">
                固定章节 Abstract → Introduction → Related Work → Method → Experiments → Conclusion → References；
                References 只来自选中论文的真实参考文献与库内条目（DOI/arXiv/标题归一去重），
                正文 [R#] 白名单外引用自动剔除；References 由代码从正文实际引用组装（双向一致，0 编造）。
              </p>
              <input value={idea} onChange={(e) => setIdea(e.target.value)}
                placeholder="研究 idea，如：检索增强生成的证据可信度评测"
                style={{ width: "100%", border: "1px solid var(--line-strong)", borderRadius: "var(--radius-sm)",
                  padding: "10px 12px", fontFamily: "var(--font)", outline: "none" }} />
              <div style={{ marginTop: 10, maxHeight: 130, overflowY: "auto", display: "flex", gap: 6, flexWrap: "wrap" }}>
                {papers.map((p) => (
                  <label key={p.id} className="chip" style={{ cursor: "pointer", display: "flex", gap: 4, fontSize: 11 }}>
                    <input type="checkbox" checked={picked.includes(p.id)}
                      onChange={() => setPicked((prev) => prev.includes(p.id)
                        ? prev.filter((x) => x !== p.id) : [...prev, p.id])} />
                    {(p.title || p.id).slice(0, 30)}
                  </label>
                ))}
              </div>
              <div style={{ display: "flex", gap: 8, marginTop: 10, alignItems: "center", flexWrap: "wrap" }}>
                <button className="btn primary" onClick={runWriting} disabled={busy || !picked.length}>
                  {busy ? "生成中…" : "生成论文框架"}
                </button>
                <span className="kv">已选 {picked.length} 篇证据论文</span>
                {writing && (
                  <>
                    <button className="chip" style={{ cursor: "pointer" }} onClick={() => exportWriting("md")}>导出 MD</button>
                    <button className="chip" style={{ cursor: "pointer" }} onClick={() => exportWriting("html")}>导出 HTML</button>
                  </>
                )}
              </div>
              {writeNote && <p className="kv" style={{ marginTop: 8 }}>{writeNote}</p>}
              {writing?.guardrails?.length ? (
                <p className="kv" style={{ color: "var(--warn)" }}>
                  guardrails（自动剔除的白名单外引用/超证据标记）：{writing.guardrails.slice(0, 6).join(" · ")}
                </p>
              ) : null}
            </div>

            {writing?.sections.map((s) => (
              <div className="card" key={s.title}>
                <h2 style={{ marginTop: 0, fontSize: 15 }}>{s.title}
                  {s.deterministic && <span className="chip ok" style={{ fontSize: 10, marginLeft: 8 }}>代码组装 · 0 编造</span>}
                  {s.evidence_count != null && s.evidence_count < 3 && (
                    <span className="chip warn" style={{ fontSize: 10, marginLeft: 8 }}>证据不足</span>
                  )}
                </h2>
                <p style={{ fontSize: 13, lineHeight: 1.8, whiteSpace: "pre-wrap" }}>{s.content}</p>
              </div>
            ))}
            {writing && (
              <div className="card">
                <h2 style={{ fontSize: 14 }}>白名单审计：正文引用 {writing.references.used.length} /
                  白名单 {writing.references.whitelist_size} 条（未引用 {writing.references.unused_count} 条备选）
                </h2>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {writing.references.used.map((r) => (
                    <span key={r.ref_id} className="chip ok" style={{ fontSize: 10 }}
                      title={`${r.authors} ${r.year ? `(${r.year})` : "(年份缺失)"} ${r.venue}${r.arxiv_id ? " · arXiv:" + r.arxiv_id : ""}`}>
                      [R{r.ref_id}] {r.title.slice(0, 40)}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </>
        )}

        {tab === "viz" && (
          <>
            <div className="card">
              <h2 style={{ marginTop: 0 }}>CSV 数据可视化</h2>
              <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", alignItems: "center" }}>
                <input type="file" accept=".csv" onChange={(e) => uploadCsv(e.target.files?.[0] ?? null)}
                  style={{ fontSize: 12 }} />
                {datasets.length > 0 && (
                  <select value={dsId} onChange={(e) => setDsId(e.target.value)}
                    style={{ border: "1px solid var(--line-strong)", borderRadius: 6, padding: "6px 8px" }}>
                    {datasets.map((d) => (
                      <option key={d.id} value={d.id}>{d.name}（{d.row_count} 行）</option>
                    ))}
                  </select>
                )}
                {(["table", "line", "bar", "radar", "scatter3d"] as const).map((t) => (
                  <button key={t} className={`chip ${chartType === t ? "accent" : ""}`}
                    style={{ cursor: "pointer" }} onClick={() => setChartType(t)}
                    title={t === "radar" ? "多维指标雷达（ECharts radar，平面多维轴）"
                      : t === "scatter3d" ? "真 3D 散点（echarts-gl grid3D，可拖拽旋转）" : undefined}>
                    {t === "radar" ? "雷达（多维）" : t === "scatter3d" ? "3D 散点"
                      : t === "table" ? "表格" : t === "line" ? "折线" : "柱状"}
                  </button>
                ))}
                <button className="btn primary" onClick={runChart} disabled={!dsId}>生成图表 + 图注</button>
              </div>
              {dsId && (() => {
                const ds = datasets.find((d) => d.id === dsId);
                return ds ? (
                  <div style={{ marginTop: 8 }}>
                    <span className="kv" style={{ fontSize: 11 }}>X 轴：</span>
                    <select value={xCol} onChange={(e) => setXCol(e.target.value)}
                      style={{ border: "1px solid var(--line-strong)", borderRadius: 6, padding: "4px 6px", fontSize: 12 }}>
                      {ds.columns.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
                    </select>
                    <span className="kv" style={{ fontSize: 11, marginLeft: 12 }}>Y 数值列：</span>
                    {ds.columns.filter((c) => c.dtype === "number").map((c) => (
                      <label key={c.name} className="chip" style={{ fontSize: 11, cursor: "pointer", marginLeft: 4 }}>
                        <input type="checkbox" checked={yCols.includes(c.name)}
                          onChange={() => setYCols((prev) => prev.includes(c.name)
                            ? prev.filter((x) => x !== c.name) : [...prev, c.name])} />
                        {c.name}
                      </label>
                    ))}
                  </div>
                ) : null;
              })()}
              {vizNote && <p className="kv" style={{ marginTop: 8 }}>{vizNote}</p>}
            </div>

            {chart && (
              <div className="card">
                <h2 style={{ marginTop: 0, fontSize: 15 }}>{chart.spec.title}
                  <span className="chip" style={{ fontSize: 10, marginLeft: 8 }}>{chart.spec.type}</span>
                </h2>
                {chart.spec.type === "table" ? (
                  <div style={{ overflowX: "auto" }}>
                    <table style={{ borderCollapse: "collapse", fontSize: 12, width: "100%" }}>
                      <thead><tr>{(chart.spec.columns ?? []).map((c) => (
                        <th key={c} style={{ border: "1px solid var(--line)", padding: "4px 8px", textAlign: "left" }}>{c}</th>
                      ))}</tr></thead>
                      <tbody>{(chart.spec.rows ?? []).slice(0, 30).map((r, i) => (
                        <tr key={i}>{r.map((cell, j) => (
                          <td key={j} style={{ border: "1px solid var(--line)", padding: "4px 8px" }}>{cell}</td>
                        ))}</tr>
                      ))}</tbody>
                    </table>
                    {chart.spec.row_count && chart.spec.row_count > 30 && (
                      <p className="kv">…共 {chart.spec.row_count} 行（预览前 30 行）</p>
                    )}
                  </div>
                ) : <EChart spec={chart.spec} />}
                {chart.caption && (
                  <p style={{ fontSize: 13, marginTop: 10, fontStyle: "italic" }}>
                    <b>Figure Caption：</b>{chart.caption}
                    <span className="chip" style={{ fontSize: 10, marginLeft: 8 }}
                      title="图注数字全部来自数据集确定性统计（极值/趋势/对比），可逐个回溯">数字可回溯数据集</span>
                  </p>
                )}
              </div>
            )}
          </>
        )}

        {tab === "translate" && (
          <div className="card">
            <h2 style={{ marginTop: 0 }}>中 → 英学术翻译</h2>
            <p className="kv">中文摘要/段落 → 符合学术规范的英文：术语一致、不增删论断、公式与 [n] 引用原样保留（占位符校验不过自动回退）。</p>
            <textarea value={zhText} onChange={(e) => setZhText(e.target.value)}
              placeholder="粘贴中文摘要或段落……"
              style={{ width: "100%", minHeight: 150, border: "1px solid var(--line-strong)",
                borderRadius: "var(--radius-sm)", padding: 10, fontFamily: "var(--font)", outline: "none" }} />
            <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
              <button className="btn primary" onClick={runTranslate}>翻译为学术英文</button>
              {enOut && (
                <button className="chip" style={{ cursor: "pointer" }} onClick={() => {
                  const a = document.createElement("a");
                  a.href = URL.createObjectURL(new Blob([enOut], { type: "text/markdown" }));
                  a.download = "translation-en.md";
                  a.click();
                }}>导出 MD</button>
              )}
            </div>
            {trNote && <p className="kv" style={{ marginTop: 8 }}>{trNote}</p>}
            {enOut && (
              <div style={{ marginTop: 10, background: "var(--surface-2, #f8fafc)", borderRadius: 8, padding: 12 }}>
                <p style={{ fontSize: 13, lineHeight: 1.9, whiteSpace: "pre-wrap" }}>{enOut}</p>
              </div>
            )}
          </div>
        )}

        {tab === "diagram" && (
          <>
            <div className="card">
              <h2 style={{ marginTop: 0 }}>架构拓扑图脚本</h2>
              <p className="kv">描述系统/方法 → 结构化 DiagramSpec → 确定性生成 Mermaid / Graphviz DOT / TikZ 源码 + matplotlib 渲染 PNG（不执行任意代码；幽灵边自动剔除）。</p>
              <textarea value={archDesc} onChange={(e) => setArchDesc(e.target.value)}
                placeholder="如：一个 RAG 系统：Query 改写器 → Retriever（从向量库取 top-k）→ Reranker → Generator 生成带引用的回答"
                style={{ width: "100%", minHeight: 100, border: "1px solid var(--line-strong)",
                  borderRadius: "var(--radius-sm)", padding: 10, fontFamily: "var(--font)", outline: "none" }} />
              <button className="btn primary" style={{ marginTop: 10 }} onClick={runDiagram}>生成四种脚本</button>
              {dgNote && <p className="kv" style={{ marginTop: 8 }}>{dgNote}</p>}
            </div>

            {diagram && (
              <>
                <div className="card">
                  <h2 style={{ marginTop: 0, fontSize: 15 }}>Mermaid 预览
                    <span className={`chip ${diagram.validation?.mermaid?.ok ? "ok" : "warn"}`} style={{ fontSize: 10, marginLeft: 8 }}>
                      {diagram.validation?.mermaid?.ok ? "语法通过" : "校验失败"}
                    </span>
                  </h2>
                  <MermaidPreview code={diagram.sources.mermaid} />
                </div>
                {(["dot", "tikz"] as const).map((k) => (
                  <div className="card" key={k}>
                    <h2 style={{ marginTop: 0, fontSize: 15 }}>
                      {k === "dot" ? "Graphviz DOT" : "TikZ (LaTeX)"}
                      <span className={`chip ${diagram.validation?.[k]?.ok ? "ok" : "warn"}`} style={{ fontSize: 10, marginLeft: 8 }}>
                        {diagram.validation?.[k]?.ok ? "结构校验通过" : "校验失败"}
                      </span>
                      <button className="chip" style={{ cursor: "pointer", fontSize: 10, marginLeft: 8 }}
                        onClick={() => copySrc(diagram.sources[k])}>复制源码</button>
                    </h2>
                    <pre style={{ fontSize: 11, overflowX: "auto", background: "var(--surface-2, #f8fafc)",
                      padding: 10, borderRadius: 8 }}>{diagram.sources[k]}</pre>
                  </div>
                ))}
                <div className="card">
                  <h2 style={{ marginTop: 0, fontSize: 15 }}>Matplotlib 渲染
                    <span className={`chip ${diagram.matplotlib?.ok ? "ok" : "warn"}`} style={{ fontSize: 10, marginLeft: 8 }}>
                      {diagram.matplotlib?.ok ? "sandbox 渲染成功" : "渲染失败（可复制 Mermaid/DOT 源码使用）"}
                    </span>
                  </h2>
                  {diagram.matplotlib?.ok && diagram.matplotlib.image_url && (
                    <img src={diagram.matplotlib.image_url} alt="拓扑图"
                      style={{ maxWidth: "100%", borderRadius: "var(--radius-sm)" }} />
                  )}
                </div>
              </>
            )}
          </>
        )}

        <p className="kv" style={{ marginTop: 16 }}>
          Copilot 全部产出遵循 Evidence-First：写作引用白名单与证据包、图注数字可回溯、
          翻译占位符校验。LLM 不可用时各功能如实降级（不冒充）。前往
          <Link href="/chat" style={{ marginLeft: 4 }}>Chat</Link> 体验 Agent 编排，或
          <Link href="/discover" style={{ marginLeft: 4 }}>检索发现</Link> 补充文献。
        </p>
      </div>
    </div>
  );
}
