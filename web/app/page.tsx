"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { API, Job, Paper, listPapers } from "@/lib/api";

export default function LibraryPage() {
  const [papers, setPapers] = useState<Paper[]>([]);
  const [jobs, setJobs] = useState<Record<string, Job>>({});        // jobId -> Job
  const [paperJob, setPaperJob] = useState<Record<string, string>>({}); // paperId -> jobId
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => setPapers(await listPapers()), []);

  useEffect(() => {
    const timer = setInterval(async () => {
      const running = Object.values(jobs).filter(
        (j) => j.status === "queued" || j.status === "running"
      );
      if (!running.length) return;
      for (const j of running) {
        const r = await fetch(`${API}/api/jobs/${j.id}`, { cache: "no-store" });
        if (r.ok) {
          const fresh: Job = await r.json();
          setJobs((prev) => ({ ...prev, [j.id]: fresh }));
          if (fresh.status === "done" || fresh.status === "error") refresh();
        }
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [jobs, refresh]);

  useEffect(() => { refresh(); }, [refresh]);

  const upload = async (files: FileList) => {
    setUploading(true);
    setNotice("");
    try {
      for (const file of Array.from(files)) {
        const fd = new FormData();
        fd.append("file", file);
        const r = await fetch(`${API}/api/papers`, { method: "POST", body: fd });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || "上传失败");
        if (data.dedup === "exact_file") {
          setNotice(`《${data.existing_title}》已存在（文件完全相同），已复用既有记录。`);
        } else if (data.job_id) {
          setJobs((prev) => ({ ...prev, [data.job_id]: {
            id: data.job_id, status: "queued", progress: 0, message: "排队中" } }));
          setPaperJob((prev) => ({ ...prev, [data.paper_id]: data.job_id }));
        }
      }
      await refresh();
    } catch (e) {
      setNotice(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <div>
      <h1>文献库</h1>
      <div className="card">
        <h2>上传 PDF</h2>
        <input ref={fileRef} type="file" accept="application/pdf" multiple
          onChange={(e) => e.target.files && upload(e.target.files)} disabled={uploading} />
        {uploading && <span style={{ marginLeft: 10 }}>上传中…</span>}
        <p className="kv" style={{ marginTop: 8 }}>
          解析在后台进行（进度条实时显示）；同名旧版论文会触发内容级去重预警。
        </p>
        {notice && <div className="alert">{notice}</div>}
      </div>

      <div className="card">
        <h2>已入库（{papers.length}）</h2>
        {papers.length === 0 && <p className="kv">暂无论文，先上传一份 PDF。</p>}
        {papers.map((p) => {
          const jobId = paperJob[p.id];
          const job = jobId ? jobs[jobId] : undefined;
          return (
            <div key={p.id}>
              {p.dedup_alert && (
                <div className="alert">
                  ⚠️ <b>疑似旧版本：</b>库中已有《{p.dedup_alert.title}》，
                  当前上传与其高度相似（{p.dedup_alert.reason}），建议保留库中已有版本。
                </div>
              )}
              <div className="paper-row">
                <div className="t">
                  <b>{p.title || "(解析中…)"}</b>
                  <small>
                    {p.arxiv_id ? `arXiv:${p.arxiv_id} · ` : ""}
                    {p.page_count ? `${p.page_count} 页 · ` : ""}
                    id {p.id}
                  </small>
                </div>
                <div className="actions">
                  {p.parsed ? (
                    <>
                      <a href={`/reader/${p.id}`}>阅读</a>
                      <a href={`/chat?paper=${p.id}`}>问答</a>
                    </>
                  ) : job && job.status !== "done" ? (
                    <span className="badge gray">
                      {job.status === "error" ? `失败：${job.message}` : `${job.message} ${Math.round(job.progress * 100)}%`}
                    </span>
                  ) : (
                    <span className="badge gray">解析中…</span>
                  )}
                </div>
              </div>
              {job && job.status !== "done" && job.status !== "error" && (
                <div style={{ margin: "6px 0 10px" }}>
                  <div className="progress">
                    <div style={{ width: `${Math.round(job.progress * 100)}%` }} />
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
