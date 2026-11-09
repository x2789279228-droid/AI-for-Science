"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  Anchor, ChartSpec, Claim, CompareCell, CompareTable, FinalEvent, GraphData, Hit,
  Outline, Paper, TLEvent, TraceCard, listPapers, resumeChat, streamChat,
} from "@/lib/api";
import { ChevronRight, Spark } from "@/components/icons";
import TracePanel from "@/components/chat/TracePanel";
import PdfAnchored from "@/components/PdfAnchored";

/** 证据浮层定位：anchor（paper/page/bbox）+ 可选引文与编号。 */
interface EvidenceLoc {
  eid?: string;
  anchor: Anchor;
  quote?: string;
}

interface Turn {
  question: string;
  answer: string;
  trace: TraceCard[];
  retrieval: { channels: string[]; hits: Hit[] } | null;
  final: FinalEvent | null;
  threadId?: string;   // 断点续跑：本轮线程 id，失败后「继续执行」用
  failed?: boolean;    // 图级执行失败（可恢复）
  tl: TLEvent[];       // 协议 v1.1：有序时间线事件流（右侧 TracePanel 数据源）
}

const GUARD_LABEL: Record<string, string> = {
  no_citation_found: "回答无引用锚点",
  invalid_citation_removed: "无效引用已剔除",
  llm_unavailable_fallback: "LLM 不可用 → 抽取式降级",
  empty_index: "索引为空",
  compare_scope_refusal: "Compare 范围校验 · 单篇拒绝转介",
  reader_scope_locked: "Reader 范围锁定 · 仅精读首篇",
  writer_insufficient_evidence: "Writer 证据池不足 · 拒写（Evidence-First）",
  viz_scope_refusal: "Viz 范围校验 · 无选中论文拒绝",
  viz_no_tables: "Viz 数据校验 · 论文无表格拒绝出图",
  viz_no_numeric_columns: "Viz 数据校验 · 表格无数值列拒绝出图",
  tool_failure_degraded: "工具失败 → 抽取式降级",
  rerank_degraded: "Rerank 精排失败 → 融合排序降级",
  quote_unverified: "部分引文未通过原文比对",
  number_not_in_evidence: "部分数值未在证据中命中",
  semantic_judge_skipped: "语义 judge 跳过（LLM 不可用）",
  semantic_judge_off: "语义 judge 关闭 · 论断仅过 L1-L3",
};

/** guardrail 标签：精确匹配 + 前缀匹配（graph_error:* / quote_unverified:2 等动态键）。 */
const guardLabel = (g: string): string => {
  if (GUARD_LABEL[g]) return GUARD_LABEL[g];
  if (g.startsWith("graph_error:")) return `图执行异常：${g.slice("graph_error:".length)}`;
  if (g.startsWith("agent_unready:")) return `Agent 未实装降级：${g.slice("agent_unready:".length)}`;
  if (g.startsWith("quote_unverified:")) return `${g.slice("quote_unverified:".length)} 条引文未通过原文比对`;
  if (g.startsWith("number_not_in_evidence:")) return `${g.slice("number_not_in_evidence:".length)} 条论断数值待核对`;
  if (g.startsWith("compare_coverage_low:"))
    return `对比表命中不足：${g.slice("compare_coverage_low:".length)} 格`;
  if (g.startsWith("compare_conflict:")) return `${g.slice("compare_conflict:".length)} 个维度双侧数值冲突`;
  if (g.startsWith("compare_one_sided:"))
    return `对比单侧风险：叙述仅覆盖 ${g.slice("compare_one_sided:".length)} 篇`;
  return "";
};

const AGENT_LABEL: Record<string, string> = {
  reader: "Reader · 单篇精读",
  compare: "Compare · 跨文对比",
  graph: "Graph · 图谱综述",
  writer: "Writer · 写作",
  viz: "Viz · 数据出图",
  none: "闲聊应答",
};

/** Agent 选择器项：value 与后端 FORCED_AGENT_VALUES 对齐（五 Agent 全实装）。 */
const AGENT_OPTIONS: { value: string; label: string; ready: boolean }[] = [
  { value: "auto", label: "自动路由", ready: true },
  { value: "reader", label: "Reader", ready: true },
  { value: "compare", label: "Compare", ready: true },
  { value: "graph", label: "Graph", ready: true },
  { value: "writer", label: "Writer", ready: true },
  { value: "viz", label: "Viz", ready: true },
];

/** 图级失败判定：runner 兜底文本或 graph_error 前缀。 */
const isFailedFinal = (d: FinalEvent): boolean =>
  d.answer.startsWith("[执行失败") || d.guardrails.some((g) => g.startsWith("graph_error:"));

const CONV_KEY = "meridian_conversation_id";

const secName = (s: string) => (s === "_front" || !s ? "前言" : s);

/** 时间线事件追加：ask 更新最后一轮，resume 按索引更新。 */
function appendTL(t: Turn[], i: number, kind: TLEvent["kind"], data: TLEvent["data"]): Turn[] {
  const c = [...t];
  if (c[i]) c[i] = { ...c[i], tl: [...c[i].tl, { kind, data }] };
  return c;
}

export default function ChatPage() {
  const [papers, setPapers] = useState<Paper[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [capHint, setCapHint] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [showScope, setShowScope] = useState(false);
  const [showTrace, setShowTrace] = useState(true);   // 右侧 Trace 时间线开关
  const [agent, setAgent] = useState("auto");          // UI 强制指定 Agent（auto=自动路由）
  const [evidence, setEvidence] = useState<EvidenceLoc | null>(null);  // 引用点击 → 原文浮层
  const msgsRef = useRef<HTMLDivElement>(null);
  const convRef = useRef<string>("");

  /** 打开原文溯源浮层：anchor 缺失（无锚点数据）时静默忽略。 */
  const showEvidence = (eid?: string, anchor?: Anchor, quote?: string) => {
    if (!anchor) return;
    setEvidence({ eid, anchor, quote });
  };

  /** 会话级 conversation_id（localStorage 持久化）：断点续跑按会话归属。 */
  const conversationId = () => {
    if (!convRef.current) {
      convRef.current = localStorage.getItem(CONV_KEY) ?? crypto.randomUUID();
      localStorage.setItem(CONV_KEY, convRef.current);
    }
    return convRef.current;
  };

  useEffect(() => {
    listPapers().then((ps) => {
      setPapers(ps);
      const parsed = ps.filter((p) => p.parsed);
      const fromUrl = new URLSearchParams(window.location.search).get("paper");
      const init = fromUrl && parsed.some((p) => p.id === fromUrl)
        ? [fromUrl] : parsed.slice(0, 1).map((p) => p.id);
      setSelected(new Set(init));
    });
  }, []);

  useEffect(() => {
    msgsRef.current?.scrollTo({ top: msgsRef.current.scrollHeight });
  }, [turns]);

  const ask = async () => {
    const q = question.trim();
    if (!q || busy) return;
    setQuestion("");
    setShowScope(false);
    setBusy(true);
    const t0 = performance.now();
    const turn: Turn = { question: q, answer: "", trace: [], retrieval: null, final: null, tl: [] };
    setTurns((t) => [...t, turn]);
    try {
      await streamChat(q, Array.from(selected), {
        guardrail: (d) => setCapHint(String(d.message ?? "")),
        trace: (card) => setTurns((t) => {
          const c = [...t];
          const i = c.length - 1;
          c[i] = { ...c[i], trace: [...c[i].trace, card] };
          return appendTL(c, i, "trace", card);
        }),
        plan: (d) => setTurns((t) => appendTL(t, t.length - 1, "plan", d)),
        toolCall: (d) => setTurns((t) => appendTL(t, t.length - 1, "tool_call", d)),
        toolResult: (d) => setTurns((t) => appendTL(t, t.length - 1, "tool_result", d)),
        evidence: (d) => setTurns((t) => appendTL(t, t.length - 1, "evidence", d)),
        verifier: (d) => setTurns((t) => appendTL(t, t.length - 1, "verifier", d)),
        retrieval: (d) => setTurns((t) => {
          const c = [...t]; c[c.length - 1] = { ...c[c.length - 1], retrieval: d }; return c;
        }),
        delta: (text) => setTurns((t) => {
          const c = [...t];
          c[c.length - 1] = { ...c[c.length - 1], answer: c[c.length - 1].answer + text };
          return c;
        }),
        final: (d) => setTurns((t) => {
          const c = [...t];
          c[c.length - 1] = {
            ...c[c.length - 1],
            answer: c[c.length - 1].answer || d.answer,  // 抽取式模式无 delta，答案在 final 里
            trace: d.trace?.length ? d.trace : c[c.length - 1].trace,
            final: d,
            threadId: d.thread_id ?? c[c.length - 1].threadId,
            failed: isFailedFinal(d),
          };
          return c;
        }),
      }, conversationId(), agent);
    } catch (e) {
      setTurns((t) => {
        const c = [...t];
        c[c.length - 1] = {
          ...c[c.length - 1],
          answer: c[c.length - 1].answer + `\n[请求失败：${e instanceof Error ? e.message : e}]`,
        };
        return c;
      });
    } finally {
      setBusy(false);
    }
  };

  /** 继续执行：从最近 checkpoint 恢复失败线程，按索引更新原 Turn（不追加）。 */
  const resumeTurn = async (ti: number) => {
    const turn = turns[ti];
    if (!turn?.threadId || busy) return;
    setBusy(true);
    setTurns((t) => {   // 先清空该轮，防恢复重放 delta 造成文本串接
      const c = [...t];
      c[ti] = { ...c[ti], answer: "", trace: [], retrieval: null, final: null, tl: [] };
      return c;
    });
    try {
      await resumeChat(turn.threadId, {
        trace: (card) => setTurns((t) => {
          const c = [...t];
          if (c[ti]) c[ti] = { ...c[ti], trace: [...c[ti].trace, card] };
          return appendTL(c, ti, "trace", card);
        }),
        plan: (d) => setTurns((t) => appendTL(t, ti, "plan", d)),
        toolCall: (d) => setTurns((t) => appendTL(t, ti, "tool_call", d)),
        toolResult: (d) => setTurns((t) => appendTL(t, ti, "tool_result", d)),
        evidence: (d) => setTurns((t) => appendTL(t, ti, "evidence", d)),
        verifier: (d) => setTurns((t) => appendTL(t, ti, "verifier", d)),
        retrieval: (d) => setTurns((t) => {
          const c = [...t]; if (c[ti]) c[ti] = { ...c[ti], retrieval: d }; return c;
        }),
        delta: (text) => setTurns((t) => {
          const c = [...t];
          if (c[ti]) c[ti] = { ...c[ti], answer: c[ti].answer + text };
          return c;
        }),
        final: (d) => setTurns((t) => {
          const c = [...t];
          if (c[ti]) c[ti] = {
            ...c[ti],
            answer: c[ti].answer || d.answer,
            trace: d.trace?.length ? d.trace : c[ti].trace,
            final: d,
            threadId: d.thread_id ?? c[ti].threadId,
            failed: isFailedFinal(d),
          };
          return c;
        }),
      });
    } catch (e) {
      setTurns((t) => {
        const c = [...t];
        if (c[ti]) c[ti] = {
          ...c[ti],
          answer: c[ti].answer + `\n[恢复请求失败：${e instanceof Error ? e.message : e}]`,
        };
        return c;
      });
    } finally {
      setBusy(false);
    }
  };

  const lastTurn = turns[turns.length - 1];
  const scopeTitle = selected.size === 0
    ? "未选择论文"
    : selected.size === 1
      ? papers.find((p) => selected.has(p.id))?.title.slice(0, 24) ?? "1 篇论文"
      : `${selected.size} 篇论文`;

  return (
    <div className={`chat-shell ${showTrace ? "with-trace" : ""}`}>
      <div className="topbar">
        <span className="topbar-title">溯源问答</span>
        <span className="chip accent">ClaimTrace 逐论断验证</span>
        <span style={{ flex: 1 }} />
        <span className="chip" title="本次回答的检索范围">{scopeTitle}</span>
        {capHint && <span className="chip warn" title="选文/截断提示">{capHint}</span>}
        <button className="chip" style={{ cursor: "pointer" }} onClick={() => setShowScope((s) => !s)}>
          检索范围 <ChevronRight size={11} style={{ transform: showScope ? "rotate(90deg)" : "none" }} />
        </button>
        <button className="chip" style={{ cursor: "pointer" }} onClick={() => setShowTrace((s) => !s)}>
          Trace <ChevronRight size={11} style={{ transform: showTrace ? "rotate(90deg)" : "none" }} />
        </button>
        {showScope && (
          <div style={{ position: "absolute", top: "var(--topbar-h)", right: "var(--space-6)",
            zIndex: 20, background: "var(--surface)", border: "1px solid var(--line)",
            borderRadius: "var(--radius-md)", boxShadow: "var(--shadow-md)",
            padding: "var(--space-3) var(--space-4)", minWidth: 260 }}>
            <p className="kv" style={{ marginBottom: "var(--space-2)" }}>选择参与检索的论文</p>
            {papers.filter((p) => p.parsed).map((p) => (
              <label key={p.id} style={{ display: "flex", gap: "var(--space-2)",
                alignItems: "center", padding: "4px 0", fontSize: "var(--text-sm)", cursor: "pointer" }}>
                <input type="checkbox" checked={selected.has(p.id)}
                  onChange={(e) => setSelected((s) => {
                    const n = new Set(s);
                    if (e.target.checked) {
                      if (n.size >= 8) {           // 前端钳制：选文上限 ≤8（与后端护栏一致）
                        setCapHint("选文上限 8 篇，已停止勾选（超出部分不参与检索）");
                        e.target.checked = false;
                        return n;
                      }
                      n.add(p.id);
                    } else {
                      n.delete(p.id);
                    }
                    setCapHint("");
                    return n;
                  })} />
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {p.title.slice(0, 30)}
                </span>
              </label>
            ))}
            {papers.filter((p) => p.parsed).length === 0 && (
              <p className="kv">文献库为空，先去上传 PDF。</p>
            )}
          </div>
        )}
      </div>

      <div className="chat-body">
        <div className="chat-col">
          <div className="msgs" ref={msgsRef}>
            <div className="msgs-inner">
              {turns.length === 0 && (
                <div className="placeholder" style={{ minHeight: "50vh" }}>
                  <div className="placeholder-card">
                    <div className="placeholder-icon"><Spark size={24} /></div>
                    <h2>向文献提问</h2>
                    <p>
                      例如「模型用了多少块 GPU？」「BLEU 分数是多少？」——
                      Agent 状态图（Router→Plan→并行检索→校验→回答）全程可视化，每句可溯源。
                    </p>
                  </div>
                </div>
              )}
              {turns.map((turn, ti) => (
                <div key={ti}>
                  <div className="msg user"><span>{turn.question}</span></div>
                  <div className="msg bot">
                    {turn.trace.length > 0 && <Pipeline cards={turn.trace} />}
                    {turn.retrieval && turn.retrieval.hits.length > 0 && (
                      <p className="kv" style={{ marginBottom: "var(--space-2)" }}>
                        <details>
                          <summary className="chip" style={{ cursor: "pointer", listStyle: "none" }}>
                            检索明细（{turn.retrieval.hits.length} 块）
                          </summary>
                          <div style={{ marginTop: "var(--space-2)" }}>
                            {turn.retrieval.hits.slice(0, 5).map((h) => (
                              <div key={h.chunk_id} className="kv" style={{
                                borderBottom: "1px dashed var(--line)", padding: "6px 0" }}>
                                <b style={{ color: "var(--accent)" }}>p{h.page} §{secName(h.section_path)}</b>
                                {h.rerank ? ` · rerank ${h.rerank}` : h.rrf ? ` · RRF ${h.rrf}` : ` · ${h.score}`}
                                <br />{h.text.slice(0, 110)}…
                              </div>
                            ))}
                          </div>
                        </details>
                      </p>
                    )}
                    {turn.answer && (
                      <div className="bubble">
                        {turn.final
                          ? <AnswerWithAnchors text={turn.final.answer}
                              anchors={turn.final.anchors} onEvidence={showEvidence} />
                          : turn.answer}
                        {turn.final?.compare_table && turn.final.compare_table.rows.length > 0 && (
                          <CompareTableView table={turn.final.compare_table}
                            anchors={turn.final.anchors} onEvidence={showEvidence} />
                        )}
                        {turn.final?.outline && turn.final.outline.sections.length > 0 && (
                          <OutlineView outline={turn.final.outline} />
                        )}
                        {turn.final?.graph_data && (turn.final.graph_data.metrics?.nodes.length ?? 0) > 0 && (
                          <GraphView graph={turn.final.graph_data} />
                        )}
                        {turn.final?.chart_spec && (turn.final.chart_spec.data?.length ?? 0) > 0 && (
                          <ChartView spec={turn.final.chart_spec} />
                        )}
                        {turn.final && <FinalMeta final={turn.final} />}
                      </div>
                    )}
                    {turn.failed && turn.final?.resumable && turn.threadId && (
                      <div style={{ marginTop: "var(--space-2)", display: "flex",
                        gap: "var(--space-2)", alignItems: "center", flexWrap: "wrap" }}>
                        <span className="badge warn">⚠ 图执行中断 · 断点已保存</span>
                        <button className="chip" style={{ cursor: "pointer" }} disabled={busy}
                          onClick={() => resumeTurn(ti)}>
                          继续执行（从断点恢复，已完成步骤不重跑）
                        </button>
                      </div>
                    )}
                    {turn.final && <Claims claims={turn.final.claims}
                      anchors={turn.final.anchors} hits={turn.retrieval?.hits ?? []}
                      onEvidence={showEvidence} />}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="dock-wrap">
            <div className="dock">
              <div className="dock-row">
                <input
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && ask()}
                  placeholder={busy ? "Agent 执行中…" : "对选中的论文提问，每句回答可溯源到原文…"}
                  disabled={busy}
                />
                <button className="send-btn" onClick={ask} disabled={busy || !selected.size}
                  aria-label="发送">↑</button>
              </div>
              <div className="dock-tools">
                <span className="chip">检索范围 {selected.size} 篇</span>
                <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
                  {AGENT_OPTIONS.map((o) => (
                    <button key={o.value} className={`chip ${agent === o.value ? "accent" : ""}`}
                      style={{ cursor: "pointer" }}
                      title={o.ready ? `强制使用 ${o.label} Agent`
                        : `${o.label} Agent 未实装 · 选中后由 Reader 降级执行（trace 如实标注）`}
                      onClick={() => setAgent(o.value)}>
                      {o.label}{o.ready ? "" : "·"}
                    </button>
                  ))}
                </span>
                {lastTurn?.final && (
                  <span className={`chip ${lastTurn.final.mode === "generative" ? "ok" : "warn"}`}>
                    {lastTurn.final.mode === "generative" ? "生成式 · 引用已后验"
                      : lastTurn.final.mode === "refusal" ? "范围外拒答"
                      : lastTurn.final.mode === "none" ? "未找到依据"
                      : "抽取式证据模式"}
                  </span>
                )}
              </div>
            </div>
            <div className="statusbar">
              {lastTurn?.failed
                ? "图执行中断 · 断点已保存 · 点击「继续执行」从失败节点恢复（已完成步骤不重跑）"
                : lastTurn?.final
                  ? `图执行完成 · 用时 ${lastTurn.final.elapsed ?? "-"}s · 论断 ${lastTurn.final.claims.length} 条`
                  : busy ? "状态图执行中：Router → Planner → Executor → Verifier → Responder"
                  : "就绪"}
            </div>
          </div>
        </div>
        {showTrace && (
          <TracePanel tl={lastTurn?.tl ?? []} busy={busy} final={lastTurn?.final ?? null} />
        )}
      </div>
      {evidence && <EvidenceModal loc={evidence} onClose={() => setEvidence(null)} />}
    </div>
  );
}

/** 节点即卡片：内嵌可展开执行管线（官方 Graph Execution Cards 模式的自有协议实现）。 */
function Pipeline({ cards }: { cards: TraceCard[] }) {
  const [open, setOpen] = useState(false);
  const retrieveCount = cards.filter((c) => c.node === "retrieve_task").length;
  const chips: string[] = [];
  for (const c of cards) {
    if (c.node === "retrieve_task") {
      if (!chips.includes("__rt__")) chips.push("__rt__");
    } else chips.push(c.title);
  }
  return (
    <div className="pipeline" onClick={() => setOpen((o) => !o)}>
      <div className="pipe-row">
        {chips.map((t, i) => (
          <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            {i > 0 && <span className="pipe-arrow">→</span>}
            <span className="chip">
              {t === "__rt__" ? `并行检索 ×${retrieveCount}` : t}
            </span>
          </span>
        ))}
        <ChevronRight size={12} className="pipe-caret"
          style={{ transform: open ? "rotate(90deg)" : "none", color: "var(--ink-3)" }} />
      </div>
      {open && (
        <div className="pipe-cards">
          {cards.map((c, i) => (
            <div key={i} className="pipe-card">
              <b>{c.title}</b>
              <span className="kv"> · {c.summary}</span>
              {Object.keys(c.payload).length > 0 && (
                <pre className="pipe-payload">{JSON.stringify(c.payload, null, 1).slice(0, 420)}</pre>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function FinalMeta({ final }: { final: FinalEvent }) {
  const guards = final.guardrails
    .map((g) => [g, guardLabel(g)] as const)
    .filter(([, label]) => label);
  const hasMeta = final.mode !== "generative" || guards.length > 0
    || final.agent || (final.handoffs?.length ?? 0) > 0;
  if (!hasMeta) return null;
  return (
    <p style={{ margin: "var(--space-2) 0 0", display: "flex", gap: "var(--space-2)",
      flexWrap: "wrap", alignItems: "center" }}>
      {final.agent && <span className="badge ok">◎ {AGENT_LABEL[final.agent] ?? final.agent}</span>}
      {final.handoffs && final.handoffs.length > 0 && (
        <span className="badge" title={final.handoffs.map((h) => `${h.from} → ${h.to}：${h.note}`)
          .join("\n")}>
          协作链 {[...new Set(final.handoffs.map((h) => h.to))].join(" → ")}
        </span>
      )}
      {final.mode === "extractive" && <span className="badge warn">抽取式证据模式（无 LLM）</span>}
      {guards.map(([g, label]) => <span key={g} className="badge warn">⚠ {label}</span>)}
    </p>
  );
}

/** Writer Agent 写作大纲（artifact 下发，正文按同一大纲生成）。 */
function OutlineView({ outline }: { outline: Outline }) {
  return (
    <div style={{ marginTop: "var(--space-3)", border: "1px dashed var(--line)",
      borderRadius: "var(--radius-md)", padding: "var(--space-3)" }}>
      <p className="kv" style={{ margin: "0 0 var(--space-2)" }}>
        <b>写作大纲</b>（Writer Agent · 正文按此结构生成，逐节带引用）
      </p>
      <ol style={{ margin: 0, paddingLeft: 20, fontSize: "var(--text-sm)" }}>
        {outline.sections.map((s, i) => <li key={i}>{s}</li>)}
      </ol>
    </div>
  );
}

/** Graph Agent 引用图谱摘要：节点/边/Top PageRank（G6 全图渲染预留 D7）。 */
function GraphView({ graph }: { graph: GraphData }) {
  const top = (graph.metrics?.nodes ?? []).slice(0, 3);
  const maxPr = Math.max(...top.map((t) => t.pagerank), 0.0001);
  return (
    <div style={{ marginTop: "var(--space-3)", border: "1px dashed var(--line)",
      borderRadius: "var(--radius-md)", padding: "var(--space-3)" }}>
      <p className="kv" style={{ margin: "0 0 var(--space-2)", display: "flex", gap: 8,
        alignItems: "center", flexWrap: "wrap" }}>
        <span>
          <b>引用图谱</b>（Graph Agent · 仅由 references 元数据建边 ·
          {graph.nodes.length} 节点 · {graph.metrics?.edge_count ?? 0} 边
          {graph.external_count ? ` · 外部引用 ${graph.external_count} 条` : ""}
          · backend {graph.metrics?.backend}）
        </span>
        <Link className="chip accent" style={{ fontSize: 11 }} href="/graph">
          在图谱中心打开 →（G6 交互 · 社区综述 · 研究缝隙）
        </Link>
      </p>
      {top.map((t) => (
        <div key={t.paper_id} style={{ marginBottom: 6 }}>
          <div className="kv" style={{ display: "flex", justifyContent: "space-between" }}>
            <span>{t.title.slice(0, 30)}</span>
            <span>PageRank {t.pagerank} · 被引 {t.in_degree} · 引出 {t.out_degree}</span>
          </div>
          <div style={{ height: 6, background: "var(--surface-2)", borderRadius: 3 }}>
            <div style={{ width: `${Math.round((t.pagerank / maxPr) * 100)}%`, height: "100%",
              background: "var(--accent)", borderRadius: 3 }} />
          </div>
        </div>
      ))}
      {top.length === 0 && <p className="kv">库内暂无引用边。</p>}
    </div>
  );
}

/** Viz Agent 声明式图表：零依赖 CSS 条形渲染（ECharts 升级预留），图下强制标注数据来源。 */
function ChartView({ spec }: { spec: ChartSpec }) {
  const max = Math.max(...spec.data.map((d) => Math.abs(d.value)), 0.0001);
  const prov = spec.provenance;
  return (
    <div style={{ marginTop: "var(--space-3)", border: "1px dashed var(--line)",
      borderRadius: "var(--radius-md)", padding: "var(--space-3)" }}>
      <p className="kv" style={{ margin: "0 0 var(--space-2)" }}>
        <b>{spec.title || "数据图表"}</b>（{spec.type} · chart_spec 声明式下发）
      </p>
      {spec.data.map((d, i) => (
        <div key={i} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
          <span className="kv" style={{ width: 120, overflow: "hidden",
            textOverflow: "ellipsis", whiteSpace: "nowrap", textAlign: "right" }}>{d.label}</span>
          <div style={{ flex: 1, height: 14, background: "var(--surface-2)", borderRadius: 3 }}>
            <div style={{ width: `${Math.round((Math.abs(d.value) / max) * 100)}%`,
              height: "100%", background: "var(--accent)", borderRadius: 3 }} />
          </div>
          <span className="kv" style={{ minWidth: 48 }}>{d.value}</span>
        </div>
      ))}
      <p className="kv" style={{ margin: "var(--space-2) 0 0" }}>
        横轴「{spec.x_label}」 · 纵轴「{spec.y_label}」
        {prov && ` · 来源 p${prov.page ?? 1} 表${prov.table_id ?? ""}`
          + (prov.skipped_rows ? ` · 非数值行已剔除 ${prov.skipped_rows}` : "")}
      </p>
    </div>
  );
}

/** Compare Agent 维度对齐表：维度行 × 论文列，空格如实标注「未找到」；格子 chip 可点击溯源。 */
function CompareTableView({ table, anchors, onEvidence }: {
  table: CompareTable;
  anchors?: Record<string, Anchor>;
  onEvidence?: (eid?: string, anchor?: Anchor, quote?: string) => void;
}) {
  const titleOf = (pid: string, i: number) =>
    table.titles?.[pid] || `P${i + 1}`;
  const anchorForChunk = (cid?: string) =>
    cid ? Object.values(anchors ?? {}).find((a) => a.chunk_id === cid) : undefined;
  return (
    <div style={{ marginTop: "var(--space-3)" }}>
      <p className="kv" style={{ margin: "0 0 var(--space-2)" }}>
        <b>维度对齐表</b>（Compare Agent · align_dimension 产物，点击格子查看原文）
      </p>
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "var(--text-sm)" }}>
          <thead>
            <tr>
              <th style={{ textAlign: "left", padding: "6px 8px", borderBottom: "1px solid var(--line)",
                color: "var(--ink-3)", whiteSpace: "nowrap" }}>维度</th>
              {table.papers.map((pid, i) => (
                <th key={pid} style={{ textAlign: "left", padding: "6px 8px",
                  borderBottom: "1px solid var(--line)", color: "var(--accent)" }}>
                  P{i + 1} · {titleOf(pid, i).slice(0, 22)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((r) => (
              <tr key={r.dimension}>
                <td style={{ padding: "6px 8px", borderBottom: "1px dashed var(--line)",
                  whiteSpace: "nowrap", maxWidth: 180, overflow: "hidden",
                  textOverflow: "ellipsis" }}>
                  {r.dimension}
                  {r.conflict && <span className="badge warn" title="双侧数值集合不相交——请人工核对原文（冲突双侧并列，系统不裁决）"> ⚠ 冲突</span>}
                </td>
                {r.cells.map((c: CompareCell) => (
                  <td key={c.paper_id} style={{ padding: "6px 8px",
                    borderBottom: "1px dashed var(--line)" }}>
                    {c.found ? (
                      <>
                        <span className={`chip accent anchor-chip ${anchorForChunk(c.chunk_id) ? "clickable" : ""}`}
                          onClick={() => {
                            const a = anchorForChunk(c.chunk_id);
                            if (a) onEvidence?.(c.eid, a, c.quote);
                          }}
                          title={anchorForChunk(c.chunk_id) ? "点击查看原文页 + 高亮" : undefined}>
                          {c.eid ? `${c.eid} · ` : ""}原文 p{c.page ?? 1} · §{secName(c.section_path ?? "")}
                        </span>
                        <span className="kv">{(c.quote ?? "").slice(0, 90)}
                          {(c.quote ?? "").length > 90 ? "…" : ""}</span>
                      </>
                    ) : (
                      <span className="badge warn">未找到</span>
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** [E#] → 可点击锚点徽章（anchors 提供页码/章节，点击弹原文浮层 + bbox 高亮）。 */
function AnswerWithAnchors({ text, anchors, onEvidence }: {
  text: string;
  anchors?: Record<string, Anchor>;
  onEvidence?: (eid?: string, anchor?: Anchor, quote?: string) => void;
}) {
  const parts = text.split(/(\[E\d+\])/g);
  return (
    <>
      {parts.map((part, i) =>
        /^\[E\d+\]$/.test(part) ? (
          (() => {
            const a = anchors?.[part];
            if (!a) {
              return <span key={i} className="chip accent anchor-chip" style={{ opacity: 0.55 }}
                title="无锚点数据（历史轮次或降级模式）">{part}</span>;
            }
            return (
              <span key={i} className="chip accent anchor-chip clickable"
                onClick={() => onEvidence?.(part, a)}
                title={`点击查看原文 p${a.page} · §${secName(a.section_path)}`}>
                {part} · p{a.page} · §{secName(a.section_path)}
              </span>
            );
          })()
        ) : (
          <span key={i}>{part}</span>
        )
      )}
    </>
  );
}

/** 原文溯源浮层：PDF 画布渲染对应页 + bbox 红框高亮；无 PDF 数据时回退引文卡片。 */
function EvidenceModal({ loc, onClose }: { loc: EvidenceLoc; onClose: () => void }) {
  const { anchor, quote, eid } = loc;
  const a = anchor;
  return (
    <div className="ev-overlay" onClick={onClose}>
      <div className="ev-modal" onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)",
          marginBottom: "var(--space-2)", flexWrap: "wrap" }}>
          <b>{eid ? `${eid} · ` : ""}原文溯源</b>
          <span className="chip accent">p{a.page} · §{secName(a.section_path)}</span>
          {a.paper_label && <span className="chip">{a.paper_label}</span>}
          <span style={{ flex: 1 }} />
          <button className="btn ghost" style={{ padding: "2px 10px" }}
            onClick={onClose}>关闭 ✕</button>
        </div>
        {a.paper_id && a.page > 0 ? (
          <PdfAnchored paperId={a.paper_id} page={a.page} bbox={a.bbox?.length === 4 ? a.bbox : undefined} />
        ) : (
          <div className="evidence"><i>该锚点缺少论文/页码信息，无法渲染原文页。</i></div>
        )}
        {quote && (
          <div className="evidence" style={{ marginTop: "var(--space-2)" }}>
            <i>引文（校验器逐字比对）</i>
            <br />{quote}
          </div>
        )}
      </div>
    </div>
  );
}

/** 逐论断溯源：三态徽章（已验证/需核对/未验证），support 可点击跳原文。 */
function Claims({ claims, anchors, hits, onEvidence }: {
  claims: Claim[];
  anchors?: Record<string, Anchor>;
  hits: Hit[];
  onEvidence?: (eid?: string, anchor?: Anchor, quote?: string) => void;
}) {
  const [open, setOpen] = useState<number | null>(null);
  if (!claims.length) return null;
  return (
    <div style={{ marginTop: "var(--space-3)" }}>
      <p className="kv" style={{ margin: 0 }}>
        <b>逐论断溯源</b>（L1-L4 校验 · 点击出处查看原文）
      </p>
      {claims.map((c, i) => (
        <div key={i}
          className={`claim ${c.status === "unsupported" ? "unverified"
            : c.status === "needs_check" ? "needs-check" : ""}`}
          onClick={() => setOpen(open === i ? null : i)}>
          <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "baseline",
            flexWrap: "wrap" }}>
            {c.status === "supported"
              ? <span className="badge ok">已验证</span>
              : c.status === "needs_check"
                ? <span className="badge warn" title="数值/引文未在证据中逐字命中，请人工核对">需核对</span>
                : <span className="badge warn">⚠ 未验证</span>}
            {c.quote_status === "unverified" && (
              <span className="badge warn" title="引文与原文比对未达标（L2）">引文待核</span>
            )}
            {c.semantic_status === "partial" && (
              <span className="badge" title="语义 judge：证据部分支撑（L4）">部分支撑</span>
            )}
            {c.semantic_status === "unchecked" && (
              <span className="badge" title="语义 judge 未执行（关闭或 LLM 不可用）——绿色仅代表 L2/L3 通过">语义未核</span>
            )}
            <span>{c.text.slice(0, 160)}{c.text.length > 160 ? "…" : ""}</span>
          </div>
          {c.status === "needs_check" && c.unverified_numbers?.length ? (
            <div className="anchor" style={{ color: "var(--warn)" }}>
              以下数值未在证据原文中命中：{c.unverified_numbers.join("、")}（请人工核对）
            </div>
          ) : null}
          {c.support.length > 0 && (
            <div className="anchor">
              出处：{c.support.map((s, k) => {
                const a0 = anchors?.[s.eid ?? ""];
                // 原句块级锚点优先：support.block_bbox 是 quote 定位回的具体 DocIR 块
                const a = a0 && s.block_bbox?.length === 4 ? { ...a0, bbox: s.block_bbox } : a0;
                return (
                  <span key={k} className="chip anchor-chip clickable"
                    onClick={(e) => {
                      e.stopPropagation();
                      onEvidence?.(s.eid, a, s.quote);
                    }}
                    title={a ? `点击查看原文 p${s.page}${s.block_bbox ? "（原句块级高亮）" : ""}` : undefined}>
                    {s.eid ? `${s.eid} · ` : ""}p{s.page} §{secName(s.section_path)}
                  </span>
                );
              })}
            </div>
          )}
          {open === i && c.support.map((s, k) => {
            const hit = hits.find((h) => h.chunk_id === s.chunk_id);
            return (
              <div key={k} className="evidence">
                <i>{s.eid ? `${s.eid} · ` : ""}原文 · 第 {s.page} 页 · {secName(s.section_path)}</i>
                <br />{s.quote || hit?.text.slice(0, 220) || ""}
              </div>
            );
          })}
          {open === i && c.status === "unsupported" && (
            <div className="evidence"><i>检索结果中未找到支撑此论断的原文，请人工核对。</i></div>
          )}
        </div>
      ))}
    </div>
  );
}
