"use client";

/**
 * 阅读器：PDF 原文 / DocIR 文本 / 双语对照 / 原版对照 四视图。
 * - 按需加载（4.3）：首屏只拉 outline（meta+toc+references，几 KB）→ 秒开标题树；
 *   正文按页 ?page=N 拉取（541 页书不再一次拖数 MB）
 * - 对照模式（4.3）：左原版 PDF + 右 DocIR 文本，页码联动；公式点击 → 左栏 bbox 红框联动
 * - 阅读顺序红绿开关（4.3 + 模块三 E 泛化）：demo 素材 / meta.raw_orders 解析记录（真实，红）
 *   / x0 启发（模拟，黄）三级还原，text/compare 视图常驻，如实标注
 * - 双语对照（4.7）：block 级原文/译文两栏 + IntersectionObserver 滚动同步（防回环 +
 *   F 比例对齐微调；流式翻译期间不再重建 observer）
 * - 划词翻译浮窗 + 术语表 / [n] ↔ References 双向跳转（4.7）；PDF 视图 TextLayer 取词（B0）
 * - 模块三 A：标题树块级跳转（section_id）+ 当前章节高亮/面包屑 + 跳转 flash
 * - 模块三 C：阅读进度条 + 前后章按钮 + localStorage 续读恢复（?page= 深链优先）
 * - 模块三 D：页内/全文双模式搜索（FTS 端点跨页命中 → 块级定位）
 * - 模块三 F：悬停翻译（只读缓存，点按才译单块）+ 快捷键 ←/→ j/k t / Esc
 * - 模块三 G：整篇翻译（缓存续译 + AbortController 中断）+ 中英对照 MD/HTML 导出
 * - 公式 KaTeX + 扫描件徽章 + 解析报告（含解析峰值内存曲线，4.3）
 */
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import katex from "katex";
import "katex/dist/katex.min.css";
import { API, Paper, getPaper } from "@/lib/api";
import {
  TranslationEntry, fetchTranslation, streamTranslate, translateSelection, addTerm,
  SearchHit, searchPaper,
} from "@/lib/api";
import PdfAnchored from "@/components/PdfAnchored";

type ViewMode = "pdf" | "text" | "bilingual" | "compare" | "pdf-bilingual";

interface Block {
  id: string; type: string; text: string; latex?: string | null; page: number;
  bbox: number[]; section_path: string; level?: number;
  section_id?: string;      // 模块三 A：块级章节锚（backfill_sections 派生，与 toc.id 对齐）
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
interface TocEntry {
  level: number; title: string; page: number; appendix?: boolean;
  id?: string; parent_id?: string;   // 模块三 A：toc.id="s{n}"（_ensure_toc_ids 派生）
}
interface DocIRMeta {
  title: string; arxiv_id: string; page_count: number;
  parse_engine?: string; route_mode?: string; parse_quality?: number;
  scanned?: boolean; long_doc?: boolean; scanned_pages?: number[];
  quality?: Quality;
  demo_raw_order?: DemoRawOrder;
  raw_orders?: Record<string, number[]>;   // 模块三 E：按页真实重排置换（键=0 基页码）
  parse_memory?: { peak_mb: number; samples: number };
  // 模块一：结构化元数据 / 附录 / 批切页降级记录
  authors?: { name: string; affiliation: string }[];
  abstract?: string;
  venue?: string;
  year?: number;
  doi?: string;
  meta_source?: string;
  appendix?: { start_page: number; sections: string[] };
  fallback_segments?: { start: number; end: number; reason: string }[];
  chunked?: { segments: number; mineru_ok: number };
}
interface PagePayload { blocks: Block[]; tables: { id: string; page: number; markdown: string; headers?: string[]; rows?: string[][] }[]; figures: Figure[] }
interface OutlinePayload {
  meta: DocIRMeta;
  toc: TocEntry[];
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
function QualityPanel({ meta, paperId }: { meta: DocIRMeta; paperId?: string }) {
  const [reparsing, setReparsing] = useState(false);
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
      {meta.fallback_segments?.length ? (
        <p className="kv" style={{ color: "var(--warn)", marginTop: "var(--space-2)" }}>
          ⚠ 批切页解析中 {meta.fallback_segments.length} 段走 PyMuPDF 兜底
          （{meta.fallback_segments.map((f) => `p${f.start + 1}-${f.end}`).join("、")}
          ），该段无公式 LaTeX，如实标注。
        </p>
      ) : null}
      {meta.appendix && (
        <p className="kv" style={{ marginTop: "var(--space-2)" }}>
          附录：{meta.appendix.sections.length} 节（起 p{(meta.appendix.start_page ?? 0) + 1}）
        </p>
      )}
      {paperId && (
        <button className="mini-btn" style={{ marginTop: "var(--space-2)" }}
          disabled={reparsing}
          onClick={async () => {
            setReparsing(true);
            try {
              const { reparsePaper } = await import("@/lib/api");
              await reparsePaper(paperId);
              window.location.reload();
            } finally { setReparsing(false); }
          }}>
          {reparsing ? "已提交…" : "重新解析"}
        </button>
      )}
    </div>
  );
}

/** 文献信息卡（模块一 P0：作者+机构 / 摘要 / venue·DOI·年份，meta_source 如实标注）。 */
function PaperMetaCard({ meta, arxivId }: { meta: DocIRMeta; arxivId?: string }) {
  const [showAbstract, setShowAbstract] = useState(false);
  const authors = meta.authors ?? [];
  const sourceLabel: Record<string, string> = {
    arxiv_api: "arXiv API", llm: "LLM 抽取", heuristic: "启发式",
    "arxiv_api+llm": "arXiv API + LLM", engine: "解析引擎", "": "未抽取",
  };
  return (
    <div className="card paper-meta-card">
      <h2>文献信息 {meta.meta_source != null && (
        <span className="chip" style={{ fontSize: 10, marginLeft: 6 }}
          title="元数据抽取来源（诚实标注降级路径）">
          来源：{sourceLabel[meta.meta_source] ?? meta.meta_source}
        </span>
      )}</h2>
      {authors.length > 0 ? (
        <div className="meta-authors">
          {authors.map((a, i) => (
            <span key={i} className="chip" style={{ fontSize: 11 }}
              title={a.affiliation || undefined}>
              {a.name}{a.affiliation ? ` · ${a.affiliation}` : ""}
            </span>
          ))}
        </div>
      ) : <p className="kv muted">作者信息未抽取到（扫描件或非标准版式可能缺失）。</p>}
      {(meta.venue || meta.year || meta.doi || arxivId) && (
        <div className="meta-authors" style={{ marginTop: 8 }}>
          {meta.venue && <span className="chip" style={{ fontSize: 11 }}>{meta.venue}</span>}
          {meta.year ? <span className="chip" style={{ fontSize: 11 }}>{meta.year} 年</span> : null}
          {arxivId && (
            <a className="chip" style={{ fontSize: 11 }}
              href={`https://arxiv.org/abs/${arxivId.split("v")[0]}`} target="_blank">
              arXiv:{arxivId.split("v")[0]}
            </a>
          )}
          {meta.doi && (
            <a className="chip" style={{ fontSize: 11 }} href={`https://doi.org/${meta.doi}`}
              target="_blank">DOI</a>
          )}
        </div>
      )}
      {meta.abstract && (
        <div style={{ marginTop: 10 }}>
          <b className="kv" style={{ cursor: "pointer" }}
            onClick={() => setShowAbstract((v) => !v)}>
            摘要 {showAbstract ? "▾" : "▸"}
          </b>
          {showAbstract && (
            <p style={{ fontSize: "var(--text-sm)", lineHeight: 1.6, margin: "6px 0 0",
              color: "var(--ink-2)" }}>{meta.abstract}</p>
          )}
        </div>
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
  const [copied, setCopied] = useState(false);

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
            <button className="chip" style={{ cursor: "pointer", fontSize: 11 }}
              onClick={() => {
                navigator.clipboard.writeText(sel.text).then(() => {
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1500);
                });
              }}>
              {copied ? "已复制 ✓" : "复制原文"}
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
  const [showMeta, setShowMeta] = useState(false);
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

  // ---- 模块三 A/C/D/F/G ----
  const [flashBid, setFlashBid] = useState<string | null>(null);          // A：跳转闪高亮（2.5s 自清，与 highlightRef 分离）
  const [pendingSection, setPendingSection] = useState<TocEntry | null>(null); // A：TOC 块级跳转
  const [pendingBid, setPendingBid] = useState<string | null>(null);      // D：全文搜索跨页块跳转
  const [activeSectionId, setActiveSectionId] = useState<string | null>(null); // A：当前章节（滚动推导）
  const [toast, setToast] = useState<string | null>(null);                // C：续读恢复提示
  const [searchMode, setSearchMode] = useState<"page" | "full">("page");  // D：页内过滤 / 全文 FTS
  const [searchHits, setSearchHits] = useState<SearchHit[] | null>(null); // D：null=未查询
  const [searching, setSearching] = useState(false);                      // D
  const [hoverTrans, setHoverTrans] = useState<{ x: number; y: number; bid: string } | null>(null); // F
  const [translatingAll, setTranslatingAll] = useState(false);            // G：整篇翻译
  const [exporting, setExporting] = useState(false);                      // G：MD/HTML 导出
  const [autoHover, setAutoHover] = useState(false);                       // P1：悬停即译开关
  useEffect(() => {
    setAutoHover(localStorage.getItem("hover_auto_translate") === "1");
  }, []);
  const toggleAutoHover = () => {
    setAutoHover((v) => {
      localStorage.setItem("hover_auto_translate", v ? "0" : "1");
      return !v;
    });
  };
  const searchInputRef = useRef<HTMLInputElement | null>(null);           // F：/ 聚焦搜索框
  const flashTimer = useRef<number | null>(null);
  const hoverTimer = useRef<number | null>(null);
  const sectionTrackTimer = useRef<number | null>(null);
  const toastTimer = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);                  // G：整篇翻译中断（断流即停）
  const restoredRef = useRef(false);                                      // C：续读一次性判定
  const globalRefsRef = useRef<Map<number, { bid: string; page: number }> | null>(null); // [n]→首个引用块（跨页反向跳转，懒加载缓存）

  // ---- 4.7 双滚动同步 ----
  const origPaneRef = useRef<HTMLDivElement | null>(null);
  const zhPaneRef = useRef<HTMLDivElement | null>(null);
  const pdfBiPaneRef = useRef<HTMLDivElement | null>(null);   // P0-3：PDF↔译文右栏
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

  /** A：跳转闪高亮（2.5s 自动清除；与 highlightRef 分离——后者无超时是既有语义）。 */
  const flash = useCallback((bid: string) => {
    setFlashBid(bid);
    if (flashTimer.current) window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => setFlashBid(null), 2500);
  }, []);

  /** C：简易 toast（3s 自消）。 */
  const showToast = useCallback((msg: string) => {
    setToast(msg);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 3000);
  }, []);

  /** A：text/bilingual 视图滚动 → 当前章节（首个可见块的 section_id，150ms 节流）。 */
  const trackSection = useCallback(() => {
    if (sectionTrackTimer.current) return;
    sectionTrackTimer.current = window.setTimeout(() => {
      sectionTrackTimer.current = null;
      const pane = view === "bilingual" ? origPaneRef.current : textPaneRef.current;
      if (!pane) return;
      const paneTop = pane.getBoundingClientRect().top;
      for (const el of pane.querySelectorAll<HTMLElement>("[data-bid]")) {
        if (el.getBoundingClientRect().bottom > paneTop + 40) {
          const blk = (pageData?.blocks ?? []).find((b) => b.id === el.dataset.bid);
          if (blk?.section_id) setActiveSectionId(blk.section_id);
          break;
        }
      }
    }, 150);
  }, [view, pageData]);

  /** 单侧滚动 → 对侧同步（IntersectionObserver：跨视口中线的块视为当前块）。
   *  F：① deps 移除 trans——observer 绑定的是 DOM 元素，流式译文更新只变内容不变结构，
   *     无需反复断开重建；② 比例对齐——按目标块中心在源视口的相对位置映射到对侧
   *     scrollTop（两侧块高不等时的视觉累积偏差校正，替代纯 center 对齐）。 */
  useEffect(() => {
    if (view !== "bilingual") return;
    const makeObserver = (srcPane: HTMLElement, dstPane: HTMLElement | null) => {
      if (!dstPane) return null;
      return new IntersectionObserver((entries) => {
        if (syncingRef.current) return;
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          const src = e.target as HTMLElement;
          const bid = src.dataset.bid;
          if (!bid) continue;
          markSync();
          const dst = dstPane.querySelector(`[data-bid="${bid}"]`) as HTMLElement | null;
          window.requestAnimationFrame(() => {
            if (!dst) return;
            const rel = (src.offsetTop + src.offsetHeight / 2 - srcPane.scrollTop)
              / Math.max(1, srcPane.clientHeight);
            dstPane.scrollTop = dst.offsetTop + dst.offsetHeight / 2
              - Math.min(1, Math.max(0, rel)) * dstPane.clientHeight;
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
  }, [view, page, markSync]);

  /** P0-3 PDF↔译文联动：右栏滚动 → 跨中线块驱动左侧 PDF bbox 锚定（同页内定位，
   *  PDF 侧无滚动条，联动=bbox 红框跟随；不写对侧 scrollTop → 无回环，无需 syncingRef）。 */
  useEffect(() => {
    if (view !== "pdf-bilingual") return;
    const pane = pdfBiPaneRef.current;
    if (!pane) return;
    const obs = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        const bid = (e.target as HTMLElement).dataset.bid;
        const blk = (pageData?.blocks ?? []).find((b) => b.id === bid);
        if (blk?.bbox?.length === 4) setFocusBbox(blk.bbox);
        break;
      }
    }, { root: pane, rootMargin: "-45% 0px -45% 0px", threshold: 0 });
    pane.querySelectorAll("[data-bid]").forEach((el) => obs.observe(el));
    return () => obs.disconnect();
  }, [view, page, pageData]);

  /** A：TOC 块级跳转——pageData 就绪后定位章节首块（section_id 优先，标题前缀兜底）。 */
  useEffect(() => {
    if (!pendingSection || !pageData) return;
    const norm = (s: string) => s.toLowerCase().replace(/\s+/g, " ").trim();
    const key = norm(pendingSection.title).slice(0, 30);
    const target =
      pageData.blocks.find((b) => b.section_id && b.section_id === pendingSection.id)
      ?? (key ? pageData.blocks.find(
          (b) => b.type === "heading" && norm(b.text).startsWith(key)) : undefined);
    if (target) {
      if (view === "pdf") {
        setFocusBbox(target.bbox?.length === 4 ? target.bbox : undefined);
      } else {
        const pane = view === "bilingual" ? origPaneRef.current : textPaneRef.current;
        window.setTimeout(() => {
          pane?.querySelector(`[data-bid="${target.id}"]`)
            ?.scrollIntoView({ block: "center", behavior: "smooth" });
        }, 60);
        flash(target.id);
      }
    }
    setPendingSection(null);
  }, [pageData, pendingSection, view, flash]);

  /** D：全文搜索跨页跳转——目标块所在页数据就绪后 scrollIntoView + flash。 */
  useEffect(() => {
    if (!pendingBid || !pageData) return;
    if (!pageData.blocks.some((b) => b.id === pendingBid)) return;
    const bid = pendingBid;
    window.setTimeout(() => {
      const pane = view === "bilingual" ? origPaneRef.current : textPaneRef.current;
      pane?.querySelector(`[data-bid="${bid}"]`)
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
      flash(bid);
      setPendingBid(null);
    }, 60);
  }, [pageData, pendingBid, view, flash]);

  /** C：续读恢复（outline 首达时一次性判定；?page= 深链优先——意图明确）。 */
  useEffect(() => {
    if (!outline || restoredRef.current) return;
    restoredRef.current = true;
    if (urlPage > 0) return;
    try {
      const saved = JSON.parse(localStorage.getItem(`rdpos:${id}`) || "null");
      const maxPage = (outline.pages?.at(-1) ?? 0) + 1;
      if (saved?.page > 1 && saved.page <= maxPage) {
        setPage(saved.page);
        if (saved.view) setView(saved.view);
        showToast(`已恢复上次阅读位置：第 ${saved.page} 页`);
      }
    } catch { /* 损坏数据忽略 */ }
  }, [outline, id, urlPage, showToast]);

  /** C：阅读位置持久化（恢复判定完成后才写，防首帧覆盖存档）。 */
  useEffect(() => {
    if (!outline || !restoredRef.current) return;
    try {
      localStorage.setItem(`rdpos:${id}`, JSON.stringify({ page, view, ts: Date.now() }));
    } catch { /* 隐私模式等写入失败忽略 */ }
  }, [outline, id, page, view]);

  /** D：全文搜索防抖（300ms；<2 字符清空结果）。 */
  useEffect(() => {
    if (searchMode !== "full") { setSearchHits(null); return; }
    const q = query.trim();
    if (q.length < 2) { setSearchHits(null); return; }
    const t = window.setTimeout(() => {
      setSearching(true);
      searchPaper(id as string, q, 12)
        .then((r) => setSearchHits(r.results))
        .catch(() => setSearchHits([]))
        .finally(() => setSearching(false));
    }, 300);
    return () => window.clearTimeout(t);
  }, [searchMode, query, id]);

  /** F：键盘快捷键——←/→ 翻页、j/k 前后块、t 翻译本页、/ 聚焦搜索、Esc 关弹窗。
   *  输入控件聚焦时不拦截（Esc 仅失焦）；每次渲染重挂以引用最新闭包状态。 */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const inInput = !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
        || t.tagName === "SELECT" || t.isContentEditable);
      if (e.key === "Escape") {
        if (inInput) (t as HTMLInputElement).blur();
        else { setSel(null); setHoverTrans(null); setShowMeta(false); setShowReport(false); }
        return;
      }
      if (inInput) return;
      if (e.key === "ArrowLeft") { e.preventDefault(); setPage((p) => Math.max(1, p - 1)); }
      else if (e.key === "ArrowRight") {
        e.preventDefault();
        setPage((p) => Math.min((outline?.pages.at(-1) ?? 0) + 1, p + 1));
      } else if (e.key === "j" || e.key === "k") stepBlock(e.key === "j" ? 1 : -1);
      else if (e.key === "t") { if (view !== "pdf") translatePage(); }
      else if (e.key === "/") { e.preventDefault(); searchInputRef.current?.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

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

  // 模块四 P0-2：参考文献含金量（懒加载；无引用句条目不在其中=中性态，不假装判断）
  const [refQuality, setRefQuality] = useState<Record<number, {
    cls: string; weight: number; mention_count: number; examples: string[] }> | null>(null);
  const [hideCheapRefs, setHideCheapRefs] = useState(false);
  useEffect(() => {
    if (view === "pdf" || !outline?.references?.length) return;
    let alive = true;
    fetch(`${API}/api/papers/${id}/ref-quality`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d?.qualities) setRefQuality(d.qualities); })
      .catch(() => { /* 含金量不可用时保持中性态 */ });
    return () => { alive = false; };
  }, [id, view, outline]);

  // 阅读顺序红绿开关素材 + 重排逻辑（hooks 顺序约束：均在 early return 之前）
  const rawOrder = outline?.meta?.demo_raw_order;
  const rawOrders = outline?.meta?.raw_orders;
  // E：text/compare 视图常驻开关；off 时三级还原——demo 素材 → 解析记录（真实）→ x0 模拟
  const showOrderToggle = view === "text" || view === "compare";
  const orderOffMode: "demo" | "recorded" | "simulated" | null = useMemo(() => {
    if (orderOn || !showOrderToggle) return null;
    if (rawOrder && page === rawOrder.page && rawOrder.x0_order?.length) return "demo";
    if (rawOrders?.[String(page - 1)]?.length) return "recorded";
    return "simulated";
  }, [orderOn, showOrderToggle, rawOrder, rawOrders, page]);

  const displayBlocks: (Block & { rawIdx?: number; orderMode?: "demo" | "recorded" | "simulated" })[] = useMemo(() => {
    const bs = pageData?.blocks ?? [];
    if (!orderOffMode) return bs;
    if (orderOffMode === "demo" && rawOrder?.x0_order?.length) {
      const xs = rawOrder.x0_order;
      const idx = (x: number) => {
        const f = xs.findIndex((v) => Math.abs(v - x) < 0.01);
        return f >= 0 ? f : xs.length;
      };
      return [...bs].sort((a, b) => idx(a.bbox?.[0] ?? 0) - idx(b.bbox?.[0] ?? 0))
        .map((b) => ({ ...b, rawIdx: idx(b.bbox?.[0] ?? 0), orderMode: "demo" as const }));
    }
    if (orderOffMode === "recorded") {
      // 真实置换：perm[k] = 原始序第 k 块在重排后数组中的索引 → 按原始序还原展示
      const perm = rawOrders?.[String(page - 1)] ?? [];
      return perm
        .filter((i) => Number.isInteger(i) && i >= 0 && i < bs.length)
        .map((i, k) => ({ ...bs[i], rawIdx: k, orderMode: "recorded" as const }));
    }
    // 模拟：x0 升序启发（左栏先于右栏的近似），黄色「模拟序」如实标注
    const xs = [...new Set(bs.map((b) => b.bbox?.[0] ?? 0))].sort((a, b) => a - b);
    const idx = (x: number) => {
      const f = xs.findIndex((v) => Math.abs(v - x) < 0.01);
      return f >= 0 ? f : xs.length;
    };
    return [...bs].sort((a, b) => idx(a.bbox?.[0] ?? 0) - idx(b.bbox?.[0] ?? 0))
      .map((b) => ({ ...b, rawIdx: idx(b.bbox?.[0] ?? 0), orderMode: "simulated" as const }));
  }, [pageData, orderOffMode, rawOrder, rawOrders, page]);

  // A：当前章节 toc 条目（滚动推导优先；pdf/compare 视图按当前页反推兜底）
  const activeToc = useMemo<TocEntry | null>(() => {
    if (!outline?.toc?.length) return null;
    if ((view === "text" || view === "bilingual") && activeSectionId) {
      const hit = outline.toc.find((t) => t.id && t.id === activeSectionId);
      if (hit) return hit;
    }
    return [...outline.toc].filter((t) => t.page <= page - 1).at(-1) ?? null;
  }, [outline, view, activeSectionId, page]);
  const tocKey = (t: TocEntry) => t.id ?? `${t.page}:${t.title}`;

  if (!outline) {
    return (
      <div className="page"><div className="page-inner">
        <p className="kv">加载大纲中…（若刚上传请等解析完成）</p>
      </div></div>
    );
  }

  const meta = outline.meta;
  const pages = outline.pages;
  // D：query 过滤仅页内模式生效（全文模式命中由结果面板承载，当前页块不被隐藏）
  const blocks = displayBlocks
    .filter((b) => searchMode !== "page" || !query
      || b.text.toLowerCase().includes(query.toLowerCase()));
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
    const local = fullRefsIndex.get(n)?.[0];
    if (local) {
      const pane = view === "bilingual" ? origPaneRef.current
        : view === "compare" ? textPaneRef.current : textPaneRef.current;
      pane?.querySelector(`[data-bid="${local}"]`)
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    // 跨页反向跳转：当前页未引用 [n] → 懒加载全量 docir 建全局引用索引（一次缓存），
    // 定位首个引用块 → 换页 + pendingBid 块级定位（复用全文搜索的跨页跳转机制 D）
    void (async () => {
      try {
        if (!globalRefsRef.current) {
          const r = await fetch(`${API}/api/papers/${id}/docir`);
          if (!r.ok) return;
          const docir = await r.json();
          const m = new Map<number, { bid: string; page: number }>();
          for (const b of docir.blocks ?? []) {
            const seen = new Set<number>();
            for (const match of (b.text ?? "").matchAll(/\[(\d{1,3})\]/g)) {
              const k = Number(match[1]);
              if (!seen.has(k)) { seen.add(k); if (!m.has(k)) m.set(k, { bid: b.id, page: b.page }); }
            }
          }
          globalRefsRef.current = m;
        }
        const hit = globalRefsRef.current.get(n);
        if (!hit) return;
        if (hit.page + 1 !== page) { setPage(hit.page + 1); setPendingBid(hit.bid); }
        else {
          const pane = view === "bilingual" ? origPaneRef.current : textPaneRef.current;
          pane?.querySelector(`[data-bid="${hit.bid}"]`)
            ?.scrollIntoView({ block: "center", behavior: "smooth" });
          flash(hit.bid);
        }
      } catch { /* 全量拉取失败 → 静默保持当前页（诚实降级，不阻塞本地跳转） */ }
    })();
  };

  // ---- 模块三 A：TOC 块级跳转（pendingSection 由 effect 在 pageData 就绪后定位） ----
  const gotoSection = (t: TocEntry) => {
    if (t.page + 1 !== page) setPage(t.page + 1);
    setPendingSection(t);
  };

  // ---- 模块三 F：j/k 块导航（当前视口中心块 ±1） ----
  const stepBlock = (dir: 1 | -1) => {
    const pane = view === "bilingual" ? origPaneRef.current : textPaneRef.current;
    if (!pane) return;
    const els = Array.from(pane.querySelectorAll<HTMLElement>("[data-bid]"));
    if (!els.length) return;
    const mid = pane.getBoundingClientRect().top + pane.clientHeight / 2;
    let cur = 0;
    let best = Infinity;
    els.forEach((el, i) => {
      const r = el.getBoundingClientRect();
      const dist = Math.abs((r.top + r.bottom) / 2 - mid);
      if (dist < best) { best = dist; cur = i; }
    });
    const next = Math.max(0, Math.min(els.length - 1, cur + dir));
    els[next].scrollIntoView({ block: "center", behavior: "smooth" });
    if (els[next].dataset.bid) flash(els[next].dataset.bid!);
  };

  // ---- 模块三 F：悬停翻译——hover ≥500ms 只读缓存；点按触发单块翻译；
  //      P1「悬停即译」（autoHover 状态声明在顶部 hooks 区，此处仅行为）----
  const onBlockEnter = (e: React.MouseEvent, bid: string) => {
    if (hoverTimer.current) window.clearTimeout(hoverTimer.current);
    const x = e.clientX;
    const y = e.clientY;
    hoverTimer.current = window.setTimeout(() => {
      setHoverTrans({ x, y, bid });
      if (autoHover && !trans[bid] && !translating) translateOneBlock(bid);
    }, 500);
  };
  const onBlockLeave = () => {
    if (hoverTimer.current) window.clearTimeout(hoverTimer.current);
  };
  const translateOneBlock = (bid: string) => {
    if (translating) return;
    streamTranslate(id as string, { block_ids: [bid] }, {
      tDelta: (d) => {
        setTrans((prev) => ({ ...prev, [d.block_id]: { text: d.text, format_ok: d.format_ok } }));
        setTranslating({ done: d.done, total: d.total });
      },
      final: () => setTranslating(null),
      error: (d) => { window.alert(`翻译失败：${d.message}`); setTranslating(null); },
      done: () => setTranslating(null),
    }).catch((e) => { window.alert(`翻译请求失败：${e}`); setTranslating(null); });
  };

  // ---- 模块三 G：整篇翻译（缓存续译 + AbortController 断流即停） ----
  const translateAll = () => {
    if (translating || translatingAll) return;
    const total = outline?.block_count ?? 0;
    if (!window.confirm(
      `整篇翻译约 ${total} 个块 + 表格/图注（已译块自动跳过 · 公式块不译 · 可随时停止，已完成块落缓存续译）。开始？`)) return;
    setTranslatingAll(true);
    const ac = new AbortController();
    abortRef.current = ac;
    streamTranslate(id as string, { include_extras: true }, {
      tDelta: (d) => {
        setTrans((prev) => ({ ...prev, [d.block_id]: { text: d.text, format_ok: d.format_ok } }));
        setTranslating({ done: d.done, total: d.total });
      },
      final: () => setTranslating(null),
      error: (d) => { window.alert(`翻译失败：${d.message}`); },
      done: () => { setTranslating(null); setTranslatingAll(false); abortRef.current = null; },
    }, ac.signal).catch((e) => {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        window.alert(`翻译请求失败：${e}`);
      }
      setTranslating(null);
      setTranslatingAll(false);
      abortRef.current = null;
    });
  };
  const stopTranslateAll = () => abortRef.current?.abort();

  // ---- 模块三 P0-2：服务端导出译文 PDF（overlay 原位 / bilingual 双页） ----
  const exportServerPdf = async (layout: "overlay" | "bilingual") => {
    setExporting(true);
    try {
      const r = await fetch(`${API}/api/papers/${id}/export?fmt=pdf&target_lang=zh&mode=academic&layout=${layout}`);
      if (!r.ok) {
        const msg = await r.json().catch(() => ({ detail: r.statusText }));
        window.alert(`PDF 导出失败：${(msg as { detail?: string }).detail ?? r.status}`);
        return;
      }
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${id}.zh.${layout}.pdf`;
      a.click();
      URL.revokeObjectURL(a.href);
      const overflow = Number(r.headers.get("X-Export-Overflow") ?? "0");
      showToast(`已导出 PDF（${layout === "overlay" ? "原位替换" : "双语对照"}`
        + `${overflow ? ` · ${overflow} 块进补遗页` : ""}）`);
    } catch (e) {
      window.alert(`PDF 导出失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setExporting(false);
    }
  };

  // ---- 模块三 G：导出 Markdown / HTML（客户端 Blob 下载；公式保留 LaTeX 英文） ----
  const exportDoc = async (fmt: "md" | "html") => {
    if (exporting) return;
    setExporting(true);
    try {
      const r = await fetch(`${API}/api/papers/${id}/docir`, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      const allBlocks: Block[] = d.blocks ?? [];
      const allTables: { id: string; page: number; markdown: string }[] = d.tables ?? [];
      const textBlocks = allBlocks.filter((b) => b.type === "paragraph" || b.type === "heading");
      const translated = textBlocks.filter((b) => trans[b.id]).length;
      if (translated < textBlocks.length
        && !window.confirm(`已译 ${translated}/${textBlocks.length} 块——未译块将仅导出原文。继续？`)) return;
      const title = meta.title || paper?.title || String(id);
      const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      const stamp = new Date().toLocaleString();
      let content: string;
      let mime: string;
      let ext: string;
      if (fmt === "md") {
        const lines: string[] = [`# ${title}`, "",
          `> 中英对照导出（公式/代码保留英文 LaTeX；未译块仅原文）· ${stamp}`, ""];
        for (const b of allBlocks) {
          if (b.type === "heading") { lines.push(`## ${b.text}`, ""); continue; }
          if (b.type === "formula") { lines.push(`$$${b.latex ?? b.text}$$`, ""); continue; }
          lines.push(b.text, "");
          const t = trans[b.id];
          if (t) lines.push(`> ${t.text}`, "");
        }
        if (allTables.length) {
          lines.push(`## 附：表格（${allTables.length}）`, "");
          for (const t of allTables) lines.push(`### 表 ${t.id}（p${t.page + 1}）`, "", t.markdown, "");
        }
        content = lines.join("\n");
        mime = "text/markdown;charset=utf-8";
        ext = "zh-en.md";
      } else {
        const parts: string[] = [
          "<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\">",
          `<title>${esc(title)} · 中英对照</title></head>`,
          "<body style=\"max-width:820px;margin:24px auto;font-family:system-ui,sans-serif;line-height:1.7;color:#222\">",
          `<h1>${esc(title)}</h1>`,
          `<p style="color:#888;font-size:13px">中英对照导出 · 公式以 LaTeX 源码（$…$）保留英文 · ${stamp}</p>`,
        ];
        for (const b of allBlocks) {
          if (b.type === "heading") { parts.push(`<h2>${esc(b.text)}</h2>`); continue; }
          if (b.type === "formula") {
            parts.push(`<pre style="background:#f6f8fa;padding:8px 12px;border-radius:6px;overflow-x:auto">$$${esc(b.latex ?? b.text)}$$</pre>`);
            continue;
          }
          parts.push(`<p>${esc(b.text)}</p>`);
          const t = trans[b.id];
          if (t) parts.push(`<blockquote style="border-left:3px solid #2563eb;margin:4px 0;padding:2px 12px;color:#444">${esc(t.text)}</blockquote>`);
        }
        for (const t of allTables) {
          parts.push(`<h3>表 ${esc(t.id)}（p${t.page + 1}）</h3>`,
            `<pre style="background:#f6f8fa;padding:8px 12px;border-radius:6px;overflow-x:auto">${esc(t.markdown)}</pre>`);
        }
        parts.push("</body></html>");
        content = parts.join("\n");
        mime = "text/html;charset=utf-8";
        ext = "zh-en.html";
      }
      const url = URL.createObjectURL(new Blob([content], { type: mime }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `${title.replace(/[\\/:*?"<>|]/g, "_").slice(0, 60)}.${ext}`;
      a.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 5000);
      showToast(`已导出 ${fmt === "md" ? "Markdown" : "HTML"}（已译 ${translated}/${textBlocks.length} 块）`);
    } catch (e) {
      window.alert(`导出失败：${e instanceof Error ? e.message : e}`);
    } finally {
      setExporting(false);
    }
  };

  // ---- 模块三 D：全文搜索命中跳转（跨页 → pendingBid 定位；当前页 → 直接滚） ----
  const jumpToHit = (h: SearchHit) => {
    const bid = h.block_ids?.[0];
    if (h.page + 1 !== page) setPage(h.page + 1);
    if (view === "pdf" || view === "bilingual") setView("text");
    if (bid) {
      if (h.page + 1 === page && (pageData?.blocks ?? []).some((b) => b.id === bid)) {
        window.setTimeout(() => {
          textPaneRef.current?.querySelector(`[data-bid="${bid}"]`)
            ?.scrollIntoView({ block: "center", behavior: "smooth" });
          flash(bid);
        }, 60);
      } else {
        setPendingBid(bid);
      }
    }
  };
  const markSnippet = (snippet: string, q: string) => {
    const terms = [...new Set(q.trim().split(/\s+/).filter(Boolean))];
    if (!terms.length) return snippet;
    const escRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const re = new RegExp(`(${terms.map(escRe).join("|")})`, "gi");
    const lower = new Set(terms.map((t) => t.toLowerCase()));
    return snippet.split(re).map((part, i) =>
      lower.has(part.toLowerCase())
        ? <mark key={i} style={{ background: "var(--warn-soft, #fef3c7)" }}>{part}</mark>
        : <span key={i}>{part}</span>);
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
          title="阅读顺序可视化：重排后（正确序，绿）vs 原始序——demo 素材/解析记录为真实数据（红），无记录页为 x0 启发模拟（黄，如实标注）">
          阅读顺序：{orderOn ? "✅ 重排后（正确序）"
            : orderOffMode === "demo" ? "🔴 原始序（乱序演示）"
            : orderOffMode === "recorded" ? "🔴 原始序（解析记录）"
            : "🟡 模拟序（x0 启发）"}
        </button>
      )}
      {blocks.map((b) => (
        <div key={b.id} data-bid={b.id}
          className={`block-item${flashBid === b.id ? " flash" : ""}`}
          onMouseEnter={(e) => onBlockEnter(e, b.id)}
          onMouseLeave={onBlockLeave}
          style={{
            ...((b.bbox?.length === 4 && (urlBbox ?? focusBbox) &&
              Math.abs(b.bbox[1] - (urlBbox ?? focusBbox)![1]) < 0.02)
              ? { background: "var(--warn-soft)", borderRadius: "var(--radius-sm)" } : {}),
            ...(b.rawIdx != null
              ? { borderLeft: `3px solid ${b.orderMode === "simulated" ? "#d97706" : "#dc2626"}`, paddingLeft: 8 }
              : showOrderToggle ? { borderLeft: "3px solid #16a34a", paddingLeft: 8 } : {}),
          }}>
          {renderBlockBody(b, cite)}
          <span className="block-anchor">
            {b.id}{b.rawIdx != null ? ` · ${b.orderMode === "simulated" ? "模拟序" : "原始序"} #${b.rawIdx}` : ""}
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
        {activeToc && (
          <span className="chip" title="当前章节（随滚动/翻页更新）">
            §{activeToc.title.length > 26 ? `${activeToc.title.slice(0, 26)}…` : activeToc.title}
          </span>
        )}
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
        <button className="chip" style={{ cursor: "pointer" }} onClick={() => setShowMeta((v) => !v)}>
          文献信息{meta.authors?.length ? ` · ${meta.authors.length} 作者` : ""}
        </button>
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
                <span
                  className={`toc-item${activeToc && tocKey(t) === tocKey(activeToc) ? " active" : ""}`}
                  onClick={() => gotoSection(t)}
                  title="块级跳转：定位到本章首个标题块并高亮">
                  {t.level === 1 ? <b>{t.title}</b> : t.title}
                  {t.appendix && <span className="chip" style={{ fontSize: 9, marginLeft: 6 }}
                    title="附录章节（结构化标记）">附录</span>}
                  <span className="toc-page">p{t.page + 1}</span>
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
                  onClick={() => setPage(Math.min((pages.at(-1) ?? 0) + 1, page + 1))}
                  title="下一页（→）">→</button>
                {outline.toc.length > 0 && (() => {
                  const idx = activeToc ? outline.toc.findIndex((t) => tocKey(t) === tocKey(activeToc)) : -1;
                  const prev = idx > 0 ? outline.toc[idx - 1] : null;
                  const next = idx >= 0 && idx < outline.toc.length - 1 ? outline.toc[idx + 1] : null;
                  return (
                    <span style={{ display: "inline-flex", gap: 4 }}>
                      <button className="btn ghost" style={{ padding: "4px 10px" }} disabled={!prev}
                        onClick={() => prev && gotoSection(prev)}
                        title={prev ? `上一章：${prev.title}` : "已在最早章节"}>≪ 上一章</button>
                      <button className="btn ghost" style={{ padding: "4px 10px" }} disabled={!next}
                        onClick={() => next && gotoSection(next)}
                        title={next ? `下一章：${next.title}` : "已在最后章节"}>下一章 ≫</button>
                    </span>
                  );
                })()}
                {([["pdf", "PDF 原文"], ["text", "DocIR 文本"], ["bilingual", "双语对照"],
                  ["compare", "原版对照"], ["pdf-bilingual", "PDF↔译文"]] as [ViewMode, string][]).map(([m, label]) => (
                  <button key={m} className={`chip ${view === m ? "accent" : ""}`}
                    style={{ cursor: "pointer" }} onClick={() => setView(m)}>{label}</button>
                ))}
                {view !== "pdf" && (
                  <button className="chip" style={{ cursor: "pointer" }} onClick={translatePage}
                    title="翻译本页（block 级，公式/代码/引用标记占位符保护，缓存续译）— 快捷键 t">
                    {translating ? `翻译中 ${translating.done}/${translating.total}…` : "翻译本页"}
                  </button>
                )}
                {view !== "pdf" && !translatingAll && (
                  <button className="chip" style={{ cursor: "pointer" }} onClick={translateAll}
                    title="整篇翻译：已译块自动跳过（缓存续译），公式块不译，可中断续跑；含表格与图注">
                    翻译全文
                  </button>
                )}
                {view !== "pdf" && (
                  <span className={`chip ${autoHover ? "accent" : ""}`} style={{ cursor: "pointer" }}
                    onClick={toggleAutoHover}
                    title="悬停即译：开启后悬停未译块自动触发单块翻译（默认关闭=只读缓存，点击才译，省 token）">
                    悬停即译{autoHover ? "·开" : "·关"}
                  </span>
                )}
                {translatingAll && (
                  <button className="chip warn" style={{ cursor: "pointer" }} onClick={stopTranslateAll}
                    title="中断整篇翻译——已完成块已落缓存，重试即续译">
                    停止整篇（{translating?.done ?? 0}/{translating?.total ?? 0}）
                  </button>
                )}
                {view !== "pdf" && (
                  <>
                    <button className="chip" style={{ cursor: "pointer" }} disabled={exporting}
                      onClick={() => exportDoc("md")}
                      title="导出中英对照 Markdown（公式/代码保留英文 LaTeX；未译块仅原文）">
                      {exporting ? "导出中…" : "导出 MD"}
                    </button>
                    <button className="chip" style={{ cursor: "pointer" }} disabled={exporting}
                      onClick={() => exportDoc("html")}
                      title="导出中英对照 HTML（内联样式，离线可看）">
                      导出 HTML
                    </button>
                    <button className="chip" style={{ cursor: "pointer" }} disabled={exporting}
                      onClick={() => exportServerPdf("overlay")}
                      title="服务端导出：原 PDF 原位替换译文（公式/表格/图片不动；放不下的块进文末补遗页）">
                      导出 PDF（原位）
                    </button>
                    <button className="chip" style={{ cursor: "pointer" }} disabled={exporting}
                      onClick={() => exportServerPdf("bilingual")}
                      title="服务端导出：原页+译文页交替的双语 PDF">
                      导出 PDF（双语）
                    </button>
                  </>
                )}
                <span className={`chip ${searchMode === "full" ? "accent" : ""}`}
                  style={{ cursor: "pointer", marginLeft: "auto" }}
                  onClick={() => { setSearchMode((m) => (m === "page" ? "full" : "page")); setSearchHits(null); }}
                  title="页内：过滤当前页块；全文：FTS 检索全篇 chunk（跨页跳转）— 快捷键 / 聚焦">
                  {searchMode === "page" ? "页内" : "全文"}
                </span>
                <input ref={searchInputRef}
                  placeholder={searchMode === "page" ? "页内搜索…" : "全文搜索…（≥2 字符，FTS）"}
                  value={query} onChange={(e) => setQuery(e.target.value)}
                  style={{ border: "1px solid var(--line-strong)",
                    borderRadius: "var(--radius-sm)", padding: "6px 10px", width: 180,
                    fontFamily: "var(--font)", fontSize: "var(--text-sm)", outline: "none" }} />
              </div>
              {/* C：阅读进度（按页计——正文按需加载，页级即诚实口径） */}
              <div style={{ height: 3, background: "var(--line)", borderRadius: 2, marginTop: 8 }}
                title={`阅读进度：第 ${page}/${meta.page_count} 页`}>
                <div style={{
                  width: `${Math.min(100, Math.round((page / Math.max(1, meta.page_count)) * 100))}%`,
                  height: "100%", background: "var(--ok, #16a34a)", borderRadius: 2,
                  transition: "width .3s" }} />
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

            {/* D：全文搜索结果（跨页命中点击 → 换页 + pendingBid 块级定位） */}
            {searchMode === "full" && query.trim().length >= 2 && (searching || searchHits) && (
              <div className="card" style={{ marginBottom: 0, maxHeight: 260, overflowY: "auto" }}>
                <h2>全文搜索{searching ? "中…" : `（${searchHits?.length ?? 0} 条 · FTS）`}</h2>
                {(searchHits ?? []).map((h) => (
                  <div key={h.chunk_id} onClick={() => jumpToHit(h)}
                    style={{ padding: "6px 8px", borderRadius: 6, cursor: "pointer" }}
                    className="toc-item">
                    <span className="chip" style={{ fontSize: 10 }}>p{h.page + 1}</span>
                    {h.section_path && (
                      <span className="chip" style={{ fontSize: 10 }}>
                        §{h.section_path === "_front" ? "前言" : h.section_path}
                      </span>
                    )}
                    <p style={{ fontSize: 12, margin: "4px 0 0", color: "var(--ink-2)" }}>
                      {markSnippet(h.snippet, query)}
                    </p>
                  </div>
                ))}
                {!searching && searchHits?.length === 0 && <p className="kv">无命中。</p>}
              </div>
            )}

            {showMeta && <PaperMetaCard meta={meta} arxivId={meta.arxiv_id} />}
            {showReport && <QualityPanel meta={meta} paperId={id as string} />}

            {view === "pdf" ? (
              <div className="card" style={{ marginBottom: 0, flex: 1, overflowY: "auto" }}>
                <PdfAnchored paperId={id as string} page={page}
                  bbox={(urlBbox ?? focusBbox)?.length === 4 ? (urlBbox ?? focusBbox) : undefined}
                  width={680} onMouseUp={onTextMouseUp} />
              </div>
            ) : view === "compare" ? (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-3)", flex: 1, minHeight: 0 }}>
                <div className="card" style={{ marginBottom: 0, overflowY: "auto" }}>
                  <h2 style={{ margin: "0 0 8px" }}>原版 PDF（p{page}）</h2>
                  <PdfAnchored paperId={id as string} page={page}
                    bbox={(urlBbox ?? focusBbox)?.length === 4 ? (urlBbox ?? focusBbox) : undefined}
                    width={480} onMouseUp={onTextMouseUp} />
                </div>
                <div className="card" ref={textPaneRef} onMouseUp={onTextMouseUp}
                  onScroll={trackSection}
                  style={{ marginBottom: 0, overflowY: "auto",
                    maxHeight: "calc(100vh - 230px)", minHeight: 320 }}>
                  <h2 style={{ margin: "0 0 8px" }}>结构化文本（DocIR）{focusBbox ? " · 公式已联动定位" : ""}</h2>
                  {renderTextBlocks(true)}
                </div>
              </div>
            ) : view === "pdf-bilingual" ? (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-3)", flex: 1, minHeight: 0 }}>
                <div className="card" style={{ marginBottom: 0, overflowY: "auto" }}>
                  <h2 style={{ margin: "0 0 8px" }}>原版 PDF（p{page}）{focusBbox ? " · 已锚定滚动块" : " · 右栏滚动自动锚定"}</h2>
                  <PdfAnchored paperId={id as string} page={page}
                    bbox={focusBbox?.length === 4 ? focusBbox : undefined}
                    width={480} onMouseUp={onTextMouseUp} />
                </div>
                <div className="card" ref={pdfBiPaneRef} onMouseUp={onTextMouseUp}
                  onScroll={trackSection}
                  style={{ marginBottom: 0, overflowY: "auto",
                    maxHeight: "calc(100vh - 230px)", minHeight: 320 }}>
                  <h2 style={{ position: "sticky", top: 0, background: "var(--bg, #fff)", margin: "0 0 8px", paddingBottom: 6 }}>
                    译文（第 {page} 页 · 点击块锚定 PDF）
                  </h2>
                  {blocks.map((b) => {
                    const t = trans[b.id];
                    return (
                      <div key={b.id} data-bid={b.id}
                        className={`block-item${flashBid === b.id ? " flash" : ""}`}
                        onClick={() => { if (b.bbox?.length === 4) setFocusBbox(b.bbox); flash(b.id); }}
                        style={{ cursor: "pointer", ...(t ? {} : { opacity: 0.5 }) }}>
                        {b.type === "formula" ? (
                          <p className="kv">（公式块不翻译 · 原样见左栏 PDF）</p>
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
                  {blocks.length === 0 && <p className="kv">本页无内容。</p>}
                </div>
              </div>
            ) : view === "text" ? (
              <div className="card" ref={textPaneRef} onMouseUp={onTextMouseUp}
                onScroll={trackSection}
                style={{ marginBottom: 0, flex: 1, overflowY: "auto", position: "relative",
                  maxHeight: "calc(100vh - 230px)" }}>
                {renderTextBlocks(true)}
              </div>
            ) : (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-3)", flex: 1, minHeight: 0 }}>
                <div className="card" ref={origPaneRef} onMouseUp={onTextMouseUp}
                  onScroll={trackSection}
                  style={{ marginBottom: 0, overflowY: "auto",
                    maxHeight: "calc(100vh - 230px)", minHeight: 320 }}>
                  <h2 style={{ position: "sticky", top: 0, background: "var(--bg, #fff)", margin: 0, paddingBottom: 6 }}>原文</h2>
                  {blocks.map((b) => (
                    <div key={b.id} data-bid={b.id}
                      className={`block-item${flashBid === b.id ? " flash" : ""}`}
                      onMouseEnter={(e) => onBlockEnter(e, b.id)}
                      onMouseLeave={onBlockLeave}>
                      {renderBlockBody(b, false)}
                    </div>
                  ))}
                  {blocks.length === 0 && <p className="kv">本页无内容。</p>}
                </div>
                <div className="card" ref={zhPaneRef}
                  style={{ marginBottom: 0, overflowY: "auto",
                    maxHeight: "calc(100vh - 230px)", minHeight: 320 }}>
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
                <h2 style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  References（{refs.length} 条 · 点击条目跳到正文首个引用处）
                  {refQuality && (
                    <span className={`chip ${hideCheapRefs ? "accent" : ""}`} style={{ cursor: "pointer", fontSize: 10 }}
                      onClick={() => setHideCheapRefs((v) => !v)}
                      title="可逆过滤：只隐藏「凑数引用」（权重 0.1）；无引用句的条目保持显示为中性态；再次点击恢复">
                      {hideCheapRefs ? "已隐藏凑数（点击恢复）" : "隐藏凑数引用"}
                    </span>
                  )}
                  {refQuality && (
                    <span style={{ fontSize: 10, color: "var(--ink-3, #888)" }}>
                      实质 {Object.values(refQuality).filter((q) => q.cls === "substantive").length}
                      · 背景 {Object.values(refQuality).filter((q) => q.cls === "background").length}
                      · 凑数 {Object.values(refQuality).filter((q) => q.cls === "perfunctory").length}
                    </span>
                  )}
                </h2>
                {refs
                  .filter((r) => !hideCheapRefs || refQuality?.[r.index]?.cls !== "perfunctory")
                  .map((r) => {
                  const inLib = r.arxiv_id && libraryArxivIds.has(r.arxiv_id.replace(/v\d+$/, ""));
                  const cited = fullRefsIndex.get(r.index) ?? [];
                  const q = refQuality?.[r.index];
                  return (
                    <div key={r.index} data-refidx={r.index}
                      onClick={() => jumpFromRef(r.index)}
                      title={q ? q.examples.join("\n——\n") : undefined}
                      style={{
                        display: "flex", gap: 8, alignItems: "baseline", cursor: "pointer",
                        padding: "4px 6px", borderRadius: 6,
                        background: highlightRef === r.index ? "var(--accent-soft, #eef2ff)" : undefined,
                      }}>
                      <b className="kv" style={{ minWidth: 34 }}>[{r.index}]</b>
                      <span style={{ fontSize: 12, flex: 1 }}>
                        {r.title || r.raw || "（未解析出标题）"}
                      </span>
                      {q && (
                        <span className={`chip ${q.cls === "substantive" ? "ok" : q.cls === "perfunctory" ? "warn" : ""}`}
                          style={{ fontSize: 10 }}
                          title={`含金量 ${q.weight}（${q.mention_count} 处引用句）`}>
                          {q.cls === "substantive" ? "实质" : q.cls === "background" ? "背景" : "凑数"}
                        </span>
                      )}
                      {!q && refQuality && <span className="kv" style={{ fontSize: 10 }}>无引用句</span>}
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
                {t.headers?.length ? (
                  <div style={{ overflowX: "auto" }}>
                    <table className="three-line">
                      <thead>
                        <tr>{t.headers.map((h, i) => <th key={i}>{h}</th>)}</tr>
                      </thead>
                      <tbody>
                        {(t.rows ?? []).map((row, ri) => (
                          <tr key={ri}>{row.map((c, ci) => <td key={ci}>{c}</td>)}</tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <pre style={{ fontSize: "var(--text-xs)", overflowX: "auto", margin: 0 }}>{t.markdown}</pre>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
      <SelectionPopup sel={sel} onClose={() => setSel(null)} />

      {/* F：悬停翻译浮窗——只读缓存（hover 不调 LLM），未译块点按才触发单块翻译 */}
      {hoverTrans && (() => {
        const blk = (pageData?.blocks ?? []).find((b) => b.id === hoverTrans.bid);
        const t = blk ? trans[blk.id] : undefined;
        return (
          <div className="card rd-toast" style={{ right: 20, left: "auto", bottom: "auto",
            top: Math.min(hoverTrans.y + 14, window.innerHeight - 220), width: 340, maxWidth: "90vw" }}
            onMouseLeave={() => setHoverTrans(null)}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <b style={{ fontSize: 12 }}>悬停翻译（{hoverTrans.bid}）</b>
              <button className="chip" style={{ cursor: "pointer" }}
                onClick={() => setHoverTrans(null)}>✕</button>
            </div>
            {!blk && <p className="kv">块不在当前页。</p>}
            {blk?.type === "formula" && <p className="kv">公式块不翻译（LaTeX 原样保留）。</p>}
            {blk && blk.type !== "formula" && (
              <>
                {t ? (
                  <p style={{ fontSize: 13, margin: "8px 0 0" }}
                    className={t.format_ok ? undefined : "kv"}
                    title={t.format_ok ? undefined : "占位符校验未过 · 已回退原文"}>
                    {t.text}
                  </p>
                ) : (
                  <>
                    <p className="kv" style={{ margin: "8px 0 4px" }}>未翻译（悬停不调用 LLM）。</p>
                    <button className="chip accent" style={{ cursor: "pointer", fontSize: 11 }}
                      disabled={!!translating}
                      onClick={() => translateOneBlock(blk.id)}>
                      {translating ? "翻译中…" : "翻译本块"}
                    </button>
                  </>
                )}
              </>
            )}
          </div>
        );
      })()}

      {toast && <div className="rd-toast">{toast}</div>}
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
