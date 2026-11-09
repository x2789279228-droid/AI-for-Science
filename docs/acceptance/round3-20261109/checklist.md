# Round3 验收清单（模块三~六冲刺 + 评审意见修复）· 2026-11-09

## 自动化门禁

- [x] 1. 后端全量 pytest —— **241 passed / 0 failed**（pytest.log；本轮 +45：Phase 0 references 三通道切分 / zh2en / PDF 导出 / 多源检索 / 追踪 / 推荐 / 综述 / 外部核查 / Copilot 白名单 / CSV 护栏 / 拓扑图 / scatter3d / 大文件回归）
- [x] 2. ruff —— All checks passed
- [x] 3. web tsc + next build —— 0 error、11/11 页
- [x] 4. **Playwright UI 验收 6/6 passed**（playwright.log / playwright.last-run.json）
  - ① 双语对照滚动同步（左栏滚动 → 右栏跟随；依赖本轮修复的 pane 高度约束 bug）
  - ② [n] 引用标记 → References 跳转
  - ③ Copilot CSV → ECharts canvas + Figure Caption（数字回溯 0.92）
  - ④ Discover 多源搜索（arXiv live——依赖本轮修复的 301 重定向 bug）
  - ⑤ Graph G6 渲染节点 > 0
  - ⑥ PDF↔译文联动：点译文块 → bbox 红框锚定（本轮新增）

## 金标抗幻觉评测（eval_gold.log / eval_gold.json；门禁 Round3 收紧）

- [x] 13 项门禁：**12 PASS / 1 FAIL（如实呈现）**
  - 新进门禁：`strict_recall_at5>=0.7`（token 边界严格口径——旧口径只进报告不进门禁，Round3 应评审意见收紧）
  - 新进门禁：compare 细分（cell_coverage / eid_coverage / unsupported_ratio——旧口径只有一个端到端布尔）
- [ ] `strict_recall_at5 = 0.6`（FAIL，10 题严格口径命中 6）——构成分析：
  - 真检索缺失 ×2：「constant memory 注意力」（金标词 length）与「d_model=512」两题 top-5 未召回证据块；
  - token 边界假阴性 ×2：「warmup」命中于 "warmup_steps" 复合词、「6」命中于小数上下文——
    检索排序正确（r1 命中），是金标词与严格边界的口径产物，非检索失败。
  - 修复方向（下轮）：金标词改多关键词集合 / strict 匹配允许词元前缀；检索侧对 d_model 表格块加权。
- [x] anchor_precision = **0.75**（≥0.7 门禁，8 claims；评审所引 0.71 为 round2 旧值，本轮已提升）
- [x] rubric8 官方考点 8/8 ✓（含 [11] 引用解析、β1β2、GPU、BLEU、论证链）
- [x] compare：coverage 1.0 · eid 1.0 · unsupported 0.07 · P1P2 对称 ✓

## 评审意见 8 项核实与处置

| # | 指控 | 核实 | 处置 |
|---|---|---|---|
| 1 | 43+ 文件未提交、CI 看不到 | **属实**（73 处变更未 commit） | 本轮分类提交并推送（见提交链） |
| 2 | 验收证据停留在 11-08 | **属实** | 本目录 round3：pytest/eval/playwright 全套新证据 |
| 3 | 引用含金量未闭环 | **部分属实**（阅读器面板已有过滤；下钻区确实缺失） | discover 参考文献下钻新增「从库内论文导入」：实质/背景/凑数徽章 + 可逆过滤 + 逐条下载 |
| 4 | "3D 雷达"名不副实 | **属实** | 雷达改名「雷达（多维）」；新增 **scatter3d 真 3D 散点**（echarts-gl grid3D，≥3 数值维 + 第 4 维 visualMap，可旋转） |
| 5 | CSV 大文件截断产生非法 JSON | **属实**（字节切片烂 JSON） | 改为按行预算截断 + storage_truncated 如实上报 + 5 万行回归测试 |
| 6 | 抗幻觉指标未刷新/门禁偏宽 | **属实** | eval 重跑出当前真值（anchor_precision 0.75）；strict R@5 与 compare 细分进门禁（1 项如实 FAIL） |
| 7 | PDF 双语联动无 E2E | **属实** | 新增用例 ⑥（点块 → bbox 红框），6/6 全过 |
| 8 | 语料标签 GPT2 错误 | **属实且更糟**（2001.08361 注释还误写 Few-Shot Learners） | fetch_corpus 勘误为 ScalingLaws；补真 GPT-3 论文 2005.14165（75 页 MinerU 解析，references 118 条——顺带修了标题粘连 bug 后回填）；GPT-2 原文无 arXiv 版如实注明 |

## data-real 快照（6 篇，references 全部健康）

| 论文 | refs | 备注 |
|---|---|---|
| Attention Is All You Need | 40 | 演示件 |
| BERT | 56 | 三通道切分（原 1 脏块） |
| Language Models are Few-Shot Learners (GPT-3) | 118 | 本轮新增 |
| PreLN | 101 | 三通道切分（原 1） |
| Scaling Laws | 43 | 字母键+作者头切分（原 1） |
| Understanding Deep Learning | 245 | PyMuPDF（原 1） |

## 过程修复的存量 Bug（均有测试或 e2e 锚定）

1. references 三类脏块根因（行首 [n] / author-year 空行 / LaTeX 化字母键 + 标题粘连）
2. TMPDIR=/tmp 泄漏 → SQLite/FTS 临时文件失败（temp_store=MEMORY）
3. arXiv API 301 未跟随重定向（检索全挂）
4. 双语 pane 无高度约束 → 内部滚动从未发生（滚动同步失效）
5. CSV 存储字节切片烂 JSON
6. References 标题与首条目粘连（MinerU）→ 引擎改用 _find_refs_start
