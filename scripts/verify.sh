#!/usr/bin/env bash
# scripts/verify.sh — 一键自动验证门禁（Linux/macOS/CI 用；Windows 用 scripts/verify.ps1）
#
# 用法：
#   bash scripts/verify.sh          # 标准门禁：ruff + pytest + tsc
#   bash scripts/verify.sh --full   # 追加前端生产构建
set -u
cd "$(dirname "$0")/.."

FAILED=0
TOTAL0=$SECONDS

run() {
  local name="$1" dir="$2"; shift 2
  echo
  echo "==> [$name] $*  (cwd=$dir)"
  local t0=$SECONDS
  if ( cd "$dir" && "$@" ); then
    echo "<== [$name] PASS  ($((SECONDS - t0))s)"
  else
    echo "<== [$name] FAIL  ($((SECONDS - t0))s)"
    FAILED=$((FAILED + 1))
  fi
}

run "api/ruff 静态检查"   api uv run ruff check app/ tests/
run "api/pytest 单元测试" api uv run pytest -q
run "web/tsc 类型检查"    web npx tsc --noEmit
if [ "${1:-}" = "--full" ]; then
  run "web/build 生产构建" web pnpm build
fi

echo
echo "==== 验证结果汇总 ===="
echo "失败步骤数: $FAILED  ·  总耗时 $((SECONDS - TOTAL0))s"
if [ "$FAILED" -gt 0 ]; then
  echo "验证未通过"; exit 1
fi
echo "ALL GREEN ✔ 全部门禁通过"
