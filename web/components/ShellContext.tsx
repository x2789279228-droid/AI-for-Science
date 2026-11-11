"use client";

/**
 * 全局壳状态(AppShell 提供,页面消费):
 * - immersive 沉浸模式:隐藏侧边栏(大屏畅想),Esc 退出,右下浮动退出胶囊由 AppShell 渲染
 * - collapsed 侧边栏折叠(localStorage 持久化)
 * - theme 皮肤切换:"classic"(默认)/"riso"(第二皮肤),写 html[data-theme] + localStorage
 * - introDone 品牌动画结束信号(导航 stagger 入场的触发器)
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

export type Theme = "classic" | "riso";

interface ShellCtx {
  immersive: boolean;
  setImmersive: (v: boolean) => void;
  collapsed: boolean;
  toggleCollapsed: () => void;
  theme: Theme;
  setTheme: (t: Theme) => void;
  introDone: boolean;
  setIntroDone: () => void;
}

const Ctx = createContext<ShellCtx | null>(null);

const applyTheme = (t: Theme) => {
  if (t === "riso") document.documentElement.dataset.theme = "riso";
  else delete document.documentElement.dataset.theme;
};

export function ShellProvider({ children }: { children: React.ReactNode }) {
  const [immersive, setImmersiveState] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [theme, setThemeState] = useState<Theme>("classic");
  const [introDone, setIntroDoneState] = useState(false);

  // 持久化状态挂载后读取,避免 SSR 水合不一致(防闪烁首帧由 layout 内联脚本负责)
  useEffect(() => {
    try {
      setCollapsed(localStorage.getItem("meridian:nav-collapsed") === "1");
      const t = localStorage.getItem("meridian:theme");
      if (t === "riso") { setThemeState("riso"); applyTheme("riso"); }
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
    theme, setTheme, introDone, setIntroDone,
  }), [immersive, setImmersive, collapsed, toggleCollapsed, theme, setTheme, introDone, setIntroDone]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useShell(): ShellCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useShell 必须在 ShellProvider 内使用");
  return ctx;
}
