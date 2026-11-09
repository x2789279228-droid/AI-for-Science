"use client";

/**
 * 检索发现（模块四，四区改造）：
 * ① 搜索区：方向关键词 → arXiv+S2+OpenAlex 多源去重（断网 tool_cache 回放）；
 * ② 参考文献下钻：BibTeX/RIS/纯文本列表 → S2 batch 补全 → 勾选下载；
 * ③ 追踪订阅：关键词/作者/分类 → Sickle/Atom 定时刷新 → 新论文候选；
 * ④ 推荐流：本地库画像打分（相关性/新近/引用含金量/GitHub 复现）+ 理由 + 赞踩。
 * 卡片显示：来源徽章 · 被引 · 引用含金量（有引用句时）· 复现性 · 推荐理由。
 */
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  API, ArxivEntry, discoverDownload, fetchRepro, listPapers, multiDownload, ReproRepo,
  recommendApi, Recommendation, streamDiscover, streamMultiSearch,
  trackingApi, TrackingSub, TrackedItem, MultiEntry,
} from "@/lib/api";
import type { Paper } from "@/lib/api";

type Phase = "idle" | "searching" | "done" | "error";
type Zone = "search" | "refs" | "track" | "rec";

export default function DiscoverPage() {
  const [zone, setZone] = useState<Zone>("search");
  const [papers, setPapers] = useState<Paper[]>([]);
  useEffect(() => { listPapers().then(setPapers).catch(() => {}); }, []);

  // ---- ① 搜索区（多源） ----
  const [query, setQuery] = useState("");
  const [auto, setAuto] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [statusLine, setStatusLine] = useState("");
  const [entries, setEntries] = useState<MultiEntry[]>([]);
  const [source, setSource] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const [dlState, setDlState] = useState<{ key: string; msg: string } | null>(null);
  const [finalPaper, setFinalPaper] = useState("");
  const [repros, setRepros] = useState<Record<string, ReproRepo[]>>({});

  const run = () => {
    if (!query.trim() || phase === "searching") return;
    setPhase("searching");
    setEntries([]);
    setStatusLine("多源检索中…");
    streamMultiSearch({ query: query.trim() }, {
      status: (d) => setStatusLine(`[${d.source ?? d.stage}] ${d.note}`),
      results: (d) => {
        setEntries(d.entries as unknown as MultiEntry[]);
        setSource(d.source);
        setStatusLine(`检索完成：${(d.entries ?? []).length} 篇（多源去重后）`);
        setPhase("done");
      },
      final: (d) => { if (d.paper_id) setFinalPaper(String(d.paper_id)); },
      error: (d) => { setStatusLine(`出错：${d.message}`); setPhase("error"); },
    }).catch((e) => { setStatusLine(`请求失败：${e}`); setPhase("error"); });
    void auto; // auto 闭环沿用旧端点（保持行为）：勾选后同样可下载
  };

  const runAuto = () => {
    if (!query.trim() || phase === "searching") return;
    setPhase("searching");
    setEntries([]);
    setStatusLine("auto 闭环：改写检索词…");
    streamDiscover(query.trim(), {
      status: (d) => setStatusLine(`[${d.stage}] ${d.note}`),
      results: (d) => {
        setEntries((d.entries as unknown as ArxivEntry[]).map((e) => ({
          title: e.title, authors: e.authors, year: Number(e.published?.slice(0, 4)) || 0,
          venue: "", doi: "", arxiv_id: e.arxiv_id, citation_count: null,
          abstract: e.abstract, sources: ["arxiv"], pdf_url: e.pdf_url,
        })));
        setSource(d.source);
        setPhase("done");
      },
      final: (d) => { if (d.paper_id) setFinalPaper(String(d.paper_id)); },
      error: (d) => { setStatusLine(`出错：${d.message}`); setPhase("error"); },
    }, true).catch((e) => { setStatusLine(`请求失败：${e}`); setPhase("error"); });
  };

  const download = async (e: MultiEntry) => {
    const key = e.arxiv_id || e.title;
    if (!e.arxiv_id && !e.pdf_url) {
      setDlState({ key, msg: "无开放 PDF 链接（该条目不可下载，如实标注）" });
      return;
    }
    setPicked(key);
    setDlState({ key, msg: "下载中…" });
    try {
      const out = await multiDownload(e.arxiv_id, e.pdf_url);
      if (out.dedup === "exact_file") {
        setDlState({ key, msg: `库中已有同一文件（精确去重复用）` });
      } else {
        setDlState({ key, msg: `解析${out.parse?.status === "done" ? "完成" : "进行中"}` });
      }
      setFinalPaper(out.paper_id);
    } catch (err) {
      setDlState({ key, msg: `失败：${err}` });
    }
  };

  const loadRepro = async (key: string, title: string) => {
    setRepros((prev) => ({ ...prev, [key]: prev[key] ?? [] }));
    const out = await fetchRepro(title);
    if (out.ok) setRepros((prev) => ({ ...prev, [key]: out.repos }));
  };

  // ---- ② 参考文献下钻（含库内论文含金量闭环） ----
  const [refsText, setRefsText] = useState("");
  const [refsBusy, setRefsBusy] = useState(false);
  const [refsNote, setRefsNote] = useState("");
  const [libRefs, setLibRefs] = useState<{ entries: MultiEntry[]; quality: Record<number, {
    cls: string; weight: number; mention_count: number; examples: string[] }> } | null>(null);
  const [hideCheap, setHideCheap] = useState(false);

  const runRefs = () => {
    if (!refsText.trim() || refsBusy) return;
    setRefsBusy(true);
    setEntries([]);
    setLibRefs(null);
    setRefsNote("解析列表并补全元数据中…");
    streamMultiSearch({ references_text: refsText.trim() }, {
      status: (d) => setRefsNote(d.note),
      results: (d) => setEntries(d.entries as MultiEntry[]),
      final: (d) => setRefsNote(`✓ ${d.note ?? ""}`),
      error: (d) => setRefsNote(`出错：${d.message}`),
    }).catch((e) => setRefsNote(`请求失败：${e}`))
      .finally(() => setRefsBusy(false));
  };

  /** 从库内论文导入其参考文献（引用含金量闭环：实质/背景/凑数标注 + 可逆过滤）。 */
  const importLibRefs = async (pid: string) => {
    setRefsNote("读取论文参考文献与引用含金量…");
    try {
      const [docir, quality] = await Promise.all([
        fetch(`${API}/api/papers/${pid}/docir?outline=1`).then((r) => r.json()),
        fetch(`${API}/api/papers/${pid}/ref-quality`).then((r) => r.json()),
      ]);
      const q = quality?.qualities ?? {};
      const entries: MultiEntry[] = (docir?.references ?? []).map((r: {
        index: number; title?: string; raw?: string; arxiv_id?: string; doi?: string;
        authors?: string; year?: number; venue?: string;
      }, i: number) => ({
        title: r.title || r.raw || `（未解析标题 #${r.index}）`,
        authors: r.authors ? r.authors.split(/,| and /).slice(0, 6) : [],
        year: r.year || 0, venue: r.venue || "", doi: r.doi || "",
        arxiv_id: r.arxiv_id || "", citation_count: null,
        abstract: "", sources: ["library"], pdf_url: "",
        raw: r.raw?.slice(0, 160),
        resolved: true,
      }));
      setLibRefs({ entries, quality: q });
      setEntries([]);   // 与粘贴列表互斥展示
      const n = Object.keys(q).length;
      setRefsNote(`已导入 ${entries.length} 条参考文献（含金量覆盖 ${n} 条——有引用句的条目；其余为中性态）`);
    } catch (e) {
      setRefsNote(`导入失败：${e}`);
    }
  };

  const visibleLibRefs = libRefs
    ? (hideCheap
        ? libRefs.entries.filter((e, i) => libRefs.quality[i + 1]?.cls !== "perfunctory")
        : libRefs.entries)
    : [];

  // ---- ③ 追踪订阅 ----
  const [subs, setSubs] = useState<TrackingSub[]>([]);
  const [items, setItems] = useState<TrackedItem[]>([]);
  const [subName, setSubName] = useState("");
  const [subValue, setSubValue] = useState("");
  const [subType, setSubType] = useState<"keyword" | "author" | "category">("keyword");
  const [trackMsg, setTrackMsg] = useState("");

  const loadTrack = useCallback(async () => {
    try {
      const [s, it] = await Promise.all([
        trackingApi.subs(),
        trackingApi.items({ status: "new" }),
      ]);
      setSubs(s);
      setItems(it);
    } catch { /* 断网时保持空态 */ }
  }, []);

  useEffect(() => { if (zone === "track") void loadTrack(); }, [zone, loadTrack]);

  const addSub = async () => {
    if (!subName.trim() || !subValue.trim()) return;
    try {
      await trackingApi.addSub(subName.trim(), subType, subValue.trim());
      setSubName(""); setSubValue("");
      await loadTrack();
      setTrackMsg("订阅已添加，可立即刷新");
    } catch (e) { setTrackMsg(`添加失败：${e}`); }
  };

  const refreshTrack = async () => {
    setTrackMsg("刷新中…");
    try {
      const rs = await trackingApi.refresh();
      setTrackMsg(`刷新完成：` + rs.map((r) => `+${r.added}（${r.channel}）`).join(" · "));
      await loadTrack();
    } catch (e) { setTrackMsg(`刷新失败：${e}`); }
  };

  const ingestTracked = async (it: TrackedItem) => {
    setDlState({ key: it.arxiv_id, msg: "下载中…" });
    try {
      const out = await discoverDownload(it.arxiv_id);
      setDlState({ key: it.arxiv_id, msg: "已入库" + (out.dedup ? "（去重复用）" : "") });
      setFinalPaper(out.paper_id);
      await trackingApi.dismiss(it.id);
      await loadTrack();
    } catch (e) { setDlState({ key: it.arxiv_id, msg: `失败：${e}` }); }
  };

  // ---- ④ 推荐流 ----
  const [recs, setRecs] = useState<Recommendation[]>([]);
  const [recNote, setRecNote] = useState("");
  const [recBusy, setRecBusy] = useState(false);
  const [voted, setVoted] = useState<Record<string, 1 | -1>>({});

  const loadRecs = async (refresh: boolean) => {
    setRecBusy(true);
    try {
      const out = await recommendApi.list(refresh);
      setRecs(out.recommendations ?? []);
      setRecNote(out.note ?? "");
    } catch (e) { setRecNote(`加载失败：${e}`); }
    setRecBusy(false);
  };

  useEffect(() => { if (zone === "rec" && !recs.length) void loadRecs(false); }, [zone]); // eslint-disable-line react-hooks/exhaustive-deps

  const vote = async (r: Recommendation, v: 1 | -1) => {
    if (!r.id) return;
    setVoted((prev) => ({ ...prev, [r.id!]: v }));
    try { await recommendApi.vote(r.id!, v); } catch { /* 失败回滚提示 */ }
  };

  const entryCard = (e: MultiEntry, idxKey?: string) => {
    const key = idxKey ?? e.arxiv_id ?? e.title;
    const unresolved = e.resolved === false;
    return (
      <div className="card" key={key} style={{ opacity: unresolved ? 0.65 : 1 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
          <b style={{ flex: 1, minWidth: 200 }}>{e.title || e.raw || "（未解析标题）"}</b>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {e.sources.map((s) => <span key={s} className="chip" style={{ fontSize: 10 }}>{s}</span>)}
            {e.venue && <span className="chip" style={{ fontSize: 10 }}>{e.venue}</span>}
            {typeof e.citation_count === "number" && (
              <span className="chip" style={{ fontSize: 10 }} title="引用含金量信号（S2/OpenAlex）">
                被引 {e.citation_count}
              </span>
            )}
          </div>
        </div>
        {(e.authors?.length ?? 0) > 0 && (
          <p className="kv" style={{ margin: "6px 0" }}>{e.authors.join(" · ")}</p>
        )}
        {e.abstract && <p style={{ fontSize: 13, opacity: 0.85 }}>{e.abstract.slice(0, 220)}…</p>}
        <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", alignItems: "center" }}>
          {!unresolved && (
            <button className="chip accent" style={{ cursor: "pointer" }}
              onClick={() => download(e)}
              disabled={!!dlState && dlState.key !== key}>
              {picked === key ? "已选择" : (e.arxiv_id ? "下载并解析" : "下载开放 PDF")}
            </button>
          )}
          <button className="chip" style={{ cursor: "pointer" }}
            onClick={() => loadRepro(key, e.title)}>
            查复现性
          </button>
          {e.arxiv_id && <span className="chip">{e.arxiv_id}</span>}
          {(repros[key] ?? []).map((r) => (
            <span key={r.repo} className="chip ok"
              title={`${r.url} · stars ${r.stars} · license ${r.license || "-"}`}>
              ⭐ {r.repo} · 复现性 {r.repro_score ?? "-"}
            </span>
          ))}
          {unresolved && <span className="chip warn">未解析（如实标注，不冒充）</span>}
        </div>
        {dlState && dlState.key === key && (
          <p className="kv" style={{ marginTop: "var(--space-2)" }}>{dlState.msg}</p>
        )}
      </div>
    );
  };

  return (
    <div className="page">
      <div className="page-inner">
        <h1>检索 · 追踪 · 推荐
          <span className="chip accent">多源下钻 / arXiv 追踪 / 可解释推荐</span>
        </h1>

        <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", marginBottom: "var(--space-3)" }}>
          {([["search", "① 多源搜索"], ["refs", "② 参考文献下钻"],
             ["track", "③ arXiv 追踪"], ["rec", "④ 个性化推荐"]] as [Zone, string][]).map(([z, label]) => (
            <button key={z} className={`chip ${zone === z ? "accent" : ""}`}
              style={{ cursor: "pointer" }} onClick={() => setZone(z)}>{label}</button>
          ))}
        </div>

        {zone === "search" && (
          <>
            <div className="card">
              <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
                <input value={query} onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && run()}
                  placeholder="研究方向，如：线性注意力 Mamba（arXiv + Semantic Scholar + OpenAlex 三源去重）"
                  style={{ flex: 1, minWidth: 280, border: "1px solid var(--line-strong)",
                    borderRadius: "var(--radius-sm)", padding: "10px 12px",
                    fontFamily: "var(--font)", outline: "none" }} />
                <button className="btn primary" onClick={run} disabled={phase === "searching"}>
                  {phase === "searching" ? "检索中…" : "多源检索"}
                </button>
                <label className="chip" style={{ cursor: "pointer", display: "flex", gap: 6 }}>
                  <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
                  auto 闭环
                </label>
                {auto && (
                  <button className="chip accent" style={{ cursor: "pointer" }} onClick={runAuto}>
                    一次闭环（下载 top1 → 解析 → 证据）
                  </button>
                )}
              </div>
              {statusLine && <p className="kv" style={{ marginTop: "var(--space-2)" }}>{statusLine}</p>}
              {source === "cache_offline" && (
                <span className="chip warn">离线回放 · tool_cache（联网后自动恢复）</span>
              )}
              {finalPaper && (
                <p className="kv" style={{ display: "flex", gap: 12, marginTop: 8 }}>
                  <Link className="chip accent" href={`/reader/${finalPaper}`}>打开阅读器 →</Link>
                  <Link className="chip" href="/chat">去提问 →</Link>
                </p>
              )}
            </div>
            {entries.map((e) => entryCard(e))}
            {phase === "idle" && (
              <div className="card">
                <p className="kv">
                  三源检索（arXiv Atom / Semantic Scholar / OpenAlex，DOI→arXiv→标题归一去重）→
                  勾选下载 → 解析 → 检索闭环。断网时各源独立回放缓存。也可切换上方
                  「参考文献下钻」粘贴 BibTeX/RIS/DOI 列表批量补全。
                </p>
              </div>
            )}
          </>
        )}

        {zone === "refs" && (
          <>
            <div className="card">
              <h2 style={{ marginTop: 0 }}>参考文献列表下钻</h2>
              <p className="kv">
                粘贴 BibTeX / RIS / 逐行 DOI·arXiv·标题（S2 batch 补全后勾选入库）；
                或<b>从库内论文导入</b>——带「实质/背景/凑数」引用含金量标注与可逆过滤。
              </p>
              <textarea value={refsText} onChange={(e) => setRefsText(e.target.value)}
                placeholder={"@article{bert, title={BERT: Pre-training...}, year={2019}}\n10.1109/CVPR.2016.90\narXiv:1706.03762"}
                style={{ width: "100%", minHeight: 100, border: "1px solid var(--line-strong)",
                  borderRadius: "var(--radius-sm)", padding: 10, fontFamily: "var(--font-mono, monospace)",
                  fontSize: 12, outline: "none" }} />
              <div style={{ display: "flex", gap: "var(--space-2)", marginTop: "var(--space-2)", flexWrap: "wrap", alignItems: "center" }}>
                <button className="btn primary" onClick={runRefs} disabled={refsBusy}>
                  {refsBusy ? "解析中…" : "解析并补全"}
                </button>
                <select defaultValue=""
                  onChange={(e) => { if (e.target.value) void importLibRefs(e.target.value); }}
                  style={{ border: "1px solid var(--line-strong)", borderRadius: "var(--radius-sm)", padding: "8px 10px" }}>
                  <option value="">从库内论文导入参考文献…</option>
                  {papers.map((p) => (
                    <option key={p.id} value={p.id}>{(p.title || p.id).slice(0, 40)}</option>
                  ))}
                </select>
                {libRefs && (
                  <span className={`chip ${hideCheap ? "accent" : ""}`} style={{ cursor: "pointer" }}
                    onClick={() => setHideCheap((v) => !v)}
                    title="可逆过滤：隐藏「凑数引用」（权重 0.1）；无引用句条目保持中性态显示">
                    {hideCheap ? "已隐藏凑数（点击恢复）" : "隐藏凑数引用"}
                  </span>
                )}
              </div>
              {refsNote && <p className="kv" style={{ marginTop: 8 }}>{refsNote}</p>}
            </div>
            {libRefs && visibleLibRefs.map((e, i) => {
              const q = libRefs.quality[i + 1];
              return (
                <div className="card" key={`lr-${i}`} style={{ opacity: q?.cls === "perfunctory" ? 0.6 : 1 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                    <b style={{ flex: 1, minWidth: 200 }}>{e.title}</b>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      {q ? (
                        <span className={`chip ${q.cls === "substantive" ? "ok" : q.cls === "perfunctory" ? "warn" : ""}`}
                          style={{ fontSize: 10 }}
                          title={`含金量 ${q.weight}（${q.mention_count} 处引用句）\n${(q.examples ?? []).join("\n——\n")}`}>
                          {q.cls === "substantive" ? "实质" : q.cls === "background" ? "背景" : "凑数"}
                        </span>
                      ) : (
                        <span className="kv" style={{ fontSize: 10 }}>无引用句（中性）</span>
                      )}
                      {e.year ? <span className="chip" style={{ fontSize: 10 }}>{e.year}</span> : null}
                      {e.venue ? <span className="chip" style={{ fontSize: 10 }}>{e.venue}</span> : null}
                    </div>
                  </div>
                  {e.authors.length > 0 && <p className="kv" style={{ margin: "4px 0" }}>{e.authors.join(" · ")}</p>}
                  <div style={{ display: "flex", gap: "var(--space-2)", marginTop: 6, flexWrap: "wrap", alignItems: "center" }}>
                    {e.arxiv_id && (
                      <>
                        <span className="chip">{e.arxiv_id}</span>
                        <button className="chip accent" style={{ cursor: "pointer" }}
                          onClick={() => download(e)}>下载并解析</button>
                      </>
                    )}
                    {!e.arxiv_id && !e.doi && <span className="kv" style={{ fontSize: 11 }}>无 arXiv/DOI（需手动获取）</span>}
                  </div>
                  {dlState && dlState.key === (e.arxiv_id || e.title) && (
                    <p className="kv" style={{ marginTop: 6 }}>{dlState.msg}</p>
                  )}
                </div>
              );
            })}
            {!libRefs && entries.map((e, i) => entryCard(e, `ref-${i}`))}
          </>
        )}

        {zone === "track" && (
          <>
            <div className="card">
              <h2 style={{ marginTop: 0 }}>arXiv 动态追踪（Sickle OAI-PMH / Atom 双通道）</h2>
              <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", alignItems: "center" }}>
                <input value={subName} onChange={(e) => setSubName(e.target.value)}
                  placeholder="订阅名，如：线性注意力"
                  style={{ width: 160, border: "1px solid var(--line-strong)", borderRadius: "var(--radius-sm)",
                    padding: "8px 10px", fontFamily: "var(--font)" }} />
                <select value={subType} onChange={(e) => setSubType(e.target.value as typeof subType)}
                  style={{ border: "1px solid var(--line-strong)", borderRadius: "var(--radius-sm)", padding: "8px 10px" }}>
                  <option value="keyword">关键词</option>
                  <option value="author">作者</option>
                  <option value="category">分类 (cs.CL…)</option>
                </select>
                <input value={subValue} onChange={(e) => setSubValue(e.target.value)}
                  placeholder={subType === "category" ? "cs.CL" : subType === "author" ? "Yann LeCun" : "mamba attention"}
                  style={{ flex: 1, minWidth: 200, border: "1px solid var(--line-strong)", borderRadius: "var(--radius-sm)",
                    padding: "8px 10px", fontFamily: "var(--font)" }} />
                <button className="btn primary" onClick={addSub}>订阅</button>
                <button className="chip accent" style={{ cursor: "pointer" }} onClick={refreshTrack}>
                  立即刷新
                </button>
              </div>
              {trackMsg && <p className="kv" style={{ marginTop: 8 }}>{trackMsg}</p>}
              {subs.length > 0 && (
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
                  {subs.map((s) => (
                    <span key={s.id} className="chip" title={`类型 ${s.query_type} · 新条目 ${s.new_count ?? 0}`}>
                      📌 {s.name}（{s.value}）· 新 {s.new_count ?? 0}
                      <a style={{ marginLeft: 8, cursor: "pointer", textDecoration: "underline" }}
                        onClick={async () => { await trackingApi.delSub(s.id); await loadTrack(); }}>
                        删除
                      </a>
                    </span>
                  ))}
                </div>
              )}
            </div>
            {items.map((it) => (
              <div className="card" key={it.id}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                  <b style={{ flex: 1 }}>{it.title}</b>
                  <span className="chip">{it.published}</span>
                </div>
                <div style={{ display: "flex", gap: "var(--space-2)", marginTop: 8, flexWrap: "wrap", alignItems: "center" }}>
                  <span className="chip">{it.arxiv_id}</span>
                  <button className="chip accent" style={{ cursor: "pointer" }}
                    onClick={() => ingestTracked(it)}>
                    下载并解析
                  </button>
                  <button className="chip" style={{ cursor: "pointer" }}
                    onClick={async () => { await trackingApi.dismiss(it.id); await loadTrack(); }}>
                    忽略
                  </button>
                  {dlState?.key === it.arxiv_id && <span className="kv">{dlState.msg}</span>}
                </div>
              </div>
            ))}
            {!items.length && (
              <div className="card"><p className="kv">暂无新条目——添加订阅后点「立即刷新」；后台线程每 6 小时自动增量刷新（水位线续跑）。</p></div>
            )}
          </>
        )}

        {zone === "rec" && (
          <>
            <div className="card">
              <h2 style={{ marginTop: 0 }}>个性化推荐（本地库画像 · 可解释打分）</h2>
              <p className="kv">
                打分 = 相关性 0.35 + 新近 0.20 + 引用含金量 0.20 + GitHub 复现 0.15 + 关键词 0.10；
                候选来自你的追踪订阅（不凭空编造）。👍/👇 反馈会实时重加权你的画像。
              </p>
              <button className="btn primary" onClick={() => loadRecs(true)} disabled={recBusy}>
                {recBusy ? "打分中…" : "重新生成推荐"}
              </button>
              {recNote && <p className="kv" style={{ marginTop: 8 }}>{recNote}</p>}
            </div>
            {recs.map((r, i) => (
              <div className="card" key={r.id ?? i}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
                  <b style={{ flex: 1 }}>{r.title}</b>
                  <span className="chip accent">推荐分 {Object.values(r.scores ?? {}).length
                    ? "见理由" : ""}</span>
                </div>
                <p className="kv" style={{ margin: "6px 0" }}>💡 {r.reason}</p>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                  {r.arxiv_id && <span className="chip">{r.arxiv_id}</span>}
                  {Object.entries(r.scores ?? {}).map(([k, v]) => (
                    <span key={k} className="chip" style={{ fontSize: 10 }}
                      title={`${k} 分量`}>{k} {v ?? "N/A"}</span>
                  ))}
                  {r.id && (
                    <>
                      <button className={`chip ${voted[r.id] === 1 ? "ok" : ""}`} style={{ cursor: "pointer" }}
                        onClick={() => vote(r, 1)} disabled={voted[r.id] !== undefined}>👍 有用</button>
                      <button className={`chip ${voted[r.id] === -1 ? "warn" : ""}`} style={{ cursor: "pointer" }}
                        onClick={() => vote(r, -1)} disabled={voted[r.id] !== undefined}>👇 不相关</button>
                    </>
                  )}
                </div>
              </div>
            ))}
            {!recs.length && !recBusy && (
              <div className="card"><p className="kv">暂无推荐——先到「arXiv 追踪」添加订阅并刷新，候选池有货后这里会给出带理由的推荐。</p></div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
