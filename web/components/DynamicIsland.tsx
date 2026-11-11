"use client";

/**
 * 全局动态岛:viewport 顶部居中,墨蓝玻璃底+浅蓝渐变描边光(与浅色页面形成对比)。
 * - progress:spinner / 进度环(--p) / 迷你进度条;点击展开 detail
 * - success/error:✓/✕ + 一次绿/红闪光(islandFlash),ttl 后自动收起
 * - 宽高 morph:framer-motion layout;进度更新同 id 原位过渡(见 lib/island.tsx)
 */
import { motion, AnimatePresence } from "framer-motion";
import { useEffect, useState } from "react";
import { useIsland } from "@/lib/island";

export default function DynamicIsland() {
  const { event, dismiss } = useIsland();
  const [expanded, setExpanded] = useState(false);
  const [flash, setFlash] = useState(false);
  const kind = event?.kind;

  // 新事件到达 → 收起展开态
  useEffect(() => { setExpanded(false); }, [event?.id]);

  // 终态到达 → 闪光一次
  useEffect(() => {
    if (kind === "success" || kind === "error") {
      setFlash(true);
      const t = window.setTimeout(() => setFlash(false), 750);
      return () => window.clearTimeout(t);
    }
  }, [event?.id, kind]);

  if (!event) return null;
  const done = kind === "success" || kind === "error";
  const cls = [
    "island",
    kind === "success" ? "success" : "",
    kind === "error" ? "error" : "",
    flash ? "done-flash" : "",
  ].filter(Boolean).join(" ");

  return (
    <AnimatePresence mode="popLayout">
      <motion.div key={event.id} className={cls} layout data-testid="dynamic-island"
        title={done ? "点击关闭" : "点击展开详情"}
        onClick={() => (done ? dismiss() : setExpanded((v) => !v))}
        initial={{ opacity: 0, y: -18, scale: 0.92 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: -12, scale: 0.94 }}
        transition={{ type: "spring", stiffness: 420, damping: 30 }}
        style={{ transformOrigin: "top center" }}>
        {kind === "progress" && (event.indeterminate || event.progress == null ? (
          <span className="island-spin" />
        ) : (
          <span className="island-ring"
            style={{ ["--p" as string]: Math.round(event.progress! * 100) }} />
        ))}
        {kind === "success" && <span style={{ fontSize: 14, flex: "none", color: "#5eead4" }}>✓</span>}
        {kind === "error" && <span style={{ fontSize: 14, flex: "none", color: "#fda4af" }}>✕</span>}
        {kind === "info" && <span className="island-badge">i</span>}

        <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 1 }}>
          <span className="island-title">{event.title}</span>
          {expanded && event.detail && <span className="island-detail">{event.detail}</span>}
        </div>

        {kind === "progress" && event.progress != null && (
          <span className="island-bar">
            <i style={{ width: `${Math.round(event.progress * 100)}%` }} />
          </span>
        )}
        {done && <span className="island-badge">知道了</span>}
      </motion.div>
    </AnimatePresence>
  );
}
