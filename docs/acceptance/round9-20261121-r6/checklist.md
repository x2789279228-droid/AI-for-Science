# Round9-R6 验收清单 —— 模块七 P1「FadeMem 巩固 worker + memory_save 工具 + LLM 建边升级」

> 日期：2026-11-21 ｜ 依据：`docs/模块七-改造清单.md`（v2.1）P1 里程碑
> 范围：drift.py + worker.py + main/config 接线 + tools.py memory_save + graph.py LLM 建边升级 + 23 例单测

## 交付物

| # | 项 | 文件 | 状态 |
|---|---|---|---|
| 1 | FadeMem 双层软衰减：`v(t)=v0·exp(−λ·(t−τ)^β)`，`λ=λ_base·e^(−μI)`；**λ_base = ln2/(11.25d)^β_LML**（β 指数律下 t 半衰期位移已解，I=0 时 LML 在 11.25 天精确半衰，测试 1e-9 锁定）；LML β=0.8 / SML β=1.2；滞回迁移 0.7/0.3（中间保持防抖）；`recalculate_strengths` 批量重算**只改 strength/layer 永不删除** | `api/app/memory/drift.py` | ✅ |
| 2 | 巩固 worker：仿 tracker daemon 线程（`_stop.wait(interval)` 先等后跑 + interval≤0 关闭）；周期任务 = 强度重算 / FSM 复核（**proposed→active 与同主题 superseded；不代行 refuted/supported 判定**——判定源留给 briefing 可审计）/ 简报缓存预热（用户级 + 最近 3 会话）；config 新增 `memory_worker_interval_s` 与 fade 五参数（env 可覆盖供 P2 消融）；main lifespan 挂载/停止 | `api/app/memory/worker.py`、`config.py`、`main.py` | ✅ |
| 3 | `memory_save` 受控自写工具（MemGPT self-editing 落地）：ToolSpec 注册（第 14 个工具）；allowed_agents={reader,compare,writer,graph}；**不进 GENERAL_DISABLED_TOOLS**（写记忆非证据源，双模式均可用）；只写事件 source=agent、会话/轮次溯源经 `turn_ctx` ContextVar（runner 置值，与 set_doc_mode 同机制）；零 LLM 调用不阻塞主链路；**与轮末抽取同轮同内容去重** | `api/app/agents/tools.py`、`memory/extract.py`、`agents/runner.py` | ✅ |
| 4 | LLM 建边升级：`link_event(use_llm=True)` → `memory_relations.yaml`（v1.0.0 入台账）判六类关系或 none（kind=llm；方向约定 refutes/supersedes=新→旧、其余=旧→新）；**同事件对上关系不同的旧边（含规则边）superseded 保留演化史**（evidence.superseded_by_relation）；离线/失败/none/非法 payload 四路自动回落规则路 | `api/app/memory/graph.py`、`app/prompts/memory_relations.yaml` | ✅ |

## 验收证据

- **全量 pytest：492 passed + 1 skipped（存量），0 failed**（186.7s），见 `pytest.log`。P1 新增 23 例（drift 8 / worker 5 / save 工具 5 / 建边升级 5）全过；存量 469 零回归（含工具注册表契约测试同步 13→14）。
- **ruff**：`check app/ tests/` All checks passed。

## 实现期决策记录

1. **λ_base 的 β 位移修正**：清单 §2.3 写 `λ_base = ln2/11.25d`，但 β 指数律下「半衰期」定义在 t 轴有位移（t½ = (ln2/λ_base)^(1/β) ≈ 20.9 天 ≠ 11.25 天）。落地为 `λ_base = ln2/(11.25d)^β_LML`，使「I=0 时 LML 半衰期 = 11.25 天」在 β=0.8 律下精确成立（与陈述口径一致），测试以 1e-9 精度锁定。
2. **worker 不代行终态判定**：refuted/supported 由 briefing 显式触发（verdict_source=evidence/judge 可审计）；worker 只做 proposed→active 提升与同主题 superseded——判定权与审计权不分离。
3. **memory_save 与轮末抽取的去重方向**：agent 先写 → 抽取跳过（同轮同内容）；agent 写入优先级高于事后抽取（agent 是显式意图）。反向（抽取先写、agent 后写）由 append-only 语义允许共存（确认即演化）。
4. **LLM 建边方向映射**：refutes/supersedes 语义指向被作用方（新→旧）；supports/elaborates/same_topic/cause 沿时序先序（旧→新）——与规则路方向一致，时间线路径投影不受影响。
5. **工具注册表契约测试同步**：`test_tool_registry_13_tools_and_whitelist` 断言集合精确相等 → 14 工具 + memory_save 双模式边界断言（加工具即更新契约，属预期维护）。

## 测试基建注记

relations 用例首版跨用例共享 user_id 且事件内容相同 → 共享库中前序事件污染建边候选 top_n（单跑过、合跑挂）。修正为每用例独立 user_id——与会话共享库共存的断言隔离约定再次验证（R3 起的既有约定）。

## 边界声明（P1 不含）

ATLAS 三锚漂移指标与面板（清单 P1 尾项；裁剪预案「降级简报文字提示」待 P2 与健康中心一并评估）· judge 线上质量调优（P2 纵向金标）· 简报 worker 预热的 judge 口径统一（当前 worker 预热走 cached_brief 规则版 + 后台 judge 回填，与 R5 口径一致）。
