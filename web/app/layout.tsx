import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "科研助手 Agent",
  description: "PDF 深度解析 · 抗幻觉溯源问答 · DocIR 数据底座",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <header className="topbar">
          <a className="brand" href="/">📚 科研助手 Agent</a>
          <nav>
            <a href="/">文献库</a>
            <a href="/chat">溯源问答</a>
            <a href="/audit" target="_blank" rel="noreferrer">透明度中心 ↗</a>
          </nav>
        </header>
        <main className="container">{children}</main>
      </body>
    </html>
  );
}
