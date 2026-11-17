"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import "katex/dist/katex.min.css";
import {
  Anchor, ChartSpec, Claim, CompareCell, CompareTable, ConversationInfo, FinalEvent, GraphData, Hit,
  Outline, Paper, TLEvent, TraceCard, TurnRecord, convKeyFor, copyText, uuid,
  fetchQaTraceOrNull, getConversationTurns, getPaper, listConversations, listPapers,
  resumeChat, stopChat, streamChat,
} from "@/lib/api";
import { Activity, ChevronRight, List, Spark } from "@/components/icons";
import { useShell } from "@/components/ShellContext";
import { useIsland } from "@/lib/island";
import Stamp from "@/components/Stamp";
import TracePanel from "@/components/chat/TracePanel";
import PdfAnchored from "@/components/PdfAnchored";
import ThinkingBlock from "@/components/chat/ThinkingBlock";
import MarkdownWithCitations from "@/components/chat/MarkdownWithCitations";

/** 证据浮层定位：anchor（paper/page/bbox）+ 可选引文与编号。 */
interface EvidenceLoc {
  eid?: string;
  anchor: Anchor;
  quote?: string;
}

interface Turn {
  question: string;
  answer: string;
  thinking: string;        // 深度思考流（reasoning_delta 累加；空=未开启/降级）
  thinkingActive: boolean; // 思考进行中（首个正文 delta 到达即 false）
  thinkingMs?: number;     // 思考用时（前端实测：首 reasoning → 首 delta）
  trace: TraceCard[];
  retrieval: { channels: string[]; hits: Hit[] } | null;
  final: FinalEvent | null;
  threadId?: string;   // 断点续跑/停止生成：meta 事件先行下发（无需等 final）
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
  user_stopped: "已手动停止生成",
  aspect_judge_applied: "检索面可答性复核（LLM judge 修正覆盖判定）",
  rerank_not_configured: "Rerank 未配置 → 融合排序截断",
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
  if (g.startsWith("partial_answer:"))
    return `部分回答：${g.slice("partial_answer:".length)} 个子问题有据可答，其余如实标注未明确说明`;
  if (g.startsWith("doc_scope_pruned"))
    return "未指定文档：已按相关度自动收窄检索范围";
  return "";
};

/** 分级作答折叠（v2.3.0 UI）：把「未能回答的部分」小节从正文切出，final 后折叠成
 *  chip（默认收起、点击展开）——主体回答一屏可读；流式期照常全量显示不跳变。 */
const UNANSWERED_HEAD = /(?:^|\n)\s*(?:#{1,6}\s*)?\**\s*未能回答的部分\s*\**\s*[:：]?[ \t]*(?=\n|$)/;

function splitUnanswered(text: string): { main: string; tail: string | null } {
  const m = text.match(UNANSWERED_HEAD);
  if (!m || m.index === undefined) return { main: text, tail: null };
  return { main: text.slice(0, m.index).trimEnd(), tail: text.slice(m.index).trim() };
}

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

const THINK_KEY = "meridian_think_enabled";

const secName = (s: string) => (s === "_front" || !s ? "前言" : s);

/** 时间线事件追加：ask 更新最后一轮，resume 按索引更新。 */
function appendTL(t: Turn[], i: number, kind: TLEvent["kind"], data: TLEvent["data"]): Turn[] {
  const c = [...t];
  if (c[i]) c[i] = { ...c[i], tl: [...c[i].tl, { kind, data }] };
  return c;
}

/** 流式合帧缓冲：reasoning/delta 高密度到达时 ~50ms 批量应用，防全页重渲染风暴。 */
interface StreamAcc {
  idx: number;
  thinking: string;      // 思考流全量（替换式）
  answer: string;        // 正文增量（追加式，应用后清零）
  thinkingActive: boolean;
  thinkStart: number | null;
  answerStart: number | null;
}

const emptyAcc = (idx: number): StreamAcc => ({
  idx, thinking: "", answer: "", thinkingActive: false,
  thinkStart: null, answerStart: null,
});

export default function ChatPage() {
  const [papers, setPapers] = useState<Paper[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [capHint, setCapHint] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [showScope, setShowScope] = useState(false);
  const [traceOpen, setTraceOpen] = useState(false);   // Trace 右侧抽屉（localStorage 持久化，默认收起）
  const [agent, setAgent] = useState("auto");          // UI 强制指定 Agent（auto=自动路由）
  const [evidence, setEvidence] = useState<EvidenceLoc | null>(null);  // 引用点击 → 原文浮层
  // ChatGLM 模式：会话侧栏 + per-request 深度思考开关
  const [conversations, setConversations] = useState<ConversationInfo[]>([]);
  const [activeConvId, setActiveConvId] = useState("");
  const [thinkEnabled, setThinkEnabled] = useState(true);
  const island = useIsland();
  const { docMode, setDocMode } = useShell();
  const isGeneral = docMode === "general";
  const docModeParam = isGeneral ? "general" as const : "paper" as const;
  const msgsRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const accRef = useRef<StreamAcc>(emptyAcc(-1));
  const flushTimer = useRef<number | null>(null);
  const smoothRef = useRef<{ pending: string; raf: number | null; last: number }>(
    { pending: "", raf: null, last: 0 });

  /** 普通模式 Agent 子集（与后端 GENERAL_FORCED_AGENTS 对齐：无 Graph/Writer）。 */
  const agentOptions = useMemo(
    () => (isGeneral
      ? AGENT_OPTIONS.filter((o) => ["auto", "reader", "compare", "viz"].includes(o.value))
      : AGENT_OPTIONS),
    [isGeneral]);

  // 模式切换时若残留学术专属 Agent 强制值 → 回自动路由（与后端白名单一致）
  useEffect(() => {
    setAgent((a) => (isGeneral && ["graph", "writer"].includes(a) ? "auto" : a));
  }, [isGeneral]);

  /** §4.1 演示问题卡:按当前选中论文动态选择(Attention→专属三题;多篇→对比题优先)。 */
  const demoQuestions = useMemo(() => {
    if (isGeneral) {
      return selected.size > 1
        ? ["对比这两份文档的异同", "哪一份更适合入门？", "两份文档的术语有何差异？"]
        : ["这份文档的核心内容是什么？", "第一章讲了哪些关键概念？", "帮我总结这份文档的结构"];
    }
    if (selected.size > 1) {
      return ["对比这几篇论文的核心差异", "这些论文共同引用了哪些方法？", "哪一篇的实验设置最完整？"];
    }
    const t = (papers.find((p) => selected.has(p.id))?.title ?? "").toLowerCase();
    const isAttention = t.includes("attention is all you need")
      || (t.includes("attention") && t.includes("transform"));
    return isAttention
      ? ["模型用了多少块 GPU？", "BLEU 分数是多少？", "对比这篇论文的预训练目标"]
      : ["这篇论文的核心方法是什么？", "实验用了什么数据集和指标？", "主要结论和局限是什么？"];
  }, [selected, papers, isGeneral]);

  /** 打开原文溯源浮层：anchor 缺失（无锚点数据）时静默忽略。 */
  const showEvidence = (eid?: string, anchor?: Anchor, quote?: string) => {
    if (!anchor) return;
    setEvidence({ eid, anchor, quote });
  };

  const refreshConversations = () => {
    listConversations(50, true, docModeParam).then(setConversations)
      .catch(() => { /* 后端未起时静默 */ });
  };

  // 挂载：恢复上次会话（ChatGLM 行为，按模式隔离）+ 会话列表 + Trace 抽屉状态持久化。
  // 旧版单键迁移：meridian_conversation_id → 专业模式键（一次性、幂等）
  useEffect(() => {
    const legacy = localStorage.getItem("meridian_conversation_id");
    if (legacy && !localStorage.getItem(convKeyFor("paper"))) {
      localStorage.setItem(convKeyFor("paper"), legacy);
    }
    localStorage.removeItem("meridian_conversation_id");
    refreshConversations();
    const savedConv = localStorage.getItem(convKeyFor(docModeParam)) ?? "";
    const savedThink = localStorage.getItem(THINK_KEY);
    if (savedThink != null) setThinkEnabled(savedThink === "1");
    setTraceOpen(localStorage.getItem("meridian:trace-open") === "1");
    if (savedConv) {
      setActiveConvId(savedConv);
      void loadConversation(savedConv);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 模式切换 → 切换到该模式的活跃会话（无则清空为新对话）；busy 中不切防打断流式
  const modeInitRef = useRef(false);
  useEffect(() => {
    if (!modeInitRef.current) { modeInitRef.current = true; return; }
    if (busy) return;
    const savedConv = localStorage.getItem(convKeyFor(docModeParam)) ?? "";
    setActiveConvId(savedConv);
    if (savedConv) void loadConversation(savedConv);
    else {
      setTurns([]);
      setQuestion("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docModeParam]);

  // 全局侧栏 ↔ 问答页协调（window CustomEvent，见 AppShell 事件契约）：
  // conv-new=就地新建 / conv-open=打开指定会话 / conv-deleted=活跃会话被删 / conv-refresh=侧栏改名删除后同步标题
  useEffect(() => {
    const onNew = () => newConversation();
    const onOpen = (e: Event) => {
      const id = (e as CustomEvent<{ id: string }>).detail?.id;
      if (id) openConversation(id);
    };
    const onDeleted = (e: Event) => {
      const id = (e as CustomEvent<{ id: string }>).detail?.id;
      if (id && id === activeConvId) newConversation();
    };
    const onRefresh = () => refreshConversations();
    window.addEventListener("meridian:conv-new", onNew);
    window.addEventListener("meridian:conv-open", onOpen);
    window.addEventListener("meridian:conv-deleted", onDeleted);
    window.addEventListener("meridian:conv-refresh", onRefresh);
    return () => {
      window.removeEventListener("meridian:conv-new", onNew);
      window.removeEventListener("meridian:conv-open", onOpen);
      window.removeEventListener("meridian:conv-deleted", onDeleted);
      window.removeEventListener("meridian:conv-refresh", onRefresh);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeConvId, busy]);

  const flipTrace = () => setTraceOpen((v) => {
    localStorage.setItem("meridian:trace-open", v ? "0" : "1");
    return !v;
  });

  /** 加载会话历史：chat_threads 轮次 + 逐轮回放还原 final 元数据（claims/anchors 等）。 */
  async function loadConversation(convId: string) {
    let recs: TurnRecord[] = [];
    try {
      recs = await getConversationTurns(convId);
    } catch {
      return;   // 会话不存在（旧 localStorage 残留）→ 保持空态
    }
    const hydrated = await Promise.all(recs.map(async (r) => {
      const turn: Turn = { question: r.question, answer: r.answer || "", trace: [],
        retrieval: null, final: null, tl: [], thinking: "", thinkingActive: false,
        threadId: r.thread_id, failed: r.status === "failed" };
      if (r.status === "done" || r.status === "failed") {
        const tr = await fetchQaTraceOrNull(r.thread_id);
        const fin = tr?.events.find((e) => e.kind === "final")?.data as unknown as FinalEvent | undefined;
        if (fin) {
          turn.final = { ...fin, thread_id: r.thread_id, conversation_id: convId };
          turn.answer = fin.answer || r.answer || "";
          turn.failed = isFailedFinal(fin);
        }
        // 合并记账后的思维链（回放里连续 reasoning_delta 已由后端合并）
        const think = (tr?.events ?? [])
          .filter((e) => e.kind === "reasoning_delta")
          .map((e) => String((e.data as { text?: string }).text ?? "")).join("");
        if (think) turn.thinking = think;
      }
      return turn;
    }));
    setTurns(hydrated);
  }

  // 深链自动切模式只处理一次：防止与用户手动切换模式互相拉扯
  const handledPaperRef = useRef<string | null>(null);
  useEffect(() => {
    // 双模式：只列当前模式的文档；?paper= 指向另一类型文档时跟随其类型切模式（深链兼容）
    listPapers(false, docModeParam).then((ps) => {
      const fromUrl = new URLSearchParams(window.location.search).get("paper");
      const inList = !!fromUrl && ps.some((p) => p.id === fromUrl);
      if (fromUrl && !inList && handledPaperRef.current !== fromUrl) {
        handledPaperRef.current = fromUrl;
        getPaper(fromUrl).then((p) => {
          if (p?.doc_type === "general" && !isGeneral) setDocMode("general");
          else if (p?.doc_type !== "general" && isGeneral) setDocMode("scholar");
        }).catch(() => { /* 文档不存在时保持现状 */ });
      }
      setPapers(ps);
      const parsed = ps.filter((p) => p.parsed);
      const init = fromUrl && inList ? [fromUrl] : parsed.slice(0, 1).map((p) => p.id);
      setSelected(new Set(init));
    });
  }, [docModeParam, isGeneral, setDocMode]);

  // 智能跟随：仅当用户本就贴在底部附近时才跟随新内容滚动；发新问题（轮次增加）强制聚焦；
  // 用户上翻阅读时不拽回（ChatGLM 同款），由悬浮「↓」按钮一键回底。
  const atBottomRef = useRef(true);
  const prevTurnsLenRef = useRef(0);
  const [showJump, setShowJump] = useState(false);
  useEffect(() => {
    const el = msgsRef.current;
    if (!el) return;
    const grew = turns.length > prevTurnsLenRef.current;
    prevTurnsLenRef.current = turns.length;
    if (grew || atBottomRef.current) {
      el.scrollTo({ top: el.scrollHeight });
    }
  }, [turns]);

  // ---- 流式合帧：~50ms 批量应用 reasoning/delta，final/catch/finally 强制 flush ----

  const applyAcc = () => {
    const a = accRef.current;
    if (a.idx < 0) return;
    const idx = a.idx;
    // 关键：updater 被 React 延迟执行（并发调度/微任务），绝不能让它读可变 ref——
    // 否则同步执行的 a.answer = "" 会先于 updater 跑，读到的缓冲已清空 → delta 全丢，
    // 正文憋到 final 一次性吐出。先取快照、再清缓冲、updater 闭包只碰快照（纯函数）。
    const thinking = a.thinking;
    const chunk = a.answer;
    const active = a.thinkingActive;
    const ms = (a.answerStart != null && a.thinkStart != null)
      ? a.answerStart - a.thinkStart : undefined;
    a.answer = "";
    setTurns((t) => {
      const c = [...t];
      const turn = c[idx];
      if (!turn) return t;
      c[idx] = {
        ...turn,
        thinking,
        answer: turn.answer + chunk,
        thinkingActive: active,
        thinkingMs: ms ?? turn.thinkingMs,
      };
      return c;
    });
  };

  const scheduleFlush = () => {
    if (flushTimer.current != null) return;
    flushTimer.current = window.setTimeout(() => {
      flushTimer.current = null;
      applyAcc();
    }, 50);
  };

  const flushNow = () => {
    if (flushTimer.current != null) {
      window.clearTimeout(flushTimer.current);
      flushTimer.current = null;
    }
    applyAcc();
  };

  /** 正文平滑渲染：方舟网关对短/固定格式回答会把 content 段憋完一次性推送（实测 40 帧
   * 挤在 1ms 内到达，长回答则逐帧流式）——逐帧直渲会表现为「一次性吞吐」。delta 进平滑
   * 队列，按真实时间（dt）自适应出队：积压约 1.5 秒排完。时间驱动而非帧驱动——后台标签
   * rAF 被节流到 1fps 时 dt 变大、单帧多出队，排空节奏不变。正常逐 token 流式时队列近
   * 空、逐字透传零感知。ChatGLM 前端同款平滑层。 */
  const drainSmooth = () => {
    const s = smoothRef.current;
    const a = accRef.current;
    if (a.idx < 0 || !s.pending) {
      s.raf = null;
      return;
    }
    const now = performance.now();
    const dt = Math.min(Math.max(now - s.last, 8), 500);   // 首帧/长停顿钳制，防一次吐全量
    s.last = now;
    const chunk = Math.max(1, Math.ceil((s.pending.length / 1500) * dt));
    a.answer += s.pending.slice(0, chunk);
    s.pending = s.pending.slice(chunk);
    applyAcc();
    s.raf = requestAnimationFrame(drainSmooth);
  };

  const startSmooth = () => {
    if (smoothRef.current.raf == null) {
      smoothRef.current.last = performance.now();
      smoothRef.current.raf = requestAnimationFrame(drainSmooth);
    }
  };

  const stopSmooth = () => {
    if (smoothRef.current.raf != null) {
      cancelAnimationFrame(smoothRef.current.raf);
      smoothRef.current.raf = null;
    }
    smoothRef.current.pending = "";
  };

  /** 会话标识：当前模式的会话，无则生成并持久化（「新对话」首次提问时落库）。 */
  const conversationId = () => {
    if (activeConvId) return activeConvId;
    const id = uuid();   // 非 secure 上下文（HTTP 部署）下 crypto.randomUUID 不存在
    setActiveConvId(id);
    localStorage.setItem(convKeyFor(docModeParam), id);
    return id;
  };

  const flipThink = () => {
    setThinkEnabled((v) => {
      localStorage.setItem(THINK_KEY, v ? "0" : "1");
      return !v;
    });
  };

  const ask = async (override?: string) => {
    const q = (typeof override === "string" ? override : question).trim();
    if (!q || busy) return;
    setQuestion("");
    if (taRef.current) taRef.current.style.height = "auto";
    setShowScope(false);
    setBusy(true);
    const t0 = performance.now();
    stopSmooth();
    accRef.current = emptyAcc(turns.length);
    const turn: Turn = { question: q, answer: "", thinking: "", thinkingActive: false,
      trace: [], retrieval: null, final: null, tl: [] };
    setTurns((t) => [...t, turn]);
    island.emit({ key: "qa", kind: "progress", title: "溯源问答执行中",
      detail: "Router → Planner → Executor → Verifier → Responder", indeterminate: true });
    try {
      await streamChat(q, Array.from(selected), {
        guardrail: (d) => setCapHint(String(d.message ?? "")),
        meta: (d) => setTurns((t) => {
          const c = [...t];
          const i = c.length - 1;
          if (c[i]) c[i] = { ...c[i], threadId: d.thread_id };
          return c;
        }),
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
        reasoningDelta: (text) => {
          const a = accRef.current;
          if (a.thinkStart == null) a.thinkStart = performance.now();
          a.thinking += text;
          a.thinkingActive = true;
          scheduleFlush();
        },
        delta: (text) => {
          const a = accRef.current;
          if (a.answerStart == null) {
            a.answerStart = performance.now();
            a.thinkingActive = false;   // 正文首字 → 自动收起思考（ThinkingBlock 联动）
          }
          smoothRef.current.pending += text;   // 进平滑队列，rAF 逐帧渲染
          startSmooth();
        },
        final: (d) => {
          flushNow();
          setTurns((t) => {
            const c = [...t];
            const i = c.length - 1;
            const cur = c[i]?.answer ?? "";
            // 正文对齐（纯函数，updater 内完成）：
            // a) 流式无内容（抽取式/拒答直达）→ 用 final 全文；
            // b) final 以流式为前缀（如拒答轮 final 追加「最接近证据块」）→ 用 final，
            //    React DOM diff 下前缀不变、仅追加尾块，视觉自然；
            // c) 校验改写且非前缀（仅剔除无效引用标记）→ 保留已渲染流式文本。
            const answer = !cur ? d.answer
              : d.answer.startsWith(cur) ? d.answer
              : cur;
            c[i] = {
              ...c[i],
              answer,
              thinkingActive: false,   // final 到达 → 思考必然结束（无 delta 的拒答/抽取式轮也正确收起）
              trace: d.trace?.length ? d.trace : c[i].trace,
              final: d,
              threadId: d.thread_id ?? c[i].threadId,
              failed: isFailedFinal(d),
            };
            const secs = Math.round((performance.now() - t0) / 1000);
            island.emit(isFailedFinal(d)
              ? { key: "qa", kind: "info", title: "图执行中断", detail: "断点已保存,可从失败节点恢复" }
              : { key: "qa", kind: "success", title: `问答完成 · 用时 ${secs}s`,
                detail: `论断 ${d.claims?.length ?? 0} 条已逐条校验` });
            return c;
          });
        },
      }, conversationId(), agent, docModeParam, thinkEnabled);
    } catch (e) {
      flushNow();
      island.emit({ key: "qa", kind: "error", title: "问答请求失败",
        detail: e instanceof Error ? e.message : String(e) });
      setTurns((t) => {
        const c = [...t];
        c[c.length - 1] = {
          ...c[c.length - 1],
          answer: c[c.length - 1].answer + `\n[请求失败：${e instanceof Error ? e.message : e}]`,
        };
        return c;
      });
    } finally {
      flushNow();
      // 注意：不停平滑、不重置 accRef——final 后队列仍在排空（idx 保持有效），
      // 下次 ask/resume 启动时才 stopSmooth() 清理
      setBusy(false);
      refreshConversations();
      window.dispatchEvent(new CustomEvent("meridian:conv-refresh"));   // 全局侧栏「最近对话」同步
    }
  };

  /** 继续执行：从最近 checkpoint 恢复失败线程，按索引更新原 Turn（不追加）。 */
  const resumeTurn = async (ti: number) => {
    const turn = turns[ti];
    if (!turn?.threadId || busy) return;
    setBusy(true);
    stopSmooth();
    accRef.current = emptyAcc(ti);
    setTurns((t) => {   // 先清空该轮，防恢复重放 delta 造成文本串接
      const c = [...t];
      c[ti] = { ...c[ti], answer: "", thinking: "", thinkingActive: false, thinkingMs: undefined,
        trace: [], retrieval: null, final: null, tl: [] };
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
        reasoningDelta: (text) => {
          const a = accRef.current;
          if (a.thinkStart == null) a.thinkStart = performance.now();
          a.thinking += text;
          a.thinkingActive = true;
          scheduleFlush();
        },
        delta: (text) => {
          const a = accRef.current;
          if (a.answerStart == null) {
            a.answerStart = performance.now();
            a.thinkingActive = false;
          }
          smoothRef.current.pending += text;
          startSmooth();
        },
        final: (d) => {
          flushNow();
          setTurns((t) => {
            const c = [...t];
            if (c[ti]) {
              const cur = c[ti].answer;
              const answer = !cur ? d.answer
                : d.answer.startsWith(cur) ? d.answer
                : cur;
              c[ti] = {
                ...c[ti],
                answer,
                thinkingActive: false,   // 同 ask：final 即思考结束
                trace: d.trace?.length ? d.trace : c[ti].trace,
                final: d,
                threadId: d.thread_id ?? c[ti].threadId,
                failed: isFailedFinal(d),
              };
            }
            return c;
          });
        },
      });
    } catch (e) {
      flushNow();
      setTurns((t) => {
        const c = [...t];
        if (c[ti]) c[ti] = {
          ...c[ti],
          answer: c[ti].answer + `\n[恢复请求失败：${e instanceof Error ? e.message : e}]`,
        };
        return c;
      });
    } finally {
      flushNow();
      setBusy(false);
      refreshConversations();
    }
  };

  /** 停止生成（ChatGLM ■）：后端协作中断 → responder 以已生成部分收尾（guardrail=user_stopped），
   * final/done 正常到达，流自然结束——无需前端断连，已显示内容照常完成逐论断校验。 */
  const stopGeneration = () => {
    const tid = turns[turns.length - 1]?.threadId;
    if (!tid) return;
    stopChat(tid).catch(() => {
      island.emit({ key: "qa", kind: "error", title: "停止失败", detail: "线程不存在或已结束" });
    });
  };

  // ---- 会话管理 ----

  const newConversation = () => {
    if (busy) return;
    setActiveConvId("");
    localStorage.removeItem(convKeyFor(docModeParam));
    setTurns([]);
    setQuestion("");
  };

  const openConversation = (convId: string) => {
    if (busy || !convId || convId === activeConvId) return;
    setActiveConvId(convId);
    localStorage.setItem(convKeyFor(docModeParam), convId);
    setTurns([]);
    void loadConversation(convId);
  };

  /** 重新生成（ChatGLM ↻）：移除该轮后以原问题重问（后端自然产生新 thread）。 */
  const regenerate = (ti: number) => {
    if (busy) return;
    const q = turns[ti]?.question;
    if (!q) return;
    setTurns((t) => t.filter((_, i) => i !== ti));
    void ask(q);
  };

  const copyAnswer = async (turn: Turn) => {
    const text = turn.final?.answer ?? turn.answer;
    const ok = await copyText(text);   // HTTP 部署下自动降级 execCommand
    island.emit(ok
      ? { key: "qa", kind: "success", title: "已复制到剪贴板" }
      : { key: "qa", kind: "error", title: "复制失败", detail: "剪贴板不可用" });
  };

  const lastTurn = turns[turns.length - 1];
  const scopeTitle = selected.size === 0
    ? (isGeneral ? "未选择文档" : "未选择论文")
    : selected.size === 1
      ? papers.find((p) => selected.has(p.id))?.title.slice(0, 24) ?? (isGeneral ? "1 份文档" : "1 篇论文")
      : isGeneral ? `${selected.size} 份文档` : `${selected.size} 篇论文`;
  const convTitle = conversations.find((c) => c.id === activeConvId)?.title
    ?? (lastTurn ? lastTurn.question.slice(0, 30) : "溯源问答");

  return (
    <div className="chat-shell">
      <div className="chat-main">
        <div className="topbar" style={{ position: "relative" }}>
          <span className="topbar-title">溯源问答</span>
          <span className="topbar-title-center" title={convTitle}>{convTitle}</span>
          <span style={{ flex: 1 }} />
          <button className={`topbar-icon${traceOpen ? " on" : ""}`} data-testid="trace-toggle"
            onClick={flipTrace} title="Agent Trace 时间线（执行步骤实时点亮）">
            <Activity size={15} />
          </button>
        </div>

        <div className="chat-body">
          <div className="chat-col">
            <div className="msgs" ref={msgsRef}
              onScroll={() => {
                const el = msgsRef.current;
                if (!el) return;
                const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
                atBottomRef.current = atBottom;
                setShowJump(!atBottom && el.scrollHeight > el.clientHeight + 60);
              }}>
              <div className="msgs-inner">
                {capHint && (
                  <div className="chat-cap-hint">
                    <span className="chip warn" title="选文/截断提示">{capHint}</span>
                  </div>
                )}
                {turns.length === 0 && (
                  <div className="chat-hero" data-testid="chat-empty">
                    <p className="chat-hero-title">
                      <Spark size={16} /> 你好，我是 Meridian 科研助手
                    </p>
                    <div className="chat-demo-cards">
                      {demoQuestions.map((q, i) => (
                        <button key={i} className="chat-demo-card" data-testid={`demo-q-${i}`}
                          onClick={() => { setQuestion(q); ask(q); }}>
                          {q}
                        </button>
                      ))}
                    </div>
                    <p className="chat-hero-sub">点击即提问 · 全程可溯源 · 右侧 Trace 实时点亮</p>
                  </div>
                )}
                {turns.map((turn, ti) => (
                  <div key={ti}>
                    <div className="msg user"><span>{turn.question}</span></div>
                    <div className="msg bot">
                      <div className="bot-head">
                        <span className="bot-avatar" aria-hidden>M</span>
                        <span className="bot-name">Meridian</span>
                        {turn.final?.agent && (
                          <span className="badge ok">{AGENT_LABEL[turn.final.agent] ?? turn.final.agent}</span>
                        )}
                      </div>
                      {turn.trace.length > 0 && <Pipeline cards={turn.trace} done={!!turn.final} />}
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
                      <ThinkingBlock text={turn.thinking} active={turn.thinkingActive}
                        ms={turn.thinkingMs} />
                      {turn.answer && (() => {
                        const { main, tail } = splitUnanswered(turn.answer);
                        const streaming = !turn.final
                          || (ti === turns.length - 1 && smoothRef.current.pending.length > 0);
                        const anchors = turn.final?.anchors;
                        const nMissing = tail ? (tail.match(/未明确说明/g) || []).length : 0;
                        return (
                        <div className="answer-card">
                          <MarkdownWithCitations
                            text={main}
                            anchors={anchors}
                            onEvidence={showEvidence}
                            streaming={streaming} />
                          {tail && (streaming ? (
                            <MarkdownWithCitations
                              text={tail}
                              anchors={anchors}
                              onEvidence={showEvidence}
                              streaming />
                          ) : (
                            <details style={{ marginTop: "var(--space-2)" }}>
                              <summary className="chip" style={{ cursor: "pointer", listStyle: "none" }}>
                                未能回答的部分（{nMissing} 条未明确说明）· 点击展开
                              </summary>
                              <div style={{ marginTop: "var(--space-2)" }}>
                                <MarkdownWithCitations
                                  text={tail}
                                  anchors={anchors}
                                  onEvidence={showEvidence} />
                              </div>
                            </details>
                          ))}
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
                        );
                      })()}
                      {turn.final && (
                        <div className="msg-actions">
                          <button className="msg-action" onClick={() => void copyAnswer(turn)}>
                            ⧉ 复制
                          </button>
                          {ti === turns.length - 1 && (
                            <button className="msg-action" disabled={busy}
                              onClick={() => regenerate(ti)}>
                              ↻ 重新生成
                            </button>
                          )}
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
              {showJump && (
                <button className="msg-jump" aria-label="回到底部" title="回到底部"
                  onClick={() => {
                    atBottomRef.current = true;
                    setShowJump(false);
                    msgsRef.current?.scrollTo({ top: msgsRef.current.scrollHeight, behavior: "smooth" });
                  }}>↓</button>
              )}
              <div className="dock">
                <div className="dock-row">
                  {busy && <span className="dock-busy-bar" aria-hidden />}
                  <textarea ref={taRef} className="dock-input" rows={1}
                    value={question}
                    onChange={(e) => {
                      setQuestion(e.target.value);
                      e.target.style.height = "auto";
                      e.target.style.height = Math.min(e.target.scrollHeight, 160) + "px";
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                        e.preventDefault();
                        ask();
                      }
                    }}
                    placeholder={busy ? "Agent 执行中…"
                      : isGeneral ? "对选中的文档提问，每句回答可溯源到原文…"
                      : "对选中的论文提问，每句回答可溯源到原文…"}
                    disabled={busy}
                  />
                </div>
                <div className="dock-tools">
                  <button className={`dock-tool${thinkEnabled ? " on" : ""}`}
                    data-testid="think-switch" aria-pressed={thinkEnabled}
                    title="深度思考：回答前先流式展示模型思维链（可随时开关，仅本轮生效）"
                    onClick={flipThink}>✦</button>
                  <button className="dock-tool" onClick={() => setShowScope((s) => !s)}
                    title={`检索范围：${scopeTitle}`}>
                    <List size={15} />
                  </button>
                  <span className="tool-label" title={scopeTitle}>
                    {selected.size > 0 ? `已选 ${selected.size} 篇` : "全库检索"}
                  </span>
                  <span style={{ flex: 1 }} />
                  <AgentDrop value={agent} options={agentOptions} onChange={setAgent} />
                  {busy ? (
                    <button className="send-btn stop" data-testid="stop-btn" onClick={stopGeneration}
                      aria-label="停止生成" title="停止生成">■</button>
                  ) : (
                    <button className="send-btn" onClick={() => ask()}
                      disabled={busy || !selected.size} aria-label="发送">↑</button>
                  )}
                </div>
                {showScope && (
                  <div className="scope-pop">
                    <p className="kv" style={{ marginBottom: "var(--space-2)" }}>
                      {isGeneral ? "选择参与检索的文档" : "选择参与检索的论文"}
                    </p>
                    {papers.filter((p) => p.parsed).map((p) => (
                      <label key={p.id} style={{ display: "flex", gap: "var(--space-2)",
                        alignItems: "center", padding: "4px 0", fontSize: "var(--text-sm)",
                        cursor: "pointer" }}>
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
                        <span style={{ overflow: "hidden", textOverflow: "ellipsis",
                          whiteSpace: "nowrap" }}>
                          {p.title.slice(0, 30)}
                        </span>
                      </label>
                    ))}
                    {papers.filter((p) => p.parsed).length === 0 && (
                      <p className="kv">{isGeneral ? "文档库为空，先去上传 PDF。" : "文献库为空，先去上传 PDF。"}</p>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Trace 右侧抽屉（topbar 图标唤出，默认收起；滑入不遮挡消息交互） */}
        <div className={`trace-drawer${traceOpen ? " open" : ""}`} data-testid="trace-drawer">
          <div className="trace-drawer-head">
            <b>Agent Trace</b>
            <span style={{ flex: 1 }} />
            <button className="topbar-icon" onClick={flipTrace} title="收起">✕</button>
          </div>
          <div className="trace-drawer-body">
            <TracePanel tl={lastTurn?.tl ?? []} busy={busy} final={lastTurn?.final ?? null} />
          </div>
        </div>
      </div>
      {evidence && <EvidenceModal loc={evidence} onClose={() => setEvidence(null)} />}
    </div>
  );
}

/** Agent 选择下拉（ChatGLM「模型选择 ˅」同款位：dock 工具行右端，向上弹菜单）。 */
function AgentDrop({ value, options, onChange }: {
  value: string;
  options: { value: string; label: string; ready: boolean }[];
  onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const sel = options.find((o) => o.value === value) ?? options[0];
  return (
    <div className="agent-drop">
      {open && (
        <div style={{ position: "fixed", inset: 0, zIndex: 39 }}
          onClick={() => setOpen(false)} aria-hidden />
      )}
      <button className="agent-drop-btn" onClick={() => setOpen((o) => !o)}
        aria-expanded={open} title="选择承接本次提问的 Agent">
        {sel.label}
        <ChevronRight size={11} style={{ transform: "rotate(90deg)" }} />
      </button>
      {open && (
        <div className="agent-drop-menu" role="menu">
          {options.map((o) => (
            <button key={o.value} role="menuitem"
              className={`agent-drop-item${o.value === value ? " sel" : ""}`}
              onClick={() => { onChange(o.value); setOpen(false); }}>
              {o.label}
              <span className="drop-note">{o.ready ? "" : "降级执行"}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** 节点即卡片：内嵌可展开执行管线（官方 Graph Execution Cards 模式的自有协议实现）。
 *  v2:无缝展开(grid-rows 过渡)+ 步骤卡 stagger 入场 + 完成时末卡一次性闪光。 */
function Pipeline({ cards, done }: { cards: TraceCard[]; done?: boolean }) {
  const [open, setOpen] = useState(false);
  const retrieveCount = cards.filter((c) => c.node === "retrieve_task").length;
  const chips: string[] = [];
  for (const c of cards) {
    if (c.node === "retrieve_task") {
      if (!chips.includes("__rt__")) chips.push("__rt__");
    } else chips.push(c.title);
  }
  return (
    <div className={`pipeline acc${open ? " open" : ""}`} onClick={() => setOpen((o) => !o)}>
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
      <div className="acc-body"><div className="acc-inner">
        <div className="pipe-cards">
          {cards.map((c, i) => (
            <div key={i}
              className={`pipe-card card-in${done && i === cards.length - 1 ? " flash-once" : ""}`}
              style={{ ["--i" as string]: i }}>
              <b>{c.title}</b>
              <span className="kv"> · {c.summary}</span>
              {Object.keys(c.payload).length > 0 && (
                <pre className="pipe-payload">{JSON.stringify(c.payload, null, 1).slice(0, 420)}</pre>
              )}
            </div>
          ))}
        </div>
      </div></div>
    </div>
  );
}

function FinalMeta({ final }: { final: FinalEvent }) {
  const guards = final.guardrails
    .map((g) => [g, guardLabel(g)] as const)
    .filter(([, label]) => label);
  const hasMeta = final.mode !== "generative" || guards.length > 0
    || final.agent || (final.handoffs?.length ?? 0) > 0
    || (final.partial_missing?.length ?? 0) > 0;
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
      {(final.partial_missing?.length ?? 0) > 0 && (
        <span className="badge warn" title={`以下子问题文档未明确说明：\n${final.partial_missing!.join("\n")}`}>
          ◐ 部分回答 · {final.partial_missing!.length} 个子问题未在文档中找到明确表述
        </span>
      )}
      {guards.map(([g, label]) => <span key={g} className="badge warn">⚠ {label}</span>)}
    </p>
  );
}

/** Writer Agent 写作大纲（artifact 下发，正文按同一大纲流式生成）。 */
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

/** 逐论断溯源：三态徽章（已验证/需核对/未验证），support 可点击跳原文。
 *  整块默认折叠（ChatGLM 式 pill 头 + 校验统计摘要），用户点击展开按条查看。 */
function Claims({ claims, anchors, hits, onEvidence }: {
  claims: Claim[];
  anchors?: Record<string, Anchor>;
  hits: Hit[];
  onEvidence?: (eid?: string, anchor?: Anchor, quote?: string) => void;
}) {
  const [open, setOpen] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  if (!claims.length) return null;
  const supported = claims.filter((c) => c.status === "supported").length;
  const needs = claims.filter((c) => c.status === "needs_check").length;
  const absent = claims.filter((c) => c.status === "absent").length;
  const unverified = claims.length - supported - needs - absent;
  return (
    <div className={`claims-block acc${expanded ? " open" : ""}`}>
      <button className="think-head claims-head" data-testid="claims-toggle"
        onClick={() => setExpanded((v) => !v)}
        title="L1-L4 校验 · 展开逐条查看论断与出处">
        <Stamp kind="verified" size="xs" />
        <span>
          逐论断溯源 · {claims.length} 条
          {supported > 0 && `（已验证 ${supported}` }
          {needs > 0 && `${supported > 0 ? " · " : "（"}需核对 ${needs}` }
          {absent > 0 && `${supported > 0 || needs > 0 ? " · " : "（"}未涉及 ${absent}` }
          {unverified > 0 && `${supported > 0 || needs > 0 || absent > 0 ? " · " : "（"}未验证 ${unverified}` }
          {supported > 0 || needs > 0 || absent > 0 || unverified > 0 ? "）" : ""}
        </span>
        <span className="acc-caret think-caret" aria-hidden />
      </button>
      <div className="acc-body"><div className="acc-inner">
        <div style={{ paddingTop: "var(--space-2)" }}>
          {claims.map((c, i) => (
        <div key={i}
          className={`claim ${c.status === "unsupported" ? "unverified"
            : c.status === "absent" ? "absent"
            : c.status === "needs_check" ? "needs-check" : ""}`}
          onClick={() => setOpen(open === i ? null : i)}>
          <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "baseline",
            flexWrap: "wrap" }}>
            {c.status === "supported" && <Stamp kind="verified" size="xs" />}
            {c.status === "supported"
              ? <span className="badge ok">已验证</span>
              : c.status === "needs_check"
                ? <span className="badge warn" title="数值/引文未在证据中逐字命中，请人工核对">需核对</span>
                : c.status === "absent"
                  ? <span className="badge" title="系统如实报告「未检索到/未涉及」——诚实负向发现，非编造">未涉及</span>
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
          {open === i && c.status === "absent" && (
            <div className="evidence"><i>系统如实报告该论文未涉及此维度（诚实负向发现，非编造）。</i></div>
          )}
          {open === i && c.status === "unsupported" && (
            <div className="evidence"><i>检索结果中未找到支撑此论断的原文，请人工核对。</i></div>
          )}
            </div>
          ))}
        </div>
      </div></div>
    </div>
  );
}
