"use client";

/**
 * 健康中心(/health,§健康中心方案):把"系统能抗幻觉/可溯源/检索充分"等抽象宣称
 * 转化为具体可审计的数据——五个子分全部由运行时数据计算,公式与原始计数随卡片展示,
 * 评分本身可审计。工程健康(pytest/e2e 离线门禁)为前端静态注入(手注日期,权重 15%)。
 * 与 /audit 分工:audit=过程审计(Prompt/token),health=结果健康(质量指标+健康分)。
 */
import { useEffect, useState } from "react";
import Link from "next/link";
import { fetchHealthReport, HealthReport, HealthScore } from "@/lib/api";

/** 工程健康(静态手注,与 judge 页/README 同源;最近核对 2026-11-11)。 */
const ENGINEERING = {
  pytestPassed: 284, pytestTotal: 286,   // 2 例 tracker 环境性失败为预存(见 checklist)
  e2ePassed: 14, e2eTotal: 14,
  date: "2026-11-11",
};
const ENGINEERING_WEIGHT = 0.15;

const SUB_TITLES: Record<string, string> = {
  trace_trust: "溯源可信",
  hallucination_ctrl: "幻觉抑制",
  retrieval: "检索充分",
  parse: "解析覆盖",
  performance: "性能",
  engineering: "工程健康",
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

function ScoreCard({ name, s, staticOverride }: {
  name: string; s: HealthScore; staticOverride?: React.ReactNode;
}) {
  const v = s.value;
  const color = v != null ? scoreColor(v) : "var(--ink-3)";
  return (
    <div className="card h-score-card card-in">
      <div className="h-score-head">
        <b>{name}</b>
        <span className="chip num" title="该子分在综合健康分中的权重">
          权重 {Math.round(s.weight * 100)}%
        </span>
      </div>
      <div className="h-score-val num" style={{ color }}>
        {v == null ? "暂无数据" : `${(v * 100).toFixed(1)}`}
      </div>
      {staticOverride}
      <p className="h-formula kv" title="评分公式与原始计数——评分本身可审计">
        {s.formula}
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

  // 工程健康(静态):pytest 通过率 × e2e 通过率
  const eng = ((ENGINEERING.pytestPassed / ENGINEERING.pytestTotal)
    * (ENGINEERING.e2ePassed / ENGINEERING.e2eTotal));

  // 总分 = 后端五子分加权(权重已归一)× 0.85 + 工程健康 × 0.15
  const runtimeTotal = report?.total ?? null;
  const total = runtimeTotal == null ? null : runtimeTotal * 0.85 + eng * ENGINEERING_WEIGHT;
  const totalColor = total == null ? "var(--ink-3)" : scoreColor(total);

  const scores = report?.scores ?? {};
  const order = ["trace_trust", "hallucination_ctrl", "retrieval", "parse", "performance"];
  const claims = report?.trace.claims;

  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 1020 }}>
        <h1>健康中心
          <span className="chip accent">结果健康 · 评分公式透明</span>
        </h1>

        {failed && (
          <EmptyStateInline title="健康报告不可用" desc="后端聚合端点未响应——请确认 API 已启动后刷新。" />
        )}

        {report && (
          <>
            {/* 总分区:大数字 + 工程并入说明 */}
            <div className="h-total card-in" data-testid="health-total">
              <div className="h-total-num num" style={{ color: totalColor }}>
                {total == null ? "—" : (total * 100).toFixed(1)}
              </div>
              <div className="h-total-side">
                <b>综合健康分</b>
                <p className="kv">
                  = 运行时五子分加权（权重已归一）× 85% + 工程健康（手注静态）× 15%。
                  每个子分的公式与原始计数见下方卡片——<b>评分本身可审计</b>。
                </p>
                <p className="kv num muted">
                  运行时 {runtimeTotal != null ? (runtimeTotal * 100).toFixed(1) : "—"} ·
                  工程 {(eng * 100).toFixed(1)}
                </p>
              </div>
              <div className="h-total-bar" aria-hidden>
                {order.map((k) => {
                  const s = scores[k];
                  if (!s) return null;
                  return (
                    <span key={k} style={{ width: `${s.weight * 85}%`,
                      background: scoreColor(s.value) }} title={SUB_TITLES[k]} />
                  );
                })}
                <span style={{ width: "15%", background: "var(--ink-3)" }} title="工程健康" />
              </div>
            </div>

            {/* 五子分卡 */}
            <div className="h-grid">
              {order.map((k, i) => (
                <ScoreCard key={k} name={SUB_TITLES[k] ?? k}
                  s={scores[k] ?? { value: null, weight: 0.15, formula: "暂无数据(该指标当前无样本)" }} />
              ))}
              <div className="card h-score-card card-in">
                <div className="h-score-head">
                  <b>工程健康</b>
                  <span className="chip num" title="静态手注,不参与运行时聚合">权重 15%</span>
                </div>
                <div className="h-score-val num" style={{ color: scoreColor(eng) }}>
                  {(eng * 100).toFixed(1)}
                </div>
                <p className="h-formula kv">
                  pytest {ENGINEERING.pytestPassed}/{ENGINEERING.pytestTotal} ×
                  e2e {ENGINEERING.e2ePassed}/{ENGINEERING.e2eTotal}
                  （离线门禁 · 手注 {ENGINEERING.date} 核对,与 judge 页/README 同源）
                </p>
              </div>
            </div>

            {/* 模块明细:原始数据下钻 */}
            <div className="card card-in" data-testid="health-detail">
              <h2>模块明细（原始计数）</h2>
              <div className="h-detail-grid">
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
                        样本 {report.trace.samples} 条 trace · absent {claims.absent} 条为诚实负向,不计失败
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
                </div>
                <div className="h-detail-block">
                  <b>性能 · 问答延迟</b>
                  <p className="kv num">
                    平均 {report.qa_perf.avg_latency_ms}ms · 样本 {report.qa_perf.count} 条 ·
                    失败率 {report.qa_perf.error_rate == null ? "无数据"
                      : `${(report.qa_perf.error_rate * 100).toFixed(1)}%`}
                  </p>
                </div>
                <div className="h-detail-block">
                  <b>解析 · DocIR 质量</b>
                  <p className="kv num">
                    文本覆盖 {pct(report.parse?.mean_text_coverage)} ·
                    参考文献解析率 {pct(report.parse?.mean_ref_parse)} ·
                    覆盖 {report.parse?.papers_with_quality ?? 0}/{report.parse?.papers_total ?? 0} 篇 ·
                    扫描件 {report.parse?.scanned_papers ?? 0} 篇
                  </p>
                </div>
                <div className="h-detail-block">
                  <b>版本去重 · 预警三态</b>
                  <p className="kv num">
                    预警 {report.dedup.total} 条（待决策 {report.dedup.pending} ·
                    覆盖 {report.dedup.covered} · 保留 {report.dedup.kept} ·
                    并存 {report.dedup.coexisted}）
                  </p>
                </div>
                <div className="h-detail-block">
                  <b>离线金标评测对照 {report.offline.available
                    ? <span className="chip ok" style={{ fontSize: 10 }}>{report.offline.round}</span>
                    : <span className="chip" style={{ fontSize: 10 }}>暂无报告</span>}</b>
                  {report.offline.available ? (
                    <p className="kv num">
                      幻觉率 {report.offline.hallucination_rate} · supported
                      {" "}{pct(report.offline.supported_ratio)} · 锚点精度
                      {" "}{pct(report.offline.anchor_precision)} · recall@5
                      {" "}{report.offline.recall_at5} · 拒答准确率
                      {" "}{pct(report.offline.refusal_accuracy)}（{report.offline.date}）
                    </p>
                  ) : (
                    <p className="kv muted">未找到 docs/acceptance 下最近一轮 eval_gold.json。</p>
                  )}
                  <Link className="chip" href="/chat" style={{ marginTop: 6 }}>去产生新的溯源样本 →</Link>
                </div>
              </div>
            </div>

            <p className="kv muted" style={{ marginTop: "var(--space-4)" }}>
              口径说明:「溯源可信」分母不含 absent(诚实负向「未涉及」不是失败);
              性能分 3 秒内满分、线性衰减至 15 秒;工程健康为离线门禁手注静态数据。
              过程级审计(Prompt/token 计量)见<Link href="/audit" style={{ color: "var(--accent)" }}>透明度中心</Link>。
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
