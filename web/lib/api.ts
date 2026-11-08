export const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8100";

export interface Paper {
  id: string;
  title: string;
  arxiv_id: string;
  page_count: number;
  supersedes: string;
  parsed: boolean;
  docir_path: string;
  created_at: number;
  references_count?: number;
  dedup_alert?: {
    duplicate_of: string;
    title: string;
    existing_version: string;
    reason: string;
  };
}

export interface Hit {
  chunk_id: string;
  paper_id: string;
  page: number;            // 1 基人读页码（后端 SSE 统一输出）
  section_path: string;
  score: number;
  rrf?: number;
  rerank?: number;         // bge-reranker 精排分（配置了 rerank key 时存在）
  kind?: string;           // detail | summary | table
  aspect?: string;
  text: string;
}

export interface Support {
  eid?: string;            // 证据编号（E1..En）
  page: number;            // 1 基人读页码
  section_path: string;
  chunk_id: string;
  quote: string;
  match_score?: number;
}

export interface Claim {
  text: string;
  status: "supported" | "needs_check" | "unsupported";
  support: Support[];
  quote_status?: "verified" | "unverified";   // L2 引文子串校验结果
  quote_coverage?: number;
  unverified_numbers?: string[];              // L3：未在证据中命中的数值/型号
  semantic_status?: "partial";                // L4：部分支撑
}

/** final.anchors：E# → 块级溯源锚点（页码 1 基，bbox 归一化 0-1）。 */
export interface Anchor {
  page: number;
  section_path: string;
  paper_id: string;
  paper_label: string;
  bbox: number[];
  chunk_id: string;
  block_ids: string[];
}

export interface TraceCard {
  node: string;
  title: string;
  summary: string;
  payload: Record<string, unknown>;
}

export interface FinalEvent {
  answer: string;
  claims: Claim[];
  mode: "generative" | "extractive" | "refusal" | "none";
  guardrails: string[];
  trace?: TraceCard[];
  elapsed?: number;
  thread_id?: string;      // 断点续跑：本轮线程 id（"{conversation_id}:{turn}"）
  conversation_id?: string;
  resumable?: boolean;     // 失败收尾时为 true，可 POST /api/chat/{thread_id}/resume 恢复
  // 多智能体（协议向后兼容新增键）
  agent?: string;          // 实际承接的 Agent（reader/compare/…）
  handoffs?: Handoff[];    // Agent 交接日志
  anchors?: Record<string, Anchor>;  // E# → 块级锚点（引用点击跳 PDF + bbox 高亮）
  compare_table?: CompareTable;
  graph_data?: GraphData;
  outline?: Outline;
  chart_spec?: ChartSpec;
}

export interface GraphNode {
  paper_id: string;
  title: string;
  pagerank: number;
  in_degree: number;
  out_degree: number;
  betweenness: number;
}

export interface GraphData {
  nodes: { id: string; title: string; arxiv_id: string }[];
  edges: { source: string; target: string }[];
  external_count?: number;
  metrics?: { backend: string; nodes: GraphNode[]; edge_count: number };
  titles?: Record<string, string>;
}

export interface Outline {
  title: string;
  sections: string[];
}

export interface ChartSpec {
  type: "bar" | "line" | "pie";
  title: string;
  x_label: string;
  y_label: string;
  data: { label: string; value: number }[];
  provenance?: { paper_id?: string; page?: number; table_id?: string; skipped_rows?: number };
}

export interface Handoff {
  from: string;
  to: string;
  note: string;
}

export interface CompareCell {
  paper_id: string;
  found: boolean;
  chunk_id?: string;
  page?: number;
  section_path?: string;
  quote?: string;
  score?: number;
}

export interface CompareRow {
  dimension: string;
  cells: CompareCell[];
}

export interface CompareTable {
  papers: string[];
  titles?: Record<string, string>;
  rows: CompareRow[];
}

export interface ArtifactEvent {
  kind: "compare_table";
  table: CompareTable;
}

export interface Job {
  id: string;
  status: "queued" | "running" | "done" | "error";
  progress: number;
  message: string;
}

export interface Health {
  status: string;
  llm: { configured: boolean; available: boolean; provider: string; model: string };
  dense: boolean;
  papers: number;
}

export async function fetchHealth(): Promise<Health> {
  const r = await fetch(`${API}/api/health`, { cache: "no-store" });
  return r.json();
}

export async function listPapers(): Promise<Paper[]> {
  const r = await fetch(`${API}/api/papers`, { cache: "no-store" });
  return (await r.json()).papers;
}

export async function getPaper(id: string): Promise<Paper> {
  const r = await fetch(`${API}/api/papers/${id}`, { cache: "no-store" });
  return r.json();
}

export interface SSEHandlers {
  trace?: (d: TraceCard) => void;
  retrieval?: (d: { channels: string[]; hits: Hit[] }) => void;
  delta?: (t: string) => void;
  final?: (d: FinalEvent) => void;
  done?: (d: Record<string, unknown>) => void;
  // 协议 v1.1：Trace 时间线结构化事件（右侧 TracePanel 数据源）
  plan?: (d: PlanEvent) => void;
  toolCall?: (d: ToolCallEvent) => void;
  toolResult?: (d: ToolResultEvent) => void;
  evidence?: (d: EvidenceEvent) => void;
  verifier?: (d: VerifierEvent) => void;
  // 多智能体：结构化产物（compare_table 等）
  artifact?: (d: ArtifactEvent) => void;
}

/** SSE 帧解析（EventSource 不支持 POST，手写 fetch 流解析；streamChat/resumeChat 共用）。 */
async function consumeSSE(resp: Response, on: SSEHandlers): Promise<void> {
  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let event = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ") && event) {
          const data = JSON.parse(line.slice(6));
          if (event === "trace") on.trace?.(data);
          else if (event === "retrieval") on.retrieval?.(data);
          else if (event === "delta") on.delta?.(data.text);
          else if (event === "final") on.final?.(data);
          else if (event === "plan") on.plan?.(data);
          else if (event === "tool_call") on.toolCall?.(data);
          else if (event === "tool_result") on.toolResult?.(data);
          else if (event === "evidence") on.evidence?.(data);
          else if (event === "verifier") on.verifier?.(data);
          else if (event === "artifact") on.artifact?.(data);
          else if (event === "done") on.done?.(data);
        }
      }
      event = "";
    }
  }
}

/** POST SSE 流式问答。conversationId 可选：断点续跑按会话归属，缺省由后端生成。
 * agent 为 UI 强制指定的 Agent（auto/reader/…），最高路由优先级，缺省 auto。 */
export async function streamChat(
  question: string,
  paperIds: string[],
  on: SSEHandlers,
  conversationId?: string,
  agent: string = "auto"
): Promise<void> {
  const resp = await fetch(`${API}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question,
      paper_ids: paperIds,
      conversation_id: conversationId ?? "",
      agent,
    }),
  });
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
  await consumeSSE(resp, on);
}

/** 断点续跑：从最近 checkpoint 继续失败的线程，SSE 事件协议与 streamChat 一致。 */
export async function resumeChat(threadId: string, on: SSEHandlers): Promise<void> {
  const resp = await fetch(
    `${API}/api/chat/${encodeURIComponent(threadId)}/resume`, { method: "POST" });
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
  await consumeSSE(resp, on);
}

// ---------------- 协议 v1.1：Trace 时间线结构化事件 ----------------

export interface PlanStep {
  step_id: string;   // "s{round}-{序号}"
  title: string;     // 检索面（人读标题）
  aspect: string;
  paper_id: string;
  query: string;
}

export interface PlanEvent {
  round: number;
  mode: string;
  steps: PlanStep[];
}

export interface ToolCallEvent {
  step_id?: string;    // 计划内步骤 id（"s1-1"）；计划外工具（get_meta/align）缺省
  tool: string;
  agent?: string;      // 发起调用的 Agent（注册表工具事件携带）
  args: Record<string, unknown>;
}

export interface ToolResultEvent {
  step_id?: string;
  tool?: string;
  agent?: string;
  ok: boolean;
  degraded?: string;   // "bm25_only"：混合检索故障降级
  summary: string;
  refs: string[];      // 命中 chunk_id 列表
  elapsed_ms: number;
  error?: string;      // 工具失败详情（白名单拒绝/业务异常）
  payload?: Record<string, unknown>;
}

export interface EvidenceEvent {
  channels: string[];
  bm25_count: number;
  dense_count: number;
  hits: Hit[];
}

export interface VerifierEvent {
  verdict: "pass" | "insufficient";
  missing: string[];
  evidence_count: number;
  action: "respond" | "recover";
}

/** Trace 时间线事件（有序流）：kind=trace 时 data 为节点卡片，其余为结构化事件。 */
export type TLEvent = {
  kind: "plan" | "tool_call" | "tool_result" | "evidence" | "verifier" | "trace";
  data: PlanEvent | ToolCallEvent | ToolResultEvent | EvidenceEvent
      | VerifierEvent | TraceCard;
};

export interface QaTrace {
  trace_id: string;
  question: string;
  status: string;
  events: TLEvent[];
}

/** 回放：GET /api/qa/{trace_id}（trace_id = thread_id，含断点续跑并入的完整事件流）。 */
export async function fetchQaTrace(traceId: string): Promise<QaTrace> {
  const r = await fetch(`${API}/api/qa/${encodeURIComponent(traceId)}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// ---------------- 4.7 翻译与阅读体验 ----------------

export interface TranslationEntry { text: string; format_ok: boolean }
export interface TermHit { term: string; translation: string }
export interface SelectionResult {
  translation: string; term_hit?: TermHit | null; ok: boolean; reason?: string;
}
export interface TermEntry { id: number; term: string; translation: string; source: string }
/** DocIR.references 条目（[n] 双向跳转数据源）。 */
export interface RefEntry { index: number; raw?: string; title?: string; arxiv_id?: string; doi?: string }

export interface TranslateHandlers {
  tDelta?: (d: { block_id: string; text: string; format_ok: boolean; done: number; total: number }) => void;
  final?: (d: { done: number; total: number; cached: number; failed: number; format_bad: number; stopped?: boolean }) => void;
  error?: (d: { message: string }) => void;
  done?: () => void;
}

/** POST SSE 流式翻译（block 级增量 t_delta → final 统计 → done）。 */
export async function streamTranslate(
  paperId: string,
  body: { lang?: string; page?: number; section?: string; block_ids?: string[] },
  on: TranslateHandlers
): Promise<void> {
  const resp = await fetch(`${API}/api/papers/${encodeURIComponent(paperId)}/translate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let event = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ") && event) {
          const data = JSON.parse(line.slice(6));
          if (event === "t_delta") on.tDelta?.(data);
          else if (event === "final") on.final?.(data);
          else if (event === "error") on.error?.(data);
          else if (event === "done") on.done?.();
        }
      }
      event = "";
    }
  }
}

/** 拉取已缓存译文（可中断续译：重进页面秒出已完成块）。 */
export async function fetchTranslation(
  paperId: string, lang = "zh"
): Promise<Record<string, TranslationEntry>> {
  const r = await fetch(
    `${API}/api/papers/${encodeURIComponent(paperId)}/translation?lang=${lang}`,
    { cache: "no-store" });
  if (!r.ok) return {};
  return (await r.json()).blocks ?? {};
}

/** 划词翻译：上下文感知短译 + 术语命中。 */
export async function translateSelection(text: string, context = ""): Promise<SelectionResult> {
  const r = await fetch(`${API}/api/translate/selection`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, context }),
  });
  if (!r.ok) return { translation: "", ok: false };
  return r.json();
}

export async function fetchTerminology(): Promise<TermEntry[]> {
  const r = await fetch(`${API}/api/terminology`, { cache: "no-store" });
  if (!r.ok) return [];
  return (await r.json()).terms ?? [];
}

export async function addTerm(term: string, translation: string): Promise<TermEntry[]> {
  const r = await fetch(`${API}/api/terminology`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ term, translation }),
  });
  return r.ok ? (await r.json()).terms ?? [] : [];
}

// ---------------- 4.4 Discover 闭环 ----------------

export interface ArxivEntry {
  arxiv_id: string; title: string; authors: string[]; abstract: string;
  pdf_url: string; published: string;
}

export interface ReproRepo {
  repo: string; stars: number; pushed_at: string; license: string;
  url: string; repro_score?: number;
}

export interface DiscoverHandlers {
  status?: (d: { stage: string; note: string; arxiv_id?: string; paper_id?: string; query?: string }) => void;
  results?: (d: { query: string; entries: ArxivEntry[]; source: string }) => void;
  final?: (d: Record<string, unknown> & { note?: string }) => void;
  error?: (d: { message: string }) => void;
  done?: () => void;
}

/** POST SSE：搜索（auto=true 时自动下载 top1 → 解析 → kb_search 证据）。 */
export async function streamDiscover(
  query: string, on: DiscoverHandlers, auto = false, maxResults = 6
): Promise<void> {
  const resp = await fetch(`${API}/api/discover`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, auto, max_results: maxResults }),
  });
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let event = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ") && event) {
          const data = JSON.parse(line.slice(6));
          if (event === "status") on.status?.(data);
          else if (event === "discover_results") on.results?.(data);
          else if (event === "final") on.final?.(data);
          else if (event === "error") on.error?.(data);
          else if (event === "done") on.done?.();
        }
      }
      event = "";
    }
  }
}

/** 勾选下载：下载 → 入库 → 等解析。 */
export async function discoverDownload(arxivId: string): Promise<{
  paper_id: string; job_id: string | null; dedup?: string;
  existing_title?: string; parse?: { status: string; message?: string };
}> {
  const r = await fetch(`${API}/api/discover/download`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ arxiv_id: arxivId }),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

/** 复现性查分（github_repro 工具）。 */
export async function fetchRepro(title: string): Promise<{ ok: boolean; repos: ReproRepo[]; source?: string }> {
  const r = await fetch(
    `${API}/api/discover/repro?title=${encodeURIComponent(title)}`, { cache: "no-store" });
  if (!r.ok) return { ok: false, repos: [] };
  return r.json();
}

// ---------------- 4.6 图谱挖掘与学术洞察 ----------------

export interface GraphNode {
  paper_id: string; title: string; pagerank: number; in_degree: number; out_degree: number;
  betweenness: number; year: number | null; community: number; role: string;
  time_rank?: number | null;
}
export interface GraphEdgeW {
  source: string; target: string; weight: number;
  examples?: { sentence: string; section: string; cls: string; w: number }[];
}
export interface GraphCommunity { id: number; size: number; top_paper: string | null }
export interface GraphInsights {
  cornerstone: GraphNode[]; peripheral: GraphNode[];
  class_stat?: Record<string, number>; mention_count?: number;
}
export interface FullGraph {
  backend: string; edge_count: number; external_count: number;
  nodes: GraphNode[]; edges: GraphEdgeW[]; communities: GraphCommunity[];
  modularity: number | null; insights: GraphInsights;
}

export async function fetchGraph(paperIds?: string[]): Promise<FullGraph> {
  const q = paperIds?.length ? `?paper_ids=${paperIds.join(",")}` : "";
  const r = await fetch(`${API}/api/graph${q}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

export interface SummaryHandlers {
  status?: (d: { stage: string; note: string }) => void;
  delta?: (d: { text: string }) => void;
  final?: (d: { summary: string; claims: Record<string, unknown>[]; anchors: Record<string, unknown>; guardrails: string[] }) => void;
  error?: (d: { message: string }) => void;
  done?: () => void;
}

/** 社区综述 SSE（每句带 [E#]，经 ClaimTrace 校验）。 */
export async function streamCommunitySummary(
  paperIds: string[], communityId: number, on: SummaryHandlers
): Promise<void> {
  const resp = await fetch(`${API}/api/graph/summary`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paper_ids: paperIds, community_id: communityId }),
  });
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let event = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ") && event) {
          const data = JSON.parse(line.slice(6));
          if (event === "status") on.status?.(data);
          else if (event === "delta") on.delta?.(data);
          else if (event === "final") on.final?.(data);
          else if (event === "error") on.error?.(data);
          else if (event === "done") on.done?.();
        }
      }
      event = "";
    }
  }
}

export interface IdeaItem {
  id: number; idea_text: string; source_paper: string;
  anchor: { page: number | null; bbox: number[]; section_path: string };
  successors: string[]; cited_count: number;
  status: string; reason: string;
  evidence: { paper_id: string; page: number; text: string }[];
}
export interface IdeasHandlers {
  status?: (d: { stage: string; note: string }) => void;
  final?: (d: { solved: IdeaItem[]; open: IdeaItem[]; total: number }) => void;
  error?: (d: { message: string }) => void;
  done?: () => void;
}

/** Future Work 去重过滤 SSE。 */
export async function streamIdeas(paperIds: string[], on: IdeasHandlers): Promise<void> {
  const resp = await fetch(`${API}/api/ideas`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paper_ids: paperIds }),
  });
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let event = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ") && event) {
          const data = JSON.parse(line.slice(6));
          if (event === "status") on.status?.(data);
          else if (event === "final") on.final?.(data);
          else if (event === "error") on.error?.(data);
          else if (event === "done") on.done?.();
        }
      }
      event = "";
    }
  }
}
