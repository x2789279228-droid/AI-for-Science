"use client";

import { useEffect, useRef, useState } from "react";
import {
  Claim, FinalEvent, Hit, Paper, listPapers, streamChat,
} from "@/lib/api";
import { ChevronRight, Spark } from "@/components/icons";

interface Turn {
  question: string;
  answer: string;
  retrieval: { channels: string[]; hits: Hit[] } | null;
  final: FinalEvent | null;
  seconds?: number;
}

const GUARD_LABEL: Record<string, string> = {
  no_citation_found: "回答无引用锚点",
  invalid_citation_removed: "无效引用已剔除",
  llm_unavailable_fallback: "LLM 不可用 → 抽取式降级",
  empty_index: "索引为空",
};

const secName = (s: string) => (s === "_front" || !s ? "前言" : s);

export default function ChatPage() {
  const [papers, setPapers] = useState<Paper[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [showScope, setShowScope] = useState(false);
  const msgsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    listPapers().then((ps) => {
      setPapers(ps);
      const parsed = ps.filter((p) => p.parsed);
      const fromUrl = new URLSearchParams(window.location.search).get("paper");
      const init = fromUrl && parsed.some((p) => p.id === fromUrl)
        ? [fromUrl] : parsed.slice(0, 1).map((p) => p.id);
      setSelected(new Set(init));
    });
  }, []);

  useEffect(() => {
    msgsRef.current?.scrollTo({ top: msgsRef.current.scrollHeight });
  }, [turns]);

  const ask = async () => {
    const q = question.trim();
    if (!q || busy) return;
    setQuestion("");
    setShowScope(false);
    setBusy(true);
    const t0 = performance.now();
    const turn: Turn = { question: q, answer: "", retrieval: null, final: null };
    setTurns((t) => [...t, turn]);
    try {
      await streamChat(q, Array.from(selected), {
        retrieval: (d) => setTurns((t) => {
          const c = [...t]; c[c.length - 1] = { ...c[c.length - 1], retrieval: d }; return c;
        }),
        delta: (text) => setTurns((t) => {
          const c = [...t];
          c[c.length - 1] = { ...c[c.length - 1], answer: c[c.length - 1].answer + text };
          return c;
        }),
        final: (d) => setTurns((t) => {
          const c = [...t];
          c[c.length - 1] = {
            ...c[c.length - 1],
            answer: c[c.length - 1].answer || d.answer,  // 抽取式模式无 delta，答案在 final 里
            final: d,
            seconds: (performance.now() - t0) / 1000,
          };
          return c;
        }),
      });
    } catch (e) {
      setTurns((t) => {
        const c = [...t];
        c[c.length - 1] = {
          ...c[c.length - 1],
          answer: c[c.length - 1].answer + `\n[请求失败：${e instanceof Error ? e.message : e}]`,
        };
        return c;
      });
    } finally {
      setBusy(false);
    }
  };

  const lastTurn = turns[turns.length - 1];
  const scopeTitle = selected.size === 0
    ? "未选择论文"
    : selected.size === 1
      ? papers.find((p) => selected.has(p.id))?.title.slice(0, 24) ?? "1 篇论文"
      : `${selected.size} 篇论文`;

  return (
    <div className="chat-shell">
      <div className="topbar">
        <span className="topbar-title">溯源问答</span>
        <span className="chip accent">ClaimTrace 逐论断验证</span>
        <span style={{ flex: 1 }} />
        <span className="chip" title="本次回答的检索范围">{scopeTitle}</span>
        <button className="chip" style={{ cursor: "pointer" }} onClick={() => setShowScope((s) => !s)}>
          检索范围 <ChevronRight size={11} style={{ transform: showScope ? "rotate(90deg)" : "none" }} />
        </button>
        {showScope && (
          <div style={{ position: "absolute", top: "var(--topbar-h)", right: "var(--space-6)",
            zIndex: 20, background: "var(--surface)", border: "1px solid var(--line)",
            borderRadius: "var(--radius-md)", boxShadow: "var(--shadow-md)",
            padding: "var(--space-3) var(--space-4)", minWidth: 260 }}>
            <p className="kv" style={{ marginBottom: "var(--space-2)" }}>选择参与检索的论文</p>
            {papers.filter((p) => p.parsed).map((p) => (
              <label key={p.id} style={{ display: "flex", gap: "var(--space-2)",
                alignItems: "center", padding: "4px 0", fontSize: "var(--text-sm)", cursor: "pointer" }}>
                <input type="checkbox" checked={selected.has(p.id)}
                  onChange={(e) => setSelected((s) => {
                    const n = new Set(s);
                    if (e.target.checked) n.add(p.id); else n.delete(p.id);
                    return n;
                  })} />
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {p.title.slice(0, 30)}
                </span>
              </label>
            ))}
            {papers.filter((p) => p.parsed).length === 0 && (
              <p className="kv">文献库为空，先去上传 PDF。</p>
            )}
          </div>
        )}
      </div>

      <div className="msgs" ref={msgsRef}>
        <div className="msgs-inner">
          {turns.length === 0 && (
            <div className="placeholder" style={{ minHeight: "50vh" }}>
              <div className="placeholder-card">
                <div className="placeholder-icon"><Spark size={24} /></div>
                <h2>向文献提问</h2>
                <p>
                  例如「模型用了多少块 GPU？」「BLEU 分数是多少？」——
                  每句回答都可点击查看原文出处；查无依据时明确拒答。
                </p>
              </div>
            </div>
          )}
          {turns.map((turn, ti) => (
            <div key={ti}>
              <div className="msg user"><span>{turn.question}</span></div>
              <div className="msg bot">
                {turn.retrieval && (
                  <p className="kv" style={{ marginBottom: "var(--space-2)", display: "flex",
                    gap: "var(--space-2)", flexWrap: "wrap" }}>
                    <span className="chip">检索通道：{turn.retrieval.channels.join(" + ")}</span>
                    <span className="chip">召回 {turn.retrieval.hits.length} 块</span>
                    <details style={{ minWidth: 220 }}>
                      <summary className="chip" style={{ cursor: "pointer", listStyle: "none" }}>
                        检索明细
                      </summary>
                      <div style={{ marginTop: "var(--space-2)" }}>
                        {turn.retrieval.hits.slice(0, 5).map((h) => (
                          <div key={h.chunk_id} className="kv" style={{
                            borderBottom: "1px dashed var(--line)", padding: "6px 0" }}>
                            <b style={{ color: "var(--accent)" }}>p{h.page + 1} §{secName(h.section_path)}</b>
                            {h.rrf ? ` · RRF ${h.rrf}` : ` · BM25 ${h.score}`}
                            <br />{h.text.slice(0, 110)}…
                          </div>
                        ))}
                      </div>
                    </details>
                  </p>
                )}
                {turn.answer && (
                  <div className="bubble">
                    {turn.final ? <AnswerWithAnchors text={turn.final.answer} /> : turn.answer}
                    {turn.final && <FinalMeta final={turn.final} />}
                  </div>
                )}
                {turn.final && <Claims claims={turn.final.claims} hits={turn.retrieval?.hits ?? []} />}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="dock-wrap">
        <div className="dock">
          <div className="dock-row">
            <input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && ask()}
              placeholder={busy ? "生成中…" : "对选中的论文提问，每句回答可溯源到原文…"}
              disabled={busy}
            />
            <button className="send-btn" onClick={ask} disabled={busy || !selected.size}
              aria-label="发送">↑</button>
          </div>
          <div className="dock-tools">
            <span className="chip">检索范围 {selected.size} 篇</span>
            {lastTurn?.final && (
              <span className={`chip ${lastTurn.final.mode === "generative" ? "ok" : "warn"}`}>
                {lastTurn.final.mode === "generative" ? "生成式 · 引用已后验" : "抽取式证据模式"}
              </span>
            )}
          </div>
        </div>
        <div className="statusbar">
          {lastTurn?.retrieval
            ? `通道 ${lastTurn.retrieval.channels.join("+")} · 召回 ${lastTurn.retrieval.hits.length} 块`
            : "就绪"}
          {lastTurn?.seconds !== undefined && ` · 用时 ${lastTurn.seconds.toFixed(1)}s`}
          {lastTurn?.final && ` · 论断 ${lastTurn.final.claims.length} 条`}
        </div>
      </div>
    </div>
  );
}

function FinalMeta({ final }: { final: FinalEvent }) {
  const guards = final.guardrails.filter((g) => GUARD_LABEL[g]);
  if (final.mode === "generative" && !guards.length) return null;
  return (
    <p style={{ margin: "var(--space-2) 0 0", display: "flex", gap: "var(--space-2)",
      flexWrap: "wrap" }}>
      {final.mode !== "generative" && <span className="badge warn">抽取式证据模式（无 LLM）</span>}
      {guards.map((g) => <span key={g} className="badge warn">⚠ {GUARD_LABEL[g]}</span>)}
    </p>
  );
}

/** 把 [^p6-§xx] 渲染成锚点徽章（页码转 1 基人读页码）。 */
function AnswerWithAnchors({ text }: { text: string }) {
  const parts = text.split(/(\[\^p\d+(?:-§[^\]]*)?\])/g);
  return (
    <>
      {parts.map((part, i) =>
        /^\[\^p(\d+)/.test(part) ? (
          <span key={i} className="chip accent anchor-chip">
            {part.replace(/^\[\^p(\d+)-§([^\]]*)\]$/, (_, p, s) =>
              `原文 p${Number(p) + 1} · §${secName(s)}`
            ).replace(/^\[\^p(\d+)\]$/, (_, p) => `原文 p${Number(p) + 1}`)}
          </span>
        ) : (
          <span key={i}>{part}</span>
        )
      )}
    </>
  );
}

function Claims({ claims, hits }: { claims: Claim[]; hits: Hit[] }) {
  const [open, setOpen] = useState<number | null>(null);
  if (!claims.length) return null;
  return (
    <div style={{ marginTop: "var(--space-3)" }}>
      <p className="kv" style={{ margin: 0 }}>
        <b>逐论断溯源</b>（点击查看原文依据）
      </p>
      {claims.map((c, i) => (
        <div key={i}
          className={`claim ${c.status === "unsupported" ? "unverified" : ""}`}
          onClick={() => setOpen(open === i ? null : i)}>
          <div>
            {c.status === "supported"
              ? <span className="badge ok">已验证</span>
              : <span className="badge warn">⚠ 未验证</span>}
            {c.text.slice(0, 160)}{c.text.length > 160 ? "…" : ""}
          </div>
          {c.support.length > 0 && (
            <div className="anchor">
              出处：{c.support.map((s) => `p${s.page + 1} §${secName(s.section_path)}`).join(" / ")}
            </div>
          )}
          {open === i && c.support.map((s, k) => {
            const hit = hits.find((h) => h.chunk_id === s.chunk_id);
            return (
              <div key={k} className="evidence">
                <i>原文 · 第 {s.page + 1} 页 · {secName(s.section_path)}</i>
                <br />{s.quote || hit?.text.slice(0, 220) || ""}
              </div>
            );
          })}
          {open === i && c.status === "unsupported" && (
            <div className="evidence"><i>检索结果中未找到支撑此论断的原文，请人工核对。</i></div>
          )}
        </div>
      ))}
    </div>
  );
}
