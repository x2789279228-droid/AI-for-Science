# 演示操作手册：断点续跑（Checkpoint Resume）

> 两条演示路径均已实测通过（2026-11-07）。全部依赖已内建：故障注入器（`SCHOLAR_FAIL_AT`）
> + SqliteSaver checkpoint + 恢复端点 + 前端「继续执行」按钮。

## 准备（后台操作，约 30 秒）

```bash
# 带故障注入启动后端：verifier 节点第 1 次执行必崩（模拟硬故障）
SCHOLAR_FAIL_AT="verifier:1" uvicorn app.main:app --port 8100
# 前端正常启动
cd web && npm run dev
```

## 路径 A：手动恢复（前端按钮，主分镜，~40 秒）

| 步 | 操作 | 预期展示效果 |
|---|---|---|
| 1 | 溯源问答页输入问题（如「训练用了多少块 GPU？」）发送 | 右侧 Trace 时间线逐节点点亮：意图识别 → Reader 接手 → 检索计划 → 并行检索 |
| 2 | 执行到 verifier 时进程内故障触发 | 气泡出现红色提示「[执行失败：RuntimeError…verifier 第 1 次执行]」+ 徽章「⚠ 图执行中断 · 断点已保存」+ **「继续执行」按钮** |
| 3 | 讲解要点：**"系统在 Verifier 节点前被打断了。注意：前面 Router、Planner、三路并行检索的结果都已 checkpoint 落盘，不会重跑。"** | 可切到 `/api/threads/{tid}` 展示 JSON：`status: failed, pending: ["verifier"]`（断点位置精确到节点） |
| 4 | 点「继续执行」 | Trace 从 **verifier 节点续起**：证据校验 → 生成回答流式输出；已完成节点不重复点亮 |
| 5 | 讲解要点：**"恢复用的是 LangGraph SqliteSaver——每次节点边界写 checkpoint，`invoke(None)` 从最近断点续跑。"** | 最终回答带 `[^p6-§…]` 引用锚点 + ClaimTrace 逐论断徽章 |

**实测数据**（真机彩排）：恢复耗时 6.0s；恢复后 trace 主节点每类恰好一张卡（router/planner 来自崩溃前，verifier/responder 来自恢复后），零重复。

## 路径 B：进程级崩溃 + 重启自动恢复（进阶分镜，约 30 秒）

| 步 | 操作 | 预期展示效果 |
|---|---|---|
| 1 | 同上触发失败后，**直接杀掉后端进程**（Ctrl+C） | 网页还在，请求失败——强调"进程死了，checkpoint 在磁盘（data/checkpoints.db）" |
| 2 | 不带故障注入重新启动后端 | 启动日志/线程列表显示崩溃遗留线程被**自动恢复**（`resume_attempts: 1`） |
| 3 | 打开透明度中心 / 线程详情 | `status: done`，回答完整带引用——**没人点任何按钮，系统自愈了** |

**实测数据**：重启后自动恢复完成，回放 `/api/qa/{trace_id}` 事件流完整（trace 7 卡 + plan 1 + tool_call 1 + evidence 1 + delta 77），主节点零重复。

## 常见问题与解答

- **"为什么子图不挂 checkpoint？"** —— Executor 子图经 wrapper 手动 invoke，父图节点粒度已覆盖（检索幂等，重跑无害）；避免父子图在同一 thread_id 上写 checkpoint 冲突。
- **"恢复预算？"** —— 每线程最多 3 次恢复尝试（`MAX_RESUME_ATTEMPTS`），防死循环；完成态线程恢复返回 409。
- **"trace 为什么不重复？"** —— 累加键（trace/raw_hits…）存在 checkpoint 里，续跑从断点状态继续累加，而非从零重放。

## 复位

演示完把 `SCHOLAR_FAIL_AT` 从环境变量移除重启后端（故障计数是进程内的，重启即清零）。
