// 自动生成：scripts/write_evidence.py —— 请勿手改。
// README / /judge / /health 的数字唯一来源；更新方式见文件头说明。
export const EVIDENCE = {
  generatedAt: "2026-11-15 19:12 +08",
  commit: "10ba65a",
  branch: "main",
  pytest: { passed: 342, failed: 0, errors: 0, skipped: 0, total: 342 },
  e2e: { passed: 19, failed: 0, skipped: 0, total: 19 },
  eval: { round: "eval-20261115-r1", gatesPassed: 17, gatesTotal: 17, compareOk: true },
  corpus: { papers: 20, references: 2391, source: "scholar.db" },
} as const;

export type Evidence = typeof EVIDENCE;
