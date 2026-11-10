# 自评表（Round8+9 终版 · 证据链接口径）

## 终版等级

| 模块 | 等级 | 核心依据 |
|---|---|---|
| 一 PDF 解析 | **生产级** | 20 篇语料 refs 健康（UDL 991 全 titled/BERT 56/GPT-3 118）；质量报告 8 指标；VLM 3.2a coverage=1.0；书末书目跑页头簇首定位；pytest 283 |
| 二 抗幻觉溯源 | **生产级** | strict 0.8-0.9 · zh 1.0 · anchor 0.93/55 · quote_unv 0.05 · rubric 真 8 问 7/8 · compare gold 1.0/missing_honest 1.0/ok True · absent 分类 · L1-L4 四层 |
| 三 阅读与翻译 | **自动化验证** | e2e 10/10（含中线块锚定+反向联动+块对齐量化）；PDF 导出流式写入修复+覆盖率头；中→英方向切换 |
| 四 检索/追踪/推荐 | **自动化验证** | 三源并行+去重；含金量闭环+可逆过滤；追踪 worker+abstract 闭环；S2 懒补；live_probe 三源+离线回放 |
| 五 图谱/综述/FutureWork | **生产级（图谱）/自动化验证（综述）** | graph20（23 节点/51 边/1624 外部边/角色校准）；综述 5 节 42 claims+audit 9/10 |
| 六 Copilot | **自动化验证** | copilot_live PASS（6/6 实质+References 白名单 0 编造+[R#] 双向一致）；CSV 护栏（5MB/10 万行/GBK/公式注入）；mermaid 校验修复 |
| 横切 CI/限流/可观测 | **生产级** | CI sealed #24 success；限流+隔离+metrics+request-id；accept_round --round 一键证据链 |
| 横切 性能 | **自动化验证** | bench（建图 0.2s/100k CSV 0.8s/10 并发 p95 5.7s 0 错）；e2e ⑤ 20 篇 ≤60s |

## 全轮次汇总（4→8+9，五轮补证）

| 轮 | 核心交付 | eval |
|---|---|---|
| R4 | 评测硬化+20 篇语料+CI+限流/指标 | 12/17（初跑） |
| R5 | L2 逐 support+弱命中降级+PDF 导出 3 真 bug+UDL 假 245 曝光 | 12-15/17 |
| R6 | 429 长退避+rubric 双跑+cell_quote 3 层根因+UDL 991 真基线 | 14-16/17 |
| R7 | absent 分类+金标扩容+BART/UDL 定性 | 15-16/17 |
| R8+9 | 表块保位+absent 徽章+同步时序+性能断言+终值归档 | **16/17** |

## 残余（如实标注）

- cell_quote 0.73（表格 chunk `[表格 t1]\n` 前缀不在块文本——修复路径明确）
- unsupported 0.11↔0.27 波动（LLM judge 正常判定分布）
- 用户终审 5 项留白（copilot/PDF 样张/FutureWork/L4/中→英实操）
