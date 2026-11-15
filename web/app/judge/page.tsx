"use client";

/**
 * 评审导览（§7.7）：评委 5 分钟自助游览的静态地图。
 * 无后端依赖、无自动流程/自动播放；工程证据数字与 README/最新 checklist 手动同步。
 */
import Link from "next/link";
import Stamp from "@/components/Stamp";
import { EVIDENCE } from "@/lib/evidence";

const ROUTE: { title: string; desc: string; href: string; cta: string }[] = [
  { title: "1 · 文献库与版本去重", desc: "上传 PDF → 语义同族预警 → 三态决策 → 版本族书堆（翻面看其余版本）", href: "/", cta: "打开文献库" },
  { title: "2 · 溯源问答：E# 证据与 PDF bbox", desc: "空态点演示问题即提问；逐论断「证」章 + [E#] 锚点回跳原文红框", href: "/chat", cta: "进入溯源问答" },
  { title: "3 · 双语对照与同步滚动", desc: "五视图分段切换；双语双栏滚动同步，PDF↔译文双向锚定", href: "/reader", cta: "打开书架" },
  { title: "4 · 引用图谱：基石/边缘与社区", desc: "舞台化画布 + 社区凸包 + 标签词典零截断；点击节点看详情抽屉", href: "/graph", cta: "打开引用图谱" },
  { title: "5 · 领域综述与研究缝隙", desc: "Future Work 去重过滤：已失效/有进展 vs 真正值得做的方向", href: "/ideas", cta: "分析研究缝隙" },
  { title: "6 · 写作 Copilot 与 CSV 图表", desc: "图表/拓扑/大纲等 artifact 下发，图表带 provenance 出处", href: "/copilot", cta: "打开 Copilot" },
  { title: "7 · 检索发现与个性化推荐", desc: "arXiv+S2+OpenAlex 三源去重 → 追踪订阅 → 可解释推荐打分", href: "/discover", cta: "进入检索发现" },
  { title: "8 · 透明度中心：Prompt 审计", desc: "Prompt 版本化落库、token/延迟计量，全过程可审计", href: "/audit", cta: "打开透明度中心" },
];

/** 工程证据：唯一来源 web/lib/evidence.ts（scripts/write_evidence.py 生成）。 */
const EVIDENCE_ITEMS = [
  ["pytest", `${EVIDENCE.pytest.passed} 用例 · ${EVIDENCE.pytest.failed} 失败`],
  ["Playwright e2e", `${EVIDENCE.e2e.passed}/${EVIDENCE.e2e.passed + EVIDENCE.e2e.failed} 通过`
    + (EVIDENCE.e2e.skipped ? ` · ${EVIDENCE.e2e.skipped} 数据跳过` : "")],
  ["诚实评测 eval", `${EVIDENCE.eval.gatesPassed}/${EVIDENCE.eval.gatesTotal}`
    + (!EVIDENCE.eval.compareOk ? "（compare_ok=false 如实）" : "")],
  ["演示语料", `${EVIDENCE.corpus.papers} 篇 · ${EVIDENCE.corpus.references} refs`],
];

export default function JudgePage() {
  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 960 }}>
        <h1>评审导览
          <span className="chip accent">评委 5 分钟自助游览地图</span>
        </h1>

        <div className="card card-in" style={{ ["--i" as string]: 0 }}>
          <h2 style={{ display: "flex", alignItems: "center", gap: 8 }}>
            演示路线（8 步）
            <span className="kv muted">每步一句话 · 点击直达</span>
          </h2>
          <ol className="judge-route">
            {ROUTE.map((r) => (
              <li key={r.href} className="judge-step">
                <div>
                  <b>{r.title}</b>
                  <p className="kv">{r.desc}</p>
                </div>
                <Link className="btn ghost" href={r.href}>{r.cta} →</Link>
              </li>
            ))}
          </ol>
        </div>

        <div className="card card-in" style={{ ["--i" as string]: 1 }}>
          <h2 style={{ display: "flex", alignItems: "center", gap: 10 }}>
            工程证据 <Stamp kind="verified" size="sm" tip={`数字由 scripts/write_evidence.py 生成（${EVIDENCE.generatedAt}）`} />
          </h2>
          <div className="judge-evidence num">
            {EVIDENCE_ITEMS.map(([k, v]) => (
              <span key={k} className="judge-ev-item">{k} <b>{v}</b></span>
            ))}
          </div>
          <p className="kv muted" style={{ marginTop: "var(--space-2)" }}>
            以上数字与 README / 透明度中心同源（自动生成于 {EVIDENCE.generatedAt}，基线
            <code> {EVIDENCE.branch} @ {EVIDENCE.commit} </code>）；诚实边界见 README「边界与未完成」章节与功能索引树。
          </p>
        </div>

        <div className="card card-in" style={{ ["--i" as string]: 2 }}>
          <h2>诚实边界</h2>
          <p className="kv">
            本页不做强制流程、不做自动播放——评审按任意顺序自助游览。
            系统的如实声明（降级路径、open_unverified 诚实态、评测边界）集中在 README
            与 <Link href="/audit" style={{ color: "var(--accent)" }}>透明度中心</Link>，
            任何「未验证/需核对/未涉及」状态都会如实标注而非掩盖。
          </p>
        </div>

        <p className="kv num muted" style={{ marginTop: "var(--space-4)" }}>
          代码基线：{EVIDENCE.branch} @ {EVIDENCE.commit}（证据生成于 {EVIDENCE.generatedAt}；以 git log 为准）
        </p>
      </div>
    </div>
  );
}
