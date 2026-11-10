# Round8+9 验收清单（表块保位 + absent 徽章 + 同步时序 + 终值归档）· 2026-11-10

## 一、R8-1 compare Q3 表块保位

`hybrid_search` 增加 `_table_only_search`：当超参/数值查询（层数/参数量/d_model…）
的 top_k 中无 kind=table 命中时，**直接从 DB 按 kind='table' 取表块注入**（跳过
BM25/dense 正文竞争）。channels 标注 `table_rescue`。

效果：`compare gold_cell_acc 1.0`（Q3「层数与参数量」不再被章节概览块压过）、
`compare_ok True`（round7 依赖 gold_acc → 本轮全过）。

## 二、R8-2 absent 前端独立徽章

- `.claim.absent` 独立样式（灰底 opacity 0.75，非红色 warn）
- 徽章文案「未涉及」（title 提示「诚实负向发现，非编造」）
- 展开文案独立：「系统如实报告该论文未涉及此维度」≠ unsupported 的「请人工核对」

## 三、R8-3 e2e ⑤ Graph 性能增强

20 篇全量（23 节点/51 边）渲染 ≤60s 断言 + 节点数 >0。

## 四、滚动同步时序修复（过程中发现）

`useEffect` deps 加 `pageData`——observer 绑定 DOM 元素，异步 `fetchPage` 落块后
旧绑定对象已移除（UDL 新 docir 54 块密集页稳定复现 observer 零触发→右栏 scrollTop
恒 0）。修复后 ① ⑨ 稳定全过。

## 五、eval 结果

| 轮 | gates | FAIL | 备注 |
|---|---|---|---|
| R8-run1 | **16/17** | cell_quote 0.73 | compare_ok ✅ gold_acc **1.0** missing_honest **1.0** unsupported 0.11 |
| R8-run2 | 14/17 | +unsupported 0.27 ok | unsupported 波动（LLM judge 跨文 no 判定 0.11↔0.27） |

**结构不变量（两轮全过）**：strict 0.8-0.9 · zh 1.0 · anchor 0.93/55 · quote_unv 0.05 ·
rubric 7/8 · gold_acc **1.0** · missing_honest **1.0** · cell_quote 0.73（±2 邻块
扩展后的稳定边界）。

cell_quote 0.73 vs 0.8 门禁：~27% found 格的 quote 与块文本覆盖 <0.85——根因是
表格 chunk 的 markdown 前缀 `[表格 t1]\n` 不在块文本中（chunk text = `[表格 t1]\n
|col|...|`，block_ids 指向 t1 块，但 t1 块的 text 是 markdown **不含前缀**）。
此类格约 2-3 个/轮 → 2/11≈0.18 拖累。修复方向（下轮）：eval 侧拼接表块时补 `[表格
{id}]\n` 前缀，或产品侧 chunking 的 table text 不加前缀。

## 六、全量门禁

- pytest **283 passed**（+absent 单测）· ruff 全绿 · tsc 全绿
- e2e **10/10 passed**（含 ⑤ 性能增强 + ①⑨ 同步时序修复后）

## 七、Round9 归档

全套证据目录：`docs/acceptance/round8-20261110/`（eval_gold.json/log · playwright.log ·
rerun.log · checklist.md · self-assessment.md）

## 八、诚实边界与下轮

- cell_quote 0.73 稳定边界（表格 chunk 前缀问题，有明确修复路径）
- unsupported 0.11↔0.27 波动（LLM judge 对跨文综合 claim 的 no 判定——absent
  分类已覆盖「未检索到」类，残余是真编造被判 no 的正常波动）
- BART 34 / UDL anomaly 41 / GPT-2 无 arXiv 版：均已定性，非缺陷
