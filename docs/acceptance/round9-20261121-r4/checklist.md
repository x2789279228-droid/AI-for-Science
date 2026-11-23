# Round9-R4 验收清单 —— 模块七 R4「研究简报 + 记忆 API + responder 注入与 [M#] 校验」

> 日期：2026-11-21 ｜ 依据：`docs/模块七-改造清单.md`（v2.1）P0 里程碑 · 对应 W2/W3 检查点
> 范围：briefing.py + briefing.yaml + memory_routes.py 三端点 + responder 时间线注入 + [M#] 独立校验 + 20 例单测
> 意义：演示主链「第 30 次会话说出假设被推翻」的后端拼图在此轮补齐（简报 = extrapolation 承载机制）。

## 交付物

| # | 项 | 文件 | 状态 |
|---|---|---|---|
| 1 | 研究简报：假设 vs 其时间戳后的新证据（记忆事件 / 图谱边 / tracked_items 三源收集）；judge 路（`briefing.yaml` v1.0.0 入台账，verdict_source="judge"）离线自动降级为规则路——**refutes/supersedes 边→refuted、supports 边→supported（source=evidence）、有证据无边→neutral（source=rule）、无证据→unverified 不冒充（source=""）**；判定合法时推进 FSM（proposed→active→refuted/supported），非法转移跳过且判定照实上报；终态假设入 past_hypotheses（信念演化史） | `api/app/memory/briefing.py` | ✅ |
| 2 | 三端点 + 事件回读：GET /api/memory/timeline（ISO 8601 输出 +08:00、start/end/kind/status/conversation 过滤、边限定返回事件集）、GET /api/memory/hypotheses（FSM 状态 + transitions + legal_next）、POST /api/memory/brief（conversation 限定 / use_judge 开关）、GET /api/memory/events/{id}；挂载 main.py | `api/app/api_routes/memory_routes.py`、`app/main.py` | ✅ |
| 3 | responder 注入：`build_memory_block`（检索 top-k + 时间线路径 → [M#] 动态编号 + 回放锚点 conversation/turn/thread_id）；paper 模式只注入 hypothesis/finding/decision，general 全量（清单 §三#12）；**任何失败静默降级为空，绝不阻断问答** | `api/app/memory/retrieve.py` | ✅ |
| 4 | [M#] 独立编号校验（清单 §2.9）：`validate_memory_citations`——L1 存在性 + 无效剔除 + `invalid_memory_citation:M{n}` guardrail；与 E# 互不干扰；不参与 no_citation/L2/定向重生成；未注入记忆时出现 [M#] 一律按幻觉剔除 | `api/app/rag/answering.py` | ✅ |
| 5 | use_memory 开关全链路：ChatBody.use_memory（默认 True）→ run_stream → graph input → state.use_memory → responder 门控；memory_anchors 经 state → `_push_success` 条件下发 final 事件 | `routes.py`/`runner.py`/`state.py`/`nodes.py` | ✅ |

## 验收证据

- **全量 pytest：466 passed + 1 skipped，0 failed**（136.8s），见 `pytest.log`。R4 新增 20 例（briefing 7 / routes 4 / inject 9），存量 446 例零回归（responder 主链改动被全部 chat/audit/API 级用例覆盖验证）。skip 仍为存量环境条件项。
- **ruff**：`ruff check app/` 全仓 All checks passed（含 R4 全部新改文件）。

## 实现期决策记录

1. **relevance 归一化全零特例**（测试驱动的真实缺陷修复）：所有候选原始相关度全零时归一化会把 0 抬成 1.0（min-max 语义），导致完全不相关记忆可被注入。修复为归一化置零 + 注入门槛改看**原始**相关度（`scores.relevance_raw`，弱相关不再被归一化压成 0 误杀）——该字段同时服务 P2 消融评测。
2. ** FSM 非法转移在简报链路跳过不抛**（清单 §2.2 的 API/后台双口径落地）：如 supported→refuted 被转移表拒绝，判定仍如实上报（verdict=refuted），transitions_applied=[] 审计。
3. **compare/writer/graph/viz 路径不注入记忆块**（R4 只注入 qa 路径）：结构化 prompt 通道注入自由文本时间线易干扰格式；P1 视答辩需要再评估 compare 通道。
4. **brief 判定缓存暂缓**：清单 §2.7 的 worker 预生成缓存属 P1（worker 落地时一并做）；R4 提供按需计算（离线规则路毫秒级，judge 路 LLM 延迟由开场事件容忍）。
5. **时间线端点边子集**：edges 只返回两端都在返回事件集内的 active 边——前端 G6 因果图数据口径一次到位。

## 与清单验收标准的对应

- §七.1 的「简报正确指出被推翻假设」：后端判定链已有测试锁定（`test_refuted_chain_with_fsm_transition`：proposed→active→refuted 全链 + verdict_source=evidence）；端到端 30 会话验收在 R5（前端）/P2（种子数据）完成。
- §三#15 ChatBody.use_memory 默认 True：有测试锁定（`test_chat_body_use_memory_default`）。

## 边界声明（R4 不含）

memory_brief SSE 开场事件（R5，与前端简报卡一起做）· worker 简报缓存（P1）· PATCH append-only 修正入口（P2）· LLM judge 路的线上验证（离线环境只测降级路径，P2 eval 补）。
