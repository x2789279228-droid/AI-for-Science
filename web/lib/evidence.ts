// 自动生成：scripts/write_evidence.py —— 请勿手改。
// README / /judge / /health 的数字唯一来源；更新方式见文件头说明。
export const EVIDENCE = {
  generatedAt: "2026-11-23 13:59 +08",
  commit: "028b799",
  branch: "main",
  pytest: { passed: 518, failed: 0, errors: 0, skipped: 1, total: 519 },
  e2e: { passed: 22, failed: 0, skipped: 0, total: 22 },
  eval: { round: "eval-20261114-r3", gatesPassed: 16, gatesTotal: 17, compareOk: false },
  corpus: { papers: 20, references: 2391, source: "scholar.db" },
} as const;

export type Evidence = typeof EVIDENCE;
