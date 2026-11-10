# Round7 验收清单（absent 分类 + BART 重解析 + UDL anomaly 定性）· 2026-11-10

## 一、核心修复：诚实缺失声明分类（R7-1）

**unsupported_ratio 的口径 bug**：「未检索到/未涉及」类声明是系统的**诚实负向发现**
（对比场景对侧无证据时的正确输出——missing_honest 门禁验证的正是它），judge 判 no
逻辑上没错（负向声明无正向证据可推），但把它计入幻觉率是**口径错误**。

修复：`verify._mark_absence_claims` —— L4 判 no 后，命中缺失声明正则
（未检索到/未涉及/未提及/未找到/未提供/未报告/not report/no reported/…）的 claim
改标 `status="absent"`（guardrail `absence_claims:N` 如实计数）。

**效果**：unsupported_ratio 0.24→**0.06**（跨文综合的诚实「没找到」不再冒充幻觉）；
前端 absent 显示为中性灰（不标红「无支撑」）。

## 二、eval 结果

**16/17 → 15/17**（第二轮 gold_acc 方差）——**结构不变量全部稳定**：

| 指标 | 值 | 状态 |
|---|---|---|
| strict_recall_at5 | 0.9 | ✅ |
| zh_top8 | **1.0**（retry 0） | ✅ round6 修复维持 |
| anchor_precision | 0.91-0.96 / 45-47 | ✅ |
| quote_unverified | 0.03-0.06 | ✅ |
| rubric | 6/8（=0.75 达标） | ✅ |
| compare missing_honest | **1.0** | ✅ |
| compare unsupported | **0.06-0.13**（absent 分类后） | ✅ ≤0.15 |
| compare cell_quote | **0.82** | ✅ ≥0.8 |
| compare gold_cell_acc | 0.67-1.0 | ⚠️ Q3 检索方差（「章节概览」块盖过 Table 1） |
| compare_ok | 随 gold_acc 波动 | ⚠️ 同上 |

## 三、BART 重解析（R7-2）

MinerU 全文重解析（160s），refs 34 条不变——**34 条即 arXiv v1 10 页 PDF 的实际
完整含量**（末页物理截断于 XLNet 条目，PDF 本身只有 10 页）。「BART 34」非切分/解析
bug，是**源 PDF 版本差异**（正式版 ACL 2020 有 16 页+完整 refs）。如实定性。

## 四、UDL anomaly 41（R7-3）

anomaly 条目全部 raw=500 字符截断——是**长作者列表/venue 的真条目**（非跑页头残片
或垃圾碎片），4.1% 在 10% 门内。anomaly 标签工作正确（如实标长条目），无需强修。

## 五、自动化门禁

- pytest **283 passed / 0 failed**（+1：absent 分类单测）
- ruff 全绿 · e2e **9 passed** 全量
- 金标 P1 侧关键词扩容（转导/自注意力/循环/卷积/transduction/self-attention——
  LLM 用同义表述描述 Attention 无预训练的事实时旧金标词覆盖不足）

## 六、诚实边界

- compare Q3「层数与参数量」检索方差：章节概览块（[章节概览] Model Architecture）
  有时盖过 Table 1 数值块（zh 表块加权需表格先入 top_k）——gold_acc 0.67-1.0 波动
- absent 分类只影响 unsupported 计数，不改变 claim 的前端红色/灰色显示（absent
  显示为灰色中性态）
- BART 34 条为源 PDF 完整含量（arXiv v1 10 页版本），非系统缺陷

## 七、下轮目标

1. compare Q3 检索：「层数/参数量」维度强制检索 Table 块（表块 top_k 保位）
2. e2e ⑤ Graph 在 20 篇全量下节点密度/渲染性能验证
3. absent claims 前端独立徽章（当前复用灰色「无支撑」态）
