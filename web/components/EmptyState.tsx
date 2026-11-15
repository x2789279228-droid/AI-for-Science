"use client";

/** 全局空态三态规范(§7.9):icon/illust + 标题 + 描述 + 可选行动按钮。
 *  禁止再出现裸文字"暂无"。错误场景:操作类页面用横幅,浏览类静默降级为本组件。 */
import type { ReactNode } from "react";
import ThemeIllust from "./ThemeIllust";
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
      {/* 同名场景契约:各皮肤插画集经 ThemeIllust 分发,自守卫(classic 落 icon 槽) */}
      {illust && <ThemeIllust name={illust} size={140} />}
      {icon && !illust && <div style={{ width: 46, height: 46, borderRadius: "var(--radius-lg)",
        background: "var(--accent-soft)", color: "var(--accent)", display: "flex",
        alignItems: "center", justifyContent: "center" }}>{icon}</div>}
      <b>{title}</b>
      {desc && <p className="kv" style={{ maxWidth: 380 }}>{desc}</p>}
      {action}
    </div>
  );
}
