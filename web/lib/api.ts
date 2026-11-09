export const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8100";

/** 去重预警（双向 pending；direction=new 为新上传侧 / existing 为库中既有侧）。 */
export interface DedupAlert {
  id: string;
  score: number;
  breakdown: Record<string, number | boolean>;
  reason: string;
  direction: "new" | "existing";
  other_paper_id: string;
  other_title: string;
  other_version: string;
  /** 旧口径别名（兼容既有断言/展示） */
  duplicate_of: string;
  title: string;
}

export interface Paper {
  id: string;
  title: string;
  arxiv_id: string;
  page_count: number;
  supersedes: string;
  superseded_by?: string;
  family_id?: string;
  status?: "active" | "archived";
  abstract?: string;
  authors?: { name: string; affiliation: string }[];
  venue?: string;
  year?: number;
  doi?: string;
  meta_source?: string;
  parsed: boolean;
  docir_path: string;
  created_at: number;
  references_count?: number;
  dedup_alert?: DedupAlert;
  dedup_alerts?: DedupAlert[];
}

export interface PaperMeta {   // DocIR meta（outline 载荷），文献信息卡数据源
  title?: string;
  authors?: { name: string; affiliation: string }[];
  abstract?: string;
  venue?: string;
  year?: number;
  doi?: string;
  arxiv_id?: string;
  meta_source?: string;
  appendix?: { start_page: number; sections: string[] };
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
  block_id?: string;       // 原句块级锚点（quote 定位回 DocIR 具体块）
  block_bbox?: number[];   // 原句块归一化 bbox（优先于 chunk 级高亮）
  quote_coverages?: number[];  // L2 逐 support 覆盖率（修订后逐项校验）
}

export interface Claim {
  text: string;
  status: "supported" | "needs_check" | "unsupported";
  support: Support[];
  quote_status?: "verified" | "unverified";   // L2 引文子串校验结果
  quote_coverage?: number;
  unverified_numbers?: string[];              // L3：未在证据中命中的数值/型号
  semantic_status?: "partial" | "unchecked";  // L4：partial=部分支撑；unchecked=语义判定未执行（诚实标注）
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
  role_reason?: string;   // 模块五 P1：角色判定解释（年份/分位/跨社区比）
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
  eid?: string;            // 格级证据编号（Evidence Pack 内的 E#，点击直通原文）
  page?: number;
  section_path?: string;
  quote?: string;
  score?: number;
}

export interface CompareRow {
  dimension: string;
  cells: CompareCell[];
  conflict?: boolean;      // 行级数值冲突启发（双侧数字集合不相交 → 双侧并列人工裁决）
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
  status: "queued" | "running" | "done" | "error" | "cancel_requested" | "cancelled";
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

export async function listPapers(includeArchived = false): Promise<Paper[]> {
  const q = includeArchived ? "?include_archived=1" : "";
  const r = await fetch(`${API}/api/papers${q}`, { cache: "no-store" });
  return (await r.json()).papers;
}

/** 族视图（union-find 版本链）：canonical=族内最新 active；versions 按版本号升序。 */
export interface FamilyVersion {
  id: string;
  title: string;
  version: string;
  arxiv_id: string;
  status: "active" | "archived";
  page_count: number;
  pending_alerts: number;
  created_at: number;
}

export interface FamilyGroup {
  family_id: string;
  canonical_id: string;
  canonical_title: string;
  canonical_version: string;
  pending_alerts: number;
  versions: FamilyVersion[];
}

export async function fetchFamilies(): Promise<FamilyGroup[]> {
  const r = await fetch(`${API}/api/families`, { cache: "no-store" });
  return (await r.json()).families;
}

export async function getPaper(id: string): Promise<Paper> {
  const r = await fetch(`${API}/api/papers/${id}`, { cache: "no-store" });
  return r.json();
}

// ---------------- 模块一：CRUD / 批量上传 / 去重决策 / DiffLens ----------------

export interface UploadResult {
  paper_id?: string;
  job_id?: string | null;
  dedup?: "exact_file" | null;
  existing_title?: string;
}

/** 单文件上传（XHR 带字节进度，onProgress 0~1）。 */
export function uploadPaperWithProgress(
  file: File, onProgress: (frac: number) => void
): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API}/api/papers`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      try {
        const data = JSON.parse(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(data.detail || `HTTP ${xhr.status}`));
      } catch {
        reject(new Error(`HTTP ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error("网络错误"));
    const fd = new FormData();
    fd.append("file", file);
    xhr.send(fd);
  });
}

export interface BatchItemResult {
  filename: string;
  paper_id?: string;
  job_id?: string | null;
  dedup?: string | null;
  existing_title?: string;
  error?: string;
}

/** 批量上传（后端逐文件独立处理，单个失败不影响其他）。 */
export async function batchUploadPapers(
  files: File[], onProgress?: (done: number, total: number, item: BatchItemResult) => void
): Promise<BatchItemResult[]> {
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  const r = await fetch(`${API}/api/papers/batch`, { method: "POST", body: fd });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  const data = await r.json();
  data.results.forEach((item: BatchItemResult, i: number) =>
    onProgress?.(i + 1, data.results.length, item));
  return data.results;
}

export async function deletePaper(id: string): Promise<{ deleted: string; title: string }> {
  const r = await fetch(`${API}/api/papers/${encodeURIComponent(id)}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

export async function renamePaper(id: string, title: string): Promise<{ id: string }> {
  const r = await fetch(`${API}/api/papers/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
}

export async function restorePaper(id: string): Promise<{ id: string; status: string }> {
  const r = await fetch(`${API}/api/papers/${encodeURIComponent(id)}/restore`, { method: "POST" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

export async function reparsePaper(id: string): Promise<{ paper_id: string; job_id: string }> {
  const r = await fetch(`${API}/api/papers/${encodeURIComponent(id)}/reparse`, { method: "POST" });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
}

export async function cancelJob(jobId: string): Promise<Job> {
  const r = await fetch(`${API}/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

export type DedupDecision = "cover" | "keep" | "coexist";

export interface DecisionResult {
  decision: DedupDecision;
  new: { id: string; title: string; status: string; supersedes: string; superseded_by: string; family_id: string };
  target: { id: string; title: string; status: string; supersedes: string; superseded_by: string; family_id: string };
  diff_available: boolean;
}

/** 去重三态决策：cover=旧归档（新接管）/ keep=新归档（保留既有）/ coexist=并存。 */
export async function dedupDecision(
  paperId: string, decision: DedupDecision, targetId: string
): Promise<DecisionResult> {
  const r = await fetch(`${API}/api/papers/${encodeURIComponent(paperId)}/dedup-decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, target_id: targetId }),
  });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
  return r.json();
}

// ---------------- DiffLens：版本差异总结 ----------------

export interface DiffPara {
  op: "added" | "removed" | "replaced" | "refs_changed";
  base: string;
  target: string;
  hl?: { a: [number, number]; b: [number, number]; tag: string }[];
  numbers?: { direction: string; value: string; section: string }[];
}

export interface DiffSection {
  status: "aligned" | "added" | "removed";
  title: string;
  base_title: string;
  base_page?: number | null;
  target_page?: number | null;
  changed?: number;
  is_references?: boolean;
  detail_omitted?: boolean;
  paras?: DiffPara[];
  numbers?: { direction: string; value: string; section: string }[];
}

export interface DiffPayload {
  sections: DiffSection[];
  numbers: { direction: string; value: string; section: string }[];
  summary: { bullets: { type: string; text: string }[]; engine: "llm" | "extractive" };
  stats: {
    sections_added: number; sections_removed: number; sections_changed: number;
    paras_changed: number; numbers_changed: number;
  };
}

/** 启动 DiffLens 任务（已有缓存时直接返回 cached=true）。 */
export async function startVersionDiff(
  targetId: string, baseId: string
): Promise<{ cached: boolean; job_id: string | null; stats?: DiffPayload["stats"] }> {
  const r = await fetch(`${API}/api/papers/${encodeURIComponent(targetId)}/version-diff`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_id: baseId }),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

export async function fetchVersionDiff(
  targetId: string, baseId: string
): Promise<{ diff_id: string; stats: DiffPayload["stats"]; payload: DiffPayload }> {
  const r = await fetch(
    `${API}/api/papers/${encodeURIComponent(targetId)}/version-diff?base_id=${encodeURIComponent(baseId)}`,
    { cache: "no-store" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

export interface SSEHandlers {
  trace?: (d: TraceCard) => void;
  guardrail?: (d: { message?: string; kind?: string }) => void;
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
          else if (event === "guardrail") on.guardrail?.(data);
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

/** POST SSE 流式翻译（block 级增量 t_delta → final 统计 → done）。signal 中断整篇翻译。 */
export async function streamTranslate(
  paperId: string,
  body: { lang?: string; page?: number; section?: string; block_ids?: string[];
          include_extras?: boolean },
  on: TranslateHandlers,
  signal?: AbortSignal
): Promise<void> {
  const resp = await fetch(`${API}/api/papers/${encodeURIComponent(paperId)}/translate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
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

// ---------------- 模块三 D：全文搜索 ----------------

/** 全文搜索命中（snippet 为纯文本，前端自行 <mark> 高亮）。 */
export interface SearchHit {
  chunk_id: string; page: number; section_path: string;
  snippet: string; score: number; block_ids: string[];
}

/** GET /papers/{id}/search：FTS5 chunk 级全文检索（跨页跳转用 block_ids[0]）。 */
export async function searchPaper(
  paperId: string, q: string, topK = 10
): Promise<{ results: SearchHit[]; total: number }> {
  const r = await fetch(
    `${API}/api/papers/${encodeURIComponent(paperId)}/search`
    + `?q=${encodeURIComponent(q)}&top_k=${topK}`,
    { cache: "no-store" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
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
  status?: (d: { stage: string; note: string; arxiv_id?: string; paper_id?: string;
                  query?: string; source?: string }) => void;
  results?: (d: { query: string; entries: ArxivEntry[] | MultiEntry[]; source: string }) => void;
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

/** 勾选下载：下载 → 入库 → 等解析（arxiv_id 或开放 pdf_url 二选一）。 */
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

/** 多源候选下载：arXiv id 或开放 PDF 直链。 */
export async function multiDownload(arxivId: string, pdfUrl?: string): Promise<{
  paper_id: string; job_id: string | null; dedup?: string;
  existing_title?: string; parse?: { status: string; message?: string };
}> {
  const r = await fetch(`${API}/api/discover/download`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ arxiv_id: arxivId || "", pdf_url: pdfUrl || "" }),
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

// ---------------- 模块四：多源检索 / 追踪 / 推荐 ----------------

export interface MultiEntry {
  title: string; authors: string[]; year: number; venue: string;
  doi: string; arxiv_id: string; citation_count: number | null;
  abstract: string; sources: string[]; pdf_url?: string;
  raw?: string; resolved?: boolean;
}

/** 多源检索（SSE）：query/title/doi/arxiv_id/references_text 四选一入口。 */
export async function streamMultiSearch(
  body: { query?: string; title?: string; doi?: string; arxiv_id?: string;
          references_text?: string },
  on: DiscoverHandlers,
): Promise<void> {
  const resp = await fetch(`${API}/api/discover/search`, {
    method: "POST", headers: { "Content-Type": "application/json" },
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

/** 匿名多用户：localStorage uuid（推荐/反馈隔离键）。 */
export function userId(): string {
  if (typeof window === "undefined") return "anonymous";
  let u = localStorage.getItem("scholar_uid");
  if (!u) {
    u = `u-${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36).slice(-4)}`;
    localStorage.setItem("scholar_uid", u);
  }
  return u;
}

function _uidHeaders(): Record<string, string> {
  return { "X-User-Id": userId() };
}

export interface TrackingSub {
  id: string; name: string; query_type: string; value: string;
  enabled: number; new_count?: number; last_run?: number;
}
export interface TrackedItem {
  id: string; sub_id: string; arxiv_id: string; title: string;
  published: string; authors: string[]; status: string; added_at: number;
}

export const trackingApi = {
  subs: async (): Promise<TrackingSub[]> =>
    (await (await fetch(`${API}/api/tracking/subscriptions`)).json()).subscriptions ?? [],
  addSub: async (name: string, query_type: string, value: string) => {
    const r = await fetch(`${API}/api/tracking/subscriptions`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, query_type, value }),
    });
    if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
  },
  delSub: async (id: string) => { await fetch(`${API}/api/tracking/subscriptions/${id}`, { method: "DELETE" }); },
  refresh: async () => {
    const r = await fetch(`${API}/api/tracking/refresh`, { method: "POST" });
    if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
    return (await r.json()).results as { sub_id: string; channel: string; fetched: number; added: number }[];
  },
  items: async (params: { sub_id?: string; status?: string } = {}): Promise<TrackedItem[]> => {
    const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v) as [string, string][]);
    const r = await fetch(`${API}/api/tracking/items?${q}`, { cache: "no-store" });
    return (await r.json()).items ?? [];
  },
  dismiss: async (id: string) => { await fetch(`${API}/api/tracking/items/${id}/dismiss`, { method: "POST" }); },
};

export interface Recommendation {
  id: string | null; title: string; arxiv_id: string;
  scores: Record<string, number | null>; reason: string; my_vote?: number;
}

export const recommendApi = {
  list: async (refresh = false): Promise<{ recommendations: Recommendation[]; note?: string; profile_keywords?: Record<string, number> }> => {
    const r = await fetch(`${API}/api/recommendations?refresh=${refresh ? "true" : "false"}`,
      { headers: _uidHeaders(), cache: "no-store" });
    return r.json();
  },
  vote: async (recId: string, vote: 1 | -1) => {
    const r = await fetch(`${API}/api/recommendations/${recId}/feedback`, {
      method: "POST", headers: { "Content-Type": "application/json", ..._uidHeaders() },
      body: JSON.stringify({ vote }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
  },
};

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

// ---- 模块五：整批领域综述 / 外部进展核查 ----

export interface SurveySection {
  title: string; content: string;
  claims: Record<string, unknown>[];
  anchors: Record<string, unknown>;
}
export interface SurveyOut {
  sections: SurveySection[]; stats: Record<string, unknown>;
  guardrails?: string[]; cached?: boolean;
}

export async function streamSurvey(
  paperIds: string[], on: {
    status?: (d: { stage: string; note: string }) => void;
    delta?: (d: { section: string; text: string }) => void;
    final?: (d: SurveyOut) => void;
    error?: (d: { message: string }) => void;
  }, refresh = false,
): Promise<void> {
  const resp = await fetch(`${API}/api/graph/survey`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paper_ids: paperIds, refresh }),
  });
  if (resp.status === 400) throw new Error((await resp.json()).detail ?? "参数不合法");
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
  await _consumeSse(resp.body, (ev, data) => {
    if (ev === "status") on.status?.(data);
    else if (ev === "delta") on.delta?.(data);
    else if (ev === "final") on.final?.(data);
    else if (ev === "error") on.error?.(data);
  });
}

export interface FutureCheckItem {
  idea_text: string; source_paper: string; mention_papers?: string[];
  verdict: "solved" | "partial" | "open" | "open_unverified";
  confidence: number; reason: string;
  external: { title: string; year: number; citation_count: number | null; abstract: string }[];
}

export async function streamFutureChecks(
  paperIds: string[], on: {
    status?: (d: { stage: string; note: string }) => void;
    final?: (d: { items: FutureCheckItem[]; total: number }) => void;
    error?: (d: { message: string }) => void;
  },
): Promise<void> {
  const resp = await fetch(`${API}/api/future-checks`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paper_ids: paperIds }),
  });
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
  await _consumeSse(resp.body, (ev, data) => {
    if (ev === "status") on.status?.(data);
    else if (ev === "final") on.final?.(data);
    else if (ev === "error") on.error?.(data);
  });
}

/** 通用 SSE 消费（event/data 帧 → 回调）。 */
async function _consumeSse(
  body: ReadableStream<Uint8Array>,
  cb: (event: string, data: any) => void,
): Promise<void> {
  const reader = body.getReader();
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
          cb(event, JSON.parse(line.slice(6)));
          event = "";
        }
      }
    }
  }
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

// ---------------- 模块六：Copilot（写作 / CSV 可视化 / 拓扑图） ----------------

export interface WritingSection {
  title: string; content: string; cited_refs: number[];
  evidence_count?: number; deterministic?: boolean;
}
export interface WritingOut {
  outline: Record<string, unknown>;
  sections: WritingSection[];
  references: { used: { ref_id: number; title: string; authors: string; year: number;
    venue: string; arxiv_id: string; doi: string }[]; unused_count: number; whitelist_size: number };
  content_md: string; guardrails: string[];
}

export async function streamWriting(
  idea: string, paperIds: string[], on: {
    status?: (d: { stage: string; note: string }) => void;
    final?: (d: WritingOut) => void;
    error?: (d: { message: string }) => void;
  },
): Promise<void> {
  const resp = await fetch(`${API}/api/copilot/writing`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ idea, paper_ids: paperIds }),
  });
  if (resp.status === 400) throw new Error((await resp.json()).detail ?? "参数不合法");
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
  await _consumeSse(resp.body, (ev, data) => {
    if (ev === "status") on.status?.(data);
    else if (ev === "final") on.final?.(data);
    else if (ev === "error") on.error?.(data);
  });
}

export interface DatasetMeta {
  id: string; name: string; row_count: number;
  columns: { name: string; dtype: string }[]; created_at: number; note?: string;
}

export const datasetApi = {
  upload: async (file: File): Promise<DatasetMeta> => {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch(`${API}/api/copilot/datasets`, { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
    return r.json();
  },
  list: async (): Promise<DatasetMeta[]> =>
    (await (await fetch(`${API}/api/copilot/datasets`, { cache: "no-store" })).json()).datasets ?? [],
  preview: async (id: string): Promise<{ name: string; columns: { name: string; dtype: string }[]; rows: string[][] }> => {
    const r = await fetch(`${API}/api/copilot/datasets/${id}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  },
};

export interface ChartOut {
  id: string; caption: string;
  spec: { type: string; title: string; x?: { name: string; data: (string | number)[] };
          series?: { name: string; data: (number | null)[] }[]; indicators?: { name: string; max: number }[];
          columns?: string[]; rows?: string[][]; row_count?: number };
}

export async function buildChart(body: { dataset_id: string; chart_type: string;
  x?: string; y?: string[]; title?: string; with_caption?: boolean }): Promise<ChartOut> {
  const r = await fetch(`${API}/api/copilot/chart`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
  return r.json();
}

export interface DiagramOut {
  spec: { nodes: { id: string; label: string }[]; edges: { from: string; to: string; label?: string }[] };
  sources: { mermaid: string; dot: string; tikz: string };
  matplotlib: { ok: boolean; image_url?: string; error?: string };
  validation: Record<string, { ok: boolean | null; checker: string }>;
}

export async function generateDiagram(description: string): Promise<DiagramOut> {
  const r = await fetch(`${API}/api/copilot/diagram`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ description }),
  });
  if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
  return r.json();
}

export async function zh2enTranslate(text: string): Promise<{ translation: string; ok: boolean; reason?: string }> {
  const r = await fetch(`${API}/api/copilot/translate`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!r.ok) throw new Error((await r.json()).detail ?? `HTTP ${r.status}`);
  return r.json();
}
