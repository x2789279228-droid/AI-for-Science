# 自评表（Round4 · 证据链接口径）

> 说明：仓库中**不存在**任何「HTML 65 行自评/32 生产级」文件（`生产级` 全库 0 命中，
> 评审所指文件不在本仓库）。本表为唯一自评口径：等级只能引用下方证据产物，
> 三个等级定义——**功能完整**（端到端可用）＞**自动化验证**（有测试/门禁锚定）＞
> **生产级**（有真实数据/负载/失败注入证据）。不许无证据升级。

## 等级总览

| 模块 | 等级 | 证据 | 未达生产级的如实差距 |
|---|---|---|---|
| 一 PDF 解析（多路由/References 三通道/质量报告） | **生产级** | data-real 20 篇 refs 健康（checklist §7）；quality 六+二指标；VLM 3.2a coverage=1.0（ocr_vlm.log）；pytest 25 条解析测试 | Reformer/BART refs 切分保守（24/34 条）；UDL 541 页全书重解析 >1h（工程时长而非正确性） |
| 二 抗幻觉溯源问答（四层防御/ClaimTrace/拒答） | **自动化验证** | eval_gold 17 门禁 12 PASS（strict **0.9**、anchor **0.93/43**、rubric 真 8 问 6/8、replay 10/10）；FAIL 5 项根因见 checklist §二 | compare 对弱检索命中的诚实缺失声明不足（missing_honest=0 本轮实测）；L4 抽检仅 6 条人工表 |
| 三 阅读与双向翻译（五视图/双向方向/PDF 导出） | **自动化验证** | e2e ①②⑥⑦⑨（滚动同步量化 ≤1 块、反向锚定）；pdf_export_check（图片/矢量全保留+12 PNG 样张）；pytest 翻译 24 条 | 导出 PNG 仅人工目检未做像素级 diff；overlay 长段换行质量依赖自动字号收缩，无人工评分 |
| 四 检索/追踪/推荐（三源/含金量/追踪/推荐） | **自动化验证** | e2e ④⑧；live_probe（429/超时/断网回放实测）；追踪 worker 真实刷新记录；推荐懒补闭环测试 | S2 共享池限速常态（live_probe 如实记录）；推荐 github 分量依赖无 token 限流 |
| 五 图谱/综述/Future Work | **生产级（图谱）/自动化验证（综述）** | graph20（23 节点/51 边/1624 外部边/角色校准 8-1-14）；综述 5 节 42 claims；audit_sampling 抽检；扰动稳定性测试 | 综述 claims [E#] 支撑抽检通过率见 audit_sampling.json（首轮 0.7→口径修正后重跑）；Future Work 四态人工边界 10 条待填 |
| 六 写作与可视化 Copilot | **自动化验证** | copilot_live（白名单 0 编造/[R#] 双向一致/章节完整自动断言+人工清单）；CSV 100k/GBK/NaN 测试；mermaid 修复回归；e2e ③⑩ | 真实 LLM 写作仅 1 idea×3 论文样本；caption 数字回溯是规则校验非人工质检 |
| 横切：限流/隔离/可观测/安全 | **自动化验证** | 8 条生产化测试（429/回填/隔离前后对照/魔数/穿越）；/api/metrics 端点；request-id | 单机单进程口径（非分布式限流/多租户鉴权，README 如实注明）；无长时压测（bench 为 10 并发瞬时） |
| 横切：性能 | **自动化验证** | bench.json（建图 0.2s/100k CSV 0.8s/10 并发 p95 5.7s 0 错） | LLM on 的端到端时延随配额波动（qa_logs avg_latency_ms 可查），未设 SLA |
| 横切：CI | **生产级** | acceptance.yml sealed；run #15 success；`accept_round --round` 一键证据链（本目录全部产物） | — |

## 逐门禁引用（数值以产物为准，不手抄防漂移）

- eval：`eval_gold.json → gates`（17 项，12 PASS / 5 FAIL 如实）
- e2e：`playwright.log`（10 用例）
- 基准：`bench.json` · 外部源：`live_probe.json` · 图谱：`graph20.json`
- OCR：`ocr_vlm.log`（3.2a VLM coverage=1.0）+ `ocr_real.log`（3.2b 超时如实）
- 抽检：`audit_sampling.json` · `copilot_live/framework.json` · `pdf_export_check.json`
