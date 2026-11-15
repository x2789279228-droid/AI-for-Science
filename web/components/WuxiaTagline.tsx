"use client";

/**
 * 武侠题跋（v1.4）：路由感知的经典名句竖排题字，挂在主区右上（topbar 之下）。
 * 仅 wuxia 主题渲染；pointer-events:none 纯装饰，不与页面争交互。
 * 语料来自 lib/wuxia-quotes.ts（ROUTE_QUOTES 前缀匹配 + 兜底）。
 */
import { usePathname } from "next/navigation";
import { useShell } from "./ShellContext";
import { FALLBACK_QUOTE, ROUTE_QUOTES } from "@/lib/wuxia-quotes";

export default function WuxiaTagline() {
  const { theme } = useShell();
  const pathname = usePathname();
  if (theme !== "wuxia") return null;
  const hit = ROUTE_QUOTES.find((r) => r.match(pathname));
  const q = hit ? hit.quote : FALLBACK_QUOTE;
  return (
    <span className="wuxia-tagline" data-testid="wuxia-tagline" aria-hidden>
      <i className="wt-mark">『</i>
      <span className="wt-text">{q.text}</span>
      <span className="wt-from">—— {q.from}</span>
    </span>
  );
}
