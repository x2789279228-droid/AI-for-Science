"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  EvidenceEvent, FinalEvent, PlanEvent, ToolCallEvent, ToolResultEvent,
  TraceCard, TLEvent, VerifierEvent,
} from "@/lib/api";
import { ChevronRight } from "@/components/icons";

/**
 * 右侧 Trace 时间线（docs/00 §4.1「Agent 思考可视化」承载组件）：
 * - 计划拆成 3–5 步，每步按 tool_call/tool_result 实时点亮（pending→running→done/failed）
 * - 步骤可展开：工具参数（args）与返回值（refs/通道/耗时）
 * - Verifier「证据不足→补检索」与 recover 恢复事件高亮
 * 输入为有序 TLEvent 流（live 与 /api/qa 回放同构），trace 卡片作为 router/fuse/recover/
 * responder 行的兜底数据源；planner/verifier/retrieve_task 卡片由结构化事件承载。
 */

type StepStatus = "pending" | "running" | "done" | "degraded" | "failed";

interface StepRow {
  step_id: string;
  title: string;
  query: string;
  status: StepStatus;
  call?: ToolCallEvent;
  result?: ToolResultEvent;
}

type Row =
  | { kind: "router"; card: TraceCard }
  | { kind: "round"; plan: PlanEvent; steps: StepRow[] }
  | { kind: "tool"; step: StepRow }
  | { kind: "fuse"; card: TraceCard; evidence?: EvidenceEvent }
  | { kind: "verifier"; ev: VerifierEvent }
  | { kind: "recover"; card: TraceCard }
  | { kind: "responder"; card: TraceCard };

/** 有序事件流 → 渲染行（保持真实到达顺序，结构化事件优先、卡片兜底）。 */
function buildRows(tl: TLEvent[]): Row[] {
  const rows: Row[] = [];
  const stepIndex = new Map<string, StepRow>();
  for (const { kind, data } of tl) {
    if (kind === "trace") {
      const card = data as TraceCard;
      if (card.node === "router") rows.push({ kind: "router", card });
      else if (card.node === "executor_fuse") rows.push({ kind: "fuse", card });
      else if (card.node === "recover") rows.push({ kind: "recover", card });
      else if (card.node === "responder") rows.push({ kind: "responder", card });
      // planner / verifier / retrieve_task 卡片分别由 plan / verifier / tool_* 事件承载
    } else if (kind === "plan") {
      const plan = data as PlanEvent;
      const steps: StepRow[] = plan.steps.map((s) => ({
        step_id: s.step_id, title: s.title, query: s.query, status: "pending",
      }));
      rows.push({ kind: "round", plan, steps });
      for (const s of steps) stepIndex.set(s.step_id, s);
    } else if (kind === "tool_call") {
      const d = data as ToolCallEvent;
      const s = d.step_id ? stepIndex.get(d.step_id) : undefined;
      if (s) { s.status = "running"; s.call = d; }
      else {
        // 计划外工具（注册表直调，如 get_meta/align_dimension）：独立工具行，同样可点亮/展开
        const id = d.step_id || `tool-${rows.length}`;
        const step: StepRow = {
          step_id: id, title: d.tool, query: "", status: "running", call: d,
        };
        rows.push({ kind: "tool", step });
        stepIndex.set(id, step);
      }
    } else if (kind === "tool_result") {
      const d = data as ToolResultEvent;
      const s = d.step_id ? stepIndex.get(d.step_id) : undefined;
      if (s) { s.status = d.ok ? (d.degraded ? "degraded" : "done") : "failed"; s.result = d; }
    } else if (kind === "evidence") {
      // evidence 事件紧跟其 fuse 卡片：回填到最近的融合行
      for (let i = rows.length - 1; i >= 0; i--) {
        const r = rows[i];
        if (r.kind === "fuse") { r.evidence = data as EvidenceEvent; break; }
      }
    } else if (kind === "verifier") {
      rows.push({ kind: "verifier", ev: data as VerifierEvent });
    }
  }
  return rows;
}

const DOT_CLS: Record<StepStatus, string> = {
  pending: "", running: "tl-running", done: "tl-done",
  degraded: "tl-warn", failed: "tl-fail",
};

export default function TracePanel({ tl, busy, final }: {
  tl: TLEvent[];
  busy: boolean;
  final: FinalEvent | null;
}) {
  const rows = useMemo(() => buildRows(tl), [tl]);
  const [openSteps, setOpenSteps] = useState<Set<string>>(new Set());
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight });
  }, [tl.length]);

  const toggleStep = (id: string) => setOpenSteps((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  });

  return (
    <aside className="trace-panel">
      <div className="trace-head">
        <b style={{ fontSize: "var(--text-sm)" }}>Agent Trace</b>
        <span className={`chip ${final ? "ok" : busy ? "accent" : ""}`}>
          {final ? "已完成" : busy ? "执行中" : "待命"}
        </span>
      </div>
      <div className="trace-body" ref={bodyRef}>
        {rows.length === 0 && (
          <p className="kv">
            提问后，Agent 的检索计划、工具调用与证据校验会在这里逐步点亮；
            「证据不足 → 补检索」等恢复事件会高亮显示。
          </p>
        )}
        {rows.map((row, i) => (
          <RowView key={i} row={row} open={openSteps} toggle={toggleStep} />
        ))}
        {busy && !final && (
          <div className="tl-row">
            <span className="tl-dot tl-running" />
            <span className="kv">Agent 执行中…</span>
          </div>
        )}
        {final && (
          <div className="tl-row">
            <span className="tl-dot tl-done" />
            <span className="kv">
              图执行完成{final.elapsed != null ? ` · ${final.elapsed}s` : ""}
              {final.thread_id ? ` · trace ${final.thread_id.slice(-6)}` : ""}
            </span>
          </div>
        )}
      </div>
    </aside>
  );
}

function RowView({ row, open, toggle }: {
  row: Row;
  open: Set<string>;
  toggle: (id: string) => void;
}) {
  switch (row.kind) {
    case "router":
      return (
        <div className="tl-row">
          <span className="tl-dot tl-done" />
          <div><b>意图识别</b><span className="kv"> · {row.card.summary}</span></div>
        </div>
      );
    case "round":
      return (
        <div className="tl-round">
          <div className="tl-round-head">
            <span className="tl-dot tl-done" />
            <b>{row.plan.round > 1 ? `补检索 · 第 ${row.plan.round} 轮计划` : "检索计划"}</b>
            {row.plan.round > 1 && <span className="badge warn">Verifier 打回</span>}
            <span className="kv">{row.plan.steps.length} 步</span>
          </div>
          {row.steps.map((s) => (
            <StepView key={s.step_id} s={s} open={open} toggle={toggle} />
          ))}
        </div>
      );
    case "tool":
      return <StepView s={row.step} open={open} toggle={toggle} />;
    case "fuse":
      return (
        <div className="tl-row">
          <span className="tl-dot tl-done" />
          <div>
            <b>证据融合</b><span className="kv"> · {row.card.summary}</span>
            {row.evidence && (
              <div className="kv">
                命中 {row.evidence.hits?.length ?? 0} 块 · 通道 {row.evidence.channels.join("+") || "bm25"}
              </div>
            )}
          </div>
        </div>
      );
    case "verifier":
      return row.ev.verdict === "pass" ? (
        <div className="tl-row">
          <span className="tl-dot tl-done" />
          <div>
            <b>证据校验</b>
            <span className="kv"> · 通过 · {row.ev.evidence_count} 块覆盖全部检索面</span>
          </div>
        </div>
      ) : (
        <div className="verifier-alert">
          <div className="verifier-alert-head">
            <span className="tl-dot tl-fail" />
            <b>证据不足</b>
            <span className="badge warn">→ 触发补检索</span>
          </div>
          <div className="kv">
            缺少 {row.ev.missing.length} 个检索面：{row.ev.missing.join("、") || "—"}
          </div>
        </div>
      );
    case "recover": {
      const rewrites = row.card.payload?.rewrites as Record<string, string> | undefined;
      return (
        <div className="verifier-alert recover">
          <div className="verifier-alert-head">
            <span className="tl-dot tl-warn" />
            <b>{row.card.title}</b>
          </div>
          <div className="kv">{row.card.summary}</div>
          {rewrites && (
            <div className="kv">
              改写：{Object.entries(rewrites).map(([k, v]) => `${k} → ${v}`).join("；")}
            </div>
          )}
        </div>
      );
    }
    case "responder":
      return (
        <div className="tl-row">
          <span className="tl-dot tl-done" />
          <div><b>生成回答</b><span className="kv"> · {row.card.summary}</span></div>
        </div>
      );
  }
}

const STATUS_TEXT: Record<StepStatus, string> = {
  pending: "待执行", running: "执行中", done: "完成",
  degraded: "BM25 降级", failed: "失败",
};

function StepView({ s, open, toggle }: {
  s: StepRow;
  open: Set<string>;
  toggle: (id: string) => void;
}) {
  // 运行中步骤自动展开（点亮），完成后收起，除非用户手动固定
  const isOpen = open.has(s.step_id) || s.status === "running";
  return (
    <div className="tl-step">
      <div className="tl-step-head" onClick={() => toggle(s.step_id)}
        title={`${s.title} — ${s.query}`}>
        <span className={`tl-dot ${DOT_CLS[s.status]}`} />
        <span className="tl-step-title">{s.title}</span>
        <span className="chip">{s.call?.tool ?? "kb_search"}</span>
        <span className="kv">{STATUS_TEXT[s.status]}{s.result ? ` · ${s.result.elapsed_ms}ms` : ""}</span>
        <ChevronRight size={12} className="pipe-caret"
          style={{ transform: isOpen ? "rotate(90deg)" : "none" }} />
      </div>
      {isOpen && (
        <div className="tl-step-detail">
          {s.call && (
            <>
              <div className="tl-detail-label">工具参数</div>
              <pre className="pipe-payload">{JSON.stringify(s.call.args, null, 1)}</pre>
            </>
          )}
          {s.result && (
            <>
              <div className="tl-detail-label">返回结果</div>
              <pre className="pipe-payload">{JSON.stringify({
                ok: s.result.ok,
                ...(s.result.degraded ? { degraded: s.result.degraded } : {}),
                summary: s.result.summary,
                refs: s.result.refs?.length ?? 0,
                ...s.result.payload,
              }, null, 1)}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}
