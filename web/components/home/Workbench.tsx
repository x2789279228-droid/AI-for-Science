"use client";

/**
 * 首页工作台（前端改造方案 v1.0 §3）：数据带 + Pulse 双卡。
 * - WorkbenchStats：papers/health/graph 计数一行 mono 数据带（下分隔线，非卡片）
 * - WorkbenchPulse：左卡最近问答（/api/qa/recent）+ 右卡引用图谱只读缩略（G6 关交互）
 * 三区独立加载、失败静默降级为空态/插画（首页不是操作台，不弹错误横幅）。
 */
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Graph } from "@antv/g6";
import {
  fetchGraph, fetchHealth, fetchQaRecent, FullGraph, Health, QaRecentItem,
} from "@/lib/api";
import { RisoIllust } from "@/components/Illustrations";

function relTimeClient(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "刚刚";
  if (s < 3600) return `${Math.round(s / 60)}分钟前`;
  if (s < 86400) return `${Math.round(s / 3600)}小时前`;
  return `${Math.round(s / 86400)}天前`;
}

/* ================= 数据带 ================= */

export function WorkbenchStats({ paperCount }: { paperCount: number }) {
  const [stats, setStats] = useState<{ edges: number; external: number; q: number | null } | null>(
    () => {
      try {
        const raw = sessionStorage.getItem("meridian:stats");
        return raw ? JSON.parse(raw) : null;
      } catch { return null; }
    });
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    fetchHealth().then(setHealth).catch(() => {});
    // graph 计数拉一次并缓存(sessionStorage)——侧栏 refs 行也读这份缓存
    fetchGraph().then((g: FullGraph) => {
      const s = { edges: g.edge_count, external: g.external_count, q: g.modularity ?? null };
      setStats(s);
      try { sessionStorage.setItem("meridian:stats", JSON.stringify(s)); } catch { /* 忽略 */ }
    }).catch(() => {});
  }, []);

  const llm = health?.llm;
  return (
    <div className="wb-stats" data-testid="wb-stats">
      <span className="wb-stat"><b className="num">{paperCount}</b>篇文献</span>
      <span className="wb-sep">·</span>
      <span className="wb-stat"><b className="num">{stats?.edges ?? "—"}</b>库内引用边</span>
      <span className="wb-sep">·</span>
      <span className="wb-stat"><b className="num">{stats?.external ?? "—"}</b>外部引用追踪</span>
      <span style={{ flex: 1 }} />
      <span className={`wb-llm ${llm?.available ? "ok" : "warn"}`}>
        {llm
          ? (llm.available
            ? <>LLM 就绪({llm.model})</>
            : <>LLM 不可用 · 抽取式证据模式</>)
          : "后端连接中…"}
      </span>
      {health && (
        <span className="wb-llm muted">Dense {health.dense ? "开" : "关"}</span>
      )}
    </div>
  );
}

/* ================= Pulse 双卡 ================= */

function ClaimsMini({ c }: { c: QaRecentItem["claims"] }) {
  const parts: [string, number, string][] = [
    ["证", c.supported, "var(--ok)"],
    ["疑", c.needs_check, "var(--warn)"],
    ["缺", c.absent, "var(--ink-3)"],
  ];
  return (
    <span className="wb-claims num">
      {parts.filter(([, n]) => n > 0).map(([label, n, color]) => (
        <span key={label} style={{ color }} title={label === "证" ? "已验证论断"
          : label === "疑" ? "需核对论断" : "未涉及（诚实负向）"}>
          {label}{n}
        </span>
      ))}
      {c.unsupported > 0 && <span style={{ color: "var(--danger)" }}>✗{c.unsupported}</span>}
    </span>
  );
}

export function WorkbenchPulse() {
  const [recent, setRecent] = useState<QaRecentItem[] | null>(null); // null=加载中
  useEffect(() => {
    fetchQaRecent(3).then(setRecent).catch(() => setRecent([]));   // 失败静默降级为空态
  }, []);
  return (
    <div className="wb-pulse">
      {/* 左卡：最近溯源问答 */}
      <div className="card wb-card" data-testid="wb-qa">
        <div className="wb-card-head">
          <h2>最近溯源问答</h2>
          <Link className="chip" href="/chat">去提问 →</Link>
        </div>
        {recent === null && <div className="skeleton" style={{ height: 96 }} />}
        {recent !== null && recent.length === 0 && (
          <div className="wb-empty">
            <p>还没有问答——去 Chat 提第一个问题，每个论断都带证据。</p>
            <Link className="btn" href="/chat">开始提问 →</Link>
          </div>
        )}
        {recent !== null && recent.length > 0 && (
          <ul className="wb-qa-list">
            {recent.map((r) => (
              <li key={r.thread_id}>
                <Link href="/chat" className="wb-qa-row" title={r.question}>
                  <span className="wb-qa-q">{r.question || "（无问题文本）"}</span>
                  <span className="wb-qa-meta">
                    <ClaimsMini c={r.claims} />
                    {r.elapsed != null && <span className="num">{r.elapsed}s</span>}
                    <span>{relTimeClient(r.ts)}</span>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 右卡：引用图谱只读缩略 */}
      <GraphThumbCard />
    </div>
  );
}

/** G6 只读缩略卡：关交互/无标签/无动画;失败降级插画。 */
function GraphThumbCard() {
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [state, setState] = useState<"loading" | "ok" | "fail">("loading");
  const [meta, setMeta] = useState<{ nodes: number; edges: number; q: number | null } | null>(null);

  useEffect(() => {
    let dead = false;
    let inst: Graph | null = null;
    fetchGraph().then((g) => {
      if (dead || !boxRef.current) return;
      setMeta({ nodes: g.nodes.length, edges: g.edge_count, q: g.modularity ?? null });
      inst = new Graph({
        container: boxRef.current,
        autoFit: "view",
        padding: 8,
        animation: false,                       // 缩略图关动画
        data: {
          nodes: g.nodes.map((n) => ({ id: n.paper_id, data: { ...n } })),
          edges: g.edges.map((e, i) => ({ id: `e${i}`, source: e.source, target: e.target })),
        },
        node: {
          style: {
            size: (d) => {
              const n = (d as { data?: { pagerank?: number } }).data;
              return 6 + Math.min(16, (n?.pagerank ?? 0) * 90);
            },
            fill: "#5B8FF9",
            labelText: "",                       // 缩略图隐藏标签
          },
        },
        edge: { style: { stroke: "#94a3b8", lineWidth: 0.8, endArrow: false } },
        layout: { type: "d3-force", linkDistance: 60, nodeStrength: -180 },
        behaviors: [],                           // 只读
      });
      inst.render().then(() => setState("ok")).catch(() => setState("fail"));
    }).catch(() => !dead && setState("fail"));
    return () => { dead = true; inst?.destroy(); };
  }, []);

  return (
    <Link className="card wb-card wb-graph" href="/graph" data-testid="wb-graph">
      <div className="wb-card-head">
        <h2>引用图谱速览</h2>
        <span className="chip num" style={{ fontSize: 10 }}>
          {meta ? `${meta.nodes} 节点 · ${meta.edges} 边${meta.q != null ? ` · Q=${meta.q.toFixed(4)}` : ""}` : "…"}
        </span>
      </div>
      <div className="wb-graph-stage">
        {state === "loading" && <div className="skeleton" style={{ height: 150 }} />}
        {state === "fail" && (
          <div className="wb-empty" style={{ flexDirection: "row", gap: 16, alignItems: "center" }}>
            <RisoIllust name="lighthouse" size={120} />
            <p className="kv">图谱暂不可用——去图谱页重试。</p>
          </div>
        )}
        <div ref={boxRef} style={{ width: "100%", height: 160, display: state === "ok" ? "block" : "none" }} />
      </div>
    </Link>
  );
}
