# Meridian Agent 状态图（代码自动生成）

> 本图由 `build_graph().get_graph().draw_mermaid()` 自动生成——架构图即代码，节点/边改动后重跑该命令即可同步。评审叙事：工作流编排（LangGraph 显式状态机）+ 节点即卡片执行可视化。

## 主图（Intent Router → Planner → Executor → Verifier → Responder）

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	router(router)
	planner(planner)
	executor(executor)
	verifier(verifier)
	responder(responder)
	__end__([<p>__end__</p>]):::last
	__start__ --> router;
	executor --> verifier;
	planner --> executor;
	router -.-> planner;
	router -.-> responder;
	verifier -.-> planner;
	verifier -.-> responder;
	responder --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

## 子图展开（Executor：Send 并行扇出，xray=True）

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	router(router)
	planner(planner)
	verifier(verifier)
	responder(responder)
	__end__([<p>__end__</p>]):::last
	__start__ --> router;
	executor\3aexecutor_fuse --> verifier;
	planner --> executor\3aexecutor_entry;
	router -.-> planner;
	router -.-> responder;
	verifier -.-> planner;
	verifier -.-> responder;
	responder --> __end__;
	subgraph executor
	executor\3aexecutor_entry(executor_entry)
	executor\3aretrieve_task(retrieve_task)
	executor\3aexecutor_fuse(executor_fuse)
	executor\3aexecutor_entry -.-> executor\3aexecutor_fuse;
	executor\3aexecutor_entry -.-> executor\3aretrieve_task;
	executor\3aretrieve_task --> executor\3aexecutor_fuse;
	end
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```


## 节点清单（`build_graph().get_graph()` 实测，2026-11-08）

14 个功能节点（另有 `__start__`/`__end__` 连接子）：

| 节点 | 职责 |
|---|---|
| router | 意图识别 + Agent 分发（LLM 分类/规则兜底/UI 强制） |
| planner | 检索面规划（LLM 改写英文子查询/规则；扇出护栏 ≤12） |
| reader_entry / compare_entry / graph_entry / writer_entry / viz_entry | 五专属入口 Guardrail |
| executor | retrieve_task 并行检索（Send 扇出，≤12 路） |
| align | Compare 维度对齐表 |
| viz_extract / graph_build | Viz 表格提取→chart_spec / Graph 引用构图+指标 |
| verifier | Claim-Citation 校验（缺失面 → recover） |
| recover | 三策略恢复（①重检索 ②降级 ③定向重生成；预算 ≤3） |
| responder | 流式生成 + [E#] 后验 + ClaimTrace |
