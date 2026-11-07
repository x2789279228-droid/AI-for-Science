"use client";

import { useEffect, useState } from "react";
import { API } from "@/lib/api";

interface LogRow {
  id: number; ts: number; module: string; model: string; question: string;
  prompt_tokens: number; completion_tokens: number; latency_ms: number; guardrail: string;
}

export default function AuditPage() {
  const [logs, setLogs] = useState<LogRow[]>([]);
  const [info, setInfo] = useState<{ llm_available: boolean; dense_enabled: boolean }>();

  useEffect(() => {
    fetch(`${API}/api/audit/prompts?limit=100`, { cache: "no-store" })
      .then((r) => r.json()).then((d) => { setLogs(d.logs); setInfo(d); });
  }, []);

  return (
    <div>
      <h1>透明度中心 <span className="badge">Prompt 审计</span></h1>
      <div className="card">
        <p className="kv">
          LLM 可用：{info?.llm_available ? "✅ 是" : "❌ 否（抽取式降级中）"} ·
          Dense 检索：{info?.dense_enabled ? "✅ 开" : "❌ 关（BM25 单路）"} ·
          累计调用：{logs.length} 条
        </p>
      </div>
      <div className="card" style={{ overflowX: "auto" }}>
        <h2>调用日志（时间 / 模块 / 模型 / Token / 耗时 / Guardrail）</h2>
        <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--muted)" }}>
              <th>时间</th><th>模块</th><th>模型</th><th>输入tok</th><th>输出tok</th>
              <th>耗时ms</th><th>Guardrail</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((l) => (
              <tr key={l.id} style={{ borderTop: "1px solid var(--line)" }}>
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
  );
}
