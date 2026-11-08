"use client";

/**
 * 检索发现（Discover 闭环，4.4 加分主菜）：
 * 研究方向 → arXiv 检索 → 勾选下载 → 解析 → 跳转阅读/提问。
 * auto 模式一次闭环（下载 top1 → 等解析 → 给出证据块）；
 * 每张卡可懒加载 GitHub 复现性分（stars/活跃度/许可证加权）。
 * 断网演示：后端 tool_cache 回放（source=cache_offline 徽章如实标注）。
 */
import { useState } from "react";
import Link from "next/link";
import {
  ArxivEntry, discoverDownload, fetchRepro, ReproRepo, streamDiscover,
} from "@/lib/api";

type Phase = "idle" | "searching" | "done" | "error";

export default function DiscoverPage() {
  const [query, setQuery] = useState("");
  const [auto, setAuto] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [statusLine, setStatusLine] = useState("");
  const [entries, setEntries] = useState<ArxivEntry[]>([]);
  const [source, setSource] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [dlState, setDlState] = useState<{ arxiv: string; msg: string } | null>(null);
  const [finalNote, setFinalNote] = useState("");
  const [finalPaper, setFinalPaper] = useState<string>("");
  const [repros, setRepros] = useState<Record<string, ReproRepo[]>>({});

  const run = () => {
    if (!query.trim() || phase === "searching") return;
    setPhase("searching");
    setEntries([]);
    setSource("");
    setPicked(null);
    setDlState(null);
    setFinalNote("");
    setFinalPaper("");
    setStatusLine("改写检索词…");
    streamDiscover(query.trim(), {
      status: (d) => setStatusLine(`[${d.stage}] ${d.note}`),
      results: (d) => {
        setEntries(d.entries);
        setSource(d.source);
        setStatusLine(`检索完成：${d.entries.length} 篇`);
        setPhase("done");
      },
      final: (d) => {
        setFinalNote(String(d.note ?? ""));
        if (d.paper_id) setFinalPaper(String(d.paper_id));
        setPhase("done");
      },
      error: (d) => {
        setStatusLine(`出错：${d.message}`);
        setPhase("error");
      },
    }, auto).catch((e) => {
      setStatusLine(`请求失败：${e}`);
      setPhase("error");
    });
  };

  const download = async (arxivId: string) => {
    if (dlState) return;
    setPicked(arxivId);
    setDlState({ arxiv: arxivId, msg: "下载中…" });
    try {
      const out = await discoverDownload(arxivId);
      if (out.dedup === "exact_file") {
        setDlState({ arxiv: arxivId,
          msg: `库中已有同一文件：${out.existing_title ?? out.paper_id}（精确去重复用）` });
        setFinalPaper(out.paper_id);
      } else {
        setDlState({ arxiv: arxivId,
          msg: `解析${out.parse?.status === "done" ? "完成" : "进行中"}（${out.parse?.status ?? "?"}）` });
        setFinalPaper(out.paper_id);
      }
    } catch (e) {
      setDlState({ arxiv: arxivId, msg: `失败：${e}` });
    }
  };

  const loadRepro = async (arxivId: string, title: string) => {
    setRepros((prev) => ({ ...prev, [arxivId]: prev[arxivId] ?? [] }));
    const out = await fetchRepro(title);
    if (out.ok) setRepros((prev) => ({ ...prev, [arxivId]: out.repos }));
  };

  return (
    <div className="page">
      <div className="page-inner">
        <h1>检索发现 <span className="chip accent">arXiv → 下载 → 解析 → 检索 闭环</span></h1>
        <div className="card">
          <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
            <input value={query} onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && run()}
              placeholder="研究方向，如：2024 年后关于 Mamba 的线性注意力论文"
              style={{ flex: 1, minWidth: 280, border: "1px solid var(--line-strong)",
                borderRadius: "var(--radius-sm)", padding: "10px 12px",
                fontFamily: "var(--font)", outline: "none" }} />
            <label className="chip" style={{ cursor: "pointer", display: "flex", gap: 6 }}>
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
              auto 闭环（自动下载 top1）
            </label>
            <button className="btn primary" onClick={run} disabled={phase === "searching"}>
              {phase === "searching" ? "检索中…" : "检索"}
            </button>
          </div>
          {statusLine && <p className="kv" style={{ marginTop: "var(--space-2)" }}>{statusLine}</p>}
          {source === "cache_offline" && (
            <span className="chip warn" title="当前为离线缓存回放，联网后自动恢复实况">
              离线回放 · tool_cache
            </span>
          )}
          {finalNote && <p className="kv" style={{ color: "var(--ok, #16a34a)" }}>✓ {finalNote}</p>}
          {finalPaper && (
            <p className="kv" style={{ display: "flex", gap: 12, marginTop: 8 }}>
              <Link className="chip accent" href={`/reader/${finalPaper}`}>打开阅读器 →</Link>
              <Link className="chip" href="/chat">去就该论文提问 →</Link>
            </p>
          )}
        </div>

        {entries.map((e) => (
          <div className="card" key={e.arxiv_id}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
              <b>{e.title}</b>
              <span className="chip">{e.published}</span>
            </div>
            <p className="kv" style={{ margin: "6px 0" }}>{e.authors.join(" · ")}</p>
            <p style={{ fontSize: 13, opacity: 0.85 }}>{e.abstract}…</p>
            <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", alignItems: "center" }}>
              <button className="chip accent" style={{ cursor: "pointer" }}
                onClick={() => download(e.arxiv_id)}
                disabled={!!dlState && dlState.arxiv !== picked}>
                {picked === e.arxiv_id ? "已选择" : "下载并解析"}
              </button>
              <button className="chip" style={{ cursor: "pointer" }}
                onClick={() => loadRepro(e.arxiv_id, e.title)}>
                查复现性
              </button>
              <span className="chip">{e.arxiv_id}</span>
              {(repros[e.arxiv_id] ?? []).map((r) => (
                <span key={r.repo} className="chip ok"
                  title={`${r.url} · stars ${r.stars} · license ${r.license || "-"}`}>
                  ⭐ {r.repo} · 复现性 {r.repro_score ?? "-"}
                </span>
              ))}
            </div>
            {dlState && dlState.arxiv === e.arxiv_id && (
              <p className="kv" style={{ marginTop: "var(--space-2)" }}>{dlState.msg}</p>
            )}
          </div>
        ))}

        {phase === "idle" && (
          <div className="card">
            <p className="kv">
              输入研究方向开始。流程：LLM 改写英文检索词 → arXiv Atom API（时间倒序）→
              勾选下载（arxiv.org 主源 + export 备源，精确去重复用）→ 解析路由（MinerU/PyMuPDF）→
              kb_search 证据块。断网时自动回放上次结果缓存。
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
