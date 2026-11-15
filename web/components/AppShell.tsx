"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { motion } from "framer-motion";
import { Health, Paper, fetchHealth, listPapers } from "@/lib/api";
import { IslandProvider, useIsland } from "@/lib/island";
import { RisoFlagGlyph } from "./Illustrations";
import { MedinaArchGlyph } from "./MedinaIllustrations";
import { SwordGlyph } from "./WuxiaIllustrations";
import WuxiaTagline from "./WuxiaTagline";
import { SIDE_MOTTO } from "@/lib/wuxia-quotes";
import { ShellProvider, useShell } from "./ShellContext";
import AppearancePanel from "./AppearancePanel";
import BrandIntro from "./BrandIntro";
import DynamicIsland from "./DynamicIsland";
import {
  Activity, Chat, Compass, Eye, Graph, Judge, Library, LogoMark, Palette, PanelLeft, Pen, Plus, Reader,
} from "./icons";

interface NavItem {
  href: string;
  label: string;
  Icon: (p: { size?: number; className?: string }) => React.JSX.Element;
  /** 双模式可见性：缺省=仅专业模式（学术专属页）；"both"=两模式都显示 */
  modes?: "both";
}

const NAV: NavItem[] = [
  { href: "/", label: "文献库", Icon: Library, modes: "both" },
  { href: "/chat", label: "溯源问答", Icon: Chat, modes: "both" },
  { href: "/reader", label: "阅读器", Icon: Reader, modes: "both" },
  { href: "/graph", label: "引用图谱", Icon: Graph },
  { href: "/discover", label: "检索发现", Icon: Compass },
  { href: "/ideas", label: "研究缝隙", Icon: Graph },
  { href: "/copilot", label: "写作 Copilot", Icon: Pen },
  { href: "/judge", label: "评审导览", Icon: Judge },
  { href: "/audit", label: "透明度中心", Icon: Eye },
  { href: "/health", label: "健康中心", Icon: Activity },
];

/** 普通模式可见的导航集合（学术专属页全部隐藏） */
const GENERAL_NAV_HREFS = new Set(NAV.filter((n) => n.modes === "both").map((n) => n.href));

function relTime(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "刚刚";
  if (s < 3600) return `${Math.round(s / 60)}分钟前`;
  if (s < 86400) return `${Math.round(s / 3600)}小时前`;
  return `${Math.round(s / 86400)}天前`;
}

/** 组合根:Shell(沉浸/折叠/intro 信号) + Island(全局事件) + 品牌动画 + 动态岛 */
export default function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <ShellProvider>
      <IslandProvider>
        <ShellFrame>{children}</ShellFrame>
        <BrandIntro />
        <DynamicIsland />
      </IslandProvider>
    </ShellProvider>
  );
}

function ShellFrame({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { immersive, setImmersive, collapsed, toggleCollapsed, theme, docMode, setDocMode } = useShell();
  const isGeneral = docMode === "general";
  const [papers, setPapers] = useState<Paper[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const island = useIsland();
  const llmPrev = useRef<boolean | null>(null);
  const [refsCount, setRefsCount] = useState<number | null>(null);
  const [skinOpen, setSkinOpen] = useState(false);      // 外观面板开合(v1.3)

  // 数据规模行 refs 数:C2 首页工作台拉取图谱统计后写入 sessionStorage,侧栏读缓存(零额外请求)
  useEffect(() => {
    try {
      const v = sessionStorage.getItem("meridian:stats.refs");
      if (v) setRefsCount(Number(v));
    } catch { /* 忽略 */ }
  }, []);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        // 双模式：侧栏文献树/统计只展示当前模式的文档（general ↔ doc_type=general）
        const [ps, h] = await Promise.all([
          listPapers(false, isGeneral ? "general" : "paper"), fetchHealth(),
        ]);
        if (alive) { setPapers(ps); setHealth(h); }
      } catch { /* 后端未起时侧边栏静默降级 */ }
    };
    load();
    const t = setInterval(load, 6000);
    return () => { alive = false; clearInterval(t); };
  }, [isGeneral]);

  // 切到普通模式时若停在学术专属页 → 回文献库（导航已隐藏，防滞留）。
  // 详情路由（如 /reader/[id]）按前缀匹配，不误伤普通模式内的子页面。
  useEffect(() => {
    if (!isGeneral) return;
    const visible = GENERAL_NAV_HREFS.has(pathname)
      || Array.from(GENERAL_NAV_HREFS).some((h) => h !== "/" && pathname.startsWith(h + "/"));
    if (!visible) router.replace("/");
  }, [isGeneral, pathname, router]);

  // LLM 可用性翻转 → 动态岛 ping(每会话只在状态变化时提示)
  useEffect(() => {
    const avail = health?.llm.available;
    if (avail === undefined) return;
    if (llmPrev.current !== null && llmPrev.current !== avail) {
      island.emit(avail
        ? { key: "llm", kind: "info", title: `LLM 已就绪(${health?.llm.model ?? ""})` }
        : { key: "llm", kind: "info", title: "LLM 不可用 · 已降级为抽取式证据模式" });
    }
    llmPrev.current = avail;
  }, [health, island]);

  // 快捷键 [ 折叠侧边栏(输入控件聚焦时不拦截)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const inInput = !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
        || t.tagName === "SELECT" || t.isContentEditable);
      if (inInput) return;
      if (e.key === "[") { e.preventDefault(); toggleCollapsed(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleCollapsed]);

  return (
    <div className={`app${immersive ? " immersive" : ""}${collapsed ? " nav-collapsed" : ""}`}>
      <aside className="sidebar">
        <div className="side-brand">
          <span className="brand-mark">
            {theme === "riso" ? <RisoFlagGlyph size={22} />
              : theme === "majorelle" ? <MedinaArchGlyph size={22} />
              : theme === "wuxia" ? <SwordGlyph size={22} />
              : <LogoMark size={22} />}
          </span>
          {!collapsed && <span className="brand-name">Meridian</span>}
          {!collapsed && <span className="brand-badge">{isGeneral ? "DOCS" : "SCHOLAR"}</span>}
        </div>

        <Link href="/" className="side-cta" title={isGeneral ? "上传普通文档" : "上传论文"}>
          <Plus size={16} /> {!collapsed && (isGeneral ? "上传文档" : "上传论文")}
        </Link>

        {!collapsed && (
          <div className="side-group">
            <span className="side-group-label">模块</span>
          </div>
        )}
        <div className="side-group" style={collapsed ? { marginTop: 0 } : undefined}>
          {NAV.filter((n) => !isGeneral || n.modes === "both").map(({ href, label, Icon }, i) => {
            const active =
              href === "/" ? pathname === "/" :
              href === "/chat" ? pathname === "/chat" :
              href === "/reader" ? pathname.startsWith("/reader") :
              pathname.startsWith(href);
            return (
              <Link key={href} href={href}
                title={collapsed ? label : undefined}
                className={`side-item ${active ? "active" : ""} nav-in`}
                style={{ ["--i" as string]: i }}>
                {active && (
                  <motion.span layoutId="nav-pill" className="nav-pill"
                    transition={{ type: "spring", stiffness: 400, damping: 30 }} />
                )}
                <Icon size={16} />
                {!collapsed && <span>{label}</span>}
              </Link>
            );
          })}
        </div>

        {!collapsed && (
          <div className="side-group" style={{ marginTop: "auto" }}>
            <span className="side-group-label">模式</span>
            <div className="seg mode-switch" data-testid="mode-switch"
              style={{ display: "flex", width: "100%", margin: "4px 0 10px" }}>
              <button className={!isGeneral ? "on" : ""} style={{ flex: 1 }}
                data-testid="mode-scholar"
                onClick={() => setDocMode("scholar")}
                title="专业模式：学术论文全功能（图谱/发现/写作/治理）">
                <Reader size={12} style={{ verticalAlign: -2, marginRight: 4 }} />专业模式
              </button>
              <button className={isGeneral ? "on" : ""} style={{ flex: 1 }}
                data-testid="mode-general"
                onClick={() => setDocMode("general")}
                title="普通模式：任意 PDF 文档的阅读与问答（教材/报告/合同）">
                <Library size={12} style={{ verticalAlign: -2, marginRight: 4 }} />普通模式
              </button>
            </div>
          </div>
        )}
        {collapsed && (
          <div style={{ display: "flex", justifyContent: "center", padding: "0 0 10px" }}>
            <button className="mini-btn" data-testid="mode-switch"
              title={isGeneral ? "切换到专业模式" : "切换到普通模式"}
              onClick={() => setDocMode(isGeneral ? "scholar" : "general")}
              style={{ width: 24, height: 24, padding: 0, display: "inline-flex",
                alignItems: "center", justifyContent: "center", borderRadius: 8,
                color: isGeneral ? "var(--accent)" : "inherit" }}>
              <Reader size={13} />
            </button>
          </div>
        )}

        {!collapsed && (
          <div className="side-group side-papers">
            <span className="side-group-label">
              {isGeneral ? "文档库" : "文献库"} <em>{papers.length}</em>
            </span>
            <Suspense fallback={null}>
              <PaperTree papers={papers} isGeneral={isGeneral} />
            </Suspense>
            {papers.length === 0 && (
              <p className="side-empty">{isGeneral ? "暂无文档，去上传一份 PDF" : "暂无论文，去上传一份 PDF"}</p>
            )}
          </div>
        )}

        {!collapsed && (
          <div className="side-stats num" data-testid="side-stats">
            {isGeneral ? `${papers.length} 份文档` : `${papers.length} 篇${refsCount ? ` · ${refsCount} refs` : ""}`}
          </div>
        )}

        {/* 武侠立言(仅 wuxia):侧栏右缘竖排站桩铭志 */}
        {theme === "wuxia" && !collapsed && (
          <div className="side-motto" data-testid="side-motto"
            title={`${SIDE_MOTTO.text} —— ${SIDE_MOTTO.from}`}>
            {SIDE_MOTTO.text}
          </div>
        )}

        <div className="side-footer">
          <span className="avatar">M</span>
          {!collapsed && <span className="side-user">Meridian Demo</span>}
          <span className={`status-dot ${health?.llm.available ? "dot-ok" : "dot-warn"}`}
            title={health
              ? `LLM ${health.llm.available ? `就绪(${health.llm.model})` : "不可用·抽取式降级"} · Dense ${health.dense ? "开" : "关"}`
              : "后端未连接"} />
          {/* 外观面板唤起钮(v1.3 恢复切换器):Palette 图标 + 相对定位锚点 */}
          <div className="skin-anchor">
            <button className="mini-btn" data-testid="appearance-btn"
              onClick={() => setSkinOpen((v) => !v)} aria-haspopup="dialog" aria-expanded={skinOpen}
              title="外观（皮肤）"
              style={{ width: 24, height: 24, padding: 0, display: "inline-flex",
                alignItems: "center", justifyContent: "center", borderRadius: 8 }}>
              <Palette size={13} />
            </button>
            <AppearancePanel open={skinOpen} onClose={() => setSkinOpen(false)} />
          </div>
          <button className="mini-btn" onClick={toggleCollapsed}
            title={collapsed ? "展开侧边栏（[）" : "折叠侧边栏（[）"}
            style={{ marginLeft: collapsed ? 0 : "auto", flex: collapsed ? "none" : "none",
              width: 24, height: 24, padding: 0, display: "inline-flex",
              alignItems: "center", justifyContent: "center", borderRadius: 8 }}>
            <PanelLeft size={13} />
          </button>
        </div>
      </aside>

      <main className="main">
        <WuxiaTagline />
        {children}
      </main>

      {immersive && (
        <button className="immersive-exit" data-testid="immersive-exit"
          onClick={() => setImmersive(false)}>
          退出沉浸 <kbd>Esc</kbd>
        </button>
      )}
    </div>
  );
}

function PaperTree({ papers, isGeneral }: { papers: Paper[]; isGeneral: boolean }) {
  const params = useSearchParams();
  const currentPaper = params.get("paper");
  const famCount = new Map<string, number>();
  for (const p of papers) {
    if (p.family_id) famCount.set(p.family_id, (famCount.get(p.family_id) ?? 0) + 1);
  }
  return (
    <>
      {papers.map((p) => {
        const famN = p.family_id ? famCount.get(p.family_id) ?? 1 : 1;
        return (
          <Link key={p.id}
            href={`/chat?paper=${p.id}`}
            className={`side-paper ${currentPaper === p.id ? "active" : ""}`}
            title={p.title}>
            <span className="paper-top">
              {/* 双模式：族徽章/版本号仅学术文献渲染（普通文档无版本族概念） */}
              {!isGeneral && famN > 1 && (
                <span className="paper-fam" title={`同族 ${famN} 个版本`}>▦</span>
              )}
              <span className="paper-title">{p.title || "解析中…"}</span>
              {!isGeneral && p.version && <span className="paper-ver">{p.version}</span>}
            </span>
            <span className="paper-meta">
              {p.parsed ? `${p.page_count} 页 · ${relTime(p.created_at)}` : "解析中…"}
            </span>
          </Link>
        );
      })}
    </>
  );
}
