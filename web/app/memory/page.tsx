"use client";

/**
 * 模块七 /memory 研究时间线页（第 11 个导航页，学术专属）：
 * 左列记忆事件流（kind/status 着色 + 会话轮次溯源）｜右列 G6 因果边图 + 假设 FSM 状态板。
 * 筛选框接后端 timeparse：自然语言时间表达式（上季度/最近两周/last month）直接作为查询参数。
 * 数据源：GET /api/memory/timeline + GET /api/memory/hypotheses（只读）。
 */
import { useCallback, useEffect, useState } from "react";
import {
  fetchMemoryHypotheses, fetchMemoryTimeline,
  HypothesisItem, MemoryEvent, TimelineResp,
} from "@/lib/api";
import { useShell } from "@/components/ShellContext";
import MemoryGraph, { KIND_COLOR, KIND_LABEL } from "@/components/memory/MemoryGraph";

const STATUS_LABEL: Record<string, string> = {
  proposed: "提出", active: "验证中", supported: "已支持",
  refuted: "已推翻", superseded: "已被取代",
};

const STATUS_COLOR: Record<string, string> = {
  proposed: "var(--ink-3)", active: "var(--warn)",
  supported: "var(--ok)", refuted: "var(--danger)", superseded: "var(--ink-3)",
};

export default function MemoryPage() {
  const { theme } = useShell();
  const [tl, setTl] = useState<TimelineResp | null>(null);
  const [hyps, setHyps] = useState<HypothesisItem[]>([]);
  const [expr, setExpr] = useState("");
  const [applied, setApplied] = useState<{ expr: string; kind: string }>({ expr: "", kind: "" });
  const [failed, setFailed] = useState(false);

  const load = useCallback((f: { expr: string; kind: string }) => {
    setFailed(false);
    fetchMemoryTimeline({ expr: f.expr || undefined, kind: f.kind || undefined, limit: 200 })
      .then(setTl).catch(() => setFailed(true));
    fetchMemoryHypotheses().then((r) => setHyps(r.hypotheses)).catch(() => { /* 状态板非关键 */ });
  }, []);

  useEffect(() => { load({ expr: "", kind: "" }); }, [load]);

  const apply = () => {
    setApplied({ expr: expr.trim(), kind: applied.kind });
    load({ expr: expr.trim(), kind: applied.kind });
  };

  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 1180 }} data-testid="memory-page">
        <h1>
          记忆与研究状态
          <span className="chip accent">模块七 · 时间感知记忆 · 事件流/图谱/FSM 三层分离</span>
        </h1>

        {/* 筛选行：自然语言时间表达式 → 后端 timeparse 解析（清单 §三#3） */}
        <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center",
          flexWrap: "wrap", marginBottom: "var(--space-3)" }}>
          <input className="input" data-testid="memory-expr-input"
            placeholder="时间表达式：上季度 / 最近两周 / last month…"
            value={expr}
            onChange={(e) => setExpr(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") apply(); }}
            style={{ minWidth: 260 }} />
          <button className="btn" data-testid="memory-apply" onClick={apply}>应用筛选</button>
          <button className="btn ghost" onClick={() => {
            setExpr(""); setApplied({ expr: "", kind: "" }); load({ expr: "", kind: "" });
          }}>重置</button>
          {tl?.meta.time_range && (
            <span className="chip accent" data-testid="memory-time-range">
              {tl.meta.time_range.expr}：{tl.meta.time_range.start_iso.slice(0, 10)}
              ~ {tl.meta.time_range.end_iso.slice(0, 10)}
            </span>
          )}
        </div>

        {failed && (
          <div className="kv" style={{ color: "var(--danger)" }}>
            记忆端点不可用——请确认 API 已启动后刷新。
          </div>
        )}

        <div style={{ display: "grid", gridTemplateColumns: "1.05fr 1fr",
          gap: "var(--space-4)", alignItems: "start" }}>
          {/* 左列：记忆事件流 */}
          <section className="card-in" style={{ padding: "var(--space-3)" }}
            data-testid="memory-timeline-list">
            <b style={{ display: "block", marginBottom: "var(--space-2)" }}>
              记忆事件流 · {tl?.meta.n_events ?? "—"} 条（append-only）
            </b>
            {!tl && <div className="kv muted">加载中…</div>}
            {tl && tl.events.length === 0 && (
              <div className="kv muted" data-testid="memory-timeline-empty">
                没有匹配的记忆事件——去 /chat 问几轮，轮末抽取会自动写入。
              </div>
            )}
            {tl?.events.map((e: MemoryEvent) => (
              <div key={e.id} className="card-in" data-testid="memory-event-item"
                style={{ padding: "var(--space-2) var(--space-3)", marginBottom: "var(--space-2)",
                  borderLeft: `3px solid ${KIND_COLOR[e.kind] ?? "var(--line)"}` }}>
                <div style={{ fontSize: 12.5, lineHeight: 1.6 }}>{e.content}</div>
                <div style={{ display: "flex", gap: "var(--space-2)", marginTop: 4,
                  flexWrap: "wrap", alignItems: "center" }}>
                  <span className="chip"
                    style={{ color: KIND_COLOR[e.kind] }}>
                    {KIND_LABEL[e.kind] ?? e.kind}
                  </span>
                  <span className="chip">{e.ts_iso.slice(0, 16).replace("T", " ")}</span>
                  {e.conversation_id && (
                    <span className="chip">会话 {e.conversation_id.slice(0, 8)} · 第 {e.turn} 轮</span>
                  )}
                  <span className="chip" title={`source=${e.source} · importance=${e.importance} · strength=${e.strength}`}>
                    {e.source === "llm" ? "抽取" : e.source === "rule" ? "规则" : e.source}
                  </span>
                </div>
              </div>
            ))}
          </section>

          <div style={{ display: "grid", gap: "var(--space-4)" }}>
            {/* 右上：因果边图 */}
            <section className="card-in" style={{ padding: "var(--space-3)" }}>
              <b style={{ display: "block", marginBottom: "var(--space-2)" }}>
                因果边图 · {tl?.meta.n_edges ?? "—"} 条（THEANINE 时间图谱 · 过期标 superseded 不删除）
              </b>
              <MemoryGraph events={tl?.events ?? []} edges={tl?.edges ?? []} theme={theme} />
            </section>

            {/* 右下：假设 FSM 状态板（ClaimTrace 四态色 tokens） */}
            <section className="card-in" style={{ padding: "var(--space-3)" }}
              data-testid="memory-fsm-board">
              <b style={{ display: "block", marginBottom: "var(--space-2)" }}>
                假设生命周期 FSM · {hyps.length} 条活跃
              </b>
              {hyps.length === 0 && (
                <div className="kv muted">暂无活跃假设——说「我假设…」即可开启一条研究假设。</div>
              )}
              {hyps.map((h) => (
                <div key={h.id} className="card-in" data-testid="memory-hypothesis-item"
                  style={{ padding: "var(--space-2) var(--space-3)", marginBottom: "var(--space-2)" }}>
                  <div style={{ fontSize: 12.5, lineHeight: 1.6 }}>{h.content}</div>
                  <div style={{ display: "flex", gap: "var(--space-2)", marginTop: 4,
                    flexWrap: "wrap", alignItems: "center" }}>
                    <span className="chip" style={{ color: STATUS_COLOR[h.status], fontWeight: 600 }}
                      data-testid="memory-hyp-status">
                      {STATUS_LABEL[h.status] ?? h.status}
                    </span>
                    {h.legal_next.length > 0 && (
                      <span className="chip" title="FSM 合法转移表（非法转移被拒绝并审计）">
                        可转移 → {h.legal_next.map((s) => STATUS_LABEL[s] ?? s).join(" / ")}
                      </span>
                    )}
                    {h.transitions.length > 0 && (
                      <span className="chip">
                        转移 {h.transitions.length} 次 · 最近：{h.transitions[h.transitions.length - 1]?.from}
                        →{h.transitions[h.transitions.length - 1]?.to}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </section>
          </div>
        </div>
      </div>
    </div>
  );
}
