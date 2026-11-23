# Round9-R8 验收清单 —— 模块七 P2 收尾「memory_health + PATCH + 漂移提示 + 文档收口」

> 日期：2026-11-21 ｜ 依据：`docs/模块七-改造清单.md`（v2.1）P2 尾项 · 验收标准 §七.5
> 意义：**模块七八轮全部收口**——P0/P1/P2 三里程碑前后端、评测、文档、证据链完整。

## 交付物

| # | 项 | 文件 | 状态 |
|---|---|---|---|
| 1 | 健康中心 memory_health 只读披露（三处接线）：/api/health-report 返回体（events/hypotheses/active/edges/stale/LLM 抽取占比，**不入 7 子分权重**，注记"抽取 LLM 占比低=规则兜底口径在生效"）；`api.ts` HealthReport 字段；health 页模块明细区新块（memory-health-block testid） | `api_routes/routes.py`、`web/lib/api.ts`、`web/app/health/page.tsx` | ✅ |
| 2 | `PATCH /api/memory/events/{id}` append-only 修正（清单 §2.6，MemGPT self-editing 人工版）：content 修正 → 新事件（source=manual，meta.corrected_from/correction_note）+ 旧事件 superseded；**终态事件修正 → 新事件回 active 重新判定**（如实）；status 修正走 FSM 合法转移表（非法 400；非 hypothesis 400）；空体/同内容 400；404 | `api_routes/memory_routes.py` | ✅ |
| 3 | ATLAS 漂移降级口径落地（清单 P1 裁剪预案）：`drift.drift_hint`（近期 vs 更早假设/决定主题词面重叠 <15% → 文字提示，明确标注"借思想，非隐层干预"）→ `build_brief.drift_hint` 字段 → 前端简报卡渲染（memory-drift-hint testid） | `memory/drift.py`、`memory/briefing.py`、`web/app/chat/page.tsx` | ✅ |
| 4 | 文档收口（§七.5 五项声明齐备）：三层分离架构说明 · FadeMem **arXiv 预印本**标注（参数消融如实）· ATLAS「借思想非隐层干预」边界 · memory_save 白名单边界 · **"70% 无出处数字不引用"声明**（改以本仓库可复现评测说话）；另含运行复现命令与八轮归档索引；README 头部模块七简介 + 链接 | `docs/模块七-实现说明.md`、`README.md` | ✅ |
| 5 | evidence.ts 最终同步（唯一事实源脚本重跑）：**pytest 509/510 · e2e 22/22 · generatedAt 2026-11-21 22:04 +08**——README//judge//health 随构建刷新 | `web/lib/evidence.ts` | ✅ |

## 验收证据

- **后端**：全量 pytest **509 passed + 1 skipped（存量环境项）零失败**（139.9s，`pytest.log`）；ruff `check app/ tests/ scripts/` All checks passed。
- **前端**：`pnpm build` ✓（Compiled successfully，/memory 6.06 kB / /health 6.77 kB）；全量 e2e **22 passed / 0 failed**（2.6m，`playwright.log`）。
- r8 新增 8 例：PATCH 4（修正链/终态回活/FSM 修正与非法 400/校验）+ drift_hint 3（连贯静默/迁移触发含"降级口径"标注/样本不足）+ health 块 1。
- 过程缺陷自愈 1 例：drift_hint 接线笔误（`user` 未定义）导致 /brief 降级为无简报——被既有测试 `test_chat_sse_emits_memory_brief` 立即捕获（降级护栏按设计工作，未污染问答流），修复后全绿。

## 清单验收标准总核对（§七）

1. ✅ 30 会话种子后第 30 次会话开场简报正确指出被推翻假设（r7 PASS，自检内置每次重跑复验）；[M#] 时间线 + [E#] 证据链可点击溯源（r5 e2e ㉑㉒）；
2. ✅ pytest 全绿不回归（509，存量 350→全程零回归）+ web tsc/build/e2e 全绿（19→22）；
3. ✅ 时间表达式解析单测 ≥20 组（48 组参数化，含时区边界）；
4. ✅ eval 报告入 docs/acceptance/（r7：矛盾检出 R 100% vs 朴素基线 0%，结论成立；FadeMem 网格不敏感如实标注）；
5. ✅ 文档含三层分离说明 · FadeMem 预印本标注 · ATLAS 借思想边界 · memory_save 白名单边界 · 70% 数字不引用声明（docs/模块七-实现说明.md）。

## 实现期决策记录

1. **memory_health 不入 ORDER 权重**：健康中心 7 子分体系保持稳定，记忆指标走模块明细区只读披露（与 retrieval_health 同模式）。
2. **PATCH 终态修正回活语义**：修正被推翻/被取代的结论 = 对旧判定提出异议 → 新事实回 active 待重新判定（简报/worker 重判），旧终态保留在演化史——修正即新的信念演化事件，与 append-only 同哲学。
3. **ATLAS 降级口径落在简报而非面板**：三锚 embedding 不可行（黑盒 API），文字提示随简报直达用户，标注义务随文案内置——比独立面板更诚实且零新增外部依赖。

## 模块七总账（八轮）

| 轮次 | 内容 | 全量 pytest | 门 |
|---|---|---|---|
| r1 | 三表 + store FSM + timeparse | 419 | ruff |
| r2 | 轮末抽取 + runner 钩子 | 427 | ruff |
| r3 | 三信号检索 + 规则图谱 | 446 | ruff |
| r4 | 简报 + API + 注入 + [M#] | 466 | ruff |
| r5 | SSE + 前端页 + 回放 | 469 | build + e2e 22 |
| r6 | FadeMem worker + memory_save + LLM 建边 | 492 | ruff |
| r7 | 种子生成器 + 纵向金标评测 + 消融 | 501 | 报告归档 |
| **r8** | **memory_health + PATCH + 漂移提示 + 文档** | **509** | **build + e2e 22/22** |

外部依赖新增 0 · 模块删除 0 · 存量测试全程零回归。
