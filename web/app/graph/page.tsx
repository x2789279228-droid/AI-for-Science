"use client";

/**
 * 引用图谱（模块五）：AntV G6 交互图谱 + 批量选择（5–20 篇）。
 * - 节点大小 = PageRank、颜色 = Louvain 社区、边粗细 = 引用含金量（加权）
 * - 基石论文 🪨 / 边缘衍生 🌿 角色徽章（规则可解释，答辩可讲）
 * - 整批领域综述（五节结构，SSE 流式，[E#] 锚点 + ClaimTrace 校验，缓存复用）
 * - 外部最新进展核查（S2 证据，open_unverified 诚实态）+ PNG/JSON 导出
 */
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { motion, AnimatePresence } from "framer-motion";
import type { Graph as G6Graph } from "@antv/g6";
import { loadG6 } from "@/lib/g6-loader";
import {
  FullGraph, FutureCheckItem, GraphNode, IdeaItem, fetchGraph, listPapers,
  streamCommunitySummary, streamFutureChecks, streamIdeas, streamSurvey, SurveySection,
} from "@/lib/api";
import type { HullOptions } from "@antv/g6/lib/plugins/hull/index";
import { useIsland } from "@/lib/island";
import { useShell } from "@/components/ShellContext";
import { canvasTheme } from "@/lib/theme-palettes";
import { Maximize } from "@/components/icons";
import type { Paper } from "@/lib/api";

// 社区色板已按主题收敛至 lib/theme-palettes.ts（v1.3，design-tokens 使用规则 6）

const ROLE_BADGE: Record<string, string> = { cornerstone: "🪨 基石", peripheral: "🌿 边缘衍生" };

/** 节点标签词典（§5.2,可按语料扩充）：缩写命中 → 冒号前短名 → 前 3 词回退；中文取前 10 字。 */
const LABEL_ABBR = ["LoRA", "GPT-3", "GPT-2", "ViT", "T5", "XLNet", "BERT", "RoBERTa",
  "DeBERTa", "ALBERT", "ELECTRA", "BART", "Longformer", "Big Bird", "Reformer",
  "FlashAttention", "Megatron-LM", "Scaling Laws"];

function nodeLabel(title: string): string {
  const t = title.replace(/\n/g, " ").trim();
  const pre = t.split(":")[0].trim();
  if (t.includes(":") && /^[A-Z][A-Za-z0-9\-+]{1,14}$/.test(pre)) return pre;
  for (const a of LABEL_ABBR) {
    if (t.toLowerCase().includes(a.toLowerCase())) return a;
  }
  const words = t.split(/\s+/).filter(Boolean);
  if (/[\u4e00-\u9fff]/.test(t)) return words.join("").slice(0, 10);
  return words.slice(0, 3).join(" ").slice(0, 20);
}

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
  const graphRef = useRef<G6Graph | null>(null);
  const [graph, setGraph] = useState<FullGraph | null>(null);
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [selectedEdges, setSelectedEdges] = useState<FullGraph["edges"]>([]);
  const [err, setErr] = useState("");
  const { immersive, setImmersive, theme } = useShell();
  const island = useIsland();
  // 沉浸切换的画布适配：原实现用 epoch +400ms 强制重建实例——但 G6 v5 的
  // destroy() 是异步的，同步重建会导致旧实例渲染层残留（画布叠两幅图）。
  // 改由 ResizeObserver 跟随容器尺寸变化 resize+fitView（见下方 G6 useEffect），不再重建。

  // 模块五 P0-1：批量选择（默认全库；2–50 护栏与后端一致）
  const [papers, setPapers] = useState<Paper[]>([]);
  const [pickedIds, setPickedIds] = useState<string[]>([]);
  const [showPicker, setShowPicker] = useState(false);
  const [infoOpen, setInfoOpen] = useState(true);   // 图谱信息 popover(默认展开,保持统计可见)

  // 整批领域综述
  const [surveySections, setSurveySections] = useState<SurveySection[]>([]);
  const [surveyNote, setSurveyNote] = useState("");
  const [surveying, setSurveying] = useState(false);
  const [surveyCached, setSurveyCached] = useState(false);

  // 外部进展核查
  const [checks, setChecks] = useState<FutureCheckItem[] | null>(null);
  const [checkMsg, setCheckMsg] = useState("");

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

  const activeIds = pickedIds.length ? pickedIds : papers.map((p) => p.id);

  useEffect(() => {
    listPapers().then((ps) => setPapers(ps)).catch(() => setPapers([]));
  }, []);

  useEffect(() => {
    setGraph(null);
    setSelected(null);
    fetchGraph(pickedIds.length ? pickedIds : undefined).then((g) => {
      // 修复「主体图缩成小团」：无引用边的孤立节点（如普通文档/测试页）会被
      // d3-force 的多体斥力推到几千像素外，撑爆包围盒——fitView 为装下它们
      // 将缩放压到 ~0.23，主体图被缩成小团。引用图谱只展示有引用关系的节点。
      const isolated = new Set(g.nodes
        .filter((n) => !g.edges.some((e) => e.source === n.paper_id || e.target === n.paper_id))
        .map((n) => n.paper_id));
      const cleaned: FullGraph = isolated.size
        ? { ...g, nodes: g.nodes.filter((n) => !isolated.has(n.paper_id)) }
        : g;
      setGraph(cleaned);
      if (cleaned.communities.length) setCommId(cleaned.communities[0].id);
    }).catch((e) => setErr(String(e)));
  }, [pickedIds]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!graph || !containerRef.current) return;
    let disposed = false;
    let cleanup: () => void = () => {};
    // G6 动态加载（性能修复：约 500kB 的库不进首包），就绪后再建画布
    loadG6().then(({ Graph }) => {
      if (disposed || !containerRef.current) return;
    const pal = canvasTheme(theme);          // 画布色随皮肤切换（重建画布）
    // G6 v5 约定：业务字段放 data 挂载点，style 回调从 d.data 读取
    // §5.3 社区凸包：按 Louvain community 分组的 Hull 插件（fill=社区色 8% 透明度）。
    // 失败风险兜底：Hull options 类型不符会在 render 时报错并被下方 catch 捕获，
    // 届时移除 plugins 重建一次（降级：社区色仅靠节点填充区分，如实记录于 checklist）。
    const hullPlugins = (() => {
      try {
        const byComm = new Map<number, string[]>();
        for (const n of graph.nodes) {
          const arr = byComm.get(n.community) ?? [];
          arr.push(n.paper_id);
          byComm.set(n.community, arr);
        }
        return [...byComm.entries()].map(([cid, members], i) => ({
          key: `hull-${cid}`,
          type: "hull",
          members,
          fill: `${pal.community[i % pal.community.length]}14`,   // 8% 透明度
          stroke: "transparent",
          corner: "rounded",
          padding: 18,
        } as HullOptions & { key: string; type: string }));
      } catch { return []; }
    })();
    let g6: G6Graph;
    // 节点/画布点击事件（抽公共函数：主实例与降级重建实例都必须绑定——
    // 此前降级实例未绑事件，重建后点节点无反应）
    const bindEvents = (inst: G6Graph) => {
      // G6 v5 IEvent 目标节点 id 在 evt.target.id（类型宽松处，见 G6 5.x 文档事件示例）
      inst.on("node:click", (evt: any) => {
        const id = evt?.target?.id as string | undefined;
        if (!id) return;
        const n = graph.nodes.find((x) => x.paper_id === id);
        setSelected(n ?? null);
        setSelectedEdges(graph.edges.filter((e) => e.source === id || e.target === id));
      });
      inst.on("canvas:click", () => setSelected(null));
    };
    try {
      g6 = new Graph({
        container: containerRef.current,
        autoFit: "view",
        padding: 20,
        plugins: hullPlugins,
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
          fill: (d) => pal.community[((d as { data?: GraphNode }).data?.community ?? 0) % pal.community.length],
          labelText: (d) => nodeLabel(String((d as { data?: GraphNode }).data?.title ?? "")),
          labelPlacement: "bottom",
          labelFontSize: 11,
          labelFill: "#ffffff",
          labelBackground: true,
          labelBackgroundFill: pal.labelBg,
          labelPadding: [1, 3],
          labelBackgroundRadius: 3,
          badges: (d): any => {
            const role = (d as { data?: GraphNode }).data?.role;
            return role === "cornerstone"
              ? [{ text: "基", placement: "right-top", background: pal.cornerBadge,
                   color: "#ffffff", fontSize: 8, padding: [1, 3] }]
              : [];
          },
          stroke: (d) => {
            const role = (d as { data?: GraphNode }).data?.role;
            return role === "cornerstone" ? pal.cornerStroke
              : role === "peripheral" ? pal.peripheralStroke : pal.nodeStroke;
          },
          lineWidth: (d) => ((d as { data?: GraphNode }).data?.role ? 2.5 : 1),
        },
      },
      edge: {
        style: {
          stroke: (d) => {
            const w = (d as { data?: { weight?: number } }).data?.weight ?? 0;
            return w >= 1.0 ? pal.edgeStrong : w > 0 ? pal.edgeMid : pal.edgeWeak;
          },
          lineWidth: (d) => {
            const w = (d as { data?: { weight?: number } }).data?.weight ?? 0;
            return 1 + Math.min(5, w * 2);
          },
          endArrow: true,
        },
      },
      layout: { type: "d3-force", linkDistance: 130, nodeStrength: -400, animation: false },
      behaviors: ["drag-canvas", "zoom-canvas", "drag-element"],
      });
    } catch (e) {
      // 插件选项异常等构造期失败 → 降级为无插件重建(§5.3 降级路径的同步分支)
      setErr(`G6 初始化失败，已降级重试：${e}`);
      const g6b = new Graph({
        container: containerRef.current, autoFit: "view", padding: 20,
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
            fill: (d) => pal.community[((d as { data?: GraphNode }).data?.community ?? 0) % pal.community.length],
            labelText: (d) => nodeLabel(String((d as { data?: GraphNode }).data?.title ?? "")),
            labelPlacement: "bottom", labelFontSize: 11,
          },
        },
        layout: { type: "d3-force", linkDistance: 130, nodeStrength: -400, animation: false },
        behaviors: ["drag-canvas", "zoom-canvas", "drag-element"],
      });
      graphRef.current = g6b;
      g6b.render().then(() => bindEvents(g6b)).catch(() => {});
      cleanup = () => { try { g6b?.destroy(); } catch { /* 忽略 */ } graphRef.current = null; };
      return;
    }
    graphRef.current = g6;
    const render = g6.render().then(() => {
      // 修复「图缩成小团」：autoFit 只作用于首帧，力导向把连通分量拉向质心
      // 收敛后视口不跟随——布局完成时主动 fitView 一次。
      try { g6.fitView(); } catch { /* 忽略 */ }
      return bindEvents(g6);
    });
    // Hull 插件降级路径(§5.3)：render 失败且本次启用了 hull → 去插件重建一次。
    // 修复「画布叠两幅图」：重建前必须销毁渲染失败的旧实例——否则其半成品
    // canvas 残留在容器里，与重建实例同时显示。
    render.catch((e0) => {
      if (!hullPlugins.length) throw e0;
      if (!containerRef.current) throw e0;
      try { g6.destroy(); } catch { /* 实例已失效则忽略 */ }
      // destroy 对「render 中途失败」的实例可能清不干净（canvas 残留 →
      // 重建后画布叠两幅相同的图）——无条件移除容器内全部残留画布。
      try {
        containerRef.current.querySelectorAll("canvas").forEach((c) => c.remove());
      } catch { /* 忽略 */ }
      const g6b = new Graph({
        container: containerRef.current, autoFit: "view", padding: 20,
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
            fill: (d) => pal.community[((d as { data?: GraphNode }).data?.community ?? 0) % pal.community.length],
            labelText: (d) => nodeLabel(String((d as { data?: GraphNode }).data?.title ?? "")),
            labelPlacement: "bottom", labelFontSize: 11,
          },
        },
        layout: { type: "d3-force", linkDistance: 130, nodeStrength: -400, animation: false },
        behaviors: ["drag-canvas", "zoom-canvas", "drag-element"],
      });
      graphRef.current = g6b;
      return g6b.render().then(() => { try { g6b.fitView(); } catch { /* 忽略 */ } return bindEvents(g6b); });
    }).catch((e) => setErr(`G6 渲染失败：${e}`));
    cleanup = () => {
      // 销毁「当前生效实例」而非闭包里的 g6：走降级重建路径后生效的是 g6b，
      // 只杀 g6 会泄漏正在显示的实例（画布残留旧图）。
      const doomed = graphRef.current;
      let dp: any = null;
      try { dp = doomed?.destroy(); } catch { /* 容器已随 HMR/卸载销毁等场景,忽略 */ }
      // 根因修复「画布叠两幅图」：G6 v5 的 destroy() 是异步的（返回 Promise），
      // 同步阶段不移除 canvas——旧实例的渲染层与新实例叠加显示，且旧实例的
      // 事件监听残留导致面板触发两次。同步清一次 + destroy 完成后再兜底清一次。
      if (containerRef.current) {
        try { containerRef.current.querySelectorAll("canvas").forEach((c) => c.remove()); } catch { /* 忽略 */ }
      }
      if (dp && typeof dp.then === "function") {
        dp.then(() => {
          try { containerRef.current?.querySelectorAll("canvas").forEach((c) => c.remove()); } catch { /* 忽略 */ }
        }).catch(() => { /* 忽略 */ });
      }
      graphRef.current = null;
      try { ro?.disconnect(); } catch { /* 忽略 */ }
    };
    // 容器尺寸自适应（修复：节点详情面板开合会改变画布宽度，autoFit 只在
    // 首次 render 生效——不跟随时图会缩成一小团挤在旧区域）。ResizeObserver
    // 防抖 200ms 后 resize + fitView。
    const ro = new ResizeObserver((entries) => {
      const inst = graphRef.current;
      const box = containerRef.current;
      if (!inst || !box) return;
      const { width, height } = entries[0].contentRect;
      if (width < 50 || height < 50) return;   // 面板动画中间态忽略
      clearTimeout((ro as any)._t);
      (ro as any)._t = setTimeout(() => {
        try {
          inst.resize(Math.round(width), Math.round(height));
          inst.fitView();
        } catch { /* 实例已销毁则忽略 */ }
      }, 200);
    });
    ro.observe(containerRef.current);
    });
    return () => { disposed = true; cleanup(); };
  }, [graph, theme]);

  const runSummary = () => {
    if (summarizing || !graph) return;
    setSummarizing(true);
    setSummary("");
    setClaims([]);
    setGuardrails([]);
    const paperIds = graph.nodes.map((n) => n.paper_id);
    island.emit({ key: "summary", kind: "progress", title: `社区 ${commId} 综述生成中`,
      detail: "逐论断经 ClaimTrace 校验", indeterminate: true });
    streamCommunitySummary(paperIds, commId, {
      delta: (d) => setSummary((prev) => prev + d.text),
      final: (d) => {
        setSummary(d.summary);
        setClaims(d.claims ?? []);
        setAnchors(d.anchors ?? {});
        setGuardrails(d.guardrails ?? []);
        setSummarizing(false);
        island.emit({ key: "summary", kind: "success", title: `社区 ${commId} 综述完成`,
          detail: `${d.claims?.length ?? 0} 条论断已校验` });
      },
      error: (d) => { setSummary(`（出错：${d.message}）`); setSummarizing(false);
        island.emit({ key: "summary", kind: "error", title: "社区综述失败", detail: d.message }); },
      done: () => setSummarizing(false),
    }).catch((e) => { setSummary(`（请求失败：${e}）`); setSummarizing(false);
      island.emit({ key: "summary", kind: "error", title: "社区综述请求失败", detail: String(e) }); });
  };

  const runIdeas = () => {
    if (!graph || ideasMsg) return;
    setIdeasMsg("扫描 Conclusion/Limitation…");
    island.emit({ key: "ideas", kind: "progress", title: "研究缝隙分析中",
      detail: "Future Work 去重过滤", indeterminate: true });
    streamIdeas(graph.nodes.map((n) => n.paper_id), {
      status: (d) => { setIdeasMsg(d.note);
        island.emit({ key: "ideas", kind: "progress", title: "研究缝隙分析中",
          detail: d.note, indeterminate: true }); },
      final: (d) => { setIdeas({ solved: d.solved, open: d.open });
        setIdeasMsg(`共 ${d.total} 条想法`);
        island.emit({ key: "ideas", kind: "success", title: `缝隙分析完成 · ${d.total} 条想法`,
          detail: `${d.open.length} 条 open · ${d.solved.length} 条已有进展` }); },
      error: (d) => { setIdeasMsg(`出错：${d.message}`);
        island.emit({ key: "ideas", kind: "error", title: "缝隙分析失败", detail: d.message }); },
      done: () => { },
    }).catch((e) => { setIdeasMsg(`请求失败：${e}`);
      island.emit({ key: "ideas", kind: "error", title: "缝隙分析请求失败", detail: String(e) }); });
  };

  // ---- 模块五 P0-2：整批领域综述（SSE 流式，五节结构） ----
  const runSurvey = () => {
    if (surveying || activeIds.length < 2) return;
    setSurveying(true);
    setSurveySections([]);
    setSurveyCached(false);
    setSurveyNote("收集证据…");
    island.emit({ key: "survey", kind: "progress", title: `领域综述生成中（${activeIds.length} 篇）`,
      detail: "五节结构 · SSE 流式", indeterminate: true });
    streamSurvey(activeIds, {
      status: (d) => { setSurveyNote(d.note);
        island.emit({ key: "survey", kind: "progress", title: "领域综述生成中",
          detail: d.note, indeterminate: true }); },
      delta: (d) => setSurveySections((prev) => {
        const i = prev.findIndex((s) => s.title === d.section);
        if (i >= 0) {
          const cp = [...prev];
          cp[i] = { ...cp[i], content: cp[i].content + d.text };
          return cp;
        }
        return [...prev, { title: d.section, content: d.text, claims: [], anchors: {} }];
      }),
      final: (d) => {
        setSurveySections(d.sections);
        setSurveyCached(Boolean(d.cached));
        setSurveyNote(`完成：${d.sections.length} 节 · ${Array.isArray(d.stats?.years) ? `${d.stats.years[0]}–${d.stats.years[1]}` : ""}`);
        setSurveying(false);
        island.emit({ key: "survey", kind: "success",
          title: `领域综述完成 · ${d.sections.length} 节`,
          detail: d.cached ? "缓存命中" : undefined });
      },
      error: (d) => { setSurveyNote(`出错：${d.message}`); setSurveying(false);
        island.emit({ key: "survey", kind: "error", title: "领域综述失败", detail: d.message }); },
    }).catch((e) => { setSurveyNote(`请求失败：${e}`); setSurveying(false);
      island.emit({ key: "survey", kind: "error", title: "综述请求失败", detail: String(e) }); });
  };

  const exportSurveyMd = () => {
    const md = "# 领域综述（自动生成 · 机器草稿）\n\n"
      + surveySections.map((s) => `## ${s.title}\n\n${s.content}`).join("\n\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
    a.download = "survey.md";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  // ---- 模块五 P0-3：外部最新进展核查 ----
  const runChecks = () => {
    if (checkMsg && checks) { setChecks(null); setCheckMsg(""); return; }
    setCheckMsg("抽取并去重 Future Work…");
    setChecks(null);
    island.emit({ key: "checks", kind: "progress", title: "外部进展核查中",
      detail: "S2 证据 · open_unverified 诚实态", indeterminate: true });
    streamFutureChecks(activeIds, {
      status: (d) => { setCheckMsg(d.note);
        island.emit({ key: "checks", kind: "progress", title: "外部进展核查中",
          detail: d.note, indeterminate: true }); },
      final: (d) => { setChecks(d.items); setCheckMsg(`共 ${d.total} 条（外部证据：S2）`);
        island.emit({ key: "checks", kind: "success", title: `核查完成 · ${d.total} 条`,
          detail: "外部证据:S2" }); },
      error: (d) => { setCheckMsg(`出错：${d.message}`);
        island.emit({ key: "checks", kind: "error", title: "核查失败", detail: d.message }); },
    }).catch((e) => { setCheckMsg(`请求失败：${e}`);
      island.emit({ key: "checks", kind: "error", title: "核查请求失败", detail: String(e) }); });
  };

  // ---- 导出 ----
  const exportPng = async () => {
    const g6 = graphRef.current;
    if (!g6) return;
    try {
      const url = await (g6 as unknown as { toDataURL: (t?: string) => Promise<string> })
        .toDataURL("image/png");
      const a = document.createElement("a");
      a.href = url;
      a.download = "citation-graph.png";
      a.click();
    } catch { window.alert("PNG 导出失败（G6 版本差异）——可用 JSON 导出"); }
  };
  const exportJson = () => {
    if (!graph) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(
      new Blob([JSON.stringify(graph, null, 2)], { type: "application/json" }));
    a.download = "citation-graph.json";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const togglePick = (id: string) => {
    setPickedIds((prev) => prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]);
  };

  return (
    <div className="page">
      <div className="page-inner">
        <h1>引用图谱 <span className="chip accent">PageRank · Louvain 社区 · 引用含金量</span></h1>
        {err && <div className="card"><p className="kv">{err}</p></div>}
        {graph && (
          <>
            <div className="card" style={{ position: "relative" }}>
              <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", alignItems: "center" }}>
                <button className={`chip ${infoOpen ? "accent" : ""}`} style={{ cursor: "pointer" }}
                  data-testid="graph-info"
                  onClick={() => setInfoOpen((v) => !v)}>
                  ⓘ 图谱信息
                </button>
                <span style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  <button className={`chip ${showPicker ? "accent" : ""}`} style={{ cursor: "pointer" }}
                    onClick={() => setShowPicker((v) => !v)}>
                    选择论文（{pickedIds.length || "全部"}）
                  </button>
                  <button className="chip" style={{ cursor: "pointer" }} data-testid="graph-immersive"
                    title="大屏畅想：隐藏侧边栏，画布占满视口（Esc 退出）"
                    onClick={() => setImmersive(!immersive)}>
                    <Maximize size={12} /> {immersive ? "退出沉浸" : "沉浸全屏"}
                  </button>
                  <button className="chip" style={{ cursor: "pointer" }} onClick={exportPng}>导出 PNG</button>
                  <button className="chip" style={{ cursor: "pointer" }} onClick={exportJson}>导出 JSON</button>
                  <select value={commId} onChange={(e) => setCommId(Number(e.target.value))}
                    style={{ border: "1px solid var(--line-strong)", borderRadius: 6, padding: "4px 8px" }}>
                    {graph.communities.map((c) => (
                      <option key={c.id} value={c.id}>社区 {c.id}（{c.size} 篇）</option>
                    ))}
                  </select>
                  <button className="btn primary" onClick={runSurvey} disabled={surveying || activeIds.length < 2}>
                    {surveying ? "领域综述生成中…" : "生成领域综述"}
                  </button>
                  <button className="btn primary" onClick={runSummary} disabled={summarizing}>
                    {summarizing ? "社区综述…" : "社区综述"}
                  </button>
                  <button className="btn ghost" onClick={runIdeas}>研究缝隙分析</button>
                  <button className="btn ghost" onClick={runChecks}>
                    {checks ? "收起外部核查" : "外部最新进展核查"}
                  </button>
                </span>
              </div>
              {infoOpen && (
                <div className="graph-info-pop" data-testid="graph-info-pop">
                  <span className="chip num">{graph.nodes.length} 节点 · {graph.edge_count} 边</span>
                  <span className="chip num">外部引用 {graph.external_count} 条</span>
                  <span className="chip num">backend {graph.backend}</span>
                  {graph.modularity != null && (
                    <span className="chip num">模块度 Q={graph.modularity}</span>
                  )}
                  {graph.insights?.cornerstone?.length > 0 && (
                    <span className="chip ok">🪨 基石 ×{graph.insights.cornerstone.length}</span>
                  )}
                  {graph.insights?.peripheral?.length > 0 && (
                    <span className="chip">🌿 边缘衍生 ×{graph.insights.peripheral.length}</span>
                  )}
                  {(() => {
                    // 含金量统计兼容：新后端写入 insights；旧响应/缓存挂在响应顶层
                    // （web/lib/api.ts 的 FullGraph 尚未声明顶层字段，用局部断言兼容，不改共享类型）
                    const legacy = graph as FullGraph & {
                      class_stat?: Record<string, number>;
                      mention_count?: number;
                    };
                    const stat = graph.insights?.class_stat ?? legacy.class_stat;
                    if (!stat) return null;
                    const mentions = graph.insights?.mention_count ?? legacy.mention_count;
                    return (
                      <span className="chip" title={`实质/背景/凑数 · 共 ${mentions ?? 0} 处引用标记`}>
                        含金量：实质 {stat.substantive ?? 0} ·
                        背景 {stat.background ?? 0} ·
                        凑数 {stat.perfunctory ?? 0}
                      </span>
                    );
                  })()}
                </div>
              )}
              {showPicker && (
                <div style={{ marginTop: 10, borderTop: "1px dashed var(--line)", paddingTop: 10 }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                    <button className="chip" style={{ cursor: "pointer" }}
                      onClick={() => setPickedIds(papers.map((p) => p.id))}>全选</button>
                    <button className="chip" style={{ cursor: "pointer" }}
                      onClick={() => setPickedIds([])}>清空（=全部）</button>
                    <span className="kv" style={{ fontSize: 11 }}>
                      已选 {pickedIds.length || papers.length} / {papers.length} 篇（赛题建议批量 5–20 篇；≥2 篇可建图）
                    </span>
                  </div>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", maxHeight: 140, overflowY: "auto" }}>
                    {papers.map((p) => (
                      <label key={p.id} className="chip"
                        style={{ cursor: "pointer", display: "flex", gap: 4, alignItems: "center", fontSize: 11 }}>
                        <input type="checkbox"
                          checked={pickedIds.length ? pickedIds.includes(p.id) : true}
                          onChange={() => togglePick(p.id)} />
                        {(p.title || p.id).slice(0, 26)}
                      </label>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {surveySections.length > 0 && (
              <div className="card card-in" style={{ ["--i" as string]: 2 }}>
                <h2>领域综述（整批 {activeIds.length} 篇 · 五节结构）
                  {surveyCached && <span className="chip ok" style={{ marginLeft: 8 }}>缓存命中</span>}
                  <button className="chip" style={{ cursor: "pointer", marginLeft: 8 }}
                    onClick={exportSurveyMd}>导出 MD</button>
                  <button className="chip" style={{ cursor: "pointer", marginLeft: 6 }}
                    onClick={() => { setSurveySections([]); setSurveyNote(""); }}>收起</button>
                </h2>
                {surveyNote && <p className="kv">{surveyNote}</p>}
                {surveySections.map((s) => (
                  <div key={s.title} style={{ marginBottom: 12 }}>
                    <h3 style={{ margin: "8px 0 4px" }}>{s.title}</h3>
                    <EvidenceText text={s.content} anchors={s.anchors ?? {}} />
                  </div>
                ))}
              </div>
            )}

            {checks && (
              <div className="card card-in" style={{ ["--i" as string]: 3 }}>
                <h2>外部最新进展核查
                  <span className="chip accent" style={{ marginLeft: 8 }}>S2 证据 · open_unverified 诚实态</span>
                </h2>
                <p className="kv">{checkMsg}</p>
                {checks.length === 0 && <p className="kv">未抽取到 future work 想法（所选论文尾章无明确未来方向表述）。</p>}
                {checks.map((c, i) => (
                  <div key={i} className="kv" style={{
                    border: `1px solid ${c.verdict === "solved" ? "var(--warn)" : c.verdict === "open_unverified" ? "var(--line)" : "var(--accent)"}`,
                    borderRadius: 8, padding: 8, marginBottom: 8,
                  }}>
                    <span className={`chip ${c.verdict === "solved" ? "warn" : c.verdict === "partial" ? "" : "ok"}`}
                      style={{ fontSize: 10, marginRight: 8 }}>
                      {c.verdict}{c.confidence ? ` ${Math.round(c.confidence * 100)}%` : ""}
                    </span>
                    {c.idea_text}
                    {c.mention_papers && c.mention_papers.length > 1 && (
                      <span className="chip" style={{ fontSize: 10, marginLeft: 6 }}>
                        {c.mention_papers.length} 篇共同提及
                      </span>
                    )}
                    {c.external.length > 0 ? (
                      <div style={{ fontSize: 11, opacity: 0.75, margin: "6px 0 0" }}>
                        外部证据：
                        {c.external.map((e, j) => (
                          <div key={j}>· [{e.year}] {e.title}
                            {e.citation_count != null ? `（被引 ${e.citation_count}）` : ""}</div>
                        ))}
                        {c.reason && <div>判定：{c.reason}</div>}
                      </div>
                    ) : (
                      <p style={{ fontSize: 11, opacity: 0.7, margin: "4px 0 0" }}>{c.reason}</p>
                    )}
                  </div>
                ))}
              </div>
            )}

            <div className="card graph-canvas-card card-in" style={{
              height: immersive
                ? "calc(100vh - var(--topbar-h) - var(--space-8))"
                : "max(560px, calc(100vh - var(--topbar-h) - 180px))",
              position: "relative", ["--i" as string]: 1,
            }}>
              <div ref={containerRef} style={{ width: "100%", height: "100%" }} />
              <div className="graph-legend">
                <span>● 大小=PageRank</span>
                <span>◐ 颜色=社区</span>
                <span>证=已验证</span>
                <span>基=基石论文</span>
                <span>— 边粗=含金量</span>
              </div>
              <div style={{ position: "absolute", right: 12, bottom: 10, fontSize: 11, opacity: 0.6 }}>
                （红边=基石 / 绿边=边缘衍生）· 点击节点 → 详情抽屉
              </div>
            </div>

            {/* 节点详情抽屉：点击节点右侧弹簧滑入，点遮罩/空白画布关闭 */}
            <AnimatePresence>
              {selected && (
                <>
                  <motion.div className="drawer-mask" key="g-mask"
                    initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
                    transition={{ duration: 0.2 }}
                    onClick={() => setSelected(null)} />
                  <motion.aside className="drawer" key="g-drawer" data-testid="graph-drawer"
                    initial={{ x: "105%" }} animate={{ x: 0 }} exit={{ x: "105%" }}
                    transition={{ type: "spring", stiffness: 320, damping: 32 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8,
                      padding: "var(--space-4)", borderBottom: "1px solid var(--line)",
                      flexShrink: 0 }}>
                      <b style={{ flex: 1, minWidth: 0, overflow: "hidden",
                        textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{selected.title}</b>
                      <button className="chip" style={{ cursor: "pointer", flexShrink: 0 }}
                        onClick={() => setSelected(null)}>✕ 关闭</button>
                    </div>
                    <div style={{ padding: "var(--space-4)", overflowY: "auto", flex: 1 }}>
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
                        {selected.role && (
                          <span className={`chip ${selected.role === "cornerstone" ? "ok" : ""}`}
                            title={selected.role_reason ?? undefined}>
                            {ROLE_BADGE[selected.role] ?? selected.role}
                          </span>
                        )}
                        {selected.year && <span className="chip num">{selected.year} 年</span>}
                        <span className="chip">社区 {selected.community}</span>
                        <Link className="chip accent" href={`/reader/${selected.paper_id}`}>打开阅读器 →</Link>
                        <Link className="chip" href={`/chat?paper=${selected.paper_id}`}>去问答 →</Link>
                      </div>
                      <div className="kv num" style={{ lineHeight: 2 }}>
                        PageRank <b>{selected.pagerank}</b> · 被引 <b>{selected.in_degree}</b> ·
                        引出 <b>{selected.out_degree}</b> · 介数 <b>{selected.betweenness}</b>
                        {selected.time_rank != null && <> · 时间序位 <b>{selected.time_rank}</b></>}
                      </div>
                      {selected.role_reason && (
                        <p className="kv" style={{ fontSize: 12, color: "var(--ink-2)",
                          background: "var(--surface-2)", borderRadius: 6, padding: "6px 8px" }}>
                          🧭 {selected.role_reason}
                        </p>
                      )}
                      <h2 style={{ fontSize: 13, marginTop: 12 }}>引用明细（实质性引用句）</h2>
                      {selectedEdges.flatMap((e) => e.examples ?? []).length === 0 && (
                        <p className="kv">无实质性引用句记录（边或为背景/凑数引用）。</p>
                      )}
                      {selectedEdges.flatMap((e) => e.examples ?? []).slice(0, 4).map((x, i) => (
                        <p key={i} className="kv" style={{ borderBottom: "1px dashed var(--line)",
                          padding: "4px 0", fontSize: 12 }}>
                          「{x.sentence.slice(0, 80)}」<span className="chip ok" style={{ fontSize: 10 }}>实质</span>
                        </p>
                      ))}
                    </div>
                  </motion.aside>
                </>
              )}
            </AnimatePresence>

            {summary && (
              <div className="card card-in" style={{ ["--i" as string]: 2 }}>
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
              <div className="card card-in" style={{ ["--i" as string]: 3 }}>
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
