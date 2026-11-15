/** @type {import('next').NextConfig} */
const nextConfig = {
  // 产物目录分离（性能修复）：dev（next dev）与生产（next start）此前共用 .next，
  // 交替运行会互相覆写产物（next start 报 MODULE_NOT_FOUND）。
  // dev 默认写 .next；生产以 NEXT_DIST_DIR=.next-prod 显式指定，见 deploy/start-prod.bat。
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

export default nextConfig;
