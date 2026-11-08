# 科研助手 Agent · 一键入口（Windows 本地开发 + docker 两用）
# demo 数据快照：data-real/（v7 + UDL 已解析语料）；make demo 恢复后直接可演示。

SHELL := /bin/bash

.PHONY: install api web dev demo test lint cases smoke clean

install:            ## 安装后端 + 前端依赖
	cd api && uv sync
	cd web && pnpm install

api:                ## 本地起 API（uvicorn :8100）
	cd api && uv run uvicorn app.main:app --port 8100 --reload

web:                ## 本地起前端（:3000）
	cd web && pnpm dev

demo:               ## 演示环境：恢复 data-real 快照（预置 v7/UDL 已解析语料）+ docker 起全栈
	@if [ -d data-real ] && [ ! -f data/.demo_loaded ]; then mkdir -p data && cp -r data-real/. data/ && touch data/.demo_loaded; echo '已恢复 data-real 演示快照'; fi
	docker compose up --build -d
	@echo "→ http://localhost:3000  （API http://localhost:8100/api/health）"

test:               ## 全量门禁：后端 ruff+pytest、前端 tsc+build
	cd api && uv run ruff check app/ tests/ && uv run pytest -q
	cd web && npx tsc --noEmit && pnpm build

lint:               ## 仅静态检查
	cd api && uv run ruff check app/ tests/
	cd web && npx tsc --noEmit && pnpm lint

cases:              ## 官方 3 用例断言式验收（进程内，无需起服务）
	cd api && uv run python scripts/run_official_cases.py

smoke:              ## 真 HTTP 冒烟（先 make api 或 compose 起栈）
	cd api && uv run python scripts/smoke_web.py

clean:              ## 清理本地运行数据（保留 data-real 快照）
	rm -rf api/data api/tests/_p*_*/
	docker compose down -v 2>/dev/null || true

