# Round2 验收报告

- 时间：2026-11-08 10:43:44
- 仓库：`D:\传智杯\scholar-agent`
- 命令：`scripts/accept_round.py --full --strict-git --min-tests 116`

## 结果

| 步骤 | 结果 | 说明 |
|---|---|---|
| git 工作区 | **FAIL** | 1 项未提交（--strict-git 时为 FAIL） |
| git 已推送 | **PASS** | 与 origin/main 同步 |
| HEAD 有验收 tag | **SKIP** | 未打 tag（建议验收后 git tag v0.5.0） |
| Dockerfile 含 uv.lock | **PASS** | ok |
| .gitignore 覆盖测试临时目录 | **PASS** | _prs_* |
| .env 未被跟踪 | **PASS** | ok |
| ruff 静态检查 | **PASS** | All checks passed |
| pytest 全量 | **PASS** | 117 passed / 0 failed（下限 116） |
| P0 回归测试 | **PASS** | 8 条（下限 7）· test_p0_regressions.py:8 |
| 官方 3 用例（容器等价：引擎关） | **PASS** | 汇总：3/3 PASS |
| web tsc --noEmit | **PASS** |  |
| web next lint | **PASS** |  |
| web next build | **PASS** |  |
| docker build api | **PASS** |  |
| docker build web | **PASS** |  |
| 真 HTTP smoke（9 项） | **PASS** | 冒烟：9/9 PASS |

**总计：14 PASS / 1 FAIL / 1 SKIP**
