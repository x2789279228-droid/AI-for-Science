"use client";

/**
 * 健康中心(/health,§健康中心方案 2026-11-15 重写):
 * 把"系统健康"拆成 7 个固定权重的运行时子分,全部由窗口内生产数据计算:
 *   可靠性 / 回答可信 / 回答覆盖 / 检索质量 / 解析质量 / 性能 / 数据卫生
 * 口径要点:
 * - 只统计最近 7 天本机运行轨迹(排除 kappa_/gold_/bench_/e2e_ 等评测/测试前缀);
 *   样本 < 30 条时不出总分,避免小样本虚高。
 * - 失败 trace / 失败任务进可靠性分母;拒答/无答案进"回答覆盖",不再因分母缩小而提分。
 * - 缺失子分按 0 计且列入 missing,不重归一化。
 * - 外部金标(最新 eval_gold)gates 未全过时总分封顶 0.90,并展示失败项。
 * - 工程快照(pytest/e2e,来自 web/lib/evidence.ts)单独展示,不计入运行时健康分。
 * 与 /audit 分工:audit=过程审计(Prompt/token),health=结果健康(质量指标+健康分)。
 */
import { useEffect, useState } from "react";
import Link from "next/link";
import { fetchHealthReport, HealthReport, HealthScore } from "@/lib/api";
import { EVIDENCE } from "@/lib/evidence";

/** 工程快照：数字唯一来源 web/lib/evidence.ts（scripts/write_evidence.py 自动生成）。 */
const ENGINEERING = {
  pytestPassed: EVIDENCE.pytest.passed,
  pytestTotal: Math.max(EVIDENCE.pytest.total, 1),
  e2ePassed: EVIDENCE.e2e.passed,
  // 数据条件 skip 不计入分母；环境性 failed 计入
  e2eTotal: Math.max(EVIDENCE.e2e.passed + EVIDENCE.e2e.failed, 1),
  e2eSkipped: EVIDENCE.e2e.skipped,
  date: EVIDENCE.generatedAt,
  commit: EVIDENCE.commit,
};

/** 运行时健康分：7 个固定权重子分（缺失按 0 计，不重归一化）。 */
const ORDER = [
  "reliability", "grounded", "answer_coverage", "retrieval",
  "parse", "performance", "data_health",
] as const;

const SUB_TITLES: Record<string, string> = {
  reliability: "可靠性",
  grounded: "回答可信",
  answer_coverage: "回答覆盖",
  retrieval: "检索质量",
  parse: "解析质量",
  performance: "性能",
  data_health: "数据卫生",
};

const MODE_LABELS: Record<string, string> = {
  generative: "生成式",
  extractive: "抽取式",
  refusal: "诚实拒答",
  none: "无 final",
  unknown: "未知",
};

/** ClaimTrace 四态分布条形(标签/字形/色)。 */
const CLAIM_BARS: [string, string, string][] = [
  ["supported", "证", "var(--ok)"],
  ["needs_check", "疑", "var(--warn)"],
  ["unsupported", "✗", "var(--danger)"],
  ["absent", "缺", "var(--ink-3)"],
];

function pct(v: number | null | undefined, digits = 1): string {
  return v == null ? "暂无数据" : `${(v * 100).toFixed(digits)}%`;
}

/** 分数色:≥0.9 绿 / ≥0.75 蓝 / ≥0.6 琥珀 / 更低红。 */
function scoreColor(v: number): string {
  return v >= 0.9 ? "var(--ok)" : v >= 0.75 ? "var(--accent)"
    : v >= 0.6 ? "var(--warn)" : "var(--danger)";
}

function ScoreCard({ name, s, missing }: {
  name: string; s?: HealthScore; missing?: boolean;
}) {
  const v = s?.value ?? null;
  const color = v != null ? scoreColor(v) : "var(--ink-3)";
  const weight = s?.weight ?? 0;
  const ci = s?.ci;
  return (
    <div className={`card h-score-card card-in${missing ? " h-score-card-missing" : ""}`}
      data-testid={`health-score-${name}`}>
      <div className="h-score-head">
        <b>{name}</b>
        <span className="chip num" title="固定权重：缺失维度按 0 计，不重归一化">
          {missing ? "缺失 · 按 0 计" : `权重 ${Math.round(weight * 100)}%`}
        </span>
      </div>
      <div className="h-score-val num" style={{ color }}>
        {v == null ? "—" : (v * 100).toFixed(1)}
      </div>
      <p className="kv num muted" style={{ fontSize: 11 }}>
        {s?.n != null ? `n=${s.n}` : "无可用样本"}
        {ci ? ` · 95%CI ${(ci[0] * 100).toFixed(0)}–${(ci[1] * 100).toFixed(0)}%` : ""}
      </p>
      <p className="h-formula kv" title="评分公式与原始计数——评分本身可审计">
        {missing ? "该维度当前无可用数据；固定权重下按 0 计（不抬高其他维度）。"
          : s?.formula}
      </p>
    </div>
  );
}

export default function HealthPage() {
  const [report, setReport] = useState<HealthReport | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    fetchHealthReport().then(setReport).catch(() => setFailed(true));
  }, []);

  // 工程快照（单独展示，不计入运行时健康分）
  const eng = ((ENGINEERING.pytestPassed / ENGINEERING.pytestTotal)
    * (ENGINEERING.e2ePassed / ENGINEERING.e2eTotal));

  const total = report?.total ?? null;
  const runtimeTotal = report?.runtime_total ?? null;
  const totalColor = total == null ? "var(--ink-3)" : scoreColor(total);
  const scores = report?.scores ?? {};
  const missing = report?.missing ?? [];
  const claims = report?.trace.claims;
  const ext = report?.offline;
  const modes = report?.samples?.modes ?? {};
  const parse = report?.parse;
  const dedup = report?.dedup;
  const perf = report?.qa_perf;

  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 1020 }}>
        <h1>健康中心
          <span className="chip accent">结果健康 · 生产样本 · 公式透明</span>
        </h1>

        {failed && (
          <EmptyStateInline title="健康报告不可用" desc="后端聚合端点未响应——请确认 API 已启动后刷新。" />
        )}

        {report && (
          <>
            {/* 总分区：运行时健康分；工程快照不计分 */}
            <div className="h-total card-in" data-testid="health-total">
              <div className="h-total-num num" style={{ color: totalColor }}
                data-testid="health-total-num">
                {total == null ? "—" : (total * 100).toFixed(1)}
              </div>
              <div className="h-total-side">
                <b>
                  运行时健康分
                  {report.cap_applied ? "（外部校准封顶）" : ""}
                </b>
                <p className="kv">
                  = 7 个固定权重子分加权（缺失按 0 计、不重归一化）·
                  生产轨迹窗口 {report.window.days} 天 / n={report.window.traces} 条 ·
                  外部校准 {ext?.available
                    ? `${ext.gates_passed}/${ext.gates_total} gates`
                    : "暂无离线报告"}
                  {report.cap_applied && report.cap != null
                    ? ` → 总分封顶 ${(report.cap * 100).toFixed(0)}` : ""}。
                </p>
                <p className="kv num muted">
                  加权原始 {runtimeTotal != null ? (runtimeTotal * 100).toFixed(1) : "—"} ·
                  缺失 {missing.length} 项 · 工程快照见下方（不计分）
                </p>
              </div>
              <div className="h-total-bar" aria-hidden>
                {ORDER.map((k) => {
                  const s = scores[k];
                  const w = s?.weight ?? report.weights[k] ?? 0;
                  const color = s ? scoreColor(s.value)
                    : "var(--line)";
                  return <span key={k} style={{ width: `${w * 100}%`, background: color }}
                    title={`${SUB_TITLES[k]} ${s ? (s.value * 100).toFixed(1) : "缺失"}`} />;
                })}
              </div>
            </div>

            {report.insufficient && (
              <div className="card card-in" data-testid="health-insufficient"
                style={{ marginBottom: "var(--space-4)" }}>
                <b>样本不足，不出总分</b>
                <p className="kv">{report.insufficient_reason}</p>
              </div>
            )}

            {/* 7 个运行时子分 + 工程快照（不计分） */}
            <div className="h-grid">
              {ORDER.map((k) => (
                <ScoreCard key={k} name={SUB_TITLES[k]} s={scores[k]}
                  missing={missing.includes(k)} />
              ))}
              <div className="card h-score-card card-in" data-testid="health-score-engineering">
                <div className="h-score-head">
                  <b>工程快照</b>
                  <span className="chip num" title="离线门禁快照，仅作参考，不并入运行时健康分">
                    不计分
                  </span>
                </div>
                <div className="h-score-val num" style={{ color: scoreColor(eng) }}>
                  {(eng * 100).toFixed(1)}
                </div>
                <p className="h-formula kv">
                  pytest {ENGINEERING.pytestPassed}/{ENGINEERING.pytestTotal} ×
                  e2e {ENGINEERING.e2ePassed}/{ENGINEERING.e2eTotal}
                  {ENGINEERING.e2eSkipped > 0 ? `（另 ${ENGINEERING.e2eSkipped} 条数据条件跳过）` : ""}
                  （证据 {ENGINEERING.date} · commit {ENGINEERING.commit}）
                </p>
              </div>
            </div>

            {/* 模块明细：原始计数 */}
            <div className="card card-in" data-testid="health-detail">
              <h2>模块明细（原始计数）</h2>
              <div className="h-detail-grid">
                <div className="h-detail-block">
                  <b>样本与口径</b>
                  <p className="kv num">
                    生产轨迹 {report.samples.traces} 条（failed {report.samples.failed}）·
                    有论断 {report.samples.answered} / 无答案 {report.samples.no_answer}
                  </p>
                  <p className="kv num">
                    模式 {Object.entries(modes).map(([k, v]) =>
                      `${MODE_LABELS[k] ?? k} ${v}`).join(" · ") || "暂无"}
                  </p>
                  <p className="kv num">
                    任务 {report.samples.jobs} 个（失败 {report.samples.jobs_failed}）·
                    LLM 调用 {report.samples.llm_calls} 次（错误 {report.samples.llm_errors}，
                    {pct(report.samples.llm_error_rate)}）
                  </p>
                </div>

                <div className="h-detail-block">
                  <b>溯源 · ClaimTrace 四态分布</b>
                  {claims && (
                    <>
                      <div className="h-bars">
                        {CLAIM_BARS.map(([k, label, color]) => {
                          const total4 = Object.values(claims).reduce((a, b) => a + b, 0) || 1;
                          return (
                            <div key={k} className="h-bar-row" title={`${label} ${claims[k]}`}>
                              <span className="kv" style={{ width: 76 }}>{label} {k}</span>
                              <span className="h-bar-track">
                                <i style={{ width: `${(claims[k] / total4) * 100}%`,
                                  background: color }} />
                              </span>
                              <span className="kv num" style={{ width: 30, textAlign: "right" }}>
                                {claims[k]}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                      <p className="kv muted">
                        判定 {claims.supported + claims.needs_check + claims.unsupported} 条 ·
                        absent {claims.absent} 条为诚实负向，不计失败
                      </p>
                    </>
                  )}
                </div>

                <div className="h-detail-block">
                  <b>检索 · Verifier 与补检索</b>
                  <p className="kv num">
                    通过 {report.trace.verifier_pass} · 打回 {report.trace.verifier_fail} ·
                    计划 {report.trace.plans} 轮（补检索 {report.trace.replans}）
                  </p>
                  <p className="kv num">
                    离线 recall@5 {ext?.recall_at5 ?? "—"} · mrr@5 {ext?.mrr_at5 ?? "—"}
                  </p>
                </div>

                <div className="h-detail-block">
                  <b>性能 · 问答 LLM 延迟（窗口内）</b>
                  <p className="kv num">
                    p50 {perf?.p50_ms ?? "—"}ms · p95 {perf?.p95_ms ?? "—"}ms ·
                    均值 {perf?.avg_latency_ms ?? "—"}ms · n={perf?.count ?? 0} ·
                    失败率 {pct(perf?.error_rate ?? null)}
                  </p>
                </div>

                <div className="h-detail-block">
                  <b>解析 · DocIR 质量</b>
                  <p className="kv num">
                    页数加权 {pct(parse?.page_weighted)} ·
                    质量元数据覆盖 {parse?.papers_with_quality ?? 0}/{parse?.papers_total ?? 0} 篇 ·
                    文本覆盖 {pct(parse?.mean_text_coverage)} · ref 解析 {pct(parse?.mean_ref_parse)}
                  </p>
                  <p className="kv num">
                    扫描件 {parse?.scanned_papers ?? 0} 篇 · 解析失败 {parse?.parse_fail ?? 0} 篇 ·
                    总页数 {parse?.page_total ?? 0}
                  </p>
                </div>

                <div className="h-detail-block">
                  <b>数据卫生 · 去重与解析健康</b>
                  <p className="kv num">
                    去重预警 {dedup?.total ?? 0} 条（待决策 {dedup?.pending ?? 0} ·
                    陈旧 {dedup?.stale_pending ?? 0} · 覆盖 {dedup?.covered ?? 0} ·
                    保留 {dedup?.kept ?? 0} · 并存 {dedup?.coexisted ?? 0}）
                  </p>
                  <p className="kv muted">
                    新鲜 pending 视为正常待办；超过窗口未决策才扣分。
                  </p>
                </div>

                <div className="h-detail-block">
                  <b>外部校准 · 最新离线金标</b>
                  {ext?.available ? (
                    <>
                      <p className="kv num">
                        {ext.round}（{ext.date}）· gates {ext.gates_passed}/{ext.gates_total} ·
                        失败项 {ext.gates_failed?.length ? ext.gates_failed.join(" / ") : "无"}
                      </p>
                      <p className="kv num">
                        幻觉率 {ext.hallucination_rate} · supported {pct(ext.supported_ratio)} ·
                        锚点精度 {pct(ext.anchor_precision)} · recall@5 {ext.recall_at5} ·
                        拒答准确率 {pct(ext.refusal_accuracy)}
                      </p>
                    </>
                  ) : (
                    <p className="kv muted">未找到 docs/acceptance 下最近一轮 eval_gold.json。</p>
                  )}
                  <p className="kv muted">
                    未全过时总分封顶 90；外部校准不替代运行时指标，只防止内部指标掩盖已知回归。
                  </p>
                </div>
              </div>
            </div>

            <p className="kv muted" style={{ marginTop: "var(--space-4)" }}>
              口径说明：最近 {report.window.days} 天本机运行轨迹（排除
              kappa_/gold_/bench_/e2e_ 等评测/测试前缀）；失败进可靠性、无答案进回答覆盖，
              不做分母缩小；缺失子分按 0 计且不重归一化；外部金标 gates 未全过时总分封顶 90；
              工程快照来自 evidence.ts，不计入健康分。过程级审计见
              <Link href="/audit" style={{ color: "var(--accent)" }}>透明度中心</Link>。
            </p>
          </>
        )}
      </div>
    </div>
  );
}

function EmptyStateInline({ title, desc }: { title: string; desc: string }) {
  return (
    <div className="card" style={{ textAlign: "center", padding: "var(--space-6)" }}>
      <b>{title}</b>
      <p className="kv">{desc}</p>
    </div>
  );
}
