"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  API, DedupAlert, FamilyGroup, Job, Paper,
  cancelJob, dedupDecision, deletePaper, fetchFamilies, listPapers,
  renamePaper, reparsePaper, restorePaper, uploadPaperWithProgress,
} from "@/lib/api";
import { Alert, Upload } from "@/components/icons";

type Phase = "uploading" | "parsing" | "done" | "reused" | "error";

interface QueueItem {
  key: string;
  name: string;
  frac: number;        // 上传字节进度 0~1
  phase: Phase;
  message: string;
  paperId?: string;
  jobId?: string;
}

const BREAKDOWN_LABELS: Record<string, string> = {
  title: "标题", vector: "摘要向量", author: "作者", minhash: "正文指纹",
  rerank: "精排", llm: "LLM复核", containment: "包含度", ngram: "字符n-gram", year: "年份",
};

export default function LibraryPage() {
  const [papers, setPapers] = useState<Paper[]>([]);
  const [archived, setArchived] = useState<Paper[]>([]);
  const [jobs, setJobs] = useState<Record<string, Job>>({});        // jobId -> Job
  const [paperJob, setPaperJob] = useState<Record<string, string>>({}); // paperId -> jobId
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [notice, setNotice] = useState("");
  const [dragging, setDragging] = useState(false);
  const [busyId, setBusyId] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [families, setFamilies] = useState<FamilyGroup[]>([]);
  const [collapsedFams, setCollapsedFams] = useState<Record<string, boolean>>({});
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    const [active, all, fams] = await Promise.all([
      listPapers(), listPapers(true), fetchFamilies().catch(() => []),
    ]);
    setPapers(active);
    setArchived(all.filter((p) => p.status === "archived"));
    setFamilies(fams);
  }, []);

  // 族分组（L8 族视图）：≥2 成员的族成组渲染，散篇保持平铺
  const grouped = useMemo(() => {
    const map: Record<string, FamilyGroup> = {};
    for (const f of families) for (const v of f.versions) map[v.id] = f;
    const groups: { group: FamilyGroup; rows: Paper[] }[] = [];
    const singles: Paper[] = [];
    const seen = new Set<string>();
    for (const p of papers) {
      const f = map[p.id];
      if (f && f.versions.length >= 2) {
        if (seen.has(f.family_id)) continue;
        seen.add(f.family_id);
        const ids = new Set(f.versions.map((v) => v.id));
        groups.push({ group: f, rows: papers.filter((x) => ids.has(x.id)) });
      } else {
        singles.push(p);
      }
    }
    return { groups, singles };
  }, [papers, families]);

  useEffect(() => {
    const timer = setInterval(async () => {
      const running = Object.values(jobs).filter(
        (j) => j.status === "queued" || j.status === "running"
          || j.status === "cancel_requested"
      );
      if (!running.length) return;
      for (const j of running) {
        const r = await fetch(`${API}/api/jobs/${j.id}`, { cache: "no-store" });
        if (r.ok) {
          const fresh: Job = await r.json();
          setJobs((prev) => ({ ...prev, [j.id]: fresh }));
          if (["done", "error", "cancelled"].includes(fresh.status)) {
            refresh();
            setQueue((prev) => prev.map((q) => q.jobId === j.id && q.phase === "parsing"
              ? { ...q, phase: fresh.status === "done" ? "done" : fresh.status === "error"
                ? "error" : "done",
                message: fresh.status === "done" ? "解析完成" : fresh.message }
              : q));
          }
        }
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [jobs, refresh]);

  useEffect(() => { refresh(); }, [refresh]);

  const upload = async (files: File[]) => {
    const items: QueueItem[] = files.map((f, i) => ({
      key: `${Date.now()}_${i}_${f.name}`, name: f.name, frac: 0,
      phase: "uploading", message: "上传中 0%",
    }));
    setQueue((prev) => [...items, ...prev].slice(0, 12));
    setNotice("");
    for (let i = 0; i < files.length; i++) {
      const key = items[i].key;
      try {
        // 逐文件独立处理：单个失败不影响其他（批量 P0 硬要求）
        const data = await uploadPaperWithProgress(files[i], (frac) => {
          setQueue((prev) => prev.map((q) => q.key === key
            ? { ...q, frac, message: `上传中 ${Math.round(frac * 100)}%` } : q));
        });
        if (data.dedup === "exact_file") {
          setQueue((prev) => prev.map((q) => q.key === key
            ? { ...q, phase: "reused", message: `与《${data.existing_title}》文件相同，已复用` } : q));
        } else if (data.job_id) {
          setJobs((prev) => ({ ...prev, [data.job_id!]: {
            id: data.job_id!, status: "queued", progress: 0, message: "排队中" } }));
          setPaperJob((prev) => ({ ...prev, [data.paper_id!]: data.job_id! }));
          setQueue((prev) => prev.map((q) => q.key === key
            ? { ...q, phase: "parsing", frac: 1, paperId: data.paper_id, jobId: data.job_id!,
              message: "排队解析…" } : q));
        } else {
          setQueue((prev) => prev.map((q) => q.key === key
            ? { ...q, phase: "done", message: "完成" } : q));
        }
      } catch (e) {
        setQueue((prev) => prev.map((q) => q.key === key
          ? { ...q, phase: "error", message: e instanceof Error ? e.message : String(e) } : q));
      }
    }
    await refresh();
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const files = Array.from(e.dataTransfer.files).filter(
      (f) => f.name.toLowerCase().endsWith(".pdf"));
    if (files.length) upload(files);
  };

  const decide = async (p: Paper, alert: DedupAlert, decision: "cover" | "keep" | "coexist") => {
    setBusyId(p.id);
    try {
      await dedupDecision(p.id, decision, alert.other_paper_id);
      setNotice(`已${decision === "cover" ? "覆盖（旧版已归档）" : decision === "keep"
        ? "保留库中版本（新上传已归档）" : "两版本并存"}：《${alert.other_title}》`);
      await refresh();
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId("");
    }
  };

  const diffHref = (p: Paper, alert: DedupAlert) =>
    alert.direction === "new"
      ? `/diff/${p.id}?base=${alert.other_paper_id}`
      : `/diff/${alert.other_paper_id}?base=${p.id}`;

  const rowActions = async (p: Paper, action: "rename" | "reparse" | "delete") => {
    setBusyId(p.id);
    try {
      if (action === "rename") {
        const title = window.prompt("修改标题", p.title);
        if (title && title.trim()) { await renamePaper(p.id, title.trim()); await refresh(); }
      } else if (action === "reparse") {
        const r = await reparsePaper(p.id);
        setJobs((prev) => ({ ...prev, [r.job_id]: {
          id: r.job_id, status: "queued", progress: 0, message: "重新排队解析" } }));
        setPaperJob((prev) => ({ ...prev, [p.id]: r.job_id }));
      } else if (action === "delete") {
        if (!window.confirm(`彻底删除《${p.title}》？文件与全部索引将被清理，不可恢复。`)) return;
        await deletePaper(p.id);
        await refresh();
      }
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId("");
    }
  };

  const paperRow = (p: Paper) => {
    const jobId = paperJob[p.id];
    const job = jobId ? jobs[jobId] : undefined;
    const active = job && !["done", "error", "cancelled"].includes(job.status);
    const alerts = p.dedup_alerts ?? (p.dedup_alert ? [p.dedup_alert] : []);
    return (
      <div key={p.id}>
        {alerts.map((a) => (
          <div key={a.id} className="alert" style={{ flexWrap: "wrap" }}>
            <Alert size={15} />
            <div style={{ flex: 1, minWidth: 260 }}>
              <div>
                <b>{a.direction === "new" ? "疑似同族版本：" : "库中新上传了同族版本："}</b>
                《{a.other_title}》{a.other_version ? `（${a.other_version}）` : ""}
                ，综合相似度 <b>{Math.round(a.score * 100)}%</b>。{a.reason}
              </div>
              <div className="score-chips">
                {Object.entries(a.breakdown)
                  .filter(([k, v]) => typeof v === "number" && BREAKDOWN_LABELS[k])
                  .map(([k, v]) => (
                    <span key={k} className="chip score-chip">
                      {BREAKDOWN_LABELS[k]} {Number(v).toFixed(2)}
                    </span>
                  ))}
                {a.breakdown.same_arxiv_id ? <span className="chip score-chip">arXiv 同源</span> : null}
                <a className="chip score-chip link" href={diffHref(p, a)}>查看差异 →</a>
              </div>
              <div className="decision-row">
                <button className="mini-btn" disabled={busyId === p.id}
                  onClick={() => decide(p, a, "cover")}>覆盖（启用新版）</button>
                <button className="mini-btn" disabled={busyId === p.id}
                  onClick={() => decide(p, a, "keep")}>保留库中版本</button>
                <button className="mini-btn" disabled={busyId === p.id}
                  onClick={() => decide(p, a, "coexist")}>并存</button>
              </div>
            </div>
          </div>
        ))}
        <div className="paper-row">
          <div className="t">
            <b>{p.title || "(解析中…)"}</b>
            <small>
              {p.arxiv_id ? `arXiv:${p.arxiv_id} · ` : ""}
              {p.page_count ? `${p.page_count} 页 · ` : ""}
              {p.supersedes ? "取代库中旧版 · " : ""}
              {p.superseded_by ? "已被新版取代 · " : ""}
              id {p.id}
            </small>
          </div>
          <div className="actions">
            {p.parsed ? (
              <>
                <a href={`/reader/${p.id}`}>阅读</a>
                <a href={`/chat?paper=${p.id}`}>问答</a>
              </>
            ) : active ? (
              <span className="chip">
                {job!.status === "error" ? `失败：${job!.message}`
                  : `${job!.message || job!.status} ${Math.round(job!.progress * 100)}%`}
              </span>
            ) : (
              <span className="chip">解析中…</span>
            )}
            {active && job && (
              <button className="mini-btn" disabled={busyId === p.id}
                onClick={async () => { await cancelJob(job.id); }}>
                取消
              </button>
            )}
            <button className="mini-btn" disabled={busyId === p.id}
              onClick={() => rowActions(p, "rename")}>重命名</button>
            <button className="mini-btn" disabled={busyId === p.id}
              onClick={() => rowActions(p, "reparse")}>重新解析</button>
            <button className="mini-btn danger" disabled={busyId === p.id}
              onClick={() => rowActions(p, "delete")}>删除</button>
          </div>
        </div>
        {active && job && (
          <div style={{ margin: "var(--space-2) 0 var(--space-3)" }}>
            <div className="progress">
              <div style={{ width: `${Math.round(job.progress * 100)}%` }} />
            </div>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="page">
      <div className="page-inner">
        <h1>文献库</h1>

        <div className="card">
          <h2>上传 PDF</h2>
          <div
            className={`dropzone${dragging ? " drag" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => fileRef.current?.click()}
          >
            <Upload size={22} />
            <div><b>拖拽 PDF 到此处</b>，或点击选择文件（支持批量）</div>
            <small className="muted">
              矢量 PDF / 扫描件 / 超长文档；同名同文件自动去重，语义同族上传后预警
            </small>
            <input ref={fileRef} type="file" accept="application/pdf" multiple
              onChange={(e) => e.target.files && upload(Array.from(e.target.files))}
              style={{ display: "none" }} />
          </div>
          {queue.length > 0 && (
            <div className="queue-list">
              {queue.map((q) => (
                <div key={q.key} className={`queue-item ${q.phase}`}>
                  <span className="queue-name">{q.name}</span>
                  <span className="queue-msg">
                    {q.phase === "error" ? `失败：${q.message}` : q.message}
                  </span>
                  {q.phase === "uploading" && (
                    <div className="progress">
                      <div style={{ width: `${Math.round(q.frac * 100)}%` }} />
                    </div>
                  )}
                  {q.phase === "parsing" && q.jobId && jobs[q.jobId] && (
                    <div className="progress">
                      <div style={{ width: `${Math.round((jobs[q.jobId].progress || 0) * 100)}%` }} />
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          {notice && (
            <div className="alert" style={{ marginTop: "var(--space-3)", marginBottom: 0 }}>
              <Alert size={15} />{notice}
            </div>
          )}
        </div>

        <div className="card">
          <h2>已入库（{papers.length}）</h2>
          {papers.length === 0 && <p className="kv">暂无论文，先上传一份 PDF。</p>}
          {grouped.groups.map(({ group, rows }) => {
            const collapsed = !!collapsedFams[group.family_id];
            return (
              <div key={group.family_id}
                style={{ border: "1px solid var(--border, #e5e7eb)", borderRadius: 8,
                         padding: "var(--space-2)", margin: "var(--space-2) 0" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap",
                              cursor: "pointer" }}
                  onClick={() => setCollapsedFams((m) => ({ ...m, [group.family_id]: !collapsed }))}>
                  <b>{group.canonical_title}</b>
                  <span className="chip">{group.versions.length} 个版本</span>
                  {group.canonical_version &&
                    <span className="chip score-chip">最新 {group.canonical_version}</span>}
                  {group.pending_alerts > 0 && (
                    <span className="chip score-chip">{group.pending_alerts} 条待决策</span>
                  )}
                  <span className="muted">{collapsed ? "展开 ▸" : "收起 ▾"}</span>
                </div>
                {!collapsed && (
                  <>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap",
                                  margin: "var(--space-2) 0" }}>
                      {group.versions.map((v) => (
                        <span key={v.id}
                          className={`chip${v.status === "archived" ? " muted" : ""}`}>
                          {v.status === "archived"
                            ? <s>{v.version || "无版本号"}（已归档）</s>
                            : <a href={`/reader/${v.id}`}>{v.version || "无版本号"}</a>}
                          {v.pending_alerts > 0 ? " ⚠" : ""}
                        </span>
                      ))}
                    </div>
                    {rows.map(paperRow)}
                  </>
                )}
              </div>
            );
          })}
          {grouped.singles.map(paperRow)}
        </div>

        {archived.length > 0 && (
          <div className="card">
            <h2 style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 8 }}
              onClick={() => setShowArchived((v) => !v)}>
              已归档（{archived.length}）{showArchived ? "▾" : "▸"}
            </h2>
            <p className="kv muted">
              被覆盖/被保留决策归档的版本；不参与检索与问答，可恢复或彻底删除。
            </p>
            {showArchived && archived.map((p) => (
              <div className="paper-row" key={p.id}>
                <div className="t">
                  <b>{p.title}</b>
                  <small>
                    {p.superseded_by ? `被 ${p.superseded_by} 取代 · ` : ""}
                    {p.page_count ? `${p.page_count} 页 · ` : ""}id {p.id}
                  </small>
                </div>
                <div className="actions">
                  <button className="mini-btn" disabled={busyId === p.id}
                    onClick={async () => {
                      setBusyId(p.id);
                      try { await restorePaper(p.id); await refresh(); }
                      finally { setBusyId(""); }
                    }}>恢复</button>
                  <button className="mini-btn danger" disabled={busyId === p.id}
                    onClick={async () => {
                      if (!window.confirm(`彻底删除《${p.title}》？不可恢复。`)) return;
                      setBusyId(p.id);
                      try { await deletePaper(p.id); await refresh(); }
                      finally { setBusyId(""); }
                    }}>彻底删除</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
