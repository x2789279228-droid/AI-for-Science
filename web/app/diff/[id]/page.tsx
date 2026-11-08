"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import {
  API, DiffPayload, DiffPara, DiffSection, Job,
  fetchVersionDiff, getPaper, startVersionDiff,
} from "@/lib/api";

type HL = { a: [number, number]; b: [number, number]; tag: string };

/** 按 SequenceMatcher 区间给段落着色（replaced 段的字符级高亮）。 */
function Highlighted({ text, ranges, side }: { text: string; ranges?: HL[]; side: "a" | "b" }) {
  if (!text) return <span className="muted">（无）</span>;
  const marks = (ranges ?? [])
    .map((h) => ({ start: h[side][0], end: h[side][1], tag: h.tag }))
    .filter((m) => m.end > m.start && m.end <= text.length)
    .sort((x, y) => x.start - y.start);
  if (!marks.length) return <>{text}</>;
  const out: React.ReactNode[] = [];
  let pos = 0;
  let key = 0;
  for (const m of marks) {
    if (m.start < pos) continue;
    if (m.start > pos) out.push(<span key={key++}>{text.slice(pos, m.start)}</span>);
    out.push(<span key={key++} className={`hl-${m.tag}`}>{text.slice(m.start, m.end)}</span>);
    pos = m.end;
  }
  if (pos < text.length) out.push(<span key={key++}>{text.slice(pos)}</span>);
  return <>{out}</>;
}

const OP_LABEL: Record<string, string> = {
  added: "新增", removed: "删除", replaced: "修改", refs_changed: "参考文献条数变化",
};

export default function DiffPage() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const baseId = search.get("base") ?? "";
  const targetId = params.id;

  const [payload, setPayload] = useState<DiffPayload | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [titles, setTitles] = useState<{ base: string; target: string }>({ base: "", target: "" });
  const [showUnchanged, setShowUnchanged] = useState(false);

  const load = useCallback(async () => {
    if (!baseId) { setError("缺少 ?base= 旧版本 id"); return; }
    try {
      const [bp, tp] = await Promise.all([getPaper(baseId), getPaper(targetId)]);
      setTitles({ base: bp.title, target: tp.title });
      const start = await startVersionDiff(targetId, baseId);
      if (start.cached) {
        setPayload((await fetchVersionDiff(targetId, baseId)).payload);
        return;
      }
      // 轮询 diff 任务
      const timer = setInterval(async () => {
        const r = await fetch(`${API}/api/jobs/${start.job_id}`, { cache: "no-store" });
        if (!r.ok) return;
        const j: Job = await r.json();
        setJob(j);
        if (j.status === "done") {
          clearInterval(timer);
          setPayload((await fetchVersionDiff(targetId, baseId)).payload);
        } else if (j.status === "error" || j.status === "cancelled") {
          clearInterval(timer);
          setError(j.message || "差异任务失败");
        }
      }, 1000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [baseId, targetId]);

  useEffect(() => { load(); }, [load]);

  const s = payload?.stats;
  const sections = payload?.sections ?? [];
  const changedSections = sections.filter((x) => x.status !== "aligned" || (x.changed ?? 0) > 0);
  const unchangedSections = sections.filter((x) => x.status === "aligned" && (x.changed ?? 0) <= 0);

  const sectionCard = (sec: DiffSection) => {
    const tone = sec.status === "added" ? "diff-added"
      : sec.status === "removed" ? "diff-removed"
        : (sec.changed ?? 0) > 0 ? "diff-changed" : "diff-same";
    return (
      <div key={`${sec.status}-${sec.title}`} className={`diff-section ${tone}`}>
        <div className="diff-section-head">
          <span className="diff-op">
            {sec.status === "added" ? "+ 新增章节" : sec.status === "removed" ? "− 移除章节"
              : `↻ ${sec.changed ?? 0} 处修改`}
          </span>
          <b>{sec.title}</b>
          {sec.base_title && sec.base_title !== sec.title && (
            <small className="muted">（原：{sec.base_title}）</small>
          )}
          <small className="muted" style={{ marginLeft: "auto" }}>
            {sec.base_page != null ? `旧 p${(sec.base_page ?? 0) + 1}` : ""}
            {sec.target_page != null ? ` → 新 p${(sec.target_page ?? 0) + 1}` : ""}
          </small>
        </div>
        {sec.detail_omitted && (
          <p className="kv muted">变更较轻，明细已折叠（Top-N 高置信差异之外）。</p>
        )}
        {sec.is_references && sec.paras?.length ? (
          <p className="kv muted">{sec.paras[0]?.base} → {sec.paras[0]?.target}</p>
        ) : null}
        {(sec.paras ?? []).filter((pa) => pa.op !== "refs_changed").map((pa: DiffPara, i) => (
          <div key={i} className={`diff-para op-${pa.op}`}>
            <span className="diff-op-tag">{OP_LABEL[pa.op] ?? pa.op}</span>
            <div className="diff-cols">
              <div className="diff-col base">
                <Highlighted text={pa.base} ranges={pa.hl} side="a" />
              </div>
              <div className="diff-col target">
                <Highlighted text={pa.target} ranges={pa.hl} side="b" />
              </div>
            </div>
            {(pa.numbers ?? []).length > 0 && (
              <div className="diff-numbers">
                {(pa.numbers ?? []).map((n, j) => (
                  <span key={j} className={`chip num-chip ${n.direction}`}>
                    {n.direction === "added" ? "+" : "−"}{n.value}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    );
  };

  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 1080 }}>
        <div className="topbar-row">
          <h1>DiffLens · 版本差异</h1>
          <a className="btn ghost" href={`/reader/${targetId}`}>← 返回阅读</a>
        </div>
        <p className="kv muted">
          旧版《{titles.base}》 → 新版《{titles.target}》
        </p>

        {error && <div className="alert">{error}</div>}
        {!payload && !error && (
          <div className="card"><p className="kv">
            正在对齐章节并计算差异…{job ? ` ${job.message || ""} ${Math.round((job.progress || 0) * 100)}%` : ""}
          </p><div className="progress"><div style={{ width: `${Math.round((job?.progress || 0.1) * 100)}%` }} /></div>
          </div>
        )}

        {payload && (
          <>
            <div className="card">
              <div className="diff-stats">
                <span className="chip">章节变更 {s?.sections_changed ?? 0}</span>
                <span className="chip">新增 {s?.sections_added ?? 0}</span>
                <span className="chip">移除 {s?.sections_removed ?? 0}</span>
                <span className="chip">段落 {s?.paras_changed ?? 0}</span>
                <span className="chip">数值 {s?.numbers_changed ?? 0}</span>
                <span className="chip">{payload.summary.engine === "llm" ? "LLM 摘要" : "抽取式摘要"}</span>
              </div>
              <div className="diff-summary">
                {payload.summary.bullets.map((b, i) => (
                  <div key={i} className="diff-bullet">
                    <span className="chip bullet-type">{b.type}</span>
                    <span>{b.text}</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="card">
              <h2>章节级差异（Top-N 高置信）</h2>
              {changedSections.length === 0 && <p className="kv">两版本结构高度一致，未检测到实质性修订。</p>}
              {changedSections.map(sectionCard)}
              {unchangedSections.length > 0 && (
                <div style={{ marginTop: "var(--space-3)" }}>
                  <button className="mini-btn" onClick={() => setShowUnchanged((v) => !v)}>
                    {showUnchanged ? "收起未变化章节" : `展开未变化章节（${unchangedSections.length}）`}
                  </button>
                  {showUnchanged && unchangedSections.map(sectionCard)}
                </div>
              )}
            </div>

            {(payload.numbers?.length ?? 0) > 0 && (
              <div className="card">
                <h2>数值变化（{payload.numbers.length}）</h2>
                <div className="diff-numbers">
                  {payload.numbers.slice(0, 60).map((n, i) => (
                    <span key={i} className={`chip num-chip ${n.direction}`} title={n.section}>
                      {n.direction === "added" ? "+" : "−"}{n.value}
                      <small className="muted"> @{n.section.slice(0, 18)}</small>
                    </span>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
