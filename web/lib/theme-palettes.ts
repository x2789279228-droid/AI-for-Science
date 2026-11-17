/**
 * 画布图表调色板（v1.6 起降级为 re-export shim）：
 * 色板真身已收敛至 lib/theme-registry.ts（design-tokens.md 使用规则 6/7，
 * 含 v1.6 新增的 text/textSoft/axisLine/splitLine 深色适配四件套）。
 * 本文件仅为既有 import 路径（graph/copilot/Workbench）保留，请勿在此新增定义。
 */
export { canvasTheme } from "./theme-registry";
export type { CanvasTheme } from "./theme-registry";
