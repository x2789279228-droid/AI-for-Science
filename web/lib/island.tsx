"use client";

/**
 * 全局动态岛事件总线：IslandProvider + useIsland().emit()
 * 语义：progress(进行中,可带进度/不确定态) → success/error(终态,ttl 自动收起) / info(轻提示)
 * 同 key 的 progress 事件原位更新(id 不变) → 岛不闪烁重建,宽度 morph 由 DynamicIsland 的
 * framer-motion layout 接管。任何页面/组件 `const island = useIsland()` 后一行 emit 即接入。
 */
import {
  createContext, useCallback, useContext, useMemo, useRef, useState,
} from "react";

export type IslandKind = "progress" | "success" | "error" | "info";

export interface IslandEvent {
  id: number;
  key: string;              // 同类事件标识(progress 更新去重)
  kind: IslandKind;
  title: string;
  detail?: string;
  progress?: number;        // 0~1,progress 态可选
  indeterminate?: boolean;  // true → spinner 而非进度环
  ttl?: number;             // 终态展示时长 ms(默认 3200)
}

export interface IslandEmit {
  key?: string;
  kind: IslandKind;
  title: string;
  detail?: string;
  progress?: number;
  indeterminate?: boolean;
  ttl?: number;
}

interface IslandCtx {
  event: IslandEvent | null;
  emit: (e: IslandEmit) => void;
  dismiss: () => void;
}

const Ctx = createContext<IslandCtx | null>(null);

const DEFAULT_TTL = 3200;

export function IslandProvider({ children }: { children: React.ReactNode }) {
  const [event, setEvent] = useState<IslandEvent | null>(null);
  const seq = useRef(0);
  const timer = useRef<number | null>(null);

  const dismiss = useCallback(() => {
    if (timer.current) { window.clearTimeout(timer.current); timer.current = null; }
    setEvent(null);
  }, []);

  const emit = useCallback((e: IslandEmit) => {
    seq.current += 1;
    const key = e.key ?? e.title;
    setEvent((prev) => {
      // progress 态同 key 原位更新:保留旧 id,framer layout 平滑 morph
      if (prev && prev.key === key && e.kind === "progress" && prev.kind === "progress") {
        return { ...prev, title: e.title, detail: e.detail, progress: e.progress,
          indeterminate: e.indeterminate };
      }
      return { id: seq.current, key, kind: e.kind, title: e.title, detail: e.detail,
        progress: e.progress, indeterminate: e.indeterminate, ttl: e.ttl };
    });
    if (timer.current) window.clearTimeout(timer.current);
    const ttl = e.kind === "progress" ? 0 : (e.ttl ?? DEFAULT_TTL);
    if (ttl > 0) {
      timer.current = window.setTimeout(() => setEvent(null), ttl);
    }
  }, []);

  const value = useMemo(() => ({ event, emit, dismiss }), [event, emit, dismiss]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** Provider 外调用时静默降级为空实现(永不抛错)。 */
export function useIsland(): IslandCtx {
  const ctx = useContext(Ctx);
  if (ctx) return ctx;
  return {
    event: null,
    emit: () => { /* Provider 外:忽略 */ },
    dismiss: () => { /* Provider 外:忽略 */ },
  };
}
