"use client";

/**
 * ChatGLM 式「最近对话」列表（嵌入全局侧栏上下文区块）：
 * 搜索 + 时间分组（今天/昨天/7 天内/更早）+ hover 重命名/删除。
 * 外壳（新对话按钮/折叠钮/品牌行）由 AppShell 负责，本组件只渲染列表。
 * 数据源 GET /api/conversations（AppShell 拉取），跨组件协调走 window CustomEvent：
 *   meridian:conv-refresh（AppShell↔chat 互相同步列表）/ conv-deleted（删除回传）。
 */
import { useMemo, useState } from "react";

export interface ConversationItem {
  id: string;
  title: string;
  turns: number;
  last_question?: string;
  updated_at: number;
}

/** 时间分组口径与 ChatGLM 一致：今天/昨天/7 天内/更早（按日界而非 24h）。 */
function groupOf(ts: number): string {
  const d = new Date(ts * 1000);
  const now = new Date();
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((day(now) - day(d)) / 86400000);
  if (days <= 0) return "今天";
  if (days === 1) return "昨天";
  if (days <= 7) return "7 天内";
  return "更早";
}

export default function ChatHistoryList({ items, activeId,
  onOpen, onRename, onDelete }: {
  items: ConversationItem[];
  activeId: string;
  onOpen: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
}) {
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const groups = useMemo(() => {
    const kw = q.trim().toLowerCase();
    const filtered = kw
      ? items.filter((c) => (c.title + (c.last_question ?? "")).toLowerCase().includes(kw))
      : items;
    const order = ["今天", "昨天", "7 天内", "更早"];
    const m = new Map<string, ConversationItem[]>();
    for (const c of filtered) {
      const g = groupOf(c.updated_at);
      if (!m.has(g)) m.set(g, []);
      m.get(g)!.push(c);
    }
    return order.filter((g) => m.has(g)).map((g) => [g, m.get(g)!] as const);
  }, [items, q]);

  const commitRename = (id: string) => {
    const t = draft.trim();
    setEditing(null);
    if (t) onRename(id, t);
  };

  return (
    <div className="side-conv">
      <input className="conv-search" placeholder="搜索会话…" value={q}
        onChange={(e) => setQ(e.target.value)} />
      <div className="conv-list">
        {groups.length === 0 && (
          <p className="conv-empty">{items.length === 0 ? "暂无历史会话" : "无匹配会话"}</p>
        )}
        {groups.map(([g, list]) => (
          <div key={g} className="conv-group">
            <span className="conv-group-label">{g}</span>
            {list.map((c) => (
              <div key={c.id}
                className={`conv-item${c.id === activeId ? " active" : ""}`}
                data-testid="conv-item"
                onClick={() => c.id !== activeId && onOpen(c.id)}>
                {editing === c.id ? (
                  <input className="conv-rename" autoFocus value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") commitRename(c.id);
                      if (e.key === "Escape") setEditing(null);
                    }}
                    onBlur={() => commitRename(c.id)}
                    onClick={(e) => e.stopPropagation()} />
                ) : (
                  <span className="conv-item-title" title={c.title}>{c.title}</span>
                )}
                {editing !== c.id && (
                  <span className="conv-item-ops">
                    <span className="conv-turns">{c.turns}轮</span>
                    <button className="conv-op" title="重命名" onClick={(e) => {
                      e.stopPropagation();
                      setEditing(c.id); setDraft(c.title);
                    }}>✎</button>
                    <button className="conv-op" title="删除" onClick={(e) => {
                      e.stopPropagation();
                      if (window.confirm("删除该会话及其全部问答记录？")) onDelete(c.id);
                    }}>🗑</button>
                  </span>
                )}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
