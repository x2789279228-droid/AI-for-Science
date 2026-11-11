"use client";

/**
 * 阅读器书架(/reader 索引页):收纳库内全部论文,点击书封进入 /reader/[id]。
 * - 已解析 → 书封(封面渐变按序轮换,显示页数/arXiv id/续读进度)
 * - 解析中 → 灰置书封占位,不可点击(诚实态)
 * - 续读进度读 localStorage rdpos:{id}(模块三 C 的既有存档),书封底部进度条
 */
import { useEffect, useState } from "react";
import Link from "next/link";
import { listPapers } from "@/lib/api";
import type { Paper } from "@/lib/api";
import { Library, Reader } from "@/components/icons";
import { RisoIllust } from "@/components/Illustrations";

interface ShelfBook {
  paper: Paper;
  savedPage?: number;   // 续读页码(1 基,来自 rdpos 存档)
}

/** 封面渐变三款轮换(全部在品牌蓝阶内)。 */
const COVER_STYLES = [
  "linear-gradient(165deg, var(--blue-500) 0%, var(--blue-700) 100%)",
  "linear-gradient(165deg, var(--sky-500) 0%, var(--blue-600) 100%)",
  "linear-gradient(165deg, var(--blue-600) 0%, var(--gray-900) 100%)",
];

export default function ReaderShelfPage() {
  const [papers, setPapers] = useState<Paper[]>([]);
  const [books, setBooks] = useState<ShelfBook[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    listPapers().then((ps) => {
      setPapers(ps);
      // 续读进度在客户端 localStorage(模块三 C 存档),挂载后读取避免水合不一致
      setBooks(ps.map((p) => {
        try {
          const saved = JSON.parse(localStorage.getItem(`rdpos:${p.id}`) || "null");
          return { paper: p, savedPage: saved?.page > 1 ? saved.page : undefined };
        } catch { return { paper: p }; }
      }));
      setLoading(false);
    }).catch(() => setLoading(false));
  }, []);

  const parsed = books.filter((b) => b.paper.parsed);
  const parsing = books.filter((b) => !b.paper.parsed);

  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 1080 }}>
        <h1>书架
          <span className="chip accent">{parsed.length} 本可读</span>
          {parsing.length > 0 && <span className="chip warn">{parsing.length} 本解析中</span>}
        </h1>

        {loading && <div className="card"><p className="kv">加载书架中…</p></div>}

        {!loading && books.length === 0 && (
          <div className="card card-in" style={{ display: "flex", gap: "var(--space-5)",
            alignItems: "center", flexWrap: "wrap" }}>
            <RisoIllust name="night-reader" size={190} />
            <div>
              <p style={{ fontWeight: 600, marginBottom: 4 }}>书架空空</p>
              <p className="kv">先到「文献库」上传一份 PDF,解析完成后这里会摆上你的第一本书。</p>
            </div>
          </div>
        )}

        {parsed.length > 0 && (
          <div className="shelf-grid">
            {parsed.map((b, i) => {
              const p = b.paper;
              const total = p.page_count || 0;
              const frac = b.savedPage && total ? Math.min(1, b.savedPage / total) : 0;
              return (
                <Link key={p.id} href={`/reader/${p.id}`} className="book card-in card-lift"
                  style={{ background: COVER_STYLES[i % COVER_STYLES.length],
                    ["--i" as string]: i }}
                  title={`${p.title || p.id} · 共 ${total} 页`}>
                  <span className="book-badge">{p.year ?? "PDF"}</span>
                  <span className="book-title">{p.title || "(未命名)"}</span>
                  <span className="book-meta num">
                    {p.arxiv_id ? `arXiv:${p.arxiv_id.split("v")[0]} · ` : ""}{total} 页
                  </span>
                  {frac > 0 ? (
                    <>
                      <span className="book-progress" aria-hidden>
                        <i style={{ width: `${Math.round(frac * 100)}%` }} />
                      </span>
                      <span className="book-read kv">续读 p{b.savedPage}/{total}</span>
                    </>
                  ) : (
                    <span className="book-read kv">从第一页开始 →</span>
                  )}
                </Link>
              );
            })}
          </div>
        )}

        {parsing.length > 0 && (
          <>
            <h2 style={{ fontSize: "var(--text-lg)", margin: "var(--space-6) 0 var(--space-3)",
              color: "var(--ink-2)" }}>解析中(暂不可读)</h2>
            <div className="shelf-grid">
              {parsing.map((b, i) => (
                <div key={b.paper.id} className="book book-unreadable card-in"
                  style={{ ["--i" as string]: i }}
                  title="解析完成后可阅读">
                  <span className="book-badge">…</span>
                  <span className="book-title">{b.paper.title || "(解析中…)"}</span>
                  <span className="book-read kv"><Reader size={12} /> 解析中…</span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
