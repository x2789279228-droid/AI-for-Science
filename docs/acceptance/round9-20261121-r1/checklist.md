# Round9-R1 验收清单 —— 模块七 R1「数据与时间基础层」

> 日期：2026-11-21 ｜ 依据：`docs/模块七-改造清单.md`（v2.1）里程碑 P0 拆轮 · W1 前半
> 范围：三表迁移 + memory 包骨架（store CRUD/FSM）+ timeparse + 单测（全离线零 LLM/零网络）

## 交付物

| # | 项 | 文件 | 状态 |
|---|---|---|---|
| 1 | memory_events / entities / memory_edges 三表（SCHEMA 内建，`CREATE TABLE IF NOT EXISTS` 即幂等迁移，沿用 db.py 既有模式；无需 ALTER 式 `_migrate_*`，因均为新表） | `api/app/db.py` | ✅ |
| 2 | timeparse：中英文时间表达式 → UTC 半开区间 [t_start, t_end)；固定 +08:00（Asia/Shanghai 无 DST，免 tzdata 依赖）；周一切割；日历季；粒度阶梯（天/ISO周/日历月/日历年）；滚动窗月为日历减法 + 月末钳制 | `api/app/memory/timeparse.py` | ✅ |
| 3 | store：三表 CRUD · 假设 FSM（`HYPOTHESIS_TRANSITIONS` 合法转移表 + `FSMError` + transitions[] append-only 审计）· 边 UPSERT（confirm_count + 最早 t_valid_from）· supports↔refutes 翻转标 superseded 保留演化史 · 实体规则消解 · supersede 幂等 · touch_access | `api/app/memory/store.py`、`api/app/memory/__init__.py` | ✅ |
| 4 | 新增单测 70 例：timeparse 48 组参数化（中英文）+ 6 结构性 + store 16 | `api/tests/test_memory_timeparse.py`、`api/tests/test_memory_store.py` | ✅ |

## 验收证据

- **全量 pytest：419 passed + 1 skipped，0 failed**（138.52s），见 `pytest.log`。
  - 存量基线：350 例（其中 1 skip）全部通过，零回归；新增 70 例全过。
  - 该 skip 为存量环境条件跳过（`test_m1_dedup_semantic.py:182 真实 UDL v1/v7 DocIR 不在本机`），与本次改动无关。
- **ruff**：`app/memory/` + 两个测试文件 + `app/db.py` 30 处风格问题已 `--fix` 归零（All checks passed）。

## 实现期决策记录（与清单的偏差，均为具体化而非方向变更）

1. **memory_edges 增加 `id TEXT PRIMARY KEY`**：清单 §五 表结构未含 id；实现需要稳定行句柄（UPSERT 复活分支、前端边操作），补主键列。
2. **memory_events 增加 `meta_json`**：清单 §2.2 说 FSM 转移历史记 `evidence_json.transitions[]`，但 §五 事件表无 evidence_json 列——落地为 `meta_json`（transitions[] / E# 映射 / corrected_from 共用）。
3. **表入 db.py SCHEMA 而非单独迁移函数**：新表由 `executescript(SCHEMA)` 幂等创建（与 version_diffs 等 8 张表同模式），无旧库列变更故不需要 `_migrate_*`。
4. **时区实现**：固定 `timezone(timedelta(hours=8))` 而非 zoneinfo——Asia/Shanghai 1988 年后无夏令时，固定偏移精确且 Windows 免 tzdata 安装。

## 与验收标准的对应（清单 §七.3）

- 「时间表达式解析单测 ≥20 组全过（中英文，含时区边界：周一切割 / 季度边界）」——**已超额**：48 组参数化全过，含周一切割（`test_week_start_is_monday`）、UTC 换算（`test_utc_epoch_conversion`）、季度边界、月末钳制（`test_month_end_clamp`）。

## 边界声明（R1 不含）

extract / graph / retrieve / briefing / worker / routes / 前端均未触碰——按轮次推进，见 R2+。
