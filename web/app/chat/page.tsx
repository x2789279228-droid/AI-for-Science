"use client";

import { useEffect, useRef, useState } from "react";
import {
  Claim, FinalEvent, Hit, Paper, listPapers, streamChat,
} from "@/lib/api";

interface Turn {
  question: string;
  answer: string;
  retrieval: { channels: string[]; hits: Hit[] } | null;
  final: FinalEvent | null;
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
    setBusy(true);
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

  return (
    <div>
      <h1>溯源问答 <span className="badge">ClaimTrace 逐论断验证</span></h1>
      <div className="chat-wrap">
        <div className="chat-main">
          <div ref={msgsRef} className="msgs">
            {turns.length === 0 && (
              <p className="kv">
                试着问：<i>「模型用了多少块 GPU？」</i>、<i>「BLEU 分数是多少？」</i>——
                每句回答都可点击查看原文出处；查无依据时会明确拒答。
              </p>
            )}
            {turns.map((turn, ti) => (
              <div key={ti}>
                <div className="msg user"><span>{turn.question}</span></div>
                <div className="msg bot">
                  {turn.retrieval && (
                    <p className="kv">
                      <span className="badge gray">
                        检索通道：{turn.retrieval.channels.join(" + ")}
                      </span>
                      <span className="badge gray">召回 {turn.retrieval.hits.length} 块</span>
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
          <div className="ask">
            <input
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && ask()}
              placeholder={busy ? "生成中…" : "对选中的论文提问…"}
              disabled={busy}
            />
            <button className="btn" onClick={ask} disabled={busy || !selected.size}>提问</button>
          </div>
        </div>

        <div className="card" style={{ alignSelf: "start" }}>
          <h2>检索范围</h2>
          {papers.filter((p) => p.parsed).map((p) => (
            <label key={p.id} style={{ display: "block", margin: "6px 0", fontSize: 14 }}>
              <input type="checkbox" checked={selected.has(p.id)}
                onChange={(e) => setSelected((s) => {
                  const n = new Set(s);
                  if (e.target.checked) n.add(p.id); else n.delete(p.id);
                  return n;
                })} /> {p.title.slice(0, 34)}
            </label>
          ))}
          {turns[0]?.retrieval && (
            <>
              <h2 style={{ marginTop: 18 }}>检索明细（top）</h2>
              {turns[turns.length - 1].retrieval!.hits.slice(0, 6).map((h) => (
                <div key={h.chunk_id} className="hit">
                  <b>p{h.page + 1} §{secName(h.section_path)}</b>
                  {h.rrf ? ` · RRF ${h.rrf}` : ` · BM25 ${h.score}`}
                  <br />{h.text.slice(0, 110)}…
                </div>
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function FinalMeta({ final }: { final: FinalEvent }) {
  return (
    <p style={{ margin: "10px 0 0" }}>
      <span className={final.mode === "generative" ? "badge ok" : "badge warn"}>
        {final.mode === "generative" ? "生成式 · 引用已后验" : "抽取式证据模式（无 LLM）"}
      </span>
      {final.guardrails
        .filter((g) => GUARD_LABEL[g])
        .map((g) => <span key={g} className="badge warn">⚠ {GUARD_LABEL[g]}</span>)}
    </p>
  );
}

/** 把 [^p6-§xx] 渲染成可点击锚点徽章（页码转 1 基人读页码）。 */
function AnswerWithAnchors({ text }: { text: string }) {
  const parts = text.split(/(\[\^p\d+(?:-§[^\]]*)?\])/g);
  return (
    <>
      {parts.map((part, i) =>
        /^\[\^p(\d+)/.test(part) ? (
          <span key={i} className="badge" style={{ margin: "0 2px" }}>
            {part.replace(/^\[\^p(\d+)-§([^\]]*)\]$/, (_, p, s) =>
              `原文 p${Number(p) + 1} · §${s === "_front" || !s ? "前言" : s}`
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
    <div style={{ marginTop: 12 }}>
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
            <div className="anchor">出处：{c.support.map((s) => `p${s.page + 1} §${secName(s.section_path)}`).join(" / ")}</div>
          )}
          {open === i && c.support.map((s, k) => {
            const hit = hits.find((h) => h.chunk_id === s.chunk_id);
            return (
              <div key={k} className="evidence">
                <i>原文 · 第 {s.page + 1} 页 · {s.section_path || "前言"}</i>
                <br />{s.quote || hit?.text.slice(0, 220) || hitTextNote(s.chunk_id, hits)}
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

function hitTextNote(id: string, hits: Hit[]) {
  return hits.length ? "" : `（片段 ${id.slice(2, 10)}）`;
}
