"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { API, getPaper } from "@/lib/api";

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
  const [paper, setPaper] = useState<Awaited<ReturnType<typeof getPaper>> | null>(null);
  const [docir, setDocir] = useState<DocIR | null>(null);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");

  useEffect(() => {
    getPaper(id as string).then(setPaper);
    fetch(`${API}/api/papers/${id}/docir`, { cache: "no-store" })
      .then((r) => r.json()).then(setDocir);
  }, [id]);

  if (!docir) return <p className="kv">加载 DocIR 中…（若刚上传请等解析完成）</p>;

  const pages = Array.from(new Set(docir.blocks.map((b) => b.page))).sort((a, b) => a - b);
  const blocks = docir.blocks
    .filter((b) => b.page === page)
    .filter((b) => !query || b.text.toLowerCase().includes(query.toLowerCase()));

  return (
    <div>
      <h1>
        {docir.meta.title || paper?.title || "阅读器"}
        <span className="badge gray" style={{ marginLeft: 10 }}>
          {docir.meta.page_count} 页 · {docir.blocks.length} blocks · {docir.tables.length} 表 · {docir.figures.length} 图
        </span>
      </h1>
      <div className="chat-wrap">
        <div className="card" style={{ maxHeight: "70vh", overflowY: "auto" }}>
          <h2>标题树（DocIR 解析）</h2>
          {docir.toc.map((t, i) => (
            <div key={i} style={{ paddingLeft: (t.level - 1) * 16, fontSize: 14 }}>
              <a href="#" onClick={(e) => { e.preventDefault(); setPage(t.page); }}
                style={{ color: "var(--ink)", textDecoration: "none" }}>
                <b style={{ opacity: 2 - t.level >= 1 ? 1 : 0.65 }}>{t.title}</b>
                <span className="kv"> · p{t.page + 1}</span>
              </a>
            </div>
          ))}
          {docir.toc.length === 0 && <p className="kv">未识别出标题结构。</p>}
        </div>

        <div>
          <div className="card">
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <b>第 {page + 1} 页</b>
              <button className="btn ghost" onClick={() => setPage(Math.max(0, page - 1))}>←</button>
              <select value={page} onChange={(e) => setPage(Number(e.target.value))}>
                {pages.map((p) => <option key={p} value={p}>p{p + 1}</option>)}
              </select>
              <button className="btn ghost" onClick={() => setPage(Math.min(pages.at(-1) ?? 0, page + 1))}>→</button>
              <input placeholder="页内搜索…" value={query} onChange={(e) => setQuery(e.target.value)}
                style={{ marginLeft: "auto", border: "1px solid var(--line)", borderRadius: 8, padding: "6px 10px" }} />
            </div>
          </div>
          <div className="card" style={{ maxHeight: "60vh", overflowY: "auto" }}>
            {blocks.map((b) => (
              <div key={b.id} style={{ margin: "10px 0" }}>
                {b.type === "heading"
                  ? <b style={{ display: "block", fontSize: 15 }}>{b.text}</b>
                  : <p style={{ margin: 0, fontSize: 14 }}>{b.text}</p>}
                <span className="kv" style={{ fontSize: 11 }}>
                  {b.id} · §{b.section_path === "_front" || !b.section_path ? "前言" : b.section_path} · bbox [{b.bbox.map((x) => x.toFixed(2)).join(", ")}]
                </span>
              </div>
            ))}
            {blocks.length === 0 && <p className="kv">本页无匹配内容。</p>}
          </div>
          {docir.tables.filter((t) => t.page === page).map((t) => (
            <div className="card" key={t.id}>
              <h2>表格 {t.id}（第 {t.page + 1} 页）</h2>
              <pre style={{ fontSize: 12, overflowX: "auto" }}>{t.markdown}</pre>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
