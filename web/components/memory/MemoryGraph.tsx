"use client";

/**
 * 模块七：记忆因果边图（G6 只读画布）。
 * 复用 g6-loader 惰性加载器 + canvasTheme 皮肤色板，按 graph 页样板实例化：
 * 节点=记忆事件（kind 着色 · importance 定尺寸），边=因果/时序关系（relation 标签，
 * refutes/supersedes 用警示色）。构造失败降级为关系列表（不空屏，如实提示）。
 */
import { useEffect, useRef, useState } from "react";
import type { MemoryEdge, MemoryEvent } from "@/lib/api";
import { loadG6 } from "@/lib/g6-loader";
import { canvasTheme } from "@/lib/theme-palettes";
import type { Theme } from "@/lib/theme-registry";

export const KIND_COLOR: Record<string, string> = {
  hypothesis: "#b5493a",        // accent 系
  experiment_result: "#2e7d5b", // ok 系
  finding: "#d9a55e",           // warn 系
  decision: "#5b6b9c",
  preference: "#8a7f6a",
  topic: "#5b6b7c",
};

export const KIND_LABEL: Record<string, string> = {
  hypothesis: "假设", experiment_result: "实验", finding: "结论",
  decision: "决定", preference: "偏好", topic: "话题",
};

const EDGE_COLOR: Record<string, string> = {
  refutes: "#b5493a", supersedes: "#8a5a1a", supports: "#2e7d5b",
};

export default function MemoryGraph({ events, edges, theme, height = 320 }: {
  events: MemoryEvent[];
  edges: MemoryEdge[];
  theme: Theme;
  height?: number;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!events.length || !containerRef.current) return;
    let disposed = false;
    let inst: { destroy?: () => void } | null = null;
    loadG6().then(({ Graph }) => {
      if (disposed || !containerRef.current) return;
      const pal = canvasTheme(theme);
      try {
        // dev StrictMode 双挂载/依赖变化会重复实例化：先清空容器防 canvas 叠加
        containerRef.current.innerHTML = "";
        const g6 = new Graph({
          container: containerRef.current,
          autoFit: "view",
          padding: 24,
          data: {
            nodes: events.map((e) => ({ id: e.id, data: { ...e } })),
            edges: edges.map((e, i) => ({ id: `e${i}`, source: e.src, target: e.dst, data: { ...e } })),
          },
          node: {
            style: {
              size: (d) => 14 + Math.max(0, ((d as { data?: MemoryEvent }).data?.importance ?? 5) - 5) * 2,
              fill: (d) => KIND_COLOR[(d as { data?: MemoryEvent }).data?.kind ?? ""] ?? pal.nodeStroke,
              labelText: (d) => {
                const e = (d as { data?: MemoryEvent }).data;
                return `${KIND_LABEL[e?.kind ?? ""] ?? e?.kind ?? ""} · ${String(e?.content ?? "").slice(0, 12)}`;
              },
              labelPlacement: "bottom",
              labelFontSize: 10,
              labelFill: pal.text,
              labelBackground: true,
              labelBackgroundFill: pal.labelBg,
              labelPadding: [1, 3],
              labelBackgroundRadius: 3,
            },
          },
          edge: {
            style: {
              stroke: (d) => {
                const rel = (d as { data?: MemoryEdge }).data?.relation ?? "";
                return EDGE_COLOR[rel] ?? pal.edgeMid;
              },
              lineWidth: (d) => 1 + Math.min(3, ((d as { data?: MemoryEdge }).data?.confirm_count ?? 1) - 1),
              labelText: (d) => (d as { data?: MemoryEdge }).data?.relation ?? "",
              labelFontSize: 9,
              labelFill: pal.textSoft,
              labelBackground: true,
              labelBackgroundFill: pal.labelBg,
              labelPadding: [0, 2],
              endArrow: true,
            },
          },
          layout: { type: "d3-force", linkDistance: 130, nodeStrength: -350, animation: false },
          behaviors: ["drag-canvas", "zoom-canvas", "drag-element"],
        });
        inst = g6 as unknown as { destroy?: () => void };
        void g6.render();
      } catch {
        setFailed(true);   // 构造失败 → 关系列表兜底（不空屏）
      }
    }).catch(() => setFailed(true));
    return () => {
      disposed = true;
      try { inst?.destroy?.(); } catch { /* 画布已卸载 */ }
    };
  }, [events, edges, theme]);

  if (!events.length) {
    return (
      <div className="kv muted" data-testid="memory-graph-empty">
        暂无记忆节点——随问答轮次自动积累（轮末异步抽取 + agent 主动写入）。
      </div>
    );
  }
  if (failed) {
    return (
      <div data-testid="memory-graph-fallback">
        <div className="kv muted">图引擎不可用，已降级为关系列表：</div>
        <ul className="kv">
          {edges.slice(0, 12).map((e) => (
            <li key={e.id}>
              {e.src.slice(0, 10)} —[{e.relation}×{e.confirm_count}]→ {e.dst.slice(0, 10)}
            </li>
          ))}
        </ul>
      </div>
    );
  }
  return <div ref={containerRef} style={{ width: "100%", height }} data-testid="memory-graph" />;
}
