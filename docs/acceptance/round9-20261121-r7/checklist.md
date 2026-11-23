# Round9-R7 验收清单 —— 模块七 P2·r7「30 会话种子生成器 + 纵向金标评测 + 消融」

> 日期：2026-11-21 ｜ 依据：`docs/模块七-改造清单.md`（v2.1）P2 里程碑 · 验收标准 §七.1 / §七.4
> 产出：seed_gold.json（半人工金标）· seed_review_sample.md（20% 抽检清单）· eval_report.md / eval_memory.json（评测报告）· eval_stdout.log

## 交付物

| # | 项 | 文件 | 状态 |
|---|---|---|---|
| 1 | 30 会话仿真生成器：四条故事线（A 被推翻 refutes 边 / B 被支持 supports 边 / C 无证据 unverified / D 被取代 superseded）+ 噪声会话干扰面 + 时间戳回填（30 天跨度）；**金标与事件同步产出**；**§七.1 自检内置**（生成后即跑开场简报核对被推翻假设）；库已存在拒绝覆盖（--force 才重生成）；`--seed` 可复现（纯函数 build_plan 有测试锁定） | `api/scripts/seed_memory_demo.py` | ✅ |
| 2 | 纵向金标评测：假设召回（verdict 精确匹配）/ 矛盾检出 P-R / 陈旧判断（hit@5 + avoid 泄漏）/ **三基线消融**（无记忆 / 朴素 top-k 纯词面 / 全历史注入）/ **FadeMem 网格消融**（β_LML×μ 3×3，含 top5 平均强度列证明机制生效）/ judge 一致性开关（--judge；离线环境如实标"未评测"）；报告 md+json 双产物 | `api/scripts/eval_memory.py` | ✅ |
| 3 | 20% 人工抽检清单：故事线事件全抽 + 噪声补足至 ≥20%（9 条）；清单含人工核对栏，**status=pending 待人工签署**——"半人工金标"口径落地（已确认决策 #3） | seed_review_sample.md | ✅ |

## 验收证据（数字全部来自本次实跑，真实）

### §七.1 第 30 会话开场简报 —— **PASS**
`假设 A 判定结果=refuted（期望 refuted）；位置=past_hypotheses；engine=rule`
（判定后假设 FSM 推进至终态 → 以"已被推翻"身份出现在简报信念演化史中——这正是简报的表述方式）

### §七.4 主口径 vs 基线（事件 37 条 · top_k=5）
| 指标 | 全功能主口径 | 朴素 top-k | 全历史 | 无记忆 |
|---|---|---|---|---|
| 假设召回（verdict 匹配） | **100%**（5/5） | 80%（内容存在口径，无判定能力） | 100%（口径必然） | 0% |
| 矛盾检出 | **P=100% · R=100%**（TP/FP/FN=1/0/0） | P 未命中 · **R=0%** | 不适用 | 0% |
| 陈旧 hit@5 | 100% | 100% | 100%（必然） | 0% |
| 陈旧 avoid 泄漏 | 50%（无窗查询）/ **0%（时间窗查询）** | 83% | **100%（必然全泄漏）** | 0% |

**结论：矛盾检出优于朴素 top-k 基线**（R 100% vs 0%；朴素口径词面无法判定方向）。

### FadeMem 网格（β_LML ∈ 0.6/0.8/1.0 × μ ∈ 0.2/0.5/0.8，θ 固定）
hit@5 全组 100%、avoid 全组 0%——**37 条小语料上 top5 边界对参数不敏感**（如实标注，不编造差异）；机制生效证明 = top5 平均强度随 μ 单调上升（0.292→0.340），随 β 变化。参数敏感性结论需更大语料（遗留，见下）。

### 评测驱动的两个真实机制修复（进生产代码）
1. **时间窗语义修正**（`retrieve._in_query_window`）：无 t_valid 的事件原实现按 `[ts,+∞)` 有效窗与查询窗"相交"——任何窗外旧事件都命中。修正为：有 t_valid 走**事实有效期交集**语义（"上季度沿用的协议"），无 t_valid 走**发生时间落窗**语义（"最近两周的实验"）——已有 4 例测试锁定（含跨窗 t_valid 用例防回归）。
2. **「当前不为真」降权**（`retrieve.STALE_PENALTY=0.3`）：superseded/refuted 记忆在无时间窗查询时排序降权（历史不删除、按时间窗仍可召回）——陈述模块三"whether still true 一等公民"的兑现，query2 avoid 泄漏 100%→0%。

### 工程门
- 全量 pytest **501 passed + 1 skipped（存量）零失败**（含 P2 新增 9 例：seed/eval 纯函数与金标结构）；ruff `check app/ tests/ scripts/` 全绿。

## 实现期决策记录

1. **§七.1 简报是用户级**：判定终态假设出现在 past_hypotheses（"已被推翻"）即为"简报指出被推翻"——自检与评测统一按 items verdict ∪ past status 双通道判定。
2. **支持实验不建 supports 边**：规则路无 supports 标记；B 线的 supports 边为直写 upsert（meta.edges 如实标注"模拟 LLM 建边产物，离线环境"）；A 的 verdict 由 refutes 边独立决定，支持实验仅作陈旧判断 avoid 目标。
3. **FadeMem 消融结论如实**：小语料无区分度 + top5 平均强度证明机制单调生效；"参数敏感性需更大语料"记为遗留——不编造差异（踩坑 #2 的诚实版）。
4. **judge 一致性**：离线环境未评测，报告如实标注"--judge + LLM 环境重跑"；R5 已记录的真实 judge 质量问题（自指句式）由 prompt 防御加固缓解，系统性调优在 LLM 环境的 eval --judge 轮进行。

## 边界声明（r7 不含）

健康中心 memory_health 只读指标 · PATCH append-only 修正 · ATLAS 漂移面板 · 人工抽检的实际签署（status=pending 待人工执行）。
