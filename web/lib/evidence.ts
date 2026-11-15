// 自动生成：scripts/write_evidence.py —— 请勿手改。
// README / /judge / /health 的数字唯一来源；更新方式见文件头说明。
export const EVIDENCE = {
  generatedAt: "2026-11-15 15:20 +08",
  commit: "77c8ff3",
  branch: "main",
  pytest: { passed: 342, failed: 0, errors: 0, skipped: 0, total: 342 },
  e2e: { passed: 19, failed: 0, skipped: 0, total: 19 },
  eval: { round: "eval-20261114-r3", gatesPassed: 16, gatesTotal: 17, compareOk: false },
  corpus: { papers: 20, references: 2391, source: "scholar.db" },
} as const;

export type Evidence = typeof EVIDENCE;
