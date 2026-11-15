"use client";

/**
 * WUXIA 插图集(第四皮肤专用):自绘内联 SVG,零依赖零版权。
 * 母题:武侠电影海报 —— 玄墨夜底 × 鎏金线描 × 月下侠客剪影 × 朱砂点睛。
 * 与 Illustrations/MedinaIllustrations 保持同名场景契约(IllustName):
 * 仅 wuxia 主题渲染;主角为原创侠客原型剪影,不描摹任何特定作品人物形象。
 * 色值带 token 回退,换季改 tokens 即可全局变色。
 */
import type { JSX } from "react";
import { useShell } from "./ShellContext";
import { SCENE_QUOTES } from "@/lib/wuxia-quotes";
import type { IllustName } from "./Illustrations";

const C = {
  night: "var(--wx-night,#16130f)",
  panel: "var(--wx-night-2,#1f1b16)",
  deep: "var(--wx-night-3,#2a241d)",
  moon: "var(--wx-moon,#ede6d6)",
  moon2: "var(--wx-moon-2,#a89c86)",
  gold: "var(--wx-gold,#d9a93c)",
  gold2: "var(--wx-gold-2,#e8bd58)",
  cin: "var(--wx-cinnabar,#e06448)",   // 朱砂
  line: "var(--wx-line-strong,#4a4136)",
};

/** 月下剪影人:头 + 身躯基础形(负重剑/立/坐变体由各场景拼装)。 */
function Figure({ x, y, s = 1, fill }: { x: number; y: number; s?: number; fill: string }) {
  return (
    <g transform={`translate(${x} ${y}) scale(${s})`} fill={fill}>
      <circle cx="0" cy="-26" r="5.4" />
      <path d="M-6.5 -20 h13 l3.4 15 -4.4 22 h-3.4 l-1 -16 -1.2 16 h-3.4 l-4.4 -22 Z" />
    </g>
  );
}

/** 8 幅江湖叙事场景(viewBox 200×140),与 riso/majorelle 场景一一对应。 */
const SCENES: Record<IllustName, JSX.Element> = {
  // 青灯黄卷·书生侠夜读:文献库/阅读器空态(对应 night-reader)
  "night-reader": (
    <g>
      <rect width="200" height="140" rx="10" fill={C.panel} />
      {/* 月窗 */}
      <rect x="126" y="22" width="66" height="78" rx="4" fill={C.night} stroke={C.line} strokeWidth="1.4" />
      <circle cx="159" cy="58" r="24" fill={C.gold} opacity="0.28" />
      <circle cx="159" cy="58" r="17" fill={C.moon} opacity="0.92" />
      <g stroke={C.line} strokeWidth="1.2" opacity="0.8">
        <line x1="159" y1="22" x2="159" y2="100" />
        <line x1="126" y1="61" x2="192" y2="61" />
      </g>
      {/* 负剑书生剪影(坐姿,面窗) */}
      <g fill="#0d0b08">
        <circle cx="104" cy="58" r="6.2" />
        <path d="M96 66 h17 l4 14 -5 26 h-5 l-1.4 -19 -1.4 19 h-5 l-5 -26 Z" />
        <path d="M118 64 l7 -3 3 5 -7 4 Z" opacity="0.9" />
      </g>
      {/* 背剑(鎏金线描) */}
      <g stroke={C.gold} strokeWidth="1.8" strokeLinecap="round" fill="none">
        <line x1="93" y1="62" x2="87" y2="102" />
        <line x1="87.5" y1="84" x2="97" y2="82" />
      </g>
      {/* 书案 + 摊开书卷 + 烛火 */}
      <rect x="18" y="98" width="168" height="9" rx="2" fill={C.deep} />
      <path d="M40 88 q14 -6 28 0 q-2 5 -6 4 h-16 q-4 1 -6 -4 Z" fill={C.moon} opacity="0.94" />
      <line x1="52" y1="89" x2="52" y2="91.5" stroke={C.panel} strokeWidth="1.2" />
      <g>
        <rect x="88" y="88" width="4.5" height="10" rx="1" fill={C.gold} opacity="0.7" />
        <ellipse cx="90" cy="84" rx="3" ry="5" fill={C.gold2} />
        <ellipse cx="90" cy="84" rx="8" ry="11" fill={C.gold} opacity="0.18" />
      </g>
      {/* 砚台 */}
      <rect x="68" y="92" width="12" height="5" rx="1.5" fill={C.moon2} />
    </g>
  ),
  // 一蓑烟雨·蓑笠客渡江:检索发现空态(对应 paper-sea)
  "paper-sea": (
    <g>
      <rect width="200" height="140" rx="10" fill={C.night} />
      {/* 鎏金大月 */}
      <circle cx="152" cy="38" r="24" fill={C.gold} opacity="0.32" />
      <circle cx="152" cy="38" r="17" fill={C.gold2} opacity="0.95" />
      {/* 远山两叠(月白淡影) */}
      <path d="M0 92 L38 58 L70 84 L104 54 L138 86 L170 66 L200 90 L200 140 L0 140 Z"
        fill={C.moon} opacity="0.09" />
      <path d="M0 108 L44 88 L86 106 L132 84 L176 106 L200 94 L200 140 L0 140 Z"
        fill={C.moon} opacity="0.05" />
      {/* 江波(鎏金细线) */}
      <g stroke={C.gold} strokeWidth="1.4" fill="none" opacity="0.55" strokeLinecap="round">
        <path d="M8 116 q10 -4 20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0" />
        <path d="M20 126 q10 -4 20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0" opacity="0.6" />
        <path d="M4 134 q10 -4 20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0" opacity="0.35" />
      </g>
      {/* 扁舟 + 蓑笠客剪影 */}
      <path d="M58 112 q42 12 84 0 l-10 10 h-64 Z" fill="#0d0b08" />
      <g fill="#0d0b08">
        <circle cx="104" cy="92" r="5.6" />
        <path d="M92 88 q12 -10 24 0 l-3 4 q-9 -7 -18 0 Z" />
        <path d="M98 98 h12 l3 12 -4 8 h-3 l-1 -11 -1 11 h-3 l-4 -8 Z" />
      </g>
      {/* 竹篙 */}
      <line x1="118" y1="86" x2="128" y2="112" stroke={C.moon2} strokeWidth="1.8" strokeLinecap="round" />
      {/* 飞鸟 */}
      <path d="M56 40 q5 -4 10 0 M66 36 q5 -4 10 0" stroke={C.moon2} strokeWidth="1.6"
        fill="none" strokeLinecap="round" />
    </g>
  ),
  // 灯在路在·亭中执灯人:问答/工作台占位(对应 lighthouse)
  lighthouse: (
    <g>
      <rect width="200" height="140" rx="10" fill={C.night} />
      {/* 远山月影 */}
      <circle cx="46" cy="34" r="15" fill={C.moon} opacity="0.85" />
      <path d="M0 96 L36 74 L66 92 L100 72 L134 94 L168 78 L200 94 L200 140 L0 140 Z"
        fill={C.moon} opacity="0.07" />
      {/* 水榭亭(鎏金线描) */}
      <g stroke={C.gold} strokeWidth="2" fill="none" strokeLinecap="round">
        <path d="M96 44 q34 -20 68 0" />
        <line x1="96" y1="44" x2="96" y2="52" />
        <line x1="164" y1="44" x2="164" y2="52" />
        <line x1="104" y1="52" x2="104" y2="102" />
        <line x1="156" y1="52" x2="156" y2="102" />
        <line x1="130" y1="52" x2="130" y2="96" opacity="0.6" />
        <line x1="96" y1="102" x2="164" y2="102" />
      </g>
      {/* 朱砂灯笼(点睛光源) */}
      <line x1="130" y1="52" x2="130" y2="60" stroke={C.gold} strokeWidth="1.4" />
      <ellipse cx="130" cy="72" rx="10" ry="12" fill={C.cin} />
      <ellipse cx="130" cy="72" rx="16" ry="19" fill={C.cin} opacity="0.16" />
      <rect x="126" y="58" width="8" height="3.5" rx="1.5" fill={C.gold} />
      <rect x="126" y="82" width="8" height="3.5" rx="1.5" fill={C.gold} />
      {/* 执灯人剪影(伸手揽光) */}
      <g fill="#0d0b08">
        <circle cx="106" cy="66" r="5.8" />
        <path d="M99 73 h14 l3.4 13 -4 20 h-3.6 l-1.2 -15 -1.2 15 h-3.6 l-4 -20 Z" />
        <path d="M113 76 l10 -4 2.5 4.5 -10 5 Z" />
      </g>
      {/* 水面波光(月白) */}
      <g stroke={C.moon} strokeWidth="1.4" fill="none" opacity="0.4" strokeLinecap="round">
        <path d="M14 118 q10 -4 20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0 t20 0" />
        <path d="M30 130 q10 -4 20 0 t20 0 t20 0 t20 0 t20 0 t20 0" opacity="0.55" />
      </g>
    </g>
  ),
  // 会当凌绝顶·剑客登峰:图谱/评审占位(对应 peaks)
  peaks: (
    <g>
      <rect width="200" height="140" rx="10" fill={C.night} />
      {/* 朱砂落日(点睛) */}
      <circle cx="160" cy="34" r="13" fill={C.cin} />
      <circle cx="160" cy="34" r="19" fill={C.cin} opacity="0.15" />
      {/* 鎏金云海 */}
      <g stroke={C.gold} strokeWidth="1.6" fill="none" opacity="0.5" strokeLinecap="round">
        <path d="M22 62 q12 -8 24 0 t24 0" />
        <path d="M120 74 q12 -8 24 0 t24 0" opacity="0.7" />
        <path d="M52 92 q12 -8 24 0" opacity="0.4" />
      </g>
      {/* 层叠墨山 */}
      <path d="M-4 140 L52 74 L96 140 Z" fill={C.moon} opacity="0.1" />
      <path d="M70 140 L128 58 L188 140 Z" fill={C.moon} opacity="0.16" />
      <path d="M140 140 L176 92 L212 140 Z" fill={C.moon} opacity="0.07" />
      {/* 主峰剑客剪影(负剑远眺) */}
      <g fill="#0d0b08">
        <circle cx="128" cy="49" r="4.6" />
        <path d="M122 55 h12 l3 10 -3.4 18 h-3.2 l-1 -12 -1 12 h-3.2 l-3.4 -18 Z" />
      </g>
      {/* 背剑(鎏金) + 剑穗(朱砂) */}
      <line x1="118" y1="58" x2="113" y2="84" stroke={C.gold} strokeWidth="1.7" strokeLinecap="round" />
      <line x1="118.5" y1="76" x2="126" y2="75" stroke={C.gold} strokeWidth="1.4" strokeLinecap="round" />
      <path d="M139 60 q4 4 2 9" stroke={C.cin} strokeWidth="1.6" fill="none" strokeLinecap="round" />
      {/* 山径(月白虚线) */}
      <path d="M128 84 q-14 18 -34 30 h-52" stroke={C.moon} strokeWidth="1.2"
        strokeDasharray="3 5" fill="none" opacity="0.4" />
    </g>
  ),
  // 真伪之辨·捕快验印:ideas 已解决空态(对应 magnify)
  magnify: (
    <g>
      <rect width="200" height="140" rx="10" fill={C.panel} />
      {/* 卷宗(月白卷轴) */}
      <rect x="24" y="30" width="108" height="76" rx="3" fill={C.moon} opacity="0.92" />
      <rect x="30" y="36" width="14" height="64" rx="2" fill={C.moon2} opacity="0.5" />
      <g stroke={C.moon2} strokeWidth="1.6" opacity="0.65" strokeLinecap="round">
        <line x1="52" y1="42" x2="120" y2="42" />
        <line x1="52" y1="52" x2="112" y2="52" />
        <line x1="52" y1="62" x2="118" y2="62" />
        <line x1="52" y1="72" x2="100" y2="72" />
      </g>
      {/* 朱砂方印(点睛) */}
      <rect x="96" y="76" width="22" height="22" rx="2.5" fill={C.cin} />
      <g stroke="#16130f" strokeWidth="1.6" opacity="0.75">
        <line x1="101" y1="81" x2="113" y2="81" />
        <line x1="101" y1="87" x2="113" y2="87" />
        <line x1="107" y1="81" x2="107" y2="93" />
      </g>
      {/* 鎏金放大镜 */}
      <circle cx="107" cy="87" r="30" fill="none" stroke={C.gold} strokeWidth="6" />
      <circle cx="107" cy="87" r="30" fill={C.gold} opacity="0.08" />
      <rect x="132" y="110" width="34" height="9" rx="4.5" transform="rotate(38 132 110)"
        fill={C.gold} />
      {/* 印泥点 */}
      <circle cx="36" cy="118" r="5" fill={C.cin} opacity="0.8" />
      <circle cx="52" cy="120" r="3.4" fill={C.cin} opacity="0.5" />
    </g>
  ),
  // 高手过招·山巅论剑:ideas 页头/研究缝隙(对应 observatory)
  observatory: (
    <g>
      <rect width="200" height="140" rx="10" fill={C.night} />
      {/* 大月当空 */}
      <circle cx="100" cy="42" r="27" fill={C.moon} opacity="0.14" />
      <circle cx="100" cy="42" r="19" fill={C.moon} opacity="0.9" />
      {/* 山脊线 */}
      <path d="M0 116 L44 92 L84 108 L128 84 L172 106 L200 92 L200 140 L0 140 Z"
        fill={C.deep} />
      <path d="M0 116 L44 92 L84 108 L128 84 L172 106 L200 92"
        stroke={C.gold} strokeWidth="1.4" fill="none" opacity="0.45" />
      {/* 双侠对峙剪影 */}
      <g fill="#0d0b08">
        <circle cx="76" cy="72" r="5.2" />
        <path d="M70 78 h12 l3.2 11 -3.4 19 h-3.4 l-1 -13 -1 13 h-3.4 l-3.4 -19 Z" />
        <circle cx="124" cy="72" r="5.2" />
        <path d="M118 78 h12 l3.4 11 -3.4 19 h-3.4 l-1 -13 -1 13 h-3.4 l-3.2 -19 Z" />
      </g>
      {/* 双剑(鎏金)对指 + 剑穗朱砂 */}
      <line x1="84" y1="84" x2="96" y2="98" stroke={C.gold} strokeWidth="1.8" strokeLinecap="round" />
      <line x1="116" y1="84" x2="104" y2="98" stroke={C.gold} strokeWidth="1.8" strokeLinecap="round" />
      <path d="M74 80 q-4 4 -2 9 M126 80 q4 4 2 9" stroke={C.cin} strokeWidth="1.5"
        fill="none" strokeLinecap="round" />
      {/* 剑气交汇(鎏金电弧) */}
      <path d="M94 102 q6 -7 12 0" stroke={C.gold2} strokeWidth="1.8" fill="none" strokeLinecap="round" />
      <path d="M96 106 q4 -4 8 0" stroke={C.gold} strokeWidth="1.2" fill="none" opacity="0.7" />
      {/* 山脚雾带 */}
      <path d="M12 132 q14 -6 28 0 t28 0 t28 0 t28 0 t28 0" stroke={C.moon2}
        strokeWidth="1.3" fill="none" opacity="0.3" strokeLinecap="round" />
    </g>
  ),
  // 此页已退出江湖·雕版匠人:404 海报页(对应 press-house)
  "press-house": (
    <g>
      <rect width="200" height="140" rx="10" fill={C.panel} />
      {/* 印坊晾纸(月白纸页) */}
      <g>
        <line x1="30" y1="16" x2="30" y2="66" stroke={C.line} strokeWidth="1.6" />
        <rect x="34" y="18" width="26" height="34" rx="1.5" fill={C.moon} opacity="0.85" />
        <rect x="34" y="58" width="26" height="22" rx="1.5" fill={C.moon} opacity="0.6" />
        <g stroke={C.moon2} strokeWidth="1.1" opacity="0.6">
          <line x1="39" y1="26" x2="55" y2="26" /><line x1="39" y1="31" x2="52" y2="31" />
          <line x1="39" y1="36" x2="55" y2="36" /><line x1="39" y1="41" x2="50" y2="41" />
        </g>
      </g>
      {/* 雕版台(月白「肆零肆」拓字位) */}
      <rect x="76" y="34" width="86" height="62" rx="3" fill={C.night} stroke={C.line} strokeWidth="1.4" />
      <text x="119" y="76" textAnchor="middle" fontFamily="var(--font-serif-cn,serif)"
        fontSize="30" fontWeight="700" fill={C.moon} opacity="0.9">肆零肆</text>
      {/* 雕版匠人剪影(推刷) */}
      <g fill="#0d0b08">
        <circle cx="176" cy="52" r="5.6" />
        <path d="M169 59 h14 l3.4 13 -4 22 h-3.6 l-1.2 -16 -1.2 16 h-3.6 l-4 -22 Z" />
        <path d="M169 62 l-12 8 2.6 4.6 12 -7 Z" />
      </g>
      {/* 鎏金棕刷 */}
      <rect x="150" y="66" width="10" height="14" rx="2" fill={C.gold} opacity="0.85" />
      <rect x="152" y="58" width="6" height="9" rx="2" fill={C.moon2} />
      {/* 朱砂印泥盒 */}
      <rect x="84" y="104" width="16" height="8" rx="2" fill={C.cin} opacity="0.85" />
      <rect x="106" y="106" width="12" height="6" rx="2" fill={C.moon2} opacity="0.5" />
    </g>
  ),
  // 旗在人在·猎猎侠旗:侧栏品牌区徽标场景(对应 wavy-flag)
  "wavy-flag": (
    <g>
      <rect width="200" height="140" rx="10" fill={C.night} />
      {/* 鎏金残月 */}
      <path d="M164 22 a13 13 0 1 0 10 21 a10.5 10.5 0 1 1 -10 -21" fill={C.gold} opacity="0.9" />
      {/* 旗杆 + 猎猎旗(月白旗面 + 朱砂「俠」) */}
      <line x1="48" y1="18" x2="48" y2="126" stroke={C.moon2} strokeWidth="2.6" strokeLinecap="round" />
      <circle cx="48" cy="16" r="3" fill={C.gold} />
      <path d="M52 26 C 72 36 90 16 108 26 C 126 36 142 18 158 28 L 152 82
               C 134 74 120 90 102 82 C 84 74 70 90 52 82 Z"
        fill={C.moon} opacity="0.94" />
      <text x="102" y="62" textAnchor="middle" fontFamily="var(--font-serif-cn,serif)"
        fontSize="26" fontWeight="900" fill={C.cin}>俠</text>
      {/* 剑穗(旗杆侧,朱砂) */}
      <line x1="52" y1="86" x2="60" y2="98" stroke={C.gold} strokeWidth="1.6" strokeLinecap="round" />
      <path d="M60 98 q5 6 3 12 M60 98 q-2 7 -7 10" stroke={C.cin} strokeWidth="1.6"
        fill="none" strokeLinecap="round" />
      {/* 双燕(月白剪影) */}
      <path d="M140 44 q6 -5 12 0 q6 -5 12 0" stroke={C.moon} strokeWidth="1.8"
        fill="none" strokeLinecap="round" />
      <path d="M120 32 q5 -4 10 0 q5 -4 10 0" stroke={C.moon2} strokeWidth="1.4"
        fill="none" strokeLinecap="round" opacity="0.7" />
    </g>
  ),
};

/** 插图组件。契约:仅 wuxia 主题渲染——其它主题返回 null,挂载点无需写条件。
 *  场景配文(经典名句)随插画展示,竖排题跋见 WuxiaTagline。 */
export function WuxiaIllust({ name, size = 150 }: { name: IllustName; size?: number }) {
  const { theme } = useShell();
  if (theme !== "wuxia") return null;
  const q = SCENE_QUOTES[name];
  return (
    <span className="wuxia-illust" aria-hidden>
      <svg width={size} height={Math.round(size * 0.7)} viewBox="0 0 200 140" fill="none">
        {SCENES[name]}
      </svg>
      {q && (
        <i className="wuxia-illust-cap">
          「{q.text}」<em>—— {q.from}</em>
        </i>
      )}
    </span>
  );
}

/** 剑穗徽标(侧栏品牌区 wuxia 态用,24px 线稿,currentColor)。 */
export function SwordGlyph({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M12 2.5 L13.4 6 v10 h-2.8 V6 Z" stroke="currentColor" strokeWidth="1.5"
        strokeLinejoin="round" />
      <line x1="7.5" y1="16" x2="16.5" y2="16" stroke="currentColor" strokeWidth="1.6"
        strokeLinecap="round" />
      <line x1="12" y1="16" x2="12" y2="20" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx="12" cy="21.2" r="1.1" fill="currentColor" />
      <path d="M10.4 21 q-2 1.6 -3.4 1 M13.6 21 q2 1.6 3.4 1" stroke="currentColor"
        strokeWidth="1.2" strokeLinecap="round" />
    </svg>
  );
}
