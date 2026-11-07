"use client";

import { useEffect, useState } from "react";
import { API, Health, fetchHealth } from "@/lib/api";

interface LogRow {
  id: number; ts: number; module: string; model: string; question: string;
  prompt_tokens: number; completion_tokens: number; latency_ms: number; guardrail: string;
}

export default function AuditPage() {
  const [logs, setLogs] = useState<LogRow[]>([]);
  const [info, setInfo] = useState<(Partial<Health> & { llm_available?: boolean; dense_enabled?: boolean }) | undefined>();

  useEffect(() => {
    fetch(`${API}/api/audit/prompts?limit=100`, { cache: "no-store" })
      .then((r) => r.json()).then((d) => {
        setLogs(d.logs);
        setInfo((prev) => ({ ...(prev ?? ({} as Health)), llm_available: d.llm_available, dense_enabled: d.dense_enabled }));
      });
    fetchHealth().then(setInfo).catch(() => {});
  }, []);

  return (
    <div className="page">
      <div className="page-inner">
        <h1>透明度中心 <span className="chip accent">Prompt 审计</span></h1>
        <div className="card">
          <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
            <span className={`chip ${info?.llm_available ? "ok" : "warn"}`}>
              LLM {info?.llm_available ? `就绪（${info.llm?.model ?? ""}）` : "不可用 · 抽取式降级"}
            </span>
            <span className={`chip ${info?.dense ? "ok" : "warn"}`}>
              Dense 检索 {info?.dense ? "开" : "关（BM25 单路）"}
            </span>
            <span className="chip">累计调用 {logs.length} 条</span>
          </div>
        </div>
        <div className="card" style={{ overflowX: "auto" }}>
          <h2>调用日志（时间 / 模块 / 模型 / Token / 耗时 / Guardrail）</h2>
          <table className="table">
            <thead>
              <tr>
                <th>时间</th><th>模块</th><th>模型</th><th>输入tok</th><th>输出tok</th>
                <th>耗时ms</th><th>Guardrail</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((l) => (
                <tr key={l.id}>
                  <td>{new Date(l.ts * 1000).toLocaleTimeString()}</td>
                  <td>{l.module}</td>
                  <td>{l.model || "—"}</td>
                  <td>{l.prompt_tokens || "—"}</td>
                  <td>{l.completion_tokens || "—"}</td>
                  <td>{l.latency_ms || "—"}</td>
                  <td>{l.guardrail || "—"}</td>
                </tr>
              ))}
              {logs.length === 0 && <tr><td colSpan={7}>暂无调用记录。</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
