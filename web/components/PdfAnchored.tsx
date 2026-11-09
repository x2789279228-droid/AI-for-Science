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
 *
 * 模块三 B0 加固：
 * - doc 缓存（LRU 2 篇）：旧实现每次翻页/换视图都整份重取 PDF + getDocument；
 *   跨视图联动（块级定位/前后章跳转）会放大该开销，故缓存文档级句柄，翻页只 getPage。
 * - TextLayer（pdfjs 4.x TextLayer 类）：透明文本覆盖层，使 PDF 视图原生可选取词
 *   （划词翻译的数据基础）；viewport 用 CSS 像素（canvas 是 2x 超采样显示为 width）。
 */
import { useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { API } from "@/lib/api";

interface Props {
  paperId: string;
  page: number;              // 1 基人读页码
  bbox?: number[];           // [x0, y0, x1, y1] 归一化坐标
  width?: number;            // 画布 CSS 宽度 px
  onMouseUp?: () => void;    // 取词翻译挂点（选区由父组件经 window.getSelection 读取）
  /** Round4 反向联动：可点击的块热点（bbox 归一化），点击回调块 id（PDF→译文方向） */
  blocks?: { id: string; bbox: number[] }[];
  onBlockClick?: (bid: string) => void;
}

// ---- 模块级 doc 缓存（跨组件实例共享；EvidenceModal/阅读器同篇复用） ----
const DOC_CACHE_MAX = 2;
const docCache = new Map<string, Promise<PDFDocumentProxy>>();   // 插入序即 LRU 序

async function getDoc(paperId: string): Promise<PDFDocumentProxy> {
  const hit = docCache.get(paperId);
  if (hit) {                                    // LRU touch：重插到尾部
    docCache.delete(paperId);
    docCache.set(paperId, hit);
    return hit;
  }
  const p = (async () => {
    const pdfjs = await import("pdfjs-dist");
    pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
    const resp = await fetch(`${API}/api/papers/${paperId}/pdf`, { cache: "no-store" });
    if (!resp.ok) throw new Error(`PDF HTTP ${resp.status}`);
    const data = await resp.arrayBuffer();
    return await pdfjs.getDocument({ data }).promise;
  })();
  docCache.set(paperId, p);
  p.catch(() => docCache.delete(paperId));      // 失败不缓存（下次重试）
  while (docCache.size > DOC_CACHE_MAX) {       // 淘汰最旧（不含刚插入的）
    const oldest = docCache.keys().next().value;
    if (oldest === undefined || oldest === paperId) break;
    const oldP = docCache.get(oldest)!;
    docCache.delete(oldest);
    void oldP.then((d) => d.destroy()).catch(() => {});
  }
  return p;
}

export default function PdfAnchored({ paperId, page, bbox, width = 620, onMouseUp,
                                      blocks, onBlockClick }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textLayerRef = useRef<HTMLDivElement>(null);
  const tokenRef = useRef(0);
  const taskRef = useRef<{ cancel(): void } | null>(null);   // 属性访问不触发 TS CFA 窄化
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const myToken = ++tokenRef.current;
    let cancelled = false;
    let textLayer: { cancel(): void } | null = null;
    setError("");
    setReady(false);
    (async () => {
      try {
        const doc = await getDoc(paperId);       // 缓存命中：翻页零网络开销
        if (cancelled || myToken !== tokenRef.current) return;
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
        if (cancelled || myToken !== tokenRef.current) return;

        const canvas = canvasRef.current;
        if (!canvas) return;
        canvas.width = off.width;
        canvas.height = off.height;
        canvas.getContext("2d")!.drawImage(off, 0, 0);
        setReady(true);

        // TextLayer：透明可选取词层（CSS 像素 viewport，与显示尺寸对齐）
        const layerDiv = textLayerRef.current;
        if (layerDiv) {
          const pdfjs = await import("pdfjs-dist");
          if (cancelled || myToken !== tokenRef.current) return;
          layerDiv.replaceChildren();
          const layer = new pdfjs.TextLayer({
            textContentSource: pdfPage.streamTextContent(),
            container: layerDiv,
            viewport: pdfPage.getViewport({ scale }),   // 非 2x：匹配 canvas 的 CSS 显示尺寸
          });
          textLayer = layer;
          await layer.render();
        }
      } catch (e) {
        if (!cancelled && myToken === tokenRef.current) {
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    })();
    return () => {
      cancelled = true;
      taskRef.current?.cancel();   // 作废在飞的 render（RenderingCancelledException → 静默）
      textLayer?.cancel();         // 作废在飞的 TextLayer
    };
  }, [paperId, page, width]);

  const hasBbox = Array.isArray(bbox) && bbox.length === 4;

  return (
    <div style={{ position: "relative", maxWidth: width, margin: "0 auto" }} onMouseUp={onMouseUp}>
      <canvas ref={canvasRef}
        style={{ width, height: "auto", display: "block", borderRadius: "var(--radius-sm)",
          border: "1px solid var(--line)", background: "var(--surface-2)" }} />
      <div ref={textLayerRef} className="textLayer" aria-hidden="true" />
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
      {/* Round4 反向联动：块级透明热点（虚线极淡指示可点）→ onBlockClick(bid) */}
      {ready && (blocks ?? []).filter((b) => b.bbox?.length === 4).map((b) => (
        <div key={b.id} data-pdf-bid={b.id} onClick={() => onBlockClick?.(b.id)}
          title="点击定位右侧译文块"
          style={{
            position: "absolute",
            left: `${b.bbox[0] * 100}%`,
            top: `${b.bbox[1] * 100}%`,
            width: `${(b.bbox[2] - b.bbox[0]) * 100}%`,
            height: `${(b.bbox[3] - b.bbox[1]) * 100}%`,
            cursor: "pointer",
            outline: "1px dashed rgba(37, 99, 235, 0.28)",
            borderRadius: 2,
          }} />
      ))}
    </div>
  );
}
