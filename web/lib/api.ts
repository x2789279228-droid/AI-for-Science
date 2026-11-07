export const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8100";

export interface Paper {
  id: string;
  title: string;
  arxiv_id: string;
  page_count: number;
  supersedes: string;
  parsed: boolean;
  docir_path: string;
  references_count?: number;
  dedup_alert?: {
    duplicate_of: string;
    title: string;
    existing_version: string;
    reason: string;
  };
}

export interface Hit {
  chunk_id: string;
  paper_id: string;
  page: number;
  section_path: string;
  score: number;
  rrf?: number;
  text: string;
}

export interface Support {
  page: number;
  section_path: string;
  chunk_id: string;
  quote: string;
  match_score?: number;
}

export interface Claim {
  text: string;
  status: "supported" | "unsupported";
  support: Support[];
}

export interface FinalEvent {
  answer: string;
  claims: Claim[];
  mode: "generative" | "extractive" | "none";
  guardrails: string[];
}

export interface Job {
  id: string;
  status: "queued" | "running" | "done" | "error";
  progress: number;
  message: string;
}

export async function listPapers(): Promise<Paper[]> {
  const r = await fetch(`${API}/api/papers`, { cache: "no-store" });
  return (await r.json()).papers;
}

export async function getPaper(id: string): Promise<Paper> {
  const r = await fetch(`${API}/api/papers/${id}`, { cache: "no-store" });
  return r.json();
}

/** POST SSE 流式解析（EventSource 不支持 POST，手写 fetch 流解析）。 */
export async function streamChat(
  question: string,
  paperIds: string[],
  on: {
    retrieval?: (d: { channels: string[]; hits: Hit[] }) => void;
    delta?: (t: string) => void;
    final?: (d: FinalEvent) => void;
  }
): Promise<void> {
  const resp = await fetch(`${API}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, paper_ids: paperIds }),
  });
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let event = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ") && event) {
          const data = JSON.parse(line.slice(6));
          if (event === "retrieval") on.retrieval?.(data);
          else if (event === "delta") on.delta?.(data.text);
          else if (event === "final") on.final?.(data);
        }
      }
      event = "";
    }
  }
}
