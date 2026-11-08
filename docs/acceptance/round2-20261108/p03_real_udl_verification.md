# P0-3 修复 · 真实 UDL DocIR 验证（2026-11-08）

- 语料：官方 541 页书 UnderstandingDeepLearning（本轮真素材用例进程落盘的 DocIR：
  blocks=11270 / toc=181 / max_block_page=540）
- 旧视图（blocks/tables 过滤、无 toc 过滤）：chunk 最大页 = 387
  ← 与本轮真素材 3.2b FAIL 报告的 387 完全一致（根因实锤）
- 新 _index_view（blocks/tables/**toc** 统一截断 cap=120）：chunk 最大页 = 116（<120）✓
  chunks=399 · toc 过滤后 49/181
- 结论：P0-3 修复对真实超长书生效；3.2b 的「chunk 最大页<120」断言在新代码下必过。
  （真素材用例进程启动于修复提交之前，故其 FAIL 属修复前基线，非当前代码缺陷。）
