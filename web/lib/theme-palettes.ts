/**
 * 画布图表调色板（v1.3，design-tokens.md 使用规则 6）：
 * G6 / ECharts 画布不走 CSS 变量，按主题独立维护。
 * 组件统一经 canvasTheme(useShell().theme) 取用；新增皮肤必须在此补板。
 * 语义契约（各主题一致）：community=社区色环，edgeStrong/Mid/Weak=边权三档，
 * cornerBadge/cornerStroke=基石徽章/描边，visualMap=3D 散点渐进色。
 */
import type { Theme } from "@/components/ShellContext";

export interface CanvasTheme {
  community: string[];
  labelBg: string;
  edgeStrong: string;
  edgeMid: string;
  edgeWeak: string;
  cornerBadge: string;
  cornerStroke: string;
  peripheralStroke: string;
  nodeStroke: string;
  visualMap: string[];
}

const CANVAS: Record<Theme, CanvasTheme> = {
  classic: {
    community: ["#5B8FF9", "#61DDAA", "#F6BD16", "#7262fd", "#78D3F8", "#9661BC", "#F6903D", "#008685"],
    labelBg: "rgba(23, 22, 26, 0.78)",
    edgeStrong: "#2563eb", edgeMid: "#94a3b8", edgeWeak: "#d4d4d8",
    cornerBadge: "#ff5f3c", cornerStroke: "#cc0000", peripheralStroke: "#00aa77",
    nodeStroke: "#ffffff",
    visualMap: ["#5B8FF9", "#61DDAA", "#F6BD16", "#E5484D"],
  },
  majorelle: {
    community: ["#4a4ae8", "#3f9d63", "#e8b33c", "#c96c9a", "#54b8d6", "#8f6fd0", "#d97f4a", "#2f8d84"],
    labelBg: "rgba(51, 38, 26, 0.8)",
    edgeStrong: "#3232c8", edgeMid: "#a3947e", edgeWeak: "#e7d9bf",
    cornerBadge: "#c96f4a", cornerStroke: "#a84e2e", peripheralStroke: "#2f6440",
    nodeStroke: "#fffaf1",
    visualMap: ["#4a4ae8", "#3f9d63", "#e8b33c", "#a84e2e"],
  },
  wuxia: {
    community: ["#e06448", "#d9a93c", "#4fa97c", "#ede6d6", "#7a94c4", "#c47ab8", "#8a9a5b", "#5bbcc0"],
    labelBg: "rgba(22, 19, 15, 0.85)",          // 墨 chip 深底，标签白字不变
    edgeStrong: "#e06448", edgeMid: "#a89c86", edgeWeak: "#383026",
    cornerBadge: "#e06448", cornerStroke: "#c0402b", peripheralStroke: "#4fa97c",
    nodeStroke: "#1f1b16",
    visualMap: ["#ede6d6", "#d9a93c", "#e06448", "#a03434"],   // 月白→鎏金→朱砂→胭脂
  },
  riso: {
    community: ["#2233c9", "#ff5f3c", "#ffd23f", "#1a2694", "#e64a26", "#5a564e", "#008685", "#d23a2e"],
    labelBg: "rgba(23, 22, 26, 0.85)",
    edgeStrong: "#2233c9", edgeMid: "#c9b995", edgeWeak: "#e0d3b8",
    cornerBadge: "#ff5f3c", cornerStroke: "#d23a2e", peripheralStroke: "#2233c9",
    nodeStroke: "#faf1df",
    visualMap: ["#2233c9", "#ff5f3c", "#ffd23f", "#d23a2e"],
  },
};

export function canvasTheme(t: Theme): CanvasTheme {
  return CANVAS[t] ?? CANVAS.classic;
}
