"use client";

/**
 * PDF 锚定画布：渲染指定论文页，并在 bbox（归一化 0-1）位置画高亮框。
 * 引用点击溯源的可见部分（评分表：「点击回答里的引用，跳到原文高亮」）。
 *
 * worker 部署在 public/pdf.worker.min.mjs（由 node_modules/pdfjs-dist/build 复制，
 * 升级 pdfjs-dist 版本后需重新复制，保持离线演示可用）。
 *
 * 渲染竞态防护（P1-5：旧实现两次异步 render 写同一可见 canvas → 黑屏/花屏）：
 * ① render token（tokenRef）作废过期异步流；② 旧 renderTask.cancel()；
 * ③ 先渲染到**离屏 canvas**，完成后一次性 drawImage 上屏（原子，无中途清屏）。
 */
import { useEffect, useRef, useState } from "react";
import { API } from "@/lib/api";

interface Props {
  paperId: string;
  page: number;              // 1 基人读页码
  bbox?: number[];           // [x0, y0, x1, y1] 归一化坐标
  width?: number;            // 画布 CSS 宽度 px
}

export default function PdfAnchored({ paperId, page, bbox, width = 620 }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const tokenRef = useRef(0);
  const taskRef = useRef<{ cancel(): void } | null>(null);   // 属性访问不触发 TS CFA 收窄
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const myToken = ++tokenRef.current;
    let cancelled = false;
    let cleanup: (() => void) | null = null;
    setError("");
    setReady(false);
    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
        const resp = await fetch(`${API}/api/papers/${paperId}/pdf`, { cache: "no-store" });
        if (!resp.ok) throw new Error(`PDF HTTP ${resp.status}`);
        const data = await resp.arrayBuffer();
        const doc = await pdfjs.getDocument({ data }).promise;
        if (cancelled || myToken !== tokenRef.current) { void doc.destroy(); return; }
        const pdfPage = await doc.getPage(Math.max(1, page));
        const base = pdfPage.getViewport({ scale: 1 });
        const scale = width / base.width;
        const viewport = pdfPage.getViewport({ scale: scale * 2 });   // 2x 渲染抗锯齿

        // 离屏渲染：完成后一次性上屏（旧任务先 cancel，杜绝双 render 竞争同一 2d 上下文）
        taskRef.current?.cancel();
        const off = document.createElement("canvas");
        off.width = viewport.width;
        off.height = viewport.height;
        const task = pdfPage.render({ canvasContext: off.getContext("2d")!, viewport });
        taskRef.current = task;
        await task.promise;
        if (cancelled || myToken !== tokenRef.current) { void doc.destroy(); return; }

        const canvas = canvasRef.current;
        if (!canvas) { void doc.destroy(); return; }
        canvas.width = off.width;
        canvas.height = off.height;
        canvas.getContext("2d")!.drawImage(off, 0, 0);
        setReady(true);
        cleanup = () => void doc.destroy();
      } catch (e) {
        if (!cancelled && myToken === tokenRef.current) {
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    })();
    return () => {
      cancelled = true;
      taskRef.current?.cancel();   // 作废在飞的 render（RenderingCancelledException → 静默）
      cleanup?.();
    };
  }, [paperId, page, width]);

  const hasBbox = Array.isArray(bbox) && bbox.length === 4;

  return (
    <div style={{ position: "relative", maxWidth: width, margin: "0 auto" }}>
      <canvas ref={canvasRef}
        style={{ width, height: "auto", display: "block", borderRadius: "var(--radius-sm)",
          border: "1px solid var(--line)", background: "var(--surface-2)" }} />
      {!ready && !error && (
        <p className="kv" style={{ position: "absolute", inset: 0,
          display: "flex", alignItems: "center", justifyContent: "center" }}>
          渲染 PDF 第 {page} 页…
        </p>
      )}
      {error && (
        <p className="kv" style={{ position: "absolute", inset: 0,
          display: "flex", alignItems: "center", justifyContent: "center",
          textAlign: "center", padding: "0 12px" }}>PDF 渲染失败：{error}</p>
      )}
      {ready && hasBbox && (
        <div style={{
          position: "absolute",
          left: `${bbox![0] * 100}%`,
          top: `${bbox![1] * 100}%`,
          width: `${(bbox![2] - bbox![0]) * 100}%`,
          height: `${(bbox![3] - bbox![1]) * 100}%`,
          border: "2px solid #e5484d",
          background: "rgba(229, 72, 77, 0.18)",
          borderRadius: 3,
          boxShadow: "0 0 0 2px rgba(229,72,77,0.15)",
          animation: "bboxPulse 1.6s ease-in-out 3",
        }} />
      )}
    </div>
  );
}
