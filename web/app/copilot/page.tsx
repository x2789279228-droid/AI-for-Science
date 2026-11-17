"use client";

/**
 * 学术写作与数据可视化 Copilot（模块六）：
 * ① 写作：idea + 选中论文 → 无幻觉论文框架（固定章节 + References 白名单，
 *    正文 [R#] 与 References 由代码双向一致化，白名单外引用自动剔除并记 guardrails）
 * ② 数据可视化：CSV 上传（5MB/10万行护栏）→ table/line/bar/多维 radar（含
 *    3D 透视变体） / 真 3D scatter3d（ECharts + echarts-gl）+ 学术 Figure
 *    Caption（数字全部可回溯数据集；bar 大数据 Top60+其他聚合降采样）
 * ③ 学术翻译：中文摘要/全文 → 英文学术语体（复用模块三双向管线）
 * ④ 拓扑图脚本：DiagramSpec → Mermaid/Graphviz DOT/TikZ 源码 + matplotlib PNG
 *    （LLM 不可用时按规则降级，仍出四格式脚本并如实标注 guardrail）
 */
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import type { EChartsType } from "echarts";
import {
  ChartOut, DatasetMeta, DiagramOut, WritingOut, buildChart, datasetApi,
  generateDiagram, listPapers, streamWriting, zh2enTranslate,
} from "@/lib/api";
import type { Paper } from "@/lib/api";
import { useShell } from "@/components/ShellContext";
import { canvasTheme, themeMermaid } from "@/lib/theme-registry";

type Tab = "write" | "viz" | "translate" | "diagram";

/** 后端静态产物（sandbox PNG 等）返回的是相对路径；<img> 必须拼 API base，
 *  否则会指向 Next 自身源（如 3005）而 404。 */
const API_ORIGIN = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8100";

function mediaUrl(url?: string): string {
  if (!url) return "";
  if (/^(https?:)?\/\//i.test(url) || url.startsWith("data:")) return url;
  return `${API_ORIGIN.replace(/\/+$/, "")}/${url.replace(/^\/+/, "")}`;
}

/** 图表 spec 的扩展字段（web/lib/api.ts 不属本轮写域，局部窄化断言）。 */
type ChartSpecExt = ChartOut["spec"] & {
  variant?: string;
  render_note?: string;
  downsampled?: { total_rows: number; shown_rows: number; method: string };
};

/** ECharts 客户端渲染（dynamic 导入防 SSR；type=table 由表格渲染；scatter3d 走 echarts-gl）。 */
function EChart({ spec }: { spec: ChartOut["spec"] }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const instRef = useRef<EChartsType | null>(null);
  const { theme } = useShell();
  useEffect(() => {
    let alive = true;
    (async () => {
      const mod = await import("echarts");
      const echarts = mod.default ?? mod;
      if (!alive || !ref.current) return;
      instRef.current?.dispose();
      const chart = echarts.init(ref.current);
      instRef.current = chart;
      const pal = canvasTheme(theme);   // 画布色随皮肤切换
      // v1.6 深色适配四件套：图例/轴文字、轴线、分隔线随肤取色（wuxia 深底下默认深灰文字不可读）
      const axis = {
        axisLabel: { color: pal.text },
        axisLine: { lineStyle: { color: pal.axisLine } },
        splitLine: { lineStyle: { color: pal.splitLine } },
      };
      const common = { tooltip: {}, legend: { top: 4, textStyle: { color: pal.text } }, color: pal.community };
      if (spec.type === "line" || spec.type === "bar") {
        chart.setOption({
          ...common,
          xAxis: { type: "category", name: spec.x?.name, data: spec.x?.data,
            nameTextStyle: { color: pal.textSoft }, ...axis },
          yAxis: { type: "value", ...axis },
          series: (spec.series ?? []).map((s) => ({
            name: s.name, type: spec.type === "line" ? "line" : "bar",
            data: s.data, smooth: true,
          })),
        });
      } else if (spec.type === "radar" || spec.type === "radar3d") {
        const radarSeries = (spec.series ?? []).map((s) => ({ name: s.name, value: s.data }));
        chart.setOption({
          ...common,
          legend: { top: 4, type: "scroll", textStyle: { color: pal.text } },
          radar: { indicator: spec.indicators ?? [], axisName: { color: pal.text },
            axisLine: { lineStyle: { color: pal.axisLine } },
            splitLine: { lineStyle: { color: pal.splitLine } },
            splitArea: { areaStyle: { opacity: 0.12 } } },
          series: [{ type: "radar", data: radarSeries, symbolSize: 4 }],
        });
      } else if (spec.type === "scatter3d") {
        await import("echarts-gl");   // 真 3D：grid3D + scatter3D（注册后全局生效）
        const s3 = spec as unknown as { axes: string[]; visual_dim: string | null;
                                        points: (number | string)[][] };
        chart.setOption({
          tooltip: {},
          visualMap: s3.visual_dim
            ? { show: true, dimension: 3, inRange: { color: pal.visualMap } }
            : undefined,
          xAxis3D: { type: "value", name: s3.axes[0], nameTextStyle: { color: pal.textSoft },
            axisLabel: { color: pal.text } },
          yAxis3D: { type: "value", name: s3.axes[1], nameTextStyle: { color: pal.textSoft },
            axisLabel: { color: pal.text } },
          zAxis3D: { type: "value", name: s3.axes[2], nameTextStyle: { color: pal.textSoft },
            axisLabel: { color: pal.text } },
          grid3D: { boxWidth: 110, boxDepth: 110, viewControl: { autoRotate: false },
            splitLine: { lineStyle: { color: pal.splitLine } } },
          series: [{ type: "scatter3D", data: s3.points, symbolSize: 6 }],
        });
      }
    })();
    return () => { alive = false; instRef.current?.dispose(); instRef.current = null; };
  }, [spec, theme]);
  return <div ref={ref} style={{ width: "100%", height: 360 }} />;
}

/** 「雷达（3D 透视）」呈现层：ECharts 5 无原生 radar3D（真 3D 用 scatter3d）。
 *  底层仍是 2D 多维雷达，这里用 CSS 三层错位 + 透视/阴影近似 3D 观感——
 *  底层两块「轴面」错位后移、顶层雷达轻微 rotateX，兼具可读性与立体感。 */
function Radar3DLayers({ spec }: { spec: ChartOut["spec"] }) {
  const plates = [
    {
      top: 26, scale: 0.9, opacity: 0.16, rot: 26,
      bg: "linear-gradient(135deg, var(--surface-2), transparent 72%)",
    },
    {
      top: 13, scale: 0.95, opacity: 0.26, rot: 17,
      bg: "linear-gradient(135deg, var(--surface-2), transparent 70%)",
    },
  ];
  return (
    <div style={{ position: "relative", padding: "6px 0 18px", perspective: 1200 }}>
      {plates.map((p) => (
        <div key={p.top} aria-hidden style={{
          position: "absolute", left: "9%", right: "9%", top: p.top, height: 360 - p.top,
          borderRadius: 14, border: "1px solid var(--line)", background: p.bg,
          opacity: p.opacity, transformOrigin: "50% 100%",
          transform: `rotateX(${p.rot}deg) scale(${p.scale})`,
          boxShadow: "var(--shadow-md)", pointerEvents: "none",
        }} />
      ))}
      <div style={{
        position: "relative", borderRadius: 14, overflow: "visible",
        transform: "rotateX(9deg) rotateZ(-2deg)", transformOrigin: "50% 100%",
        boxShadow: "var(--shadow-lg)",
      }}>
        <EChart spec={spec} />
      </div>
      <div style={{ position: "absolute", right: 4, top: 0, maxWidth: "60%" }}>
        <span className="chip warn" style={{ fontSize: 10 }}
          title={(spec as ChartSpecExt).render_note
            ?? "ECharts 无原生 radar3D：底层为 2D 多维雷达，CSS 三层错位/透视近似"}>
          3D 透视（ECharts 无原生 radar3D）
        </span>
      </div>
    </div>
  );
}

/** mermaid 源码预览（render 通过才显示，语法校验可视化）。
 *  主题随皮肤映射（v1.4 深色适配条款①）：wuxia 玄墨底用 dark，浅色皮肤用 neutral。 */
function MermaidPreview({ code }: { code: string }) {
  const [svg, setSvg] = useState("");
  const [err, setErr] = useState("");
  const { theme } = useShell();
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({ startOnLoad: false, theme: themeMermaid[theme] });
        const { svg } = await mermaid.render(`mmd-${Date.now()}`, code);
        if (alive) { setSvg(svg); setErr(""); }
      } catch (e) {
        if (alive) { setSvg(""); setErr(String(e)); }
      }
    })();
    return () => { alive = false; };
  }, [code, theme]);
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
  const [chartType, setChartType] = useState<
    "table" | "line" | "bar" | "radar" | "radar3d" | "scatter3d">("bar");
  const [xCol, setXCol] = useState("");
  const [yCols, setYCols] = useState<string[]>([]);
  const [vizNote, setVizNote] = useState("");
  const chartSpec = chart?.spec as ChartSpecExt | undefined;
  const isRadar3d = chartSpec?.variant === "radar3d";

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
  const dgGuardrail = (diagram as (DiagramOut & { guardrail?: string }) | null)?.guardrail;

  const runDiagram = async () => {
    if (!archDesc.trim()) return;
    setDgNote("解析架构描述…");
    try {
      const out = await generateDiagram(archDesc.trim());
      setDiagram(out);
      const guard = (out as DiagramOut & { guardrail?: string }).guardrail;
      setDgNote(`完成：${out.spec.nodes.length} 节点 · ${out.spec.edges.length} 边`
        + (guard ? `（LLM 不可用，已规则降级：${guard}）` : ""));
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
              {writing?.guardrails?.find((g) => g.startsWith("refs_underused:")) && (
                <p className="kv" style={{ color: "var(--warn)" }}>
                  引用不足，已如实标注：正文实际引用{" "}
                  {writing.guardrails.find((g) => g.startsWith("refs_underused:"))?.split(":")[1]} 条
                  （&lt; 3，补引重试后仍不足），未强行编造引用——已在 guardrails 记录 refs_underused。
                </p>
              )}
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
              <p className="kv" style={{ marginTop: 0 }}>
                雷达图＝多维指标雷达（指标 = 选定 y 列，每个 x 类别一个多边形，半径上限取该指标全量最大值 ×1.1）；
                3D 数据请用「3D 散点」；「雷达（3D 透视）」为 2D radar + CSS 三层透视近似
                （ECharts 无原生 radar3D）。柱状图类别 &gt;60 时按数值 Top60 +「其他（聚合）」降采样，图注统计仍用全量数据。
              </p>
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
                {(["table", "line", "bar", "radar", "radar3d", "scatter3d"] as const).map((t) => (
                  <button key={t} className={`chip ${chartType === t ? "accent" : ""}`}
                    style={{ cursor: "pointer" }} onClick={() => setChartType(t)}
                    title={t === "radar" ? "多维指标雷达：指标 = 选定 y 列，每个 x 类别一个多边形（ECharts radar 平面多维轴）"
                      : t === "radar3d" ? "3D 透视：底层同一份 2D radar 数据，CSS 三层错位/透视近似（ECharts 无原生 radar3D）"
                      : t === "scatter3d" ? "真 3D 散点（echarts-gl grid3D，可拖拽旋转）" : undefined}>
                    {t === "radar" ? "雷达（多维）" : t === "radar3d" ? "雷达（3D 透视）"
                      : t === "scatter3d" ? "3D 散点"
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
                  <span className="chip" style={{ fontSize: 10, marginLeft: 8 }}>
                    {chart.spec.type}{isRadar3d ? " · 3D 透视" : ""}
                  </span>
                  {chartSpec?.downsampled && (
                    <span className="chip warn" style={{ fontSize: 10, marginLeft: 8 }}
                      title={`类别数 ${chartSpec.downsampled.total_rows} → 展示 ${chartSpec.downsampled.shown_rows}；图注统计仍用全量数据`}>
                      大数据降采样：{chartSpec.downsampled.method}（{chartSpec.downsampled.total_rows} → {chartSpec.downsampled.shown_rows}）
                    </span>
                  )}
                  {isRadar3d && (
                    <span className="chip warn" style={{ fontSize: 10, marginLeft: 8 }}>
                      ECharts 无原生 radar3D（CSS 透近似）
                    </span>
                  )}
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
                ) : isRadar3d ? (
                  <Radar3DLayers spec={chart.spec} />
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
              <div style={{ marginTop: 10, background: "var(--surface-2)", borderRadius: 8, padding: 12 }}>
                <p style={{ fontSize: 13, lineHeight: 1.9, whiteSpace: "pre-wrap" }}>{enOut}</p>
              </div>
            )}
          </div>
        )}

        {tab === "diagram" && (
          <>
            <div className="card">
              <h2 style={{ marginTop: 0 }}>架构拓扑图脚本</h2>
              <p className="kv">
                描述系统/方法 → 结构化 DiagramSpec → 确定性生成 Mermaid / Graphviz DOT / TikZ 源码 +
                matplotlib 渲染 PNG（不执行任意代码；幽灵边自动剔除）。LLM 不可用时自动规则降级
                （按 →/{"->"}/=&gt; 拆链 + 句读/逗号拆步，≤12 节点），仍出四格式脚本并以
                guardrail=diagram_rule_fallback 如实标注。
              </p>
              <textarea value={archDesc} onChange={(e) => setArchDesc(e.target.value)}
                placeholder="如：一个 RAG 系统：Query 改写器 → Retriever（从向量库取 top-k）→ Reranker → Generator 生成带引用的回答"
                style={{ width: "100%", minHeight: 100, border: "1px solid var(--line-strong)",
                  borderRadius: "var(--radius-sm)", padding: 10, fontFamily: "var(--font)", outline: "none" }} />
              <button className="btn primary" style={{ marginTop: 10 }} onClick={runDiagram}>生成四种脚本</button>
              {dgNote && <p className="kv" style={{ marginTop: 8 }}>{dgNote}</p>}
              {dgGuardrail === "diagram_rule_fallback" && (
                <p className="kv" style={{ color: "var(--warn)" }}>
                  LLM 不可用 → 已按规则降级解析（guardrail=diagram_rule_fallback）：
                  节点/边由箭头链与句读拆分确定性生成，非 LLM 结构化结果。
                </p>
              )}
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
                    <pre style={{ fontSize: 11, overflowX: "auto", background: "var(--surface-2)",
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
                    <img src={mediaUrl(diagram.matplotlib.image_url)} alt="拓扑图"
                      style={{ maxWidth: "100%", borderRadius: "var(--radius-sm)" }} />
                  )}
                  {diagram.matplotlib?.ok && diagram.matplotlib.image_url && (
                    <p className="kv" style={{ fontSize: 12 }}>
                      PNG 经 API base 拼接：{mediaUrl(diagram.matplotlib.image_url)}
                    </p>
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
