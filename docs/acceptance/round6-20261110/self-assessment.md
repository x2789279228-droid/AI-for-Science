# 自评表（Round6 · 证据链接口径）

## 本轮等级变动

| 模块 | round5 | round6 | 依据 |
|---|---|---|---|
| 二 抗幻觉（检索/溯源） | 生产级（跨轮不变量） | **维持+稳定性补强** | zh 1.0×3 轮（retry 0）、rubric 7-8/8、cell_quote 0.82（eval 真 bug 修复后）、quote_unv 0.0-0.02、anchor 0.86-0.97/58-65、strict 0.9；unsupported 0.24 波动如实 FAIL |
| 一 解析（数据卫生） | 维持 | **副本收敛** | converge_duplicates（健康度排序归档）；refs 第三门；BART/UDL 数据缺口定性 |
| 横切 CI | 生产级 | 维持 | 本目录全套证据 + 全量 pytest 282 |

## 下一轮目标（证据排序）

1. compare unsupported 稳定性（L4 judge 对跨文 claim 的 no 判定 0.12-0.26 分布）——
   judge prompt 校准或 claim 生成侧收紧，目标跨轮 ≤0.15
2. UDL 重解析产物验收（refs 真基线 + e2e ①⑥⑨ 补跑 + 快照）
3. BART 解析层缺口（块内无边界的 ACL 风格，需 MinerU 重解析而非切分调优）
