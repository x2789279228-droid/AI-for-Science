// 自动生成：scripts/write_evidence.py —— 请勿手改。
// README / /judge / /health 的数字唯一来源；更新方式见文件头说明。
export const EVIDENCE = {
  generatedAt: "2026-11-21 22:04 +08",
  commit: "3fe4324",
  branch: "main",
  pytest: { passed: 509, failed: 0, errors: 0, skipped: 1, total: 510 },
  e2e: { passed: 22, failed: 0, skipped: 0, total: 22 },
  eval: { round: "eval-20261114-r3", gatesPassed: 16, gatesTotal: 17, compareOk: false },
  corpus: { papers: 20, references: 2391, source: "scholar.db" },
} as const;

export type Evidence = typeof EVIDENCE;
