# L4 语义 judge / Claim 状态人工抽检表（Round4）

> 样本：eval_gold generative_trace 最优 trace 的 claims（含 L1-L4 全链路后的最终状态）。
> 复核口径：supported 是否确属证据可推出（漏杀=强撑）；unsupported 是否确属推不出
>（误杀=证据其实在）；needs_check 的数值黄标是否属实。

| # | 问题 | Claim（截断） | 状态 | quote_status | 人工复核（误杀/漏杀） |
|---|---|---|---|---|---|
| 1 | Which optimizer was us | The AdamW optimizer was adopted for GPT-2 training. | supported | verified | （留白） |
| 2 | Which optimizer was us | The learning rate for GPT-2 small training is 6e-4. | supported | verified | （留白） |
| 3 | Which optimizer was us | The learning rate for GPT-2 medium training is 1.5e-4. | supported | verified | （留白） |
| 4 | Which optimizer was us | The weight decay for GPT-2 training is 0.1. | supported | verified | （留白） |
| 5 | Which optimizer was us | The LAMB optimizer was adopted for BERT-large training. | supported | verified | （留白） |
| 6 | Which optimizer was us | The learning rate for BERT-large training is 3.75e-3. | supported | verified | （留白） |
