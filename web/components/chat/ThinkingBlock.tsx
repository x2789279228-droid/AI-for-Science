"use client";

/**
 * ChatGLM 式「深度思考」折叠块：
 * - 思考中：自动展开，「深度思考中…」呼吸点动效，思考流灰字小号、内部滚动自动贴底
 * - 正文首字到达（active→false）：自动收起，标题变「已深度思考（用时 X 秒 · N 字）」
 * - 此后开合完全由用户手动控制；text 为空的轮次（思考关/拒答/抽取式降级）不渲染
 * 复用全局 .acc-body grid-rows 过渡动画（与 Pipeline/检索明细同一套折叠动效）。
 */
import { useEffect, useRef, useState } from "react";

export default function ThinkingBlock({ text, active, ms }: {
  text: string;
  active: boolean;
  ms?: number;
}) {
  const [open, setOpen] = useState(true);
  const bodyRef = useRef<HTMLDivElement>(null);
  const touched = useRef(false);   // 用户手动开合后，不再自动收展（防拉扯）

  useEffect(() => {
    if (touched.current) return;
    setOpen(active);   // 思考中展开 → 正文开始自动收起
  }, [active]);

  // 思考流贴底：仅思考中且展开时跟随，不打扰手动回看
  useEffect(() => {
    if (open && active && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [text, open, active]);

  if (!text) return null;
  const label = active
    ? "深度思考中…"
    : ms != null
      ? `已深度思考（用时 ${Math.max(1, Math.round(ms / 1000))} 秒 · ${text.length} 字）`
      : `已深度思考（${text.length} 字）`;

  return (
    <div className={`think acc${open ? " open" : ""}`}>
      <button className="think-head acc-trigger" data-testid="think-toggle"
        onClick={() => { touched.current = true; setOpen((o) => !o); }}>
        <span className={`think-glyph${active ? " pulse" : ""}`} aria-hidden>✦</span>
        <span>{label}</span>
        <span className="acc-caret think-caret" aria-hidden />
      </button>
      <div className="acc-body"><div className="acc-inner">
        <div className="think-body" ref={bodyRef}>{text}</div>
      </div></div>
    </div>
  );
}
