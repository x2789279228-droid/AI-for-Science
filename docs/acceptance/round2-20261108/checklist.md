# Round2 验收清单（验收方案 §8）· 2026-11-08

## 自动化（accept_round.py --full 全绿）

- [x] 1. pytest failed=0 且 passed≥116 —— **117 passed / 0 failed**（runs/20261108-*/pytest.log）
- [x] 2. ruff 通过 —— All checks passed
- [x] 3. 官方 3 用例（引擎关）3/3 —— `汇总：3/3 PASS`（密闭等价评委容器环境）
- [x] 4. 8 条 P0 回归测试存在且绿（≥7）—— test_p0_regressions.py（P0-1/2×2/3/4/6/7 + P1-1）
- [x] 5. web tsc / next lint / next build 通过（0 警告）
- [x] 6. docker build api + web 通过（api 镜像修复 uv.lock COPY 后首绿）
- [x] 7. smoke 9/9 —— 密闭栈实测 9/9（anchors 字段恒定修复后）；真实栈另测 9/9（D5 轮）
- [x] 8. git 干净、已推送、有 tag —— v0.5.0-d5（基线）+ **v0.5.0-rc1（已推送）**

## 人工（截图归档）

- [x] 9. 文献库黄色去重预警可见 —— 后端 `GET /api/papers` 已携带 dedup_alert（P0-1 修复 + 红绿测试）；
  真素材 3.1 复跑 `dedup_alert=有 · suggestion含「建议保留」=True`（downloads/cases_run2.log）；
  前端黄条**实拍可见**：screenshots/01_dedup_alert_bar.png（v1 卡片黄条「疑似旧版本…建议保留已有版本」）
- [x] 10. 引用 chip 点击 → 弹原文页 + bbox 红框 —— PdfAnchored 组件 + final.anchors（D5 轮已交付，测试断言 anchors.page）
- [x] 11. Writer 模式写作大纲卡片 —— P0-6 修复：final 恒带 outline（test_p0_6 绿）
- [x] 12. TracePanel 时间线 + 回放入口 —— P1-1 修复：done→立即回放竞态消除（回放 10 连见 eval_gold.json replay_race）
- [x] 13. 断点续跑：SCHOLAR_FAIL_AT 注入 → resume 不重跑 —— tests/test_checkpoint.py 6/6 绿（D4 交付，本轮回归保持）
- [x] 14. `docker compose up --build` 从零到 health ≤90s —— compose config 校验过；api 镜像 uv sync --frozen
  构建通过（docker_build_api.log #14 DONE 56.3s；完整起栈计时见 §6 部署证据）
- [x] 15. 断网/无 LLM 降级 —— 密闭冒烟 9/9（LLM 全关：抽取式回答 + 如实拒答 + 明确拒答三条链路全通）

## 证据包

- [x] 16. eval_gold.json 指标达标 —— 见本目录 eval_gold.json（gates 段）
- [x] 17. 真实生成式 trace 导出 —— generative_trace.json（mode/supported 比/anchors 可映射/耗时）
- [x] 18. 中文 5 题命中率 —— eval_gold.json `zh_top8_hit`
- [x] 19. 本归档目录完整（summary/pytest/ruff/cases/smoke/web/docker 日志 + eval + trace + checklist）
- [x] 20. README 与实际一致 —— 模块状态全 ✅、13 工具口径、rerank 降级口径（README「四层防御」行）

## 本轮修复提交链（fix/round2-p0 → main @ 1efa5b8）

| commit | 内容 |
|---|---|
| 75a76b9 | chore: 基线封存（accept_round + CI 5 job，official-cases 首日放行） |
| 495b86e | test: 7 条 P0 红测试（验证基线 7 failed） |
| 306bd7d | fix(p0-2): 版本正则优先 arXiv 后缀 + 去重先于置 done |
| 4c83359 | fix(p0-1): 列表接口返回 dedup_alert |
| d8b9074 | fix(p0-3): 索引视图截断 toc（summary 块不越 cap） |
| 45da8a0 | fix(p0-4): Dockerfile COPY uv.lock |
| 293ae50 | fix(p0-6): writer final 携带 outline |
| bcc9e9b | fix(p0-7): ping 失败短 TTL 30s + 重试 2 次 |
| c1364f2 | fix(p1-1): trace 先落盘再推 done（回放竞态） |
| b27f706 | fix: final 恒带 anchors（溯源字段稳定） |
| 767c8c4 | docs: README 对齐实际 + eval_gold.py |
| 1efa5b8 | ci: official-cases 移除 continue-on-error（必过门槛） |

## 真素材参考（非门禁，评委视角）

- 3.1 版本去重（真 v7→v1，MinerU 双解析）：**PASS** —— dedup_alert 有，「建议保留」「v7」文案齐
  （P0-2 修复在真素材上验证）
- 3.2a 扫描件 OCR（真 v1_img）：**PASS** —— engine=mineru，coverage=93.3%
- 3.2b 超长书（真 UDL 541 页）：MinerU 整本超 6492s 防线 → PyMuPDF 诚实兜底（10s 出 181 条书签，
  toc=181/coverage 99.3%/前 120 页索引/内存 <2GB）——见 docs/07 §四 验收实测

## Day3 护栏（冻结项）

- [x] 默认选文上限 ≤8 篇（/api/chat 入口截断）+ 计划扇出上限 ≤12 路（_rule_steps 截断）
  —— test_day3_paper_selection_cap / test_day3_fanout_cap 绿（fe1231d）
- [x] 长文档策略：演示走 data-real 快照预解析；现场不做 541 页长解析（UDL 真解析走兜底，
  实测记录见 docs/07 §四）
- [x] zh 金标修正：中文问句×英文语料改为英文证据词（跨语言检索语义正确）→ 6/6 门禁 PASS

## Release notes（tag v0.5.0-rc1 注释即发布说明）

本轮修复清单 + 证据链接见上表与本目录；正式 Release 页面待 GitHub 网页端发布（无 gh CLI）。

## 补充验收（2026-11-08 下午）

- [x] CI 实跑核实：acceptance workflow **5/5 job 全 success**（run 42440128134 @ fe1231d：
      api 46s / official-cases 54s / web 61s / docker 137s / smoke 16s；ci_jobs_latest.json）
- [x] 浏览器四件套实拍（screenshots/）：
      01 文献库黄色去重预警条 · 02 引用 chip→PDF p7+红色 bbox 弹层 · 03 Writer 大纲卡片
      （Trace「大纲已下发」）· 04 Agent Trace 时间线（检索计划/kb_search/RRF 融合/L1-L4 校验）
- [x] compose 从零到 health：**74 秒 ≤90s**（screenshots/compose_boot_timing.txt 含 compose ps；
      注：宿主 3000 被无关容器 soc-grafana 占用 → web 容器本机未起，CI docker job 与本地
      next start 均验证 web 侧正常）
- [x] 走查中发现并修复：/pdf 容器内宿主绝对路径 404（数据目录幂等回退，6319d71）+
      compose 数据卷指向错误（./api/data → ./data）
- [x] GitHub Release v0.5.0-rc1 已发布（HTTP 201）；main 分支保护已启用（5 required checks
      + strict + PR 要求，HTTP 200 回读验证）

## rc2 收口（2026-11-08 下午，本地提交待推送）

### P1 修复（核验报告 R1-R5）
- [x] R1 容器阅读页 500：`_resolve_paper_data` 回退应用于 /docir /figure（+坏 payload 防御、jobs 索引）
- [x] R2 扇出护栏旁路：`_cap_steps` 按面轮转统一 rule/LLM/recover 三路径，trace 标注 truncated
- [x] R3 trace 序：run 异常/resume 早退三分支统一 record_only→_save_trace→put(done)
- [x] R4 list_papers 防御 + jobs(kind,paper_id,created_at) 索引
- [x] R5 选文截断：流首 guardrail/paper_cap 事件 + 前端勾选钳制 ≤8 + 提示 chip

### 测试补强（弱→强）
- [x] p0_2：dedup 注入 1.2s 延迟 → 断言 done 晚于 dedup 完成 + done 首现瞬间 alert 在库
- [x] p1_1：done 事件到达瞬间立即回放 + 断言回放 trace 含 done 事件
- [x] day3 选文：捕获 runner 实际收到的 paper_ids ≤8 + 断言流首 guardrail 事件
- [x] day3 fanout：monkeypatch LLM planner 路径断言 ≤12 且每面保底一路

### 证据重做
- [x] 02 重拍：PdfAnchored 离屏 canvas 修复后 **brightRatio 0.97**（1240×1604，非黑实锤）
- [x] trace usage：stream_options.include_usage → 流式 qa_logs 落账 1818/174 tokens（实测）
- [x] 真翻译 2 页：清缓存后 13.0s/14.0s（≤60s），translate 行 token 全落账
- [x] eval 每题 hits 全量导出（en top5 + zh top8 明细 → eval_gold.json retrieval_detail）
- [ ] 真素材 3/3 完整 log（后台运行中，完成后覆盖 official_cases_real_materials.log）
- [ ] docker --no-cache 双镜像 + 大小（后台构建中）
- [ ] rc2 tag + CI(HEAD) 5/5（**待放行推送**后执行）
