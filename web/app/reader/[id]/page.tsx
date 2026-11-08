"use client";

/**
 * 阅读器：PDF 原文 / DocIR 文本 / 双语对照 / 原版对照 四视图。
 * - 按需加载（4.3）：首屏只拉 outline（meta+toc+references，几 KB）→ 秒开标题树；
 *   正文按页 ?page=N 拉取（541 页书不再一次拖数 MB）
 * - 对照模式（4.3）：左原版 PDF + 右 DocIR 文本，页码联动；公式点击 → 左栏 bbox 红框联动
 * - 阅读顺序红绿开关（4.3）：meta.demo_raw_order 存第 2 页原始块序，一键演示乱序→重排
 * - 双语对照（4.7）：block 级原文/译文两栏 + IntersectionObserver 滚动同步（防回环）
 * - 划词翻译浮窗 + 术语表 / [n] ↔ References 双向跳转（4.7）
 * - 公式 KaTeX + 扫描件徽章 + 解析报告（含解析峰值内存曲线，4.3）
 */
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import katex from "katex";
import "katex/dist/katex.min.css";
import { API, Paper, getPaper } from "@/lib/api";
import {
  TranslationEntry, fetchTranslation, streamTranslate, translateSelection, addTerm,
} from "@/lib/api";
import PdfAnchored from "@/components/PdfAnchored";

type ViewMode = "pdf" | "text" | "bilingual" | "compare";

interface Block {
  id: string; type: string; text: string; latex?: string | null; page: number;
  bbox: number[]; section_path: string; level?: number;
}
interface Figure {
  id: string; page: number; caption: string; bbox?: number[]; image_path?: string;
}
interface RefEntry { index: number; raw?: string; title?: string; arxiv_id?: string; doi?: string }
interface Quality {
  text_coverage?: number; toc_depth?: number; formula_count?: number;
  table_count?: number; reference_parse_rate?: number | null; scanned_ratio?: number;
}
interface DemoRawOrder { page: number; x0_order: number[] }
interface DocIRMeta {
  title: string; arxiv_id: string; page_count: number;
  parse_engine?: string; route_mode?: string; parse_quality?: number;
  scanned?: boolean; long_doc?: boolean; scanned_pages?: number[];
  quality?: Quality;
  demo_raw_order?: DemoRawOrder;
  parse_memory?: { peak_mb: number; samples: number };
}
interface PagePayload { blocks: Block[]; tables: { id: string; page: number; markdown: string }[]; figures: Figure[] }
interface OutlinePayload {
  meta: DocIRMeta;
  toc: { level: number; title: string; page: number }[];
  references: RefEntry[];
  figures: Figure[];
  pages: number[];
  block_count: number;
  tables_count: number;
}

function katexHtml(tex: string, display: boolean): string {
  try {
    return katex.renderToString(tex, { displayMode: display, throwOnError: false });
  } catch {
    return `<code>${tex.replace(/</g, "&lt;")}</code>`;
  }
}

/** [n] 引用标记 chip（正向跳 References，反向由 refsIndex 高亮）。 */
function CitationChips({ text, onCite, highlightRef }: {
  text: string; onCite?: (n: number) => void; highlightRef?: number | null;
}) {
  const parts = useMemo(
    () => text.split(/(\$[^$\n]+\$|\[\d{1,3}(?:,\s*\d{1,3})*\])/g), [text]);
  return (
    <>
      {parts.map((p, i) => {
        if (p.startsWith("$") && p.endsWith("$") && p.length > 2) {
          return <span key={i} dangerouslySetInnerHTML={{ __html: katexHtml(p.slice(1, -1), false) }} />;
        }
        const m = p.match(/^\[(\d{1,3})(?:,\s*\d{1,3})*\]$/);
        if (m && onCite) {
          const n = Number(m[1]);
          return (
            <span key={i}
              className="chip anchor-chip"
              style={{
                cursor: "pointer", fontSize: 11, padding: "0 6px",
                background: highlightRef === n ? "var(--accent, #2563eb)" : undefined,
                color: highlightRef === n ? "#fff" : undefined,
              }}
              title={`跳转到参考文献 [${n}]`}
              onClick={() => onCite(n)}>[{n}]</span>
          );
        }
        return <span key={i}>{p}</span>;
      })}
    </>
  );
}

/** 公式块：KaTeX 渲染 + LaTeX 复制 + （对照模式）点击联动原文 bbox。 */
function FormulaBlock({ latex, onLocate }: { latex: string; onLocate?: () => void }) {
  const [copied, setCopied] = useState(false);
  const html = useMemo(() => katexHtml(latex, true), [latex]);
  return (
    <div className="formula-block">
      <div dangerouslySetInnerHTML={{ __html: html }} />
      <div style={{ display: "flex", gap: 8 }}>
        <button className="chip" style={{ cursor: "pointer", fontSize: 11 }}
          onClick={() => {
            navigator.clipboard.writeText(latex).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            });
          }}>
          {copied ? "已复制 ✓" : "复制 LaTeX"}
        </button>
        {onLocate && (
          <button className="chip accent" style={{ cursor: "pointer", fontSize: 11 }} onClick={onLocate}>
            在原版 PDF 定位 →
          </button>
        )}
      </div>
    </div>
  );
}

/** 解析报告面板：六指标 + 解析引擎 + 峰值内存（4.3）。 */
function QualityPanel({ meta }: { meta: DocIRMeta }) {
  const q = meta.quality ?? {};
  const row = (k: string, v: React.ReactNode) => (
    <div key={k} style={{ display: "flex", justifyContent: "space-between",
      borderBottom: "1px dashed var(--line)", padding: "4px 0" }}>
      <span className="kv">{k}</span><span className="kv"><b>{v}</b></span>
    </div>
  );
  return (
    <div className="card" style={{ marginBottom: 0, padding: "var(--space-3)" }}>
      <h2 style={{ marginTop: 0 }}>解析报告</h2>
      {row("解析引擎", meta.parse_engine ?? "pymupdf")}
      {row("路由模式", meta.route_mode ?? "-")}
      {row("综合质量分", meta.parse_quality != null ? `${Math.round(meta.parse_quality * 100)}%` : "-")}
      {row("文本覆盖率", q.text_coverage != null ? `${Math.round(q.text_coverage * 100)}%` : "-")}
      {row("标题层级深度", q.toc_depth ?? "-")}
      {row("公式识别数", q.formula_count ?? 0)}
      {row("表格数", q.table_count ?? 0)}
      {row("参考文献解析率",
        q.reference_parse_rate != null ? `${Math.round(q.reference_parse_rate * 100)}%` : "无参考文献")}
      {row("扫描页占比", q.scanned_ratio ? `${Math.round(q.scanned_ratio * 100)}%` : "0%")}
      {meta.parse_memory && (
        <div>
          {row("解析峰值内存", `${Math.round(meta.parse_memory.peak_mb)} MB（${meta.parse_memory.samples} 采样点）`)}
          <div style={{ height: 6, background: "var(--line, #eee)", borderRadius: 3, marginTop: 6 }}>
            <div style={{
              width: `${Math.min(100, Math.round((meta.parse_memory.peak_mb / 2048) * 100))}%`,
              height: "100%",
              background: meta.parse_memory.peak_mb < 2048 ? "var(--ok, #16a34a)" : "var(--warn)",
              borderRadius: 3 }} />
          </div>
          <p className="kv" style={{ fontSize: 11, marginTop: 4 }}>
            相对 2GB 预算占用（长文档约束 &lt;2GB ✓）
          </p>
        </div>
      )}
      {meta.scanned && (
        <p className="kv" style={{ color: "var(--warn)", marginTop: "var(--space-2)" }}>
          ⚠ 本文档含扫描/OCR 识别页，识别结果可能不准确，请人工核对。
        </p>
      )}
    </div>
  );
}

/** 划词翻译浮窗：选区上方弹出，上下文短译 + 术语命中 + 加入术语表。 */
function SelectionPopup({ sel, onClose }: {
  sel: { x: number; y: number; text: string; context: string } | null;
  onClose: () => void;
}) {
  const [result, setResult] = useState<{ translation: string; term_hit?: { term: string; translation: string } | null; ok: boolean } | null>(null);
  const [loading, setLoading] = useState(false);
  const [added, setAdded] = useState(false);

  useEffect(() => {
    if (!sel) { setResult(null); setAdded(false); return; }
    setLoading(true);
    translateSelection(sel.text, sel.context)
      .then(setResult)
      .finally(() => setLoading(false));
  }, [sel]);

  if (!sel) return null;
  const term = result?.term_hit;
  return (
    <div style={{
      position: "fixed", left: Math.min(sel.x, window.innerWidth - 360), top: sel.y + 12,
      width: 340, zIndex: 50, boxShadow: "0 8px 24px rgba(0,0,0,.18)",
    }} className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <b style={{ fontSize: 12 }}>划词翻译</b>
        <button className="chip" style={{ cursor: "pointer" }} onClick={() => { onClose(); }}>✕</button>
      </div>
      <p style={{ fontSize: 12, opacity: 0.7, margin: "6px 0" }}>
        原文：{sel.text.slice(0, 80)}{sel.text.length > 80 ? "…" : ""}
      </p>
      {loading && <p className="kv">翻译中…</p>}
      {!loading && result && (
        <>
          <p style={{ fontSize: 13, margin: "6px 0" }}>{result.translation || "（翻译不可用）"}</p>
          {term && (
            <p className="kv" style={{ color: "var(--ok, #16a34a)" }}>
              术语表命中：{term.term} → {term.translation}
            </p>
          )}
          <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
            <button className="chip accent" style={{ cursor: "pointer", fontSize: 11 }}
              onClick={() => {
                const t = term?.term ?? sel.text.slice(0, 60);
                const tr = term?.translation ?? result.translation;
                if (t && tr) addTerm(t, tr).then(() => setAdded(true));
              }}>
              {added ? "已加入 ✓" : "加入术语表"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function ReaderInner() {
  const { id } = useParams<{ id: string }>();
  const search = useSearchParams();
  const [paper, setPaper] = useState<Paper | null>(null);
  const [outline, setOutline] = useState<OutlinePayload | null>(null);
  const [pageData, setPageData] = useState<PagePayload | null>(null);
  const [pageLoading, setPageLoading] = useState(false);
  const [page, setPage] = useState(1);                       // 1 基人读页码
  const [query, setQuery] = useState("");
  const [view, setView] = useState<ViewMode>("pdf");
  const [showReport, setShowReport] = useState(false);
  const [orderOn, setOrderOn] = useState(true);              // 阅读顺序开关（4.3）
  const [focusBbox, setFocusBbox] = useState<number[] | undefined>(undefined); // 公式联动

  // ---- 4.7 翻译 ----
  const [trans, setTrans] = useState<Record<string, TranslationEntry>>({});
  const [translating, setTranslating] = useState<{ done: number; total: number } | null>(null);

  // ---- 4.7 [n] 双向跳转 ----
  const [highlightRef, setHighlightRef] = useState<number | null>(null);
  const [libraryArxivIds, setLibraryArxivIds] = useState<Set<string>>(new Set());
  const refsPanelRef = useRef<HTMLDivElement | null>(null);

  // ---- 4.7 划词翻译 ----
  const [sel, setSel] = useState<{ x: number; y: number; text: string; context: string } | null>(null);

  // ---- 4.7 双滚动同步 ----
  const origPaneRef = useRef<HTMLDivElement | null>(null);
  const zhPaneRef = useRef<HTMLDivElement | null>(null);
  const syncingRef = useRef(false);
  const syncTimer = useRef<number | null>(null);
  const textPaneRef = useRef<HTMLDivElement | null>(null);

  const urlPage = Number(search.get("page")) || 0;
  const urlBbox = useMemo(() => {
    const raw = search.get("bbox") || "";
    const nums = raw.split(",").map((x) => Number(x));
    return nums.length === 4 && nums.every((n) => Number.isFinite(n)) ? nums : undefined;
  }, [search]);

  // 按需加载：首屏 outline（几 KB 秒开）+ 当前页正文
  useEffect(() => {
    getPaper(id as string).then(setPaper);
    fetch(`${API}/api/papers/${id}/docir?outline=1`, { cache: "no-store" })
      .then((r) => r.json()).then(setOutline);
    fetchTranslation(id as string).then(setTrans);
    fetch(`${API}/api/papers`, { cache: "no-store" }).then((r) => r.json()).then((d) => {
      const ids = new Set<string>();
      for (const p of d.papers ?? []) {
        const a = (p.arxiv_id ?? "").replace(/v\d+$/, "");
        if (a) ids.add(a);
      }
      setLibraryArxivIds(ids);
    }).catch(() => {});
  }, [id]);

  const fetchPage = useCallback((p: number) => {
    setPageLoading(true);
    fetch(`${API}/api/papers/${id}/docir?page=${p - 1}`, { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => setPageData({ blocks: d.blocks ?? [], tables: d.tables ?? [], figures: d.figures ?? [] }))
      .finally(() => setPageLoading(false));
  }, [id]);

  useEffect(() => {
    if (outline) fetchPage(page);
  }, [outline, page, fetchPage]);

  useEffect(() => {
    if (urlPage > 0) setPage(urlPage);
  }, [urlPage]);

  const markSync = useCallback(() => {
    syncingRef.current = true;
    if (syncTimer.current) window.clearTimeout(syncTimer.current);
    syncTimer.current = window.setTimeout(() => { syncingRef.current = false; }, 120);
  }, []);

  /** 单侧滚动 → 对侧同步（IntersectionObserver：跨视口中线的块视为当前块）。 */
  useEffect(() => {
    if (view !== "bilingual") return;
    const makeObserver = (srcPane: HTMLElement, dstPane: HTMLElement | null) => {
      if (!dstPane) return null;
      return new IntersectionObserver((entries) => {
        if (syncingRef.current) return;
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          const bid = (e.target as HTMLElement).dataset.bid;
          if (!bid) continue;
          markSync();
          const dst = dstPane.querySelector(`[data-bid="${bid}"]`);
          window.requestAnimationFrame(() => {
            dst?.scrollIntoView({ block: "center", behavior: "auto" });
          });
          break;
        }
      }, { root: srcPane, rootMargin: "-45% 0px -45% 0px", threshold: 0 });
    };
    const o1 = makeObserver(origPaneRef.current!, zhPaneRef.current);
    const o2 = makeObserver(zhPaneRef.current!, origPaneRef.current);
    const bind = (pane: HTMLElement | null, obs: IntersectionObserver | null) => {
      if (!pane || !obs) return;
      pane.querySelectorAll("[data-bid]").forEach((el) => obs.observe(el));
    };
    bind(origPaneRef.current, o1);
    bind(zhPaneRef.current, o2);
    return () => { o1?.disconnect(); o2?.disconnect(); };
  }, [view, page, trans, markSync]);

  // refsIndex：[n] → 出现的 block id 列表（一次遍历建好，反向跳转数据源）
  const fullRefsIndex = useMemo(() => {
    const m = new Map<number, string[]>();
    for (const b of pageData?.blocks ?? []) {
      for (const match of b.text.matchAll(/\[(\d{1,3})\]/g)) {
        const n = Number(match[1]);
        if (!m.has(n)) m.set(n, []);
        if (!m.get(n)!.includes(b.id)) m.get(n)!.push(b.id);
      }
    }
    return m;
  }, [pageData]);

  const refs = useMemo(
    () => [...(outline?.references ?? [])].sort((a, b) => a.index - b.index), [outline]);

  // 阅读顺序红绿开关素材 + 重排逻辑（hooks 顺序约束：均在 early return 之前）
  const rawOrder = outline?.meta?.demo_raw_order;
  const showOrderToggle = !!rawOrder && page === rawOrder.page;

  // off → 按原始 x0 序重排（红边演示"乱序"）
  const displayBlocks: (Block & { rawIdx?: number })[] = useMemo(() => {
    const bs = pageData?.blocks ?? [];
    if (!orderOn && showOrderToggle && rawOrder?.x0_order?.length) {
      const xs = rawOrder.x0_order;
      const idx = (x: number) => {
        const f = xs.findIndex((v) => Math.abs(v - x) < 0.01);
        return f >= 0 ? f : xs.length;
      };
      return [...bs].sort((a, b) => idx(a.bbox?.[0] ?? 0) - idx(b.bbox?.[0] ?? 0))
        .map((b) => ({ ...b, rawIdx: idx(b.bbox?.[0] ?? 0) }));
    }
    return bs;
  }, [pageData, orderOn, showOrderToggle, rawOrder]);

  if (!outline) {
    return (
      <div className="page"><div className="page-inner">
        <p className="kv">加载大纲中…（若刚上传请等解析完成）</p>
      </div></div>
    );
  }

  const meta = outline.meta;
  const pages = outline.pages;
  const blocks = displayBlocks
    .filter((b) => !query || b.text.toLowerCase().includes(query.toLowerCase()));
  const pageFigures = (pageData?.figures ?? outline.figures).filter((f) => f.page === page - 1);

  const gotoRef = (n: number) => {
    setHighlightRef(n);
    if (view === "pdf") setView("text");
    window.setTimeout(() => {
      refsPanelRef.current?.querySelector(`[data-refidx="${n}"]`)
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
    }, 60);
  };

  const jumpFromRef = (n: number) => {
    setHighlightRef(n);
    const firstBlock = fullRefsIndex.get(n)?.[0];
    if (firstBlock) {
      const pane = view === "bilingual" ? origPaneRef.current
        : view === "compare" ? textPaneRef.current : textPaneRef.current;
      pane?.querySelector(`[data-bid="${firstBlock}"]`)
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  };

  const translatePage = () => {
    if (translating) return;
    setTranslating({ done: 0, total: 0 });
    streamTranslate(id as string, { page: page - 1 }, {
      tDelta: (d) => {
        setTrans((prev) => ({ ...prev, [d.block_id]: { text: d.text, format_ok: d.format_ok } }));
        setTranslating({ done: d.done, total: d.total });
      },
      final: () => setTranslating(null),
      error: (d) => { window.alert(`翻译失败：${d.message}`); setTranslating(null); },
      done: () => setTranslating(null),
    }).catch((e) => { window.alert(`翻译请求失败：${e}`); setTranslating(null); });
  };

  const onTextMouseUp = () => {
    const s = window.getSelection();
    const text = s?.toString().trim() ?? "";
    if (!s || text.length < 3 || text.length > 2000) return;
    const rect = s.getRangeAt(0).getBoundingClientRect();
    const focused = s.focusNode?.parentElement?.closest("[data-bid]") as HTMLElement | null;
    const ctx = focused?.dataset?.bid
      ? (pageData?.blocks ?? []).find((b) => b.id === focused.dataset.bid)?.text ?? "" : "";
    setSel({ x: rect.left, y: rect.bottom, text, context: ctx });
  };

  const renderBlockBody = (b: Block, cite: boolean) =>
    b.type === "formula" && b.latex ? (
      <FormulaBlock latex={b.latex}
        onLocate={view === "compare" && b.bbox?.length === 4
          ? () => setFocusBbox(b.bbox) : undefined} />
    ) : b.type === "heading" ? (
      <b>{b.text}</b>
    ) : (
      <p>{cite ? <CitationChips text={b.text} onCite={gotoRef} highlightRef={highlightRef} /> : b.text}</p>
    );

  const renderTextBlocks = (cite: boolean) => (
    <>
      {showOrderToggle && (
        <button className="chip" style={{ cursor: "pointer", marginBottom: 8 }}
          onClick={() => setOrderOn((v) => !v)}
          title="演示：原始解析顺序（双栏交错）vs 阅读顺序重排">
          阅读顺序：{orderOn ? "✅ 重排后（正确序）" : "🔴 原始序（乱序演示）"}
        </button>
      )}
      {blocks.map((b) => (
        <div key={b.id} data-bid={b.id} className="block-item"
          style={{
            ...((b.bbox?.length === 4 && (urlBbox ?? focusBbox) &&
              Math.abs(b.bbox[1] - (urlBbox ?? focusBbox)![1]) < 0.02)
              ? { background: "var(--warn-soft)", borderRadius: "var(--radius-sm)" } : {}),
            ...(b.rawIdx != null
              ? { borderLeft: "3px solid #dc2626", paddingLeft: 8 }
              : showOrderToggle ? { borderLeft: "3px solid #16a34a", paddingLeft: 8 } : {}),
          }}>
          {renderBlockBody(b, cite)}
          <span className="block-anchor">
            {b.id}{b.rawIdx != null ? ` · 原始序 #${b.rawIdx}` : ""}
            {" · "}§{b.section_path === "_front" || !b.section_path ? "前言" : b.section_path}
            {b.type === "formula" ? " · formula" : ""}
            {" · "}bbox [{b.bbox.map((x) => Number(x).toFixed(2)).join(", ")}]
          </span>
        </div>
      ))}
      {blocks.length === 0 && <p className="kv">本页无匹配内容。</p>}
    </>
  );

  return (
    <>
      <div className="topbar">
        <span className="topbar-title">{meta.title || paper?.title || "阅读器"}</span>
        <span className="chip">{meta.page_count} 页</span>
        <span className="chip">{outline.block_count} blocks</span>
        <span className="chip">{outline.tables_count} 表 · {outline.figures.length} 图
          {meta.quality?.formula_count ? ` · ${meta.quality.formula_count} 公式` : ""}</span>
        {meta.scanned && (
          <span className="chip warn" title="含扫描/OCR 识别页，识别结果可能不准确">
            ⚠ 扫描件 · OCR/VLM
          </span>
        )}
        {meta.long_doc && (
          <span className="chip" title="超长文档：按需加载已启用，标题树秒开，正文按页拉取">
            长文档 · 按需加载
          </span>
        )}
        <button className="chip" style={{ cursor: "pointer" }} onClick={() => setShowReport((s) => !s)}>
          解析报告 <span style={{ opacity: 0.7 }}>{meta.parse_engine ?? ""}</span>
        </button>
      </div>
      <div className="page">
        <div className="reader-grid">
          <div className="card toc-panel" style={{ marginBottom: 0, maxHeight: "100%" }}>
            <h2>标题树（{meta.parse_engine === "mineru" ? "MinerU" : "DocIR"} 解析）</h2>
            {outline.toc.map((t, i) => (
              <div key={i} style={{ paddingLeft: (t.level - 1) * 14 }}>
                <span className="toc-item" onClick={() => setPage(t.page + 1)}>
                  {t.level === 1 ? <b>{t.title}</b> : t.title} <span className="toc-page">p{t.page + 1}</span>
                </span>
              </div>
            ))}
            {outline.toc.length === 0 && <p className="kv">未识别出标题结构。</p>}
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)", minHeight: 0 }}>
            <div className="card" style={{ marginBottom: 0 }}>
              <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", flexWrap: "wrap" }}>
                <b className="kv">第 {page} 页{pageLoading ? " · 加载中…" : ""}</b>
                <button className="btn ghost" style={{ padding: "4px 12px" }}
                  onClick={() => setPage(Math.max(1, page - 1))}>←</button>
                <select value={page} onChange={(e) => setPage(Number(e.target.value))}
                  style={{ border: "1px solid var(--line-strong)", borderRadius: "var(--radius-sm)",
                    padding: "4px 8px", fontFamily: "var(--font)", fontSize: "var(--text-sm)" }}>
                  {pages.map((p) => <option key={p} value={p + 1}>p{p + 1}</option>)}
                </select>
                <button className="btn ghost" style={{ padding: "4px 12px" }}
                  onClick={() => setPage(Math.min((pages.at(-1) ?? 0) + 1, page + 1))}>→</button>
                {([["pdf", "PDF 原文"], ["text", "DocIR 文本"], ["bilingual", "双语对照"],
                  ["compare", "原版对照"]] as [ViewMode, string][]).map(([m, label]) => (
                  <button key={m} className={`chip ${view === m ? "accent" : ""}`}
                    style={{ cursor: "pointer" }} onClick={() => setView(m)}>{label}</button>
                ))}
                {view !== "pdf" && (
                  <button className="chip" style={{ cursor: "pointer" }} onClick={translatePage}
                    title="翻译本页（block 级，公式/代码/引用标记占位符保护，缓存续译）">
                    {translating ? `翻译中 ${translating.done}/${translating.total}…` : "翻译本页"}
                  </button>
                )}
                <input placeholder="页内搜索…" value={query} onChange={(e) => setQuery(e.target.value)}
                  style={{ marginLeft: "auto", border: "1px solid var(--line-strong)",
                    borderRadius: "var(--radius-sm)", padding: "6px 10px",
                    fontFamily: "var(--font)", fontSize: "var(--text-sm)", outline: "none" }} />
              </div>
              {translating && (
                <div style={{ height: 4, background: "var(--line)", borderRadius: 2, marginTop: 8 }}>
                  <div style={{
                    width: `${translating.total ? Math.round((translating.done / translating.total) * 100) : 5}%`,
                    height: "100%", background: "var(--accent, #2563eb)", borderRadius: 2,
                    transition: "width .3s",
                  }} />
                </div>
              )}
            </div>

            {showReport && <QualityPanel meta={meta} />}

            {view === "pdf" ? (
              <div className="card" style={{ marginBottom: 0, flex: 1, overflowY: "auto" }}>
                <PdfAnchored paperId={id as string} page={page}
                  bbox={(urlBbox ?? focusBbox)?.length === 4 ? (urlBbox ?? focusBbox) : undefined}
                  width={680} />
              </div>
            ) : view === "compare" ? (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-3)", flex: 1, minHeight: 0 }}>
                <div className="card" style={{ marginBottom: 0, overflowY: "auto" }}>
                  <h2 style={{ margin: "0 0 8px" }}>原版 PDF（p{page}）</h2>
                  <PdfAnchored paperId={id as string} page={page}
                    bbox={(urlBbox ?? focusBbox)?.length === 4 ? (urlBbox ?? focusBbox) : undefined}
                    width={480} />
                </div>
                <div className="card" ref={textPaneRef} onMouseUp={onTextMouseUp}
                  style={{ marginBottom: 0, overflowY: "auto" }}>
                  <h2 style={{ margin: "0 0 8px" }}>结构化文本（DocIR）{focusBbox ? " · 公式已联动定位" : ""}</h2>
                  {renderTextBlocks(true)}
                </div>
              </div>
            ) : view === "text" ? (
              <div className="card" ref={textPaneRef} onMouseUp={onTextMouseUp}
                style={{ marginBottom: 0, flex: 1, overflowY: "auto", position: "relative" }}>
                {renderTextBlocks(true)}
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-3)", flex: 1, minHeight: 0 }}>
                <div className="card" ref={origPaneRef} onMouseUp={onTextMouseUp}
                  style={{ marginBottom: 0, overflowY: "auto" }}>
                  <h2 style={{ position: "sticky", top: 0, background: "var(--bg, #fff)", margin: 0, paddingBottom: 6 }}>原文</h2>
                  {blocks.map((b) => (
                    <div key={b.id} data-bid={b.id} className="block-item">
                      {renderBlockBody(b, false)}
                    </div>
                  ))}
                  {blocks.length === 0 && <p className="kv">本页无内容。</p>}
                </div>
                <div className="card" ref={zhPaneRef} style={{ marginBottom: 0, overflowY: "auto" }}>
                  <h2 style={{ position: "sticky", top: 0, background: "var(--bg, #fff)", margin: 0, paddingBottom: 6 }}>译文（block 级对齐）</h2>
                  {blocks.map((b) => {
                    const t = trans[b.id];
                    return (
                      <div key={b.id} data-bid={b.id} className="block-item"
                        style={!t ? { opacity: 0.45, background: "var(--line-soft, #f5f5f5)", borderRadius: 6 } : undefined}>
                        {b.type === "formula" ? (
                          <p className="kv">（公式块不翻译 · KaTeX 见左栏）</p>
                        ) : t ? (
                          <p style={t.format_ok ? undefined : { color: "var(--warn)" }}>
                            {t.text}
                            {!t.format_ok && <span className="chip warn" style={{ fontSize: 10, marginLeft: 6 }}>占位符校验未过 · 已回退原文</span>}
                          </p>
                        ) : (
                          <p className="kv">未翻译（点上方「翻译本页」）</p>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {(view !== "pdf" && refs.length > 0) && (
              <div className="card" ref={refsPanelRef} style={{ marginBottom: 0, maxHeight: 260, overflowY: "auto" }}>
                <h2>References（{refs.length} 条 · 点击条目反向高亮正文 [n]）</h2>
                {refs.map((r) => {
                  const inLib = r.arxiv_id && libraryArxivIds.has(r.arxiv_id.replace(/v\d+$/, ""));
                  const cited = fullRefsIndex.get(r.index) ?? [];
                  return (
                    <div key={r.index} data-refidx={r.index}
                      onClick={() => jumpFromRef(r.index)}
                      style={{
                        display: "flex", gap: 8, alignItems: "baseline", cursor: "pointer",
                        padding: "4px 6px", borderRadius: 6,
                        background: highlightRef === r.index ? "var(--accent-soft, #eef2ff)" : undefined,
                      }}>
                      <b className="kv" style={{ minWidth: 34 }}>[{r.index}]</b>
                      <span style={{ fontSize: 12, flex: 1 }}>
                        {r.title || r.raw || "（未解析出标题）"}
                      </span>
                      {r.arxiv_id && <span className="chip" style={{ fontSize: 10 }}>{r.arxiv_id}</span>}
                      {inLib && <span className="chip ok" style={{ fontSize: 10 }}>库内</span>}
                      {cited.length > 0 && (
                        <span className="chip" style={{ fontSize: 10 }} title="正文出现次数（当前页）">
                          正文 ×{cited.length}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {pageFigures.filter((f) => f.image_path).map((f) => (
              <div className="card" key={f.id} style={{ marginBottom: 0 }}>
                <h2>图 {f.id}（第 {f.page + 1} 页）{f.caption ? `· ${f.caption.slice(0, 60)}` : ""}</h2>
                <img src={`${API}/api/papers/${id}/figure/${f.id}`} alt={f.caption || f.id}
                  style={{ maxWidth: "100%", borderRadius: "var(--radius-sm)" }}
                  onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }} />
              </div>
            ))}

            {(pageData?.tables ?? []).filter((t) => t.page === page - 1).map((t) => (
              <div className="card" key={t.id} style={{ marginBottom: 0 }}>
                <h2>表格 {t.id}（第 {t.page + 1} 页）</h2>
                <pre style={{ fontSize: "var(--text-xs)", overflowX: "auto", margin: 0 }}>{t.markdown}</pre>
              </div>
            ))}
          </div>
        </div>
      </div>
      <SelectionPopup sel={sel} onClose={() => setSel(null)} />
    </>
  );
}

export default function ReaderPage() {
  return (
    <Suspense fallback={<div className="page"><div className="page-inner"><p className="kv">加载中…</p></div></div>}>
      <ReaderInner />
    </Suspense>
  );
}
