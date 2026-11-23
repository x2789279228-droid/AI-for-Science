# Round9-R5 验收清单 —— 模块七 R5「开场简报 SSE + /memory 页 + chat 简报卡与 [M#] 回放」

> 日期：2026-11-21 ｜ 依据：`docs/模块七-改造清单.md`（v2.1）P0 收尾轮
> 意义：P0 核心闭环（标注「不可裁」）前后端全部就绪；清单 §七.2「web tsc / build / e2e 全绿（19→22）」达成。

## 交付物

| # | 项 | 文件 | 状态 |
|---|---|---|---|
| 1 | memory_brief SSE 开场事件：meta 后直发；`cached_brief`（TTL 300s + memory_events **代数戳**自动失效，免 briefing↔store 循环依赖）——未命中同步走规则路（零 LLM 阻塞），LLM 可用时后台线程 judge 回填供下次；空简报 `{empty:true}` 前端不渲染；`use_memory=false` 整条链不下发 | `api/app/memory/briefing.py`、`api_routes/routes.py` | ✅ |
| 2 | /memory 第 11 导航页：记忆事件流（kind 着色 + 会话轮次溯源）+ G6 因果边图（`loadG6` 惰性 + `canvasTheme` 四皮肤 + kind/relation 语义色，失败降级关系列表不空屏）+ 假设 FSM 状态板（状态徽章 + legal_next + transitions）；**时间表达式筛选框直通后端 timeparse**（timeline 新增 `expr` 参数） | `web/app/memory/page.tsx`、`web/components/memory/MemoryGraph.tsx`、`AppShell.tsx`、`memory_routes.py` | ✅ |
| 3 | /chat 开场简报卡：SSE `memoryBrief` + 挂载取数双通道（统一**规则判定口径**）；verdict 徽章（已推翻/已支持/待复核/未验证）+ 证据 chip 点击回放 | `web/app/chat/page.tsx` | ✅ |
| 4 | [M#] 引用链前端闭环：`rehypeCitations` 扩展 `[M\d+]`（m-cite accent 样式，与 E# 视觉区分）+ 点击 → `MemoryLocModal` 会话轮次回放浮层（当时记录 + 打开该会话）；`memory_anchors` 随 final 下发 | `MarkdownWithCitations.tsx`、`chat/page.tsx` | ✅ |
| 5 | e2e 19→22：⑳ 时间线页渲染（事件流/状态板/G6 canvas）· ㉑ 简报卡出现且如实指出被推翻假设 · ㉒ M# chip 点击 → 回放浮层；种子经后端 venv Python 直写同一 SQLite（清旧插新，自包含无顺序依赖，不依赖 LLM 输出） | `web/e2e/acceptance.spec.ts` | ✅ |

## 验收证据

- **后端**：`pytest -q` 全量 **469 passed + 1 skipped（存量）零失败**（`pytest.log`）；`ruff check app/ tests/` 全绿。
- **前端**：`pnpm build` 通过（/memory 路由 6.05 kB / First Load 99.8 kB，tsc 零错误）。
- **e2e**：全量 **22 passed (2.3m) / 0 failed**（`playwright.log`）——19 存量无回归 + 3 新增全过，§七.2 达成。
- **唯一事实源**：`scripts/write_evidence.py` 已重跑，`web/lib/evidence.ts` 同步为 pytest 469/470 · e2e 22/22（generatedAt 2026-11-21 21:05 +08），README//judge//health 数字随构建刷新。

## R5 过程中抓到并修复的缺陷（测试/e2e 驱动）

1. **`LLMResult` 是 dict 不是属性对象**（离线测试覆盖不到、LLM 可用才触发）：`res.error/res.text` → `res.ok / res.get("text")`。`extract._llm_extract` 与 `briefing._judge_llm` 同款双修。
2. **POST /api/memory/brief 未传 use_judge**：R4 端点只把 `engine` 字符串改成 "rule" 而判定仍走 judge——判定源与标注不一致（比坏更糟的不诚实）。已修：`use_judge` 直通 `build_brief`，engine 由实际路径写入。
3. **relevance 归一化全零抬升**（R3 遗留，R5 注入门槛暴露）：归一化后门槛会误杀弱相关 → 新增 `scores.relevance_raw`，注入门槛改看原始值。
4. **G6 dev 双挂载 canvas 叠加**：容器重建前 `innerHTML=""` 清空 + e2e 断言 `.first()`。
5. **存量 ⑫ 选择器过时（非本轮回归）**：HEAD 起 dock 提问框已是 `<textarea class="dock-input">`，`.dock input` 匹配 0 元素——git 查证确认 HEAD 即坏（evidence 19/19 快照为 11-15 旧状态）；修复为 `.dock textarea`。

## 实现期决策记录

1. **chat 简报两通道统一规则判定口径**（挂载取数 `use_judge=false` + SSE `cached_brief` 未命中即规则版）：对齐清单 §2.7「开场零 LLM」；确定性同时是 e2e 稳定性的前提。judge 判定经后台回填进缓存供后续查看，P1 worker 接管预热后口径再评估。
2. **e2e 种子 = 后端 venv Python 直写同一 SQLite**（清旧插新、WAL 并发安全）：记忆内容确定性不依赖 LLM 抽取输出；conftest 会话隔离不适用于跨进程共享库，故断言全部按内容/first() 定位而非绝对计数。
3. **真实 LLM judge 质量问题如实记录（P2 评测对象）**：e2e 环境真实 doubao judge 曾把「推翻了假设 H 的结论」自指句式判成 supported 并推进 FSM（R4 的 `_apply_verdict_transition` 按设计执行，状态污染已在数据清理中回滚）。已做 prompt 自指防御加固（briefing.yaml v1.0.0→changelog 待 P1 版本化时提升），系统性调优归 P2 纵向金标评测（含 judge 一致性项）。

## 边界声明（R5 不含）

`memory_save` agent 工具 / FadeMem worker / LLM 建边升级（P1）· 种子生成器与纵向金标评测 / 健康中心 memory_health / PATCH append-only 修正（P2）。简报 judge 口径的线上质量调优归 P2（含 reasoning 与判定一致性的金标项）。
