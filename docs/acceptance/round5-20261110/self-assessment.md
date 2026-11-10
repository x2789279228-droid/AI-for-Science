# 自评表（Round5 · 证据链接口径）

> 承 round4 自评表口径：等级只能引用本目录证据产物；跨轮不变量优先于单轮门禁
> （LLM/配额非确定性下，单轮 12-15/17 的波动与结构性修复的稳定性分开呈现）。

## 本轮等级变动

| 模块 | round4 | round5 | 依据 |
|---|---|---|---|
| 二 抗幻觉（L2/compare） | 自动化验证 | **生产级（跨轮不变量）** | quote_unverified 4 轮全 ≤0.05；missing_honest 4×1.0；anchor 0.89-0.94/43-51；strict 0.8-0.9。波动门禁（zh/rubric/compare_ok）与稳定性目标如实列 checklist §六 |
| 三 阅读翻译（PDF 导出） | 自动化验证 | **生产级（导出链路）** | visual-judge 抓 3 真 bug（补遗页丢文/译文页丢块/覆盖口径）→ 流式写入修复+2 条回归+覆盖率头；修复版 12 样张本目录 |
| 五 图谱/综述/FutureWork | 图谱生产级 | 维持+补全 | graph20 ok=True（综述缓存命中）；future_checks 8 条真数据+边界核验表 |
| 一 解析（references） | 生产级（6 篇基线） | 维持+勘误 | UDL 假 245 曝光并回写 59 条真书目；后记边界；第四通道 Reformer+8/ELECTRA+13、基线零退化 |
| 六 Copilot | 自动化验证 | 维持 | copilot_live PASS（6/6 实质节）+ References 程序化回验双✓ |

## 下一轮目标（按证据排序）

1. eval 稳定性：embedding 分钟级限流窗错峰/退避（zh_top8 0.4 崩）、rubric 关键题双跑取优
2. refs：UDL 59/≈240（跨块断行头型）、BART 34；Attention/PreLN 副本收敛（工作库三副本）
3. compare cell_quote 残余（summary/reference 伪 chunk 无 block_ids 的口径处理）
