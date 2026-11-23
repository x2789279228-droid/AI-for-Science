# Round9-R3 验收清单 —— 模块七 R3「时间感知检索 + 规则版时间图谱」

> 日期：2026-11-21 ｜ 依据：`docs/模块七-改造清单.md`（v2.1）P0 里程碑 · 对应 W2 检查点前半
> 范围：retrieve.py（三信号检索全链路）+ graph.py（THEANINE 规则版建边 / 时间线路径）+ 19 例单测

## 交付物

| # | 项 | 文件 | 状态 |
|---|---|---|---|
| 1 | 三信号检索：recency=0.995^访问间隔小时 · importance · relevance（dense 余弦 / off 转 cjk bigram 包含度），**min-max 归一化等权相加** × FadeMem 强度乘子（只影响排序） | `api/app/memory/retrieve.py` | ✅ |
| 2 | 时间窗：timeparse 区间 ∩ 事件有效窗（t_valid 缺省回落 [ts, +inf)），ts 在窗外但 t_valid 覆盖即命中 | 同上 | ✅ |
| 3 | 实体过滤：query 命中 entities 表 canonical/别名 → 限定事件 | 同上 | ✅ |
| 4 | 多跳 BFS 深度≤2：邻居得分 ×0.5^hop，带 hops/via 溯源字段；返回项 touch_access | 同上 | ✅ |
| 5 | 降级路径（§2.10）：无向量/查询向量失败 → 词面通道接管，`meta.embed` 如实标注 off/dense | 同上 | ✅ |
| 6 | 规则版建边（THEANINE Phase I）：相似候选 top-3（dense 余弦 / 词面 Jaccard，evidence.engine 如实标注）；反驳标记→refutes（新→旧）、取代标记→supersedes（新→旧）、相似≥阈值→same_topic（旧→新） | `api/app/memory/graph.py` | ✅ |
| 7 | 时间线路径（Phase II）：种子沿 active 边无向连通分量按 ts 线性化 + 相邻关系投影 | 同上 | ✅ |
| 8 | 会话级补建边 build_for_conversation（幂等，UPSERT confirm_count） | 同上 | ✅ |

## 验收证据

- **全量 pytest：446 passed + 1 skipped，0 failed**（143.2s），见 `pytest.log`。R3 新增 19 例（检索 11 + 图谱 8），存量 427 例零回归。skip 仍为存量环境条件项（UDL DocIR 不在本机）。
- **ruff**：`app/memory/` + 两个新测试文件 All checks passed。

## 实现期决策记录

1. **词面通道用 cjk bigram 包含度而非新建 FTS5 表**：清单 §2.10 写"FTS5 关键词通道兜底"——memory_events 未建 FTS 索引（R1 表结构无此设计），bigram 包含度是等价的词面信号且零新表；`meta.embed` 如实区分 dense/off。P2 若评测显示词面召回不足再评估补 FTS 表。
2. **多跳邻居得分的强度不重复施加**：BFS 邻居 final = 来源节点 final × 0.5^hop（来源 final 已含其自身 strength），避免链式重乘导致深度惩罚失真。
3. **graph.link_event 的弱相关护栏**：sim < 0.12 且无反驳/取代标记 → 不建边（防词面通道在短文本上噪声建边）。
4. **retrieve 复用 indexing._pack/_unpack/_query_vector**（noqa: PLC2701 同包复用）——与 chunks.vec 完全同约定，无第二个向量格式。

## 与清单验收标准的对应

- §七.4 的纵向金标评测属 P2（R6+）；本轮交付的 scores 全字段展开（recency/importance/relevance/base/strength/final/hops/via）为消融评测的埋点。
- §一 内部替换②a 的"等权相加"语义在 `retrieve._min_max` + 均值处落地并有测试锁定（test_strength_multiplier / test_recency_signal_component）。

## 边界声明（R3 不含）

responder 注入与 [M#] 校验（R4）· briefing（R4）· LLM 建边升级（P1）· FadeMem 强度动态重算 worker（P1，R1 已落列 strength 静态初值）。
