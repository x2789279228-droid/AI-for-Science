"use client";

/**
 * ChatGLM 式回答正文：Markdown 实时渲染 + [E#] 引用锚点融合。
 * - react-markdown（默认转义原始 HTML，无 dangerouslySetInnerHTML）
 * - remark-gfm：表格/任务列表；remark-math + rehype-katex：论文公式（katex 已有依赖）
 * - 自定义 rehype 插件：非 code 上下文的文本节点按 /\[E\d+\]/ 切分为 chip（data-eid），
 *   容器 onClick 事件委托弹原文溯源浮层——流式与 final 后同一组件，流式期不再裸显 [E1]
 */
import { memo, useMemo, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import type { Anchor } from "@/lib/api";

/** hast Element 节点（仅声明插件实际触碰的字段，避免引入 hast 全量类型）。 */
interface HastNode {
  type?: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

const EID_SPLIT = /(\[(?:E|M)\d+\])/g;
const EID_EXACT = /^\[(?:E|M)\d+\]$/;

/** 文本节点切分插件：[E#]/[M#] → span.anchor-chip[data-eid]；code/pre 内保持原样。
 *  M#（模块七记忆引用）加 m-cite 类：视觉上与论文证据 E# 区分（描边用 accent 色）。 */
function rehypeCitations() {
  return (tree: HastNode) => {
    const visit = (node: HastNode, inCode: boolean) => {
      if (!node || !Array.isArray(node.children)) return;
      const inCodeNext = inCode || node.tagName === "code" || node.tagName === "pre";
      const next: HastNode[] = [];
      for (const child of node.children) {
        if (!inCodeNext && child.type === "text" && child.value
          && (child.value.includes("[E") || child.value.includes("[M"))) {
          for (const part of child.value.split(EID_SPLIT)) {
            if (!part) continue;
            if (EID_EXACT.test(part)) {
              const isMem = part.startsWith("[M");
              next.push({
                type: "element", tagName: "span",
                properties: {
                  className: ["anchor-chip", "num", "md-cite", ...(isMem ? ["m-cite"] : [])],
                  "data-eid": part,
                },
                children: [{ type: "text", value: part }],
              });
            } else {
              next.push({ type: "text", value: part });
            }
          }
        } else {
          visit(child, inCodeNext);
          next.push(child);
        }
      }
      node.children = next;
    };
    visit(tree, false);
  };
}

interface Props {
  text: string;
  anchors?: Record<string, Anchor>;
  onEvidence?: (eid?: string, anchor?: Anchor, quote?: string) => void;
  /** 模块七：[M#] 点击 → 会话轮次回放（编号空间与 E# 独立）。 */
  onMemory?: (mid: string) => void;
  /** 流式中：尾部打字光标（ChatGLM ▍）。 */
  streaming?: boolean;
}

function MarkdownWithCitationsImpl({ text, anchors, onEvidence, onMemory, streaming }: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const hasAnchors = !!anchors && Object.keys(anchors).length > 0;
  const plugins = useMemo(() => ({ remark: [remarkGfm, remarkMath], rehype: [rehypeKatex, rehypeCitations] }), []);

  // 事件委托：点击 [E#] chip → 原文浮层；[M#] chip → 会话回放（模块七）。
  // 无锚点数据（流式中/降级）时 chip 灰显不可点。
  // 注意 anchors 键为 "E3"（无括号，见后端 build_anchors），data-eid 存的是原文 "[E3]" 需归一化
  const handleClick = (e: React.MouseEvent) => {
    const el = (e.target as HTMLElement).closest("[data-eid]") as HTMLElement | null;
    if (!el) return;
    const eid = (el.getAttribute("data-eid") ?? "").replace(/^\[|\]$/g, "");
    if (eid.startsWith("M")) {
      if (onMemory) onMemory(eid);
      return;
    }
    const a = anchors?.[eid];
    if (a) onEvidence?.(eid, a);
  };

  return (
    <div ref={rootRef} className={`md-body${hasAnchors ? " ready" : ""}`} onClick={handleClick}>
      <ReactMarkdown
        remarkPlugins={plugins.remark}
        rehypePlugins={plugins.rehype}
        components={{
          a: (p) => <a {...p} target="_blank" rel="noreferrer" />,
        }}
      >
        {text}
      </ReactMarkdown>
      {streaming && <span className="type-cursor" aria-hidden />}
    </div>
  );
}

/** 流式高密度更新下按 text/anchors 浅比较跳过重复渲染。 */
const MarkdownWithCitations = memo(MarkdownWithCitationsImpl);
export default MarkdownWithCitations;
