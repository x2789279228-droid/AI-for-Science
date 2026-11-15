"use client";

/**
 * 全局壳状态(AppShell 提供,页面消费):
 * - immersive 沉浸模式:隐藏侧边栏(大屏畅想),Esc 退出,右下浮动退出胶囊由 AppShell 渲染
 * - collapsed 侧边栏折叠(localStorage 持久化)
 * - theme 皮肤切换:"classic"(默认)/"majorelle"(马拉喀什)/"riso"(品牌层),
 *   写 html[data-theme] + localStorage;外观面板(AppearancePanel)消费 THEMES 渲染卡片
 * - introDone 品牌动画结束信号(导航 stagger 入场的触发器)
 * - docMode 双模式:"scholar"(专业模式,学术文献)|"general"(普通模式,任意 PDF 文档),
 *   localStorage 持久化;决定导航项、文献列表/选文数据源与上传归属(见 AppShell)
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

export type Theme = "classic" | "riso" | "majorelle" | "wuxia";

/** 双模式：scholar ↔ 后端 doc_type=paper；general ↔ doc_type=general */
export type DocMode = "scholar" | "general";

/** 皮肤元数据(外观面板唯一取值依据;swatch = 主色/点缀色/底色) */
export const THEMES: { id: Theme; label: string; en: string; desc: string; swatch: [string, string, string] }[] = [
  { id: "classic", label: "经典浅蓝", en: "CLASSIC", desc: "默认工作皮肤 · 品牌蓝 × 浅蓝天", swatch: ["#2456e6", "#38bdf8", "#f7f8fa"] },
  { id: "majorelle", label: "蓝城花园", en: "MAJORELLE", desc: "马拉喀什 · 马约尔蓝 × 藏红花黄", swatch: ["#4a4ae8", "#e8b33c", "#f6ecda"] },
  { id: "wuxia", label: "玄墨夜行", en: "WUXIA", desc: "水墨江湖 · 玄墨 × 月白 × 鎏金朱砂", swatch: ["#e06448", "#d9a93c", "#16130f"] },
  { id: "riso", label: "印刷海报", en: "RISO", desc: "品牌层 · Klein 蓝 × 国际橙", swatch: ["#2233c9", "#ff5f3c", "#faf1df"] },
];

export const isTheme = (v: string | null | undefined): v is Theme =>
  v === "classic" || v === "riso" || v === "majorelle" || v === "wuxia";

export const isDocMode = (v: string | null | undefined): v is DocMode =>
  v === "scholar" || v === "general";

interface ShellCtx {
  immersive: boolean;
  setImmersive: (v: boolean) => void;
  collapsed: boolean;
  toggleCollapsed: () => void;
  theme: Theme;
  setTheme: (t: Theme) => void;
  introDone: boolean;
  setIntroDone: () => void;
  docMode: DocMode;
  setDocMode: (m: DocMode) => void;
}

const Ctx = createContext<ShellCtx | null>(null);

const applyTheme = (t: Theme) => {
  if (t === "classic") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
};

export function ShellProvider({ children }: { children: React.ReactNode }) {
  const [immersive, setImmersiveState] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [theme, setThemeState] = useState<Theme>("classic");
  const [introDone, setIntroDoneState] = useState(false);
  const [docMode, setDocModeState] = useState<DocMode>("scholar");

  // 持久化状态挂载后读取,避免 SSR 水合不一致(防闪烁首帧由 layout 内联脚本负责)。
  // ?theme=classic|riso|majorelle 查询参数强制覆盖(答辩截图通道,优先于 localStorage,不落盘)
  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("theme");
    try { setCollapsed(localStorage.getItem("meridian:nav-collapsed") === "1"); } catch { /* 忽略 */ }
    try {
      const savedMode = localStorage.getItem("meridian:doc-mode");
      if (isDocMode(savedMode)) setDocModeState(savedMode);
    } catch { /* 隐私模式忽略 */ }
    if (isTheme(q)) { setThemeState(q); applyTheme(q); return; }
    try {
      const saved = localStorage.getItem("meridian:theme");
      if (isTheme(saved) && saved !== "classic") {
        setThemeState(saved); applyTheme(saved);
      }
    } catch { /* 隐私模式忽略 */ }
  }, []);

  const setImmersive = useCallback((v: boolean) => setImmersiveState(v), []);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((v) => {
      const nv = !v;
      try { localStorage.setItem("meridian:nav-collapsed", nv ? "1" : "0"); } catch { /* 忽略 */ }
      return nv;
    });
  }, []);

  const setTheme = useCallback((t: Theme) => {
    setThemeState(t);
    applyTheme(t);
    try { localStorage.setItem("meridian:theme", t); } catch { /* 忽略 */ }
  }, []);

  const setDocMode = useCallback((m: DocMode) => {
    setDocModeState(m);
    try { localStorage.setItem("meridian:doc-mode", m); } catch { /* 忽略 */ }
  }, []);

  const setIntroDone = useCallback(() => setIntroDoneState(true), []);

  // Esc 退出沉浸(与 reader 页内 Esc 处理器共存:它只负责关浮窗,互不干扰)
  useEffect(() => {
    if (!immersive) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setImmersiveState(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [immersive]);

  const value = useMemo(() => ({
    immersive, setImmersive, collapsed, toggleCollapsed,
    theme, setTheme, introDone, setIntroDone, docMode, setDocMode,
  }), [immersive, setImmersive, collapsed, toggleCollapsed, theme, setTheme, introDone, setIntroDone, docMode, setDocMode]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useShell(): ShellCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useShell 必须在 ShellProvider 内使用");
  return ctx;
}
