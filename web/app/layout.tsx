import type { Metadata } from "next";
import AppShell from "@/components/AppShell";
import "./globals.css";

export const metadata: Metadata = {
  title: "Meridian · 科研助手 Agent",
  description: "PDF 深度解析 · 抗幻觉溯源问答 · DocIR 数据底座",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>
        {/* 防主题闪烁:首帧同步读取皮肤存档(ShellContext 挂载后会接管) */}
        <script dangerouslySetInnerHTML={{ __html:
          "try{if(localStorage.getItem('meridian:theme')==='riso')document.documentElement.dataset.theme='riso'}catch(e){}" }} />
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
