"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { API, Paper, getPaper } from "@/lib/api";

interface Block {
  id: string; type: string; text: string; page: number;
  bbox: number[]; section_path: string;
}
interface DocIR {
  meta: { title: string; arxiv_id: string; page_count: number };
  toc: { level: number; title: string; page: number }[];
  blocks: Block[];
  tables: { id: string; page: number; markdown: string }[];
  figures: { id: string; page: number; caption: string }[];
}

export default function ReaderPage() {
  const { id } = useParams<{ id: string }>();
  const [paper, setPaper] = useState<Paper | null>(null);
  const [docir, setDocir] = useState<DocIR | null>(null);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");

  useEffect(() => {
    getPaper(id as string).then(setPaper);
    fetch(`${API}/api/papers/${id}/docir`, { cache: "no-store" })
      .then((r) => r.json()).then(setDocir);
  }, [id]);

  if (!docir) {
    return (
      <div className="page"><div className="page-inner">
        <p className="kv">加载 DocIR 中…（若刚上传请等解析完成）</p>
      </div></div>
    );
  }

  const pages = Array.from(new Set(docir.blocks.map((b) => b.page))).sort((a, b) => a - b);
  const blocks = docir.blocks
    .filter((b) => b.page === page)
    .filter((b) => !query || b.text.toLowerCase().includes(query.toLowerCase()));

  return (
    <>
      <div className="topbar">
        <span className="topbar-title">{docir.meta.title || paper?.title || "阅读器"}</span>
        <span className="chip">{docir.meta.page_count} 页</span>
        <span className="chip">{docir.blocks.length} blocks</span>
        <span className="chip">{docir.tables.length} 表 · {docir.figures.length} 图</span>
      </div>
      <div className="page">
        <div className="reader-grid">
          <div className="card toc-panel" style={{ marginBottom: 0, maxHeight: "100%" }}>
            <h2>标题树（DocIR 解析）</h2>
            {docir.toc.map((t, i) => (
              <div key={i} style={{ paddingLeft: (t.level - 1) * 14 }}>
                <span className="toc-item" onClick={() => setPage(t.page)}>
                  {t.level === 1 ? <b>{t.title}</b> : t.title} <span className="toc-page">p{t.page + 1}</span>
                </span>
              </div>
            ))}
            {docir.toc.length === 0 && <p className="kv">未识别出标题结构。</p>}
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)", minHeight: 0 }}>
            <div className="card" style={{ marginBottom: 0 }}>
              <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", flexWrap: "wrap" }}>
                <b className="kv">第 {page + 1} 页</b>
                <button className="btn ghost" style={{ padding: "4px 12px" }}
                  onClick={() => setPage(Math.max(0, page - 1))}>←</button>
                <select value={page} onChange={(e) => setPage(Number(e.target.value))}
                  style={{ border: "1px solid var(--line-strong)", borderRadius: "var(--radius-sm)",
                    padding: "4px 8px", fontFamily: "var(--font)", fontSize: "var(--text-sm)" }}>
                  {pages.map((p) => <option key={p} value={p}>p{p + 1}</option>)}
                </select>
                <button className="btn ghost" style={{ padding: "4px 12px" }}
                  onClick={() => setPage(Math.min(pages.at(-1) ?? 0, page + 1))}>→</button>
                <input placeholder="页内搜索…" value={query} onChange={(e) => setQuery(e.target.value)}
                  style={{ marginLeft: "auto", border: "1px solid var(--line-strong)",
                    borderRadius: "var(--radius-sm)", padding: "6px 10px",
                    fontFamily: "var(--font)", fontSize: "var(--text-sm)", outline: "none" }} />
              </div>
            </div>

            <div className="card" style={{ marginBottom: 0, flex: 1, overflowY: "auto" }}>
              {blocks.map((b) => (
                <div key={b.id} className="block-item">
                  {b.type === "heading"
                    ? <b>{b.text}</b>
                    : <p>{b.text}</p>}
                  <span className="block-anchor">
                    {b.id} · §{b.section_path === "_front" || !b.section_path ? "前言" : b.section_path}
                    {" · "}bbox [{b.bbox.map((x) => x.toFixed(2)).join(", ")}]
                  </span>
                </div>
              ))}
              {blocks.length === 0 && <p className="kv">本页无匹配内容。</p>}
            </div>

            {docir.tables.filter((t) => t.page === page).map((t) => (
              <div className="card" key={t.id} style={{ marginBottom: 0 }}>
                <h2>表格 {t.id}（第 {t.page + 1} 页）</h2>
                <pre style={{ fontSize: "var(--text-xs)", overflowX: "auto", margin: 0 }}>{t.markdown}</pre>
              </div>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}
