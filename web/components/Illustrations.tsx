"use client";

/**
 * RISO 插图集(第二皮肤专用):自绘内联 SVG,零依赖零版权。
 * 风格规范(以 Tom Clohosy Cole 限色叙事为灵感):riso 限色四色
 * —— 米纸底 / Klein 蓝大色块 / 国际橙光源点缀 / 墨线勾勒,feTurbulence 颗粒覆印。
 * 仅 riso 主题渲染(classic 不挂载);色值带 token 回退,换季改 tokens 即可全局变色。
 */
import { useId } from "react";
import type { JSX } from "react";
import { useShell } from "./ShellContext";

export type IllustName =
  | "night-reader" | "paper-sea" | "lighthouse" | "peaks"
  | "magnify" | "observatory" | "press-house" | "wavy-flag";

const C = {
  paper: "var(--riso-paper,#faf1df)",
  cream: "var(--riso-cream,#f4e9d6)",
  klein: "var(--riso-klein,#2233c9)",
  kleinDeep: "var(--riso-klein-deep,#1a2694)",
  orange: "var(--riso-orange,#ff5f3c)",
  ink: "var(--riso-ink,#17161a)",
  yellow: "var(--riso-yellow,#ffd23f)",
};

/** 8 幅学术叙事场景(viewBox 200×140)。 */
const SCENES: Record<IllustName, JSX.Element> = {
  // 灯下夜读:书架空态/首页空库态
  "night-reader": (
    <g>
      <rect width="200" height="140" rx="10" fill={C.klein} />
      <circle cx="30" cy="22" r="1.8" fill={C.paper} /><circle cx="52" cy="14" r="1.4" fill={C.paper} />
      <circle cx="16" cy="40" r="1.4" fill={C.paper} /><circle cx="72" cy="24" r="1.2" fill={C.paper} />
      <circle cx="184" cy="30" r="6" fill={C.yellow} opacity="0.9" />
      <rect y="104" width="200" height="36" fill={C.ink} />
      <rect x="112" y="52" width="80" height="7" rx="3" fill={C.orange} />
      <rect x="118" y="59" width="68" height="45" rx="2" fill={C.paper} />
      <rect x="146" y="28" width="10" height="26" rx="2" fill={C.ink} />
      <circle cx="151" cy="28" r="9" fill={C.orange} />
      <circle cx="151" cy="28" r="17" fill={C.orange} opacity="0.25" />
      <circle cx="86" cy="60" r="12" fill={C.ink} />
      <rect x="68" y="70" width="36" height="34" rx="10" fill={C.ink} />
      <rect x="124" y="66" width="20" height="26" rx="2" fill={C.paper} transform="rotate(-8 134 79)" />
      <rect x="146" y="64" width="20" height="26" rx="2" fill={C.cream} transform="rotate(6 156 77)" />
      <line x1="128" y1="76" x2="142" y2="73" stroke={C.klein} strokeWidth="1.6" />
      <line x1="129" y1="82" x2="141" y2="79" stroke={C.klein} strokeWidth="1.6" />
      <rect x="26" y="96" width="36" height="8" fill={C.orange} />
      <rect x="30" y="88" width="28" height="8" fill={C.paper} />
      <rect x="26" y="80" width="36" height="8" fill={C.cream} />
    </g>
  ),
  // 书海行舟:discover 空态
  "paper-sea": (
    <g>
      <rect width="200" height="140" rx="10" fill={C.cream} />
      <circle cx="164" cy="28" r="15" fill={C.orange} />
      <circle cx="158" cy="24" r="3" fill={C.cream} opacity="0.5" />
      <rect y="86" width="200" height="54" fill={C.klein} />
      <path d="M0 94 Q 12 90 24 94 T 48 94 T 72 94 T 96 94 T 120 94 T 144 94 T 168 94 T 192 94"
        stroke={C.paper} strokeWidth="2" fill="none" opacity="0.75" />
      <path d="M0 110 Q 12 106 24 110 T 48 110 T 72 110 T 96 110 T 120 110 T 144 110 T 168 110 T 192 110"
        stroke={C.paper} strokeWidth="2" fill="none" opacity="0.45" />
      <rect x="22" y="72" width="16" height="20" rx="1" fill={C.paper} transform="rotate(-14 30 82)" />
      <rect x="148" y="66" width="16" height="20" rx="1" fill={C.paper} transform="rotate(12 156 76)" />
      <rect x="178" y="82" width="14" height="18" rx="1" fill={C.cream} transform="rotate(-8 185 91)" />
      <path d="M60 100 L140 100 L127 117 L73 117 Z" fill={C.ink} />
      <rect x="97" y="82" width="3" height="18" fill={C.ink} />
      <path d="M100 82 L124 95 L100 95 Z" fill={C.orange} />
      <circle cx="82" cy="89" r="7" fill={C.ink} />
      <rect x="73" y="94" width="19" height="9" rx="4" fill={C.kleinDeep} />
      <line x1="72" y1="100" x2="56" y2="112" stroke={C.ink} strokeWidth="2.4" strokeLinecap="round" />
    </g>
  ),
  // 溯源灯塔:chat 空态/audit 占位
  lighthouse: (
    <g>
      <rect width="200" height="140" rx="10" fill={C.kleinDeep} />
      <circle cx="38" cy="28" r="13" fill={C.paper} opacity="0.92" />
      <circle cx="43" cy="24" r="11" fill={C.kleinDeep} />
      <path d="M122 36 L198 20 L198 56 Z" fill={C.orange} opacity="0.85" />
      <rect x="104" y="46" width="24" height="58" fill={C.paper} />
      <rect x="104" y="58" width="24" height="10" fill={C.orange} />
      <rect x="104" y="78" width="24" height="10" fill={C.orange} />
      <rect x="110" y="28" width="12" height="10" rx="1" fill={C.orange} />
      <path d="M108 28 h16 l4 -7 h-24 Z" fill={C.ink} />
      <path d="M86 104 h60 l12 16 h-84 Z" fill={C.ink} />
      <rect x="148" y="26" width="28" height="20" rx="2" fill={C.paper} transform="rotate(-9 162 36)" />
      <circle cx="151" cy="31" r="3" fill={C.klein} />
      <line x1="155" y1="38" x2="170" y2="35" stroke={C.klein} strokeWidth="1.6" />
      <path d="M0 124 Q 14 118 28 124 T 56 124 T 84 124 T 112 124 T 140 124 T 168 124 T 196 124"
        stroke={C.paper} strokeWidth="2.4" fill="none" opacity="0.7" />
    </g>
  ),
  // 论文山脉:graph 空态/judge 占位
  peaks: (
    <g>
      <rect width="200" height="140" rx="10" fill={C.cream} />
      <circle cx="170" cy="28" r="14" fill={C.orange} />
      <path d="M28 32 q4 -5 8 0 M42 25 q4 -5 8 0" stroke={C.ink} strokeWidth="1.8"
        strokeLinecap="round" fill="none" />
      <path d="M-4 140 L44 58 L92 140 Z" fill={C.klein} />
      <path d="M58 140 L108 44 L158 140 Z" fill={C.ink} />
      <path d="M118 140 L162 74 L206 140 Z" fill={C.orange} />
      <line x1="108" y1="44" x2="108" y2="24" stroke={C.ink} strokeWidth="2.6" />
      <path d="M108 24 l20 6 -20 6 Z" fill={C.klein} />
      <circle cx="122" cy="118" r="4.5" fill={C.paper} />
      <rect x="118" y="121" width="8" height="12" rx="4" fill={C.paper} />
      <line x1="114" y1="112" x2="126" y2="112" stroke={C.paper} strokeWidth="2" strokeLinecap="round" />
    </g>
  ),
  // 引用放大镜:ideas 空态
  magnify: (
    <g>
      <rect width="200" height="140" rx="10" fill={C.paper} />
      <circle cx="30" cy="98" r="9" fill={C.klein} />
      <circle cx="70" cy="98" r="9" fill={C.orange} />
      <circle cx="110" cy="98" r="9" fill={C.klein} />
      <line x1="39" y1="98" x2="61" y2="98" stroke={C.ink} strokeWidth="2.6" />
      <line x1="79" y1="98" x2="101" y2="98" stroke={C.ink} strokeWidth="2.6" strokeDasharray="4 3" />
      <circle cx="128" cy="72" r="29" fill={C.cream} stroke={C.orange} strokeWidth="7" />
      <circle cx="128" cy="72" r="25" fill={C.klein} opacity="0.14" />
      <circle cx="121" cy="79" r="8" fill={C.klein} />
      <circle cx="138" cy="62" r="4" fill={C.paper} />
      <rect x="148" y="92" width="34" height="9" rx="4.5" transform="rotate(38 150 96)" fill={C.ink} />
    </g>
  ),
  // 缝隙天文台:ideas 页头
  observatory: (
    <g>
      <rect width="200" height="140" rx="10" fill={C.kleinDeep} />
      <circle cx="26" cy="24" r="1.8" fill={C.paper} /><circle cx="58" cy="16" r="1.4" fill={C.paper} />
      <circle cx="104" cy="20" r="1.6" fill={C.paper} /><circle cx="180" cy="60" r="1.5" fill={C.paper} />
      <circle cx="146" cy="40" r="17" stroke={C.orange} strokeWidth="2" strokeDasharray="5 4"
        fill="none" />
      <text x="146" y="46" textAnchor="middle" fontFamily="var(--font-mono,monospace)"
        fontSize="16" fontWeight="700" fill={C.orange}>?</text>
      <path d="M36 140 v-40 a46 46 0 0 1 92 0 v40 Z" fill={C.paper} />
      <rect x="78" y="52" width="9" height="34" fill={C.klein} />
      <rect x="86" y="34" width="48" height="9" rx="3" transform="rotate(-16 86 40)" fill={C.orange} />
      <circle cx="136" cy="26" r="5" fill={C.ink} />
      <rect x="72" y="114" width="20" height="26" fill={C.klein} />
      <circle cx="82" cy="128" r="2" fill={C.paper} />
    </g>
  ),
  // 活字工坊:404 海报页
  "press-house": (
    <g>
      <rect width="200" height="140" rx="10" fill={C.cream} />
      <rect x="28" y="28" width="94" height="82" rx="6" fill={C.klein} />
      <circle cx="75" cy="62" r="21" fill={C.paper} />
      <circle cx="75" cy="62" r="21" stroke={C.orange} strokeWidth="4.5" fill="none" />
      <line x1="75" y1="48" x2="86" y2="68" stroke={C.ink} strokeWidth="3" strokeLinecap="round" />
      <circle cx="28" cy="72" r="8" fill={C.orange} />
      <line x1="28" y1="72" x2="14" y2="86" stroke={C.ink} strokeWidth="5" strokeLinecap="round" />
      <rect x="130" y="40" width="54" height="70" rx="2" fill={C.paper}
        stroke={C.ink} strokeWidth="1.6" transform="rotate(4 157 75)" />
      <line x1="138" y1="58" x2="174" y2="56" stroke={C.klein} strokeWidth="3" strokeLinecap="round" />
      <line x1="138" y1="68" x2="172" y2="66" stroke={C.klein} strokeWidth="3" strokeLinecap="round" />
      <line x1="138" y1="78" x2="170" y2="76" stroke={C.orange} strokeWidth="3" strokeLinecap="round" />
      <line x1="138" y1="88" x2="168" y2="86" stroke={C.klein} strokeWidth="3" strokeLinecap="round" />
      <rect x="22" y="118" width="12" height="12" fill={C.orange} />
      <rect x="40" y="122" width="12" height="12" fill={C.ink} />
      <rect x="58" y="118" width="12" height="12" fill={C.klein} />
    </g>
  ),
  // 波浪旗(logo 纹样,源自品牌手绘旗):侧栏品牌区 riso 徽标
  "wavy-flag": (
    <g>
      <circle cx="172" cy="26" r="10" fill={C.orange} />
      <path d="M62 22 C 82 32 100 12 118 22 C 136 32 152 14 168 24 L 162 78
               C 144 70 130 86 112 78 C 94 70 80 86 62 78 Z"
        fill={C.paper} stroke={C.ink} strokeWidth="3.4" strokeLinejoin="round" />
      <circle cx="86" cy="44" r="2.2" fill={C.klein} /><circle cx="104" cy="42" r="2.2" fill={C.klein} />
      <circle cx="122" cy="44" r="2.2" fill={C.orange} /><circle cx="140" cy="42" r="2.2" fill={C.klein} />
      <circle cx="95" cy="58" r="2.2" fill={C.orange} /><circle cx="113" cy="56" r="2.2" fill={C.klein} />
      <circle cx="131" cy="58" r="2.2" fill={C.klein} />
    </g>
  ),
};

/** 插图组件:自动带颗粒覆印滤镜(useId 防同页 filter id 冲突)。
 *  契约:仅 riso 主题渲染——classic 下返回 null,挂载点无需写条件。 */
export function RisoIllust({ name, size = 150 }: { name: IllustName; size?: number }) {
  const { theme } = useShell();
  const fid = useId().replace(/[^a-zA-Z0-9]/g, "");
  if (theme !== "riso") return null;
  return (
    <span className="riso-illust" aria-hidden>
      <svg width={size} height={Math.round(size * 0.7)} viewBox="0 0 200 140" fill="none">
        <defs>
          <filter id={`grain-${fid}`} x="0" y="0" width="100%" height="100%">
            <feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" stitchTiles="stitch" />
            <feColorMatrix values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 0.6 0" />
          </filter>
        </defs>
        {SCENES[name]}
        <rect width="200" height="140" filter={`url(#grain-${fid})`} opacity="0.16"
          style={{ mixBlendMode: "multiply" }} />
      </svg>
    </span>
  );
}

/** 独立波浪旗徽标(侧栏品牌区 riso 态用,24px 线稿版)。 */
export function RisoFlagGlyph({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M6 4 C 9 5.6 11.5 3 14.5 4.4 C 17 5.6 19 4 21 5 L 20 14
               C 17.5 13 15.5 15 13 14 C 10.5 13 9 15 6.8 14 Z"
        stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
      <circle cx="10" cy="8.4" r="0.9" fill="currentColor" />
      <circle cx="14.4" cy="9" r="0.9" fill="currentColor" />
      <circle cx="12" cy="11.4" r="0.9" fill="currentColor" />
    </svg>
  );
}
