# Meridian Agent 状态图（代码自动生成）

> 本图由 `build_graph().get_graph().draw_mermaid()` 自动生成——架构图即代码，节点/边改动后重跑该命令即可同步：
>
> ```bash
> cd api && uv run python -c "from app.agents.graph import build_graph; print(build_graph().get_graph().draw_mermaid())"
> ```
>
> 评审叙事：工作流编排（LangGraph 显式状态机）+ 节点即卡片执行可视化。
> 当前版本（2026-11-09 重新生成）：主图 14 节点——含 5 个 Agent 专属入口（reader/compare/graph/writer/viz）与 3 个专属产物节点（align/graph_build/viz_extract）；Executor 为独立编译子图（executor_entry → Send 扇出 retrieve_task×N → executor_fuse），经 wrapper 节点手动 invoke（父图 trace 不经子图 reducer 回流）。

## 主图（Router → 5 Agent 入口 → Planner → Executor → Verifier → 产物节点 → Responder）

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	router(router)
	reader_entry(reader_entry)
	compare_entry(compare_entry)
	graph_entry(graph_entry)
	writer_entry(writer_entry)
	viz_entry(viz_entry)
	planner(planner)
	executor(executor)
	verifier(verifier)
	align(align)
	graph_build(graph_build)
	viz_extract(viz_extract)
	recover(recover)
	responder(responder)
	__end__([<p>__end__</p>]):::last
	__start__ --> router;
	align --> responder;
	compare_entry -.-> planner;
	compare_entry -.-> responder;
	executor --> verifier;
	graph_build --> responder;
	graph_entry --> planner;
	planner --> executor;
	reader_entry --> planner;
	recover -.-> __end__;
	recover -.-> executor;
	recover -.-> responder;
	responder -.-> __end__;
	responder -.-> recover;
	router -.-> compare_entry;
	router -.-> graph_entry;
	router -.-> reader_entry;
	router -.-> responder;
	router -.-> viz_entry;
	router -.-> writer_entry;
	verifier -.-> align;
	verifier -.-> graph_build;
	verifier -.-> recover;
	verifier -.-> responder;
	viz_entry -.-> responder;
	viz_entry -.-> viz_extract;
	viz_extract --> responder;
	writer_entry --> planner;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

条件边语义（`agents/graph.py`）：

- `router → *`：chitchat 直达 responder；其余按 INTENT_AGENT 路由表选 Agent 入口（UI 可 `forced_agent` 强制指定）；
- `compare_entry / viz_entry`：入口 Guardrail——compare 单篇拒绝 → responder；viz 无表拒绝 → responder；viz 正常 → 专用管线 `viz_extract`（不查 chunk，只走 DocIR 表格）；
- `verifier → *`：证据充分 → `align`（compare）/`graph_build`（graph）/`responder`；证据不足且恢复预算未尽 → `recover`；预算尽 → compare/graph 仍过产物节点出部分产物（缺额如实呈现）；
- `recover → *`：策略①改写检索面重检索（→ executor，≤2 次）/ 策略③注入纠错反馈定向重生成（→ responder，格式违规 ≤2 次）/ 预算尽 → END；全局 `MAX_RECOVER=3`；
- `responder → recover`：收尾发现 `pending_failure`（如格式无效）且预算未尽 → 回 recover 定向重生成。

## 子图（Executor：Send 并行扇出）

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	executor_entry(executor_entry)
	retrieve_task(retrieve_task)
	executor_fuse(executor_fuse)
	__end__([<p>__end__</p>]):::last
	__start__ --> executor_entry;
	executor_entry -.-> executor_fuse;
	executor_entry -.-> retrieve_task;
	retrieve_task --> executor_fuse;
	executor_fuse --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

- `executor_entry` 按（检索面 × 论文）经 LangGraph `Send` 扇出 N 路 `retrieve_task`（每路独立跑 `rag.indexing.hybrid_search`），`executor_fuse` 做 RRF(k=60) 跨分支融合 + compare 每（维度,论文）组保底 + bge-reranker 全局精排（未配 key 降级融合排序并记 guardrail）；
- **样板块降级**（Round6-P1，`retrieve_task`）：版权/许可类首页样板（`is_boilerplate_hit`，模式只收许可证措辞）移到分支命中列表尾部——只重排不删除，无更好候选时仍可入池；RRF 按位次给分，样板块不再挤进 Evidence Pack 头部。**align 格选让位**：同格存在非样板候选时样板让位（全格皆样板则保留原首选，诚实降级优于空格）——修复纯中文维度下版权块冒充格命中（weak_match 对无拉丁特征词的维度失效，金标 Q1 实证）；
- 子图经 `executor_node` wrapper 手动 invoke：只回传 evidence/branch_stats/trace/failures/guardrails；configurable 白名单只透传 queue（防父图 checkpointer 泄入无 thread_id 的子图）。

## 节点清单（`build_graph()` 实测，2026-11-09）

14 个功能节点（另有 `__start__`/`__end__` 连接子）：

| 节点 | 职责 |
|---|---|
| router | 意图识别 + Agent 分发（LLM 分类/规则兜底/UI 强制；入口前空问题 400 拦截） |
| planner | 检索面规划（LLM 改写英文子查询/规则；扇出护栏 ≤12；compare 题逐维度拆面 hint） |
| reader_entry / compare_entry / graph_entry / writer_entry / viz_entry | 五专属入口 Guardrail（范围锁定/单篇拒绝/无表拒绝） |
| executor | retrieve_task 并行检索（子图 Send 扇出，≤12 路；样板块降级）→ RRF 融合 → rerank |
| align | Compare 维度对齐表（格级 E#、行级冲突启发、样板让位） |
| viz_extract / graph_build | Viz 表格提取→chart_spec / Graph 引用构图+指标 |
| verifier | Claim-Citation 校验（L1-L4；缺失面/论文覆盖不足 → recover） |
| recover | 三策略恢复（①重检索 ②降级 ③定向重生成；全局预算 ≤3） |
| responder | 流式生成 + [E#] 后验 + ClaimTrace（support 级块锚点精化） |

**LLM 调用底座**（Round6-P1）：`llm.chat` 自带瞬时故障重试——429 限流 / 5xx / 网络异常退避 1s/3s 重试 2 次（4xx 语义错误不重试），并发问答下 planner/router 等结构化前置调用不再因单次 429 掉进规则兜底（那会把检索面退化为中文原句，BM25 全盲）；重试可审计（`qa_logs.guardrail=llm_retry:n`）。

**ClaimTrace 四态与 judge 状态机**（Round6-P1 校准后）：

- `supported`（绿）/ `needs_check`（黄：数值或引文待人工核对）/ `unsupported`（红）/ `absent`（诚实缺失声明，「未检索到/未涉及」类，不计失败）；
- L4 judge 提示词**数值/实体严格化**：数值、单位、型号、专有名词必须与证据一致（等价表示如千分位/单位换算/四舍五入算一致；语义相似但数字对不上判 no）——校准后构造真值双 κ：judge-only 0.856 / 全管线 0.928（n=28，bootstrap 95%CI [0.632,1.0] / [0.774,1.0]）；
- verdict 全量落 `semantic_status`（yes/partial/no/unchecked 观测口径）；**状态机不越级**：L2/L3 机械黄标（needs_check）不因 L4 语义 yes 被洗绿——「数值不在证据里」是机械事实，语义可推出不能清除。

## 断点续跑

- `agents/checkpointer.py` 提供 SqliteSaver（`data/checkpoints.db`），thread_id = `"{conversation_id}#{turn}"`（每轮独立，防累加键跨轮污染）；
- 图级异常时线程标记 failed，`resume_stream()` 以 `invoke(None)` 从最近 checkpoint 续跑——已完成节点不重跑，累加键（trace/raw_hits 等）从 checkpoint 恢复不重复；
- 启动时 `recovery.start_recovery_worker()` 自动派发上次崩溃/失败遗留的未完成线程续跑（`MAX_RESUME_ATTEMPTS=3` 防死循环）；
- 事件流经 `_TeeQueue` 记账落盘 `trace_events`（thread_id 即 trace_id），`GET /api/qa/{trace_id}` 完整回放（首次+续跑合并历史）。
