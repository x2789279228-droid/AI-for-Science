# 2026-11-15 修复轮验收清单（W1–W10）

> 代码基线：`main @ 77c8ff3` + 本轮工作区修复（未提交）。数字由 `scripts/write_evidence.py`
> 从本目录日志生成，`/judge`、`/health` 与 README 同源引用。

## 一、门禁（全部实跑，日志在本目录）

| 门禁 | 结果 | 日志 |
|---|---|---|
| ruff（app + tests） | **0 error** | `ruff.log` |
| pytest（全量） | **342 passed / 0 failed**（101.01s；含健康面板新口径 8 例） | `pytest.log` |
| tsc --noEmit | **exit 0** | `web_tsc.log` |
| next lint | **exit 0**（仅 1 条既有 warning：DynamicIsland.tsx useEffect deps） | `web_lint.log` |
| next build | **成功**，13 路由全生成 | `web_build.log` |
| Playwright e2e | **19 passed / 0 failed**（最终 55.9s；LLM 关闭亦可跑） | `playwright.log` |
| 官方 3 用例（真素材） | **3/3 PASS** | `official_cases.log` |

## 二、官方用例原始结果

- **3.1**：v7/v1 均 done · 去重 `score=0.93` · breakdown=True · 保留建议=True ·
  keep/coexist/cover 三态全 200 · diff done（22 章节 / 30 段落 / **618 数值** / 摘要 2 条）
- **3.2a**：`engine=mineru` · `scanned=True` · **coverage=0.933** · 公式 6 · 表格 4 · refs 33
- **3.2b**：191s · `engine=pymupdf` · toc 181 · 页数 537 · 峰值内存 706.8MB（<2GB）·
  索引 401 chunks · 最大 chunk 页 116（<120）

## 三、本轮修复专项验收（代码级）

| 项 | 验收证据 |
|---|---|
| MinHash bottom-k | 真实 v7↔v1：真实 5-gram J=0.637 → 签名 **0.570**（修复前 0.086）；官方 3.1 去重分 0.93 |
| `supersedes` 方向 | 新增 2 例单测（先 v7 后 v1 / 反向）全过；官方 3.1 三态决策不回归 |
| betweenness | `_graph_metrics` 实测 20 节点/52 边，**11/20 节点介数非 0**（修复前 0/20） |
| 图页含金量 chip | `insights.class_stat` 双写 + 前端回退；e2e ⑬ 断言可见 |
| 引用含金量 | BERT 39 mentions / GPT-3 120 / RoBERTa 58（修复前均 0）；全库覆盖 1078/2424=44.5%（基线 12%） |
| Future Work | 年份修复（Attention 2017 / GPT-3 2020 / UDL 2023）；尾章噪声 0 命中；中文 idea 去重生效；外部链 S2→OpenAlex→arXiv；规则初判标 `verdict_source=rule` |
| 阅读器 | e2e ⑯ TOC 跨页定位 / ⑰ `[n,m]` 分组引用双向 / ⑱ 大纲失败重试 / ⑲ TextLayer `--scale-factor` 全过 |
| Copilot | 白名单按篇配额（每篇≤20/总≤60/保底5）；radar 轴语义修正 + radar3d 透视；bar 万级降采样；拓扑 LLM 离线规则降级（e2e ⑩ 通过） |
| 演示快照 | 清理 Smoke Test/重复 Attention；v7 重解析（125 blocks/26 heading/5 公式/toc 22 带 id）；v7/v1 作者 8 人、摘要 1136、年份 2017；data-real 20 篇（v1 不入快照，保留官方去重演示口径） |
| 健康面板（完整重写） | 7 固定权重运行时子分（可靠性/回答可信/回答覆盖/检索/解析/性能/数据卫生）+ 工程快照独立；窗口 7 天、只统计真实用户轨迹（排除 kappa_/gold_/bench_/e2e_，n=70）；失败 trace/任务进可靠性分母；缺测按 0 计不重归一化；性能改 p50/p95 SLO；最新 eval gates 16/17 → 总分**封顶 90.0**（原面板 99.05，现运行时 90.34→封顶 90.0），每卡带 n/Wilson CI/公式；e2e ⑮ 同步断言 8 卡+生产窗口+外部校准；视觉快照 `health-panel-2026-11-15.png` |
| 数字治理 | 页面数字唯一来源 `web/lib/evidence.ts`（脚本生成）；README 286/14/16-17/22篇/2371 等过期口径已勘误 |

## 四、复跑方式

```bash
# 后端
cd api
uv run ruff check app tests
uv run pytest -q
uv run python scripts/run_official_cases.py
# 前端
cd ../web
pnpm exec tsc --noEmit && pnpm exec next lint && pnpm build
pnpm e2e            # 需本机可起 api:8100 + next dev:3005（LLM 可关闭）
# 证据再生成（覆盖 web/lib/evidence.ts 与本目录 summary.md）
cd ../api && uv run python scripts/write_evidence.py
# 演示快照清理/重解析（可选）
uv run python scripts/clean_snapshot.py
```

## 五、已知残留（如实）

- `DynamicIsland.tsx` 1 条既有 react-hooks warning（非阻断，未在本轮范围）。
- 引用含金量 coverage 受 references 解析质量限制：UDL 0.286 / Megatron 0.178 偏低（refs 缺 authors/year 的旧数据），未在本轮扩到解析层。
- Future Work 外部证据的真实网络冒烟未跑（避免外部副作用），规则/LLM 分支由单测覆盖；`partial` 为低置信规则初判，答辩不作强结论。
- 演示快照备份保留在 `data/_backup_20261115-*`（gitignore，提交前可删）。
