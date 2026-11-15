"use client";

/**
 * 主题插图统一分发（v1.4）：同一场景名（IllustName）按当前皮肤渲染对应插画集。
 * riso / majorelle / wuxia 三集各自按主题自守卫（非本主题返回 null），
 * 挂载点从此不再感知皮肤数量——新增第四/五套皮肤只改本文件。
 */
import { RisoIllust } from "./Illustrations";
import { MedinaIllust } from "./MedinaIllustrations";
import { WuxiaIllust } from "./WuxiaIllustrations";
import type { IllustName } from "./Illustrations";

export default function ThemeIllust({ name, size = 150 }: { name: IllustName; size?: number }) {
  return (
    <>
      <RisoIllust name={name} size={size} />
      <MedinaIllust name={name} size={size} />
      <WuxiaIllust name={name} size={size} />
    </>
  );
}
