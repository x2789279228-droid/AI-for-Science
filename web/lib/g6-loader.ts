"use client";

/**
 * G6 惰性加载器（性能修复）：@antv/g6 约 500kB，此前被静态 import 进
 * 首页工作台与图谱页首包（/graph first-load 561kB）。改为首次需要时
 * 动态 import——G6 chunk 只在真正渲染画布的页面按需拉取，且全应用共享
 * 同一份模块 Promise（并发调用不重复下载）。
 * 类型引用用 `import type`（编译期擦除，零运行时成本）。
 */
let _mod: Promise<typeof import("@antv/g6")> | null = null;

export function loadG6(): Promise<typeof import("@antv/g6")> {
  _mod ??= import("@antv/g6");
  return _mod;
}
