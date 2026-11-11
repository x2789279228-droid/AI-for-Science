"use client";

/**
 * 品牌印章（§1.2 语义系统）：仅两种语义,禁止营销化滥用。
 * - verified「证」：claim 已通过 L1–L4 校验 / 证据齐全
 * - cornerstone「基」：图谱基石论文（PageRank 前 20% 且出度≥3）
 * 尺寸 xs(16 行内)/sm(22 徽章)/md(36 答辩材料);落章 spring 260ms,
 * prefers-reduced-motion 直接静止呈现。同屏 ≤ 12 个(方案红线)。
 */
import { motion, useReducedMotion } from "framer-motion";

export type StampKind = "verified" | "cornerstone";
export type StampSize = "xs" | "sm" | "md";

const GLYPH: Record<StampKind, string> = { verified: "证", cornerstone: "基" };
const PX: Record<StampSize, number> = { xs: 16, sm: 22, md: 36 };
const COLOR: Record<StampKind, string> = {
  verified: "var(--stamp-verified)",
  cornerstone: "var(--stamp-cornerstone)",
};
const TIP: Record<StampKind, string> = {
  verified: "该论断已通过 L1–L4 校验",
  cornerstone: "PageRank 前 20% 且出度≥3",
};

export default function Stamp({ kind, size = "xs", tip }: {
  kind: StampKind; size?: StampSize; tip?: string;
}) {
  const reduce = useReducedMotion();
  const px = PX[size];
  const fontSize = Math.max(9, Math.round(px * (size === "xs" ? 0.62 : 0.5)));
  const innerRing = size === "xs" ? "" :
    `, inset 0 0 0 1px var(--stamp-bg), inset 0 0 0 ${size === "md" ? 2.4 : 1.8}px currentColor`;
  return (
    <motion.span
      className="stamp" data-stamp={kind} title={tip ?? TIP[kind]}
      initial={reduce ? false : { scale: 1.4, rotate: -6, opacity: 0 }}
      animate={{ scale: 1, rotate: -2, opacity: 0.9 }}
      transition={reduce ? { duration: 0 } : { type: "spring", stiffness: 320, damping: 24 }}
      style={{
        width: px, height: px, fontSize,
        color: COLOR[kind], background: "var(--stamp-bg)",
        border: "1.5px solid currentColor", borderRadius: "50%",
        boxShadow: `inset 0 0 0 0.5px currentColor${innerRing}`,
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        flex: "none", verticalAlign: "middle", cursor: "help",
        lineHeight: 1, userSelect: "none",
        fontFamily: "var(--font-display-cn)", fontWeight: 700,
      }}>
      {GLYPH[kind]}
    </motion.span>
  );
}
