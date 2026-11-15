"use client";

/**
 * MAJORELLE 插图集(第三皮肤专用):自绘内联 SVG,零依赖零版权。
 * 母题:马拉喀什 —— 马约尔蓝拱墙 / 藏红花灯光 / 沙色麦地那 / zellige 八角星花砖。
 * 与 Illustrations.tsx 保持同名场景契约(IllustName):同一挂载点按主题各取所需,
 * 仅 majorelle 主题渲染(classic/riso 不挂载);色值带 token 回退,换季改 tokens 即可全局变色。
 */
import type { JSX } from "react";
import { useShell } from "./ShellContext";
import type { IllustName } from "./Illustrations";

const C = {
  sand: "var(--maj-sand,#f6ecda)",
  sandDeep: "var(--maj-sand-deep,#efe1c8)",
  surface: "var(--maj-surface,#fffaf1)",
  blue: "var(--maj-blue,#4a4ae8)",
  blueDeep: "var(--maj-blue-deep,#3232c8)",
  saffron: "var(--maj-saffron,#e8b33c)",
  rose: "var(--maj-rose,#d96c8a)",
  palm: "var(--maj-palm,#3f7d4e)",
  terra: "var(--maj-terra,#c96f4a)",
  ink: "var(--maj-ink,#33261a)",
};

/** 八角星(khatam 两方叠加方):花砖母题复用件。 */
function Khatam({ x, y, s, fill, hole }: { x: number; y: number; s: number; fill: string; hole?: string }) {
  const c = x + s / 2, cy = y + s / 2;
  return (
    <g>
      <rect x={x} y={y} width={s} height={s} transform={`rotate(45 ${c} ${cy})`} fill={fill} />
      <rect x={x} y={y} width={s} height={s} fill={fill} />
      {hole && <circle cx={c} cy={cy} r={s / 4} fill={hole} />}
    </g>
  );
}

/** 8 幅麦地那叙事场景(viewBox 200×140),与 riso 场景一一对应。 */
const SCENES: Record<IllustName, JSX.Element> = {
  // 拱廊书市:文献库/阅读器空态(对应 riso night-reader)
  "night-reader": (
    <g>
      <rect width="200" height="140" rx="10" fill={C.sand} />
      <path d="M14 106 V62 A24 24 0 0 1 62 62 V106 Z" fill={C.sandDeep} />
      <path d="M66 106 V62 A24 24 0 0 1 114 62 V106 Z" fill={C.sandDeep} />
      <path d="M26 106 V68 A12 12 0 0 1 50 68 V106 Z" fill={C.blue} />
      <path d="M78 106 V68 A12 12 0 0 1 102 68 V106 Z" fill={C.blueDeep} />
      <rect y="106" width="200" height="34" fill={C.surface} />
      <g fill={C.saffron} opacity="0.9">
        <rect x="12" y="113" width="6" height="6" transform="rotate(45 15 116)" />
        <rect x="28" y="113" width="6" height="6" transform="rotate(45 31 116)" />
        <rect x="44" y="113" width="6" height="6" transform="rotate(45 47 116)" />
        <rect x="128" y="113" width="6" height="6" transform="rotate(45 131 116)" />
        <rect x="144" y="113" width="6" height="6" transform="rotate(45 147 116)" />
        <rect x="160" y="113" width="6" height="6" transform="rotate(45 163 116)" />
        <rect x="176" y="113" width="6" height="6" transform="rotate(45 179 116)" />
      </g>
      <rect x="116" y="44" width="76" height="9" fill={C.saffron} />
      <rect x="116" y="44" width="19" height="9" fill={C.surface} />
      <rect x="154" y="44" width="19" height="9" fill={C.surface} />
      <rect x="112" y="78" width="84" height="28" rx="2" fill={C.ink} />
      <rect x="120" y="66" width="16" height="12" rx="1" fill={C.surface} />
      <rect x="138" y="62" width="16" height="16" rx="1" fill={C.rose} />
      <rect x="156" y="68" width="14" height="10" rx="1" fill={C.saffron} />
      <circle cx="184" cy="72" r="6" fill={C.palm} />
      <path d="M190 70 q5 -3 4 3" stroke={C.palm} strokeWidth="2" fill="none" strokeLinecap="round" />
      <line x1="52" y1="0" x2="52" y2="16" stroke={C.ink} strokeWidth="2" />
      <circle cx="52" cy="25" r="8" fill={C.saffron} />
      <circle cx="52" cy="25" r="14" fill={C.saffron} opacity="0.22" />
    </g>
  ),
  // 商队纸海:discover 空态(对应 riso paper-sea)
  "paper-sea": (
    <g>
      <rect width="200" height="140" rx="10" fill={C.surface} />
      <circle cx="163" cy="30" r="15" fill={C.saffron} />
      <circle cx="158" cy="25" r="3" fill={C.surface} opacity="0.6" />
      <path d="M0 84 Q 40 68 84 84 T 200 88 V140 H0 Z" fill={C.sand} />
      <path d="M0 104 Q 56 90 110 104 T 200 108 V140 H0 Z" fill={C.sandDeep} />
      <rect x="30" y="42" width="15" height="19" rx="1" fill={C.surface} stroke={C.blue} strokeWidth="1.4" transform="rotate(-12 37 51)" />
      <rect x="152" y="52" width="14" height="18" rx="1" fill={C.surface} stroke={C.terra} strokeWidth="1.4" transform="rotate(10 159 61)" />
      <g fill={C.ink}>
        <circle cx="78" cy="88" r="7" />
        <rect x="70" y="93" width="34" height="12" rx="6" />
        <rect x="72" y="103" width="4" height="13" rx="2" />
        <rect x="98" y="103" width="4" height="13" rx="2" />
        <path d="M71 88 q-7 -2 -7 -9" stroke={C.ink} strokeWidth="3" fill="none" strokeLinecap="round" />
      </g>
      <rect x="78" y="80" width="18" height="10" rx="1" fill={C.blue} />
      <rect x="82" y="76" width="12" height="8" rx="1" fill={C.saffron} />
      <circle cx="122" cy="90" r="5" fill={C.ink} />
      <rect x="118" y="94" width="8" height="19" rx="4" fill={C.ink} />
      <line x1="104" y1="97" x2="116" y2="93" stroke={C.ink} strokeWidth="2" strokeLinecap="round" />
      <Khatam x={16} y={110} s={10} fill={C.blue} hole={C.saffron} />
    </g>
  ),
  // 庭院灯讲:chat 空态/工作台占位(对应 riso lighthouse)
  lighthouse: (
    <g>
      <rect width="200" height="140" rx="10" fill={C.blueDeep} />
      <path d="M84 140 V52 A30 30 0 0 1 144 52 V140 Z" fill={C.blue} />
      <path d="M94 140 V60 A20 20 0 0 1 134 60 V140 Z" fill={C.saffron} />
      <circle cx="114" cy="86" r="9" fill={C.ink} />
      <rect x="102" y="96" width="24" height="32" rx="9" fill={C.ink} />
      <rect x="96" y="102" width="16" height="11" rx="1" fill={C.surface} transform="rotate(-7 104 107)" />
      <line x1="40" y1="0" x2="40" y2="22" stroke={C.saffron} strokeWidth="1.6" />
      <circle cx="40" cy="30" r="8" fill={C.saffron} />
      <circle cx="40" cy="30" r="14" fill={C.saffron} opacity="0.2" />
      <line x1="168" y1="0" x2="168" y2="34" stroke={C.saffron} strokeWidth="1.6" />
      <circle cx="168" cy="41" r="6" fill={C.saffron} />
      <circle cx="168" cy="41" r="11" fill={C.saffron} opacity="0.18" />
      <rect x="20" y="120" width="160" height="10" rx="5" fill={C.surface} />
      <path d="M28 125 Q 40 121 52 125 T 76 125 T 100 125 T 124 125 T 148 125 T 172 125"
        stroke={C.blue} strokeWidth="2" fill="none" opacity="0.85" />
      <Khatam x={12} y={14} s={9} fill={C.saffron} />
    </g>
  ),
  // 阿特拉斯群峰:graph 空态/judge 占位(对应 riso peaks)
  peaks: (
    <g>
      <rect width="200" height="140" rx="10" fill={C.sand} />
      <circle cx="170" cy="28" r="14" fill={C.saffron} />
      <g stroke={C.saffron} strokeWidth="2" strokeLinecap="round">
        <line x1="170" y1="7" x2="170" y2="11" />
        <line x1="191" y1="28" x2="195" y2="28" />
        <line x1="145" y1="28" x2="149" y2="28" />
      </g>
      <path d="M-6 140 L46 62 L96 140 Z" fill={C.blue} />
      <path d="M56 140 L108 48 L160 140 Z" fill={C.ink} />
      <path d="M118 140 L164 76 L210 140 Z" fill={C.terra} />
      <path d="M96 76 L108 48 L120 76 L108 68 Z" fill={C.surface} />
      <line x1="108" y1="48" x2="108" y2="28" stroke={C.ink} strokeWidth="2.4" />
      <rect x="108" y="28" width="15" height="9" rx="1" fill={C.rose} />
      <circle cx="76" cy="112" r="4.5" fill={C.surface} />
      <rect x="72" y="116" width="8" height="11" rx="3.5" fill={C.surface} />
      <line x1="68" y1="127" x2="68" y2="112" stroke={C.saffron} strokeWidth="2" strokeLinecap="round" />
      <Khatam x={28} y={20} s={10} fill={C.blue} hole={C.sand} />
    </g>
  ),
  // 花砖放大镜:ideas 已解决空态(对应 riso magnify)
  magnify: (
    <g>
      <rect width="200" height="140" rx="10" fill={C.sand} />
      {Array.from({ length: 12 }, (_, i) => {
        const col = i % 6, row = Math.floor(i / 6);
        const cx = 20 + col * 32, cy = 26 + row * 28;
        const c = [C.blue, C.saffron, C.rose, C.palm][(col + row) % 4];
        return (
          <rect key={i} x={cx - 9} y={cy - 9} width="18" height="18"
            transform={`rotate(45 ${cx} ${cy})`} fill={c} opacity="0.26" />
        );
      })}
      <circle cx="128" cy="70" r="30" fill={C.surface} stroke={C.blue} strokeWidth="7" />
      <circle cx="128" cy="70" r="26" fill={C.blue} opacity="0.12" />
      <Khatam x={118} y={60} s={20} fill={C.saffron} hole={C.surface} />
      <rect x="152" y="90" width="34" height="9" rx="4.5" transform="rotate(38 152 94)" fill={C.terra} />
      <circle cx="34" cy="98" r="8" fill={C.blue} />
      <circle cx="72" cy="98" r="8" fill={C.saffron} />
      <circle cx="53" cy="98" r="8" fill={C.rose} />
    </g>
  ),
  // 透光拱门观星:ideas 页头/研究缝隙(对应 riso observatory)
  observatory: (
    <g>
      <rect width="200" height="140" rx="10" fill={C.blueDeep} />
      <circle cx="30" cy="22" r="1.6" fill={C.sand} /><circle cx="60" cy="14" r="1.3" fill={C.sand} />
      <circle cx="104" cy="18" r="1.5" fill={C.sand} /><circle cx="184" cy="60" r="1.4" fill={C.sand} />
      <path d="M118 140 V58 A28 28 0 0 1 174 58 V140 Z" fill={C.blue} />
      <circle cx="146" cy="34" r="12" fill={C.saffron} />
      <circle cx="152" cy="30" r="10" fill={C.blueDeep} />
      <circle cx="152" cy="72" r="1.4" fill={C.sand} /><circle cx="162" cy="62" r="1.2" fill={C.sand} />
      <rect x="104" y="112" width="84" height="28" fill={C.ink} />
      <rect x="28" y="50" width="46" height="10" rx="4" transform="rotate(-18 28 50)" fill={C.saffron} />
      <circle cx="32" cy="58" r="7" fill={C.ink} />
      <rect x="24" y="62" width="15" height="36" rx="4" fill={C.ink} />
      <circle cx="90" cy="36" r="14" stroke={C.saffron} strokeWidth="2" strokeDasharray="5 4" fill="none" />
      <text x="90" y="42" textAnchor="middle" fontFamily="var(--font-mono,monospace)"
        fontSize="15" fontWeight="700" fill={C.saffron}>?</text>
    </g>
  ),
  // 花砖工坊:404 海报页(对应 riso press-house)
  "press-house": (
    <g>
      <rect width="200" height="140" rx="10" fill={C.sand} />
      <rect x="24" y="26" width="96" height="84" rx="6" fill={C.surface} stroke={C.ink} strokeWidth="1.6" />
      {Array.from({ length: 9 }, (_, i) => {
        const col = i % 3, row = Math.floor(i / 3);
        const x = 34 + col * 30, y = 36 + row * 26;
        const c = [C.blue, C.saffron, C.rose, C.palm, C.terra][(i * 2 + row) % 5];
        return (
          <g key={i}>
            <rect x={x} y={y} width="24" height="20" rx="2" fill={c} />
            <rect x={x + 7} y={y + 5} width="10" height="10" transform={`rotate(45 ${x + 12} ${y + 10})`}
              fill={C.surface} opacity="0.85" />
          </g>
        );
      })}
      <rect x="132" y="40" width="54" height="70" rx="2" fill={C.surface}
        stroke={C.ink} strokeWidth="1.6" transform="rotate(4 159 75)" />
      <line x1="140" y1="58" x2="176" y2="56" stroke={C.blue} strokeWidth="3" strokeLinecap="round" />
      <line x1="140" y1="68" x2="174" y2="66" stroke={C.blue} strokeWidth="3" strokeLinecap="round" />
      <line x1="140" y1="78" x2="172" y2="76" stroke={C.saffron} strokeWidth="3" strokeLinecap="round" />
      <line x1="140" y1="88" x2="170" y2="86" stroke={C.rose} strokeWidth="3" strokeLinecap="round" />
      <Khatam x={20} y={116} s={13} fill={C.saffron} hole={C.sand} />
      <Khatam x={44} y={120} s={11} fill={C.blue} hole={C.sand} />
    </g>
  ),
  // 八角星旗(品牌纹样,与 riso 波浪旗同构换装):侧栏品牌区徽标
  "wavy-flag": (
    <g>
      <circle cx="172" cy="26" r="10" fill={C.saffron} />
      <path d="M62 22 C 82 32 100 12 118 22 C 136 32 152 14 168 24 L 162 78
               C 144 70 130 86 112 78 C 94 70 80 86 62 78 Z"
        fill={C.surface} stroke={C.blueDeep} strokeWidth="3.4" strokeLinejoin="round" />
      <Khatam x={99} y={37} s={22} fill={C.blue} hole={C.saffron} />
      <rect x="72" y="60" width="8" height="8" transform="rotate(45 76 64)" fill={C.rose} />
      <rect x="134" y="60" width="8" height="8" transform="rotate(45 138 64)" fill={C.palm} />
    </g>
  ),
};

/** 插图组件。契约:仅 majorelle 主题渲染——其它主题返回 null,挂载点无需写条件。 */
export function MedinaIllust({ name, size = 150 }: { name: IllustName; size?: number }) {
  const { theme } = useShell();
  if (theme !== "majorelle") return null;
  return (
    <span className="medina-illust" aria-hidden>
      <svg width={size} height={Math.round(size * 0.7)} viewBox="0 0 200 140" fill="none">
        {SCENES[name]}
      </svg>
    </span>
  );
}

/** 马蹄拱 + 八角星徽标(侧栏品牌区 majorelle 态用,24px 线稿版)。 */
export function MedinaArchGlyph({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M5 21 V11 a7 7 0 0 1 14 0 V21" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
      <rect x="9.2" y="9.2" width="5.6" height="5.6" transform="rotate(45 12 12)"
        stroke="currentColor" strokeWidth="1.4" />
      <line x1="3.5" y1="21" x2="20.5" y2="21" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}
