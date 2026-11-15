"use client";

/**
 * 外观面板（v1.3）：侧边栏用户区 Palette 图标唤起的皮肤切换器。
 * 三皮肤卡片（迷你预览缩略 + 配色条），点击即换实时生效；
 * 持久化由 ShellContext 写 localStorage，`?theme=` 答辩强制通道不受影响。
 * 交互：Esc / 点击面板外关闭；当前皮肤高亮并带「当前」徽。
 */
import { useEffect, useRef } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { THEMES, useShell } from "./ShellContext";

export default function AppearancePanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { theme, setTheme } = useShell();
  const ref = useRef<HTMLDivElement | null>(null);

  // Esc 关闭 + 点击面板外关闭（挂载期监听，open 翻转时重建）
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    window.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div key="skin-panel" ref={ref} className="skin-panel" data-testid="appearance-panel"
          initial={{ opacity: 0, y: 10, scale: 0.96 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 8, scale: 0.97, pointerEvents: "none" }}
          transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
          role="dialog" aria-label="外观设置">
          <p className="skin-panel-title">外观 · 皮肤</p>
          {THEMES.map((t) => {
            const current = theme === t.id;
            return (
              <button key={t.id} type="button"
                data-testid={`skin-card-${t.id}`}
                className={`skin-card${current ? " current" : ""}`}
                onClick={() => { setTheme(t.id); onClose(); }}
                title={t.desc}>
                {/* 迷你预览：底色 + 侧栏条 + 主/点缀横带 + 主按钮点 */}
                <span className="skin-thumb" style={{ background: t.swatch[2] }}>
                  <i className="st-side" style={{ background: t.swatch[0] }} />
                  <i className="st-line" style={{ background: t.swatch[0] }} />
                  <i className="st-line short" style={{ background: t.swatch[1] }} />
                  <b className="st-btn" style={{ background: t.swatch[0] }} />
                </span>
                <span className="skin-meta">
                  <span className="skin-name">
                    {t.label}
                    {current && <em className="skin-cur">当前</em>}
                  </span>
                  <span className="skin-en num">{t.en}</span>
                  <span className="skin-desc">{t.desc}</span>
                  <span className="skin-swatches">
                    {t.swatch.map((c) => <i key={c} style={{ background: c }} />)}
                  </span>
                </span>
              </button>
            );
          })}
          <p className="skin-tip kv">切换即时生效 · 自动记忆 · <code>?theme=</code> 供答辩截图</p>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
