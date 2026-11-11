"use client";

/** 全局空态三态规范(§7.9):icon/illust + 标题 + 描述 + 可选行动按钮。
 *  禁止再出现裸文字"暂无"。错误场景:操作类页面用横幅,浏览类静默降级为本组件。 */
import type { ReactNode } from "react";
import { RisoIllust } from "./Illustrations";
import type { IllustName } from "./Illustrations";

export default function EmptyState({ icon, illust, title, desc, action }: {
  icon?: ReactNode;
  illust?: IllustName;
  title: string;
  desc?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state card-in">
      {illust && <RisoIllust name={illust} size={140} />}
      {icon && !illust && <div style={{ width: 46, height: 46, borderRadius: "var(--radius-lg)",
        background: "var(--accent-soft)", color: "var(--accent)", display: "flex",
        alignItems: "center", justifyContent: "center" }}>{icon}</div>}
      <b>{title}</b>
      {desc && <p className="kv" style={{ maxWidth: 380 }}>{desc}</p>}
      {action}
    </div>
  );
}
