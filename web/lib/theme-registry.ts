/**
 * 主题单一清单（v1.6，design-tokens.md 使用规则 7）：
 * Theme 类型 / THEMES 元数据 / isTheme / CANVAS 画布色板 / themeMermaid 映射
 * 只准定义于本文件；ShellContext 等消费方经 import 取用，禁止散落第二份皮肤枚举。
 * 新增皮肤三步：① 本清单补 THEMES + CANVAS + themeMermaid ② tokens.css 补 [data-theme] 块
 * ③ globals.css 补装饰层；④ app/layout.tsx 防闪烁内联脚本的字面量同步（水合前执行，
 * 无法 import TS，唯一豁免点）。
 */

/* ---------- 皮肤集合与元数据 ---------- */

export type Theme = "classic" | "riso" | "majorelle" | "wuxia";

/** 皮肤元数据(外观面板唯一取值依据;swatch = 主色/点缀色/底色) */
export const THEMES: { id: Theme; label: string; en: string; desc: string; swatch: [string, string, string] }[] = [
  { id: "classic", label: "经典浅蓝", en: "CLASSIC", desc: "默认工作皮肤 · 品牌蓝 × 浅蓝天", swatch: ["#2456e6", "#38bdf8", "#f7f8fa"] },
  { id: "majorelle", label: "蓝城花园", en: "MAJORELLE", desc: "马拉喀什 · 马约尔蓝 × 藏红花黄", swatch: ["#4a4ae8", "#e8b33c", "#f6ecda"] },
  { id: "wuxia", label: "玄墨夜行", en: "WUXIA", desc: "水墨江湖 · 玄墨 × 月白 × 鎏金朱砂", swatch: ["#e06448", "#d9a93c", "#16130f"] },
  { id: "riso", label: "印刷海报", en: "RISO", desc: "品牌层 · Klein 蓝 × 国际橙", swatch: ["#2233c9", "#ff5f3c", "#faf1df"] },
];

export const isTheme = (v: string | null | undefined): v is Theme =>
  v === "classic" || v === "riso" || v === "majorelle" || v === "wuxia";

/** mermaid 初始化主题映射（v1.4 深色适配条款 ①；消费于 copilot 页 MermaidPreview） */
export const themeMermaid: Record<Theme, "dark" | "neutral"> = {
  classic: "neutral",
  riso: "neutral",
  majorelle: "neutral",
  wuxia: "dark",
};

/* ---------- 画布色板（G6 / ECharts 专用，design-tokens 使用规则 6） ----------
 * 画布不走 CSS 变量，按主题独立维护。新增皮肤必须在此补板。
 * 语义契约（各主题一致）：community=社区色环，edgeStrong/Mid/Weak=边权三档，
 * cornerBadge/cornerStroke=基石徽章/描边，visualMap=3D 散点渐进色，
 * text/textSoft/axisLine/splitLine=图例与轴文字/轴线/分隔线（v1.6 深色适配四件套，
 * 深色皮肤靠这组字段保证图表可读）。 */

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
  text: string;
  textSoft: string;
  axisLine: string;
  splitLine: string;
}

const CANVAS: Record<Theme, CanvasTheme> = {
  classic: {
    community: ["#5B8FF9", "#61DDAA", "#F6BD16", "#7262fd", "#78D3F8", "#9661BC", "#F6903D", "#008685"],
    labelBg: "rgba(23, 22, 26, 0.78)",
    edgeStrong: "#2563eb", edgeMid: "#94a3b8", edgeWeak: "#d4d4d8",
    cornerBadge: "#ff5f3c", cornerStroke: "#cc0000", peripheralStroke: "#00aa77",
    nodeStroke: "#ffffff",
    visualMap: ["#5B8FF9", "#61DDAA", "#F6BD16", "#E5484D"],
    text: "#344054", textSoft: "#667085",
    axisLine: "#d0d5dd", splitLine: "#e4e7ec",
  },
  majorelle: {
    community: ["#4a4ae8", "#3f9d63", "#e8b33c", "#c96c9a", "#54b8d6", "#8f6fd0", "#d97f4a", "#2f8d84"],
    labelBg: "rgba(51, 38, 26, 0.8)",
    edgeStrong: "#3232c8", edgeMid: "#a3947e", edgeWeak: "#e7d9bf",
    cornerBadge: "#c96f4a", cornerStroke: "#a84e2e", peripheralStroke: "#2f6440",
    nodeStroke: "#fffaf1",
    visualMap: ["#4a4ae8", "#3f9d63", "#e8b33c", "#a84e2e"],
    text: "#7a6a56", textSoft: "#a3947e",          /* 椰枣褐文字阶（maj-ink-2/3） */
    axisLine: "#d4c19e", splitLine: "#e7d9bf",     /* 暖沙线（maj-line-strong/line） */
  },
  wuxia: {
    community: ["#e06448", "#d9a93c", "#4fa97c", "#ede6d6", "#7a94c4", "#c47ab8", "#8a9a5b", "#5bbcc0"],
    labelBg: "rgba(22, 19, 15, 0.85)",          // 墨 chip 深底，标签白字不变
    edgeStrong: "#e06448", edgeMid: "#a89c86", edgeWeak: "#383026",
    cornerBadge: "#e06448", cornerStroke: "#c0402b", peripheralStroke: "#4fa97c",
    nodeStroke: "#1f1b16",
    visualMap: ["#ede6d6", "#d9a93c", "#e06448", "#a03434"],   // 月白→鎏金→朱砂→胭脂
    text: "#a89c86", textSoft: "#7a6f5c",          /* 淡墨文字阶（wx-moon-2/3） */
    axisLine: "#4a4136", splitLine: "#383026",     /* 墨面线（wx-line-strong/line） */
  },
  riso: {
    community: ["#2233c9", "#ff5f3c", "#ffd23f", "#1a2694", "#e64a26", "#5a564e", "#008685", "#d23a2e"],
    labelBg: "rgba(23, 22, 26, 0.85)",
    edgeStrong: "#2233c9", edgeMid: "#c9b995", edgeWeak: "#e0d3b8",
    cornerBadge: "#ff5f3c", cornerStroke: "#d23a2e", peripheralStroke: "#2233c9",
    nodeStroke: "#faf1df",
    visualMap: ["#2233c9", "#ff5f3c", "#ffd23f", "#d23a2e"],
    text: "#5a564e", textSoft: "#938d80",          /* 暖灰文字阶（riso-warm-gray/3） */
    axisLine: "#c9b995", splitLine: "#e0d3b8",     /* 纸边（riso-line-strong/line） */
  },
};

export function canvasTheme(t: Theme): CanvasTheme {
  return CANVAS[t] ?? CANVAS.classic;
}
