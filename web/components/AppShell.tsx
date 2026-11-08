"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { Health, Paper, fetchHealth, listPapers } from "@/lib/api";
import {
  Chat, Compass, Eye, Graph, Judge, Library, LogoMark, Pen, Plus, Reader,
} from "./icons";

interface NavItem {
  href: string;
  label: string;
  Icon: (p: { size?: number; className?: string }) => React.JSX.Element;
  schedule?: string; // 未实现模块的排期徽章
}

const NAV: NavItem[] = [
  { href: "/", label: "文献库", Icon: Library },
  { href: "/chat", label: "溯源问答", Icon: Chat },
  { href: "/reader", label: "阅读器", Icon: Reader },
  { href: "/graph", label: "引用图谱", Icon: Graph },
  { href: "/discover", label: "检索发现", Icon: Compass },
  { href: "/ideas", label: "研究缝隙", Icon: Graph },
  { href: "/copilot", label: "写作 Copilot", Icon: Pen },
  { href: "/judge", label: "评审模式", Icon: Judge, schedule: "D11" },
  { href: "/audit", label: "透明度中心", Icon: Eye },
];

function relTime(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "刚刚";
  if (s < 3600) return `${Math.round(s / 60)}分钟前`;
  if (s < 86400) return `${Math.round(s / 3600)}小时前`;
  return `${Math.round(s / 86400)}天前`;
}

export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [papers, setPapers] = useState<Paper[]>([]);
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [ps, h] = await Promise.all([listPapers(), fetchHealth()]);
        if (alive) { setPapers(ps); setHealth(h); }
      } catch { /* 后端未起时侧边栏静默降级 */ }
    };
    load();
    const t = setInterval(load, 6000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const firstParsed = papers.find((p) => p.parsed);
  const readerHref = firstParsed ? `/reader/${firstParsed.id}` : "/reader";

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="side-brand">
          <span className="brand-mark"><LogoMark size={22} /></span>
          <span className="brand-name">Meridian</span>
          <span className="brand-badge">SCHOLAR</span>
        </div>

        <Link href="/" className="side-cta">
          <Plus size={16} /> 上传论文
        </Link>

        <div className="side-group">
          <span className="side-group-label">模块</span>
          {NAV.map(({ href, label, Icon, schedule }) => {
            const active =
              href === "/" ? pathname === "/" :
              href === "/chat" ? pathname === "/chat" :
              href === "/reader" ? pathname.startsWith("/reader") :
              pathname.startsWith(href);
            return (
              <Link key={href} href={href}
                className={`side-item ${active ? "active" : ""} ${schedule ? "soon" : ""}`}>
                <Icon size={16} />
                <span>{label}</span>
                {schedule && <span className="side-schedule">{schedule}</span>}
              </Link>
            );
          })}
        </div>

        <div className="side-group side-papers">
          <span className="side-group-label">文献库 <em>{papers.length}</em></span>
          <Suspense fallback={null}>
            <PaperTree papers={papers} />
          </Suspense>
          {papers.length === 0 && <p className="side-empty">暂无论文，去上传一份 PDF</p>}
        </div>

        <div className="side-footer">
          <span className="avatar">M</span>
          <span className="side-user">Meridian Demo</span>
          <span className={`status-dot ${health?.llm.available ? "dot-ok" : "dot-warn"}`}
            title={health
              ? `LLM ${health.llm.available ? `就绪(${health.llm.model})` : "不可用·抽取式降级"} · Dense ${health.dense ? "开" : "关"}`
              : "后端未连接"} />
        </div>
      </aside>

      <main className="main">{children}</main>
    </div>
  );
}

function PaperTree({ papers }: { papers: Paper[] }) {
  const params = useSearchParams();
  const currentPaper = params.get("paper");
  return (
    <>
      {papers.map((p) => (
        <Link key={p.id}
          href={`/chat?paper=${p.id}`}
          className={`side-paper ${currentPaper === p.id ? "active" : ""}`}
          title={p.title}>
          <span className="paper-title">{p.title || "解析中…"}</span>
          <span className="paper-meta">
            {p.parsed ? `${p.page_count} 页 · ${relTime(p.created_at)}` : "解析中…"}
          </span>
        </Link>
      ))}
    </>
  );
}
