/* Meridian 图标集：inline SVG，stroke 1.5，零依赖。size 单位 px，默认 16。 */
import type { CSSProperties } from "react";

interface IconProps {
  size?: number;
  className?: string;
  style?: CSSProperties;
}

function svgProps({ size = 16, className, style }: IconProps) {
  return {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none" as const,
    stroke: "currentColor",
    strokeWidth: 1.5,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    className,
    style,
    "aria-hidden": true,
  };
}

/** Meridian 品牌：外圆 + 中央子午经线 + 赤道横线 */
export function LogoMark({ size = 22, className, style }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className={className} style={style} aria-hidden>
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.8" />
      <ellipse cx="12" cy="12" rx="4" ry="9" stroke="currentColor" strokeWidth="1.5" />
      <path d="M3 12h18" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

export const Plus = (p: IconProps) => (
  <svg {...svgProps(p)}><path d="M12 5v14M5 12h14" /></svg>
);

export const Library = (p: IconProps) => (
  <svg {...svgProps(p)}>
    <path d="M4 19.5V5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2Zm0 0a2 2 0 0 0 2 2h13v-2" />
    <path d="M9 7h6" />
  </svg>
);

export const Chat = (p: IconProps) => (
  <svg {...svgProps(p)}>
    <path d="M21 12a8 8 0 0 1-8 8H4l2.5-2.5A8 8 0 1 1 21 12Z" />
    <path d="M9 11h6M9 14h4" />
  </svg>
);

export const Reader = (p: IconProps) => (
  <svg {...svgProps(p)}>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z" />
    <path d="M14 3v5h5M9 13h6M9 17h6" />
  </svg>
);

export const Graph = (p: IconProps) => (
  <svg {...svgProps(p)}>
    <circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="8" r="2.5" />
    <circle cx="12" cy="18" r="2.5" /><circle cx="5" cy="17" r="2" />
    <path d="M8.2 7.2 15.6 8M7 8.2l3.8 7.6M16.4 10l-3 5.7M7 16.5l2.6.7" />
  </svg>
);

export const Compass = (p: IconProps) => (
  <svg {...svgProps(p)}>
    <circle cx="12" cy="12" r="9" />
    <path d="m15.5 8.5-2 5-5 2 2-5Z" />
  </svg>
);

export const Pen = (p: IconProps) => (
  <svg {...svgProps(p)}>
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
  </svg>
);

export const Judge = (p: IconProps) => (
  <svg {...svgProps(p)}>
    <path d="M12 3 4.5 6v5c0 4.5 3 8.5 7.5 10 4.5-1.5 7.5-5.5 7.5-10V6Z" />
    <path d="m9 11.5 2 2 4-4" />
  </svg>
);

export const Eye = (p: IconProps) => (
  <svg {...svgProps(p)}>
    <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);

export const Search = (p: IconProps) => (
  <svg {...svgProps(p)}>
    <circle cx="11" cy="11" r="6.5" />
    <path d="m20 20-3.8-3.8" />
  </svg>
);

export const Upload = (p: IconProps) => (
  <svg {...svgProps(p)}>
    <path d="M12 16V4m0 0 4 4m-4-4L8 8" />
    <path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
  </svg>
);

export const ChevronRight = (p: IconProps) => (
  <svg {...svgProps(p)}><path d="m9 6 6 6-6 6" /></svg>
);

export const Alert = (p: IconProps) => (
  <svg {...svgProps(p)}>
    <path d="M12 3 2.5 20h19Z" />
    <path d="M12 10v4M12 17.2v.1" />
  </svg>
);

export const Spark = (p: IconProps) => (
  <svg {...svgProps(p)}>
    <path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);
