"use client";

import { useEffect, useState } from "react";
import { API, Health, fetchHealth } from "@/lib/api";

interface LogRow {
  id: number; ts: number; module: string; model: string; question: string;
  prompt_tokens: number; completion_tokens: number; latency_ms: number; guardrail: string;
  prompt_version?: string;
}

interface PromptCatalogItem {
  module: string; version: string; changelog: string[];
  slots: Record<string, string[]>;
}

interface VersionDist { version: string; n: number }

export default function AuditPage() {
  const [logs, setLogs] = useState<LogRow[]>([]);
  const [versions, setVersions] = useState<VersionDist[]>([]);
  const [catalog, setCatalog] = useState<PromptCatalogItem[]>([]);
  const [variant, setVariant] = useState("default");
  const [info, setInfo] = useState<(Partial<Health> & { llm_available?: boolean; dense_enabled?: boolean }) | undefined>();

  useEffect(() => {
    fetch(`${API}/api/audit/prompts?limit=100`, { cache: "no-store" })
      .then((r) => r.json()).then((d) => {
        setLogs(d.logs ?? []);
        setVersions(d.prompt_versions ?? []);
        setCatalog(d.prompt_catalog ?? []);
        setVariant(d.prompt_variant ?? "default");
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
            <span className="chip accent">A/B 档位 PROMPT_VARIANT={variant}</span>
          </div>
        </div>

        <div className="card">
          <h2>Prompt 版本目录（app/prompts/*.yaml · 改一字 bump 一次版本）</h2>
          {catalog.length === 0 && <p>未加载到 YAML prompt 目录。</p>}
          {catalog.map((c) => (
            <div key={c.module} style={{ marginBottom: "var(--space-3)" }}>
              <b>{c.module}.yaml</b> <span className="chip ok">v{c.version}</span>
              <div style={{ fontSize: 12, opacity: 0.75, marginTop: 4 }}>
                {c.changelog.join(" · ")}
              </div>
              <div style={{ fontSize: 12, opacity: 0.6 }}>
                槽位：{Object.entries(c.slots).map(([v, slots]) => `${v}: ${slots.join(" / ")}`).join(" ｜ ")}
              </div>
            </div>
          ))}
          {versions.length > 0 && (
            <div style={{ marginTop: "var(--space-2)" }}>
              <b>调用版本分布：</b>{" "}
              {versions.map((v) => (
                <span key={v.version} className="chip" style={{ marginRight: 8 }}>
                  {v.version} × {v.n}
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="card" style={{ overflowX: "auto" }}>
          <h2>调用日志（时间 / 模块 / 模型 / Prompt 版本 / Token / 耗时 / Guardrail）</h2>
          <table className="table">
            <thead>
              <tr>
                <th>时间</th><th>模块</th><th>模型</th><th>Prompt版本</th><th>输入tok</th><th>输出tok</th>
                <th>耗时ms</th><th>Guardrail</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((l) => (
                <tr key={l.id}>
                  <td>{new Date(l.ts * 1000).toLocaleTimeString()}</td>
                  <td>{l.module}</td>
                  <td>{l.model || "—"}</td>
                  <td>{l.prompt_version || "legacy"}</td>
                  <td>{l.prompt_tokens || "—"}</td>
                  <td>{l.completion_tokens || "—"}</td>
                  <td>{l.latency_ms || "—"}</td>
                  <td>{l.guardrail || "—"}</td>
                </tr>
              ))}
              {logs.length === 0 && <tr><td colSpan={8}>暂无调用记录。</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
