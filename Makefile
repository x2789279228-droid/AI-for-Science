# 科研助手 Agent · 一键入口（Windows 本地开发 + docker 两用）
# demo 数据快照：data-real/（对比语料 v7/BERT/PreLN/GPT-2 + UDL 已解析产物）；
# 快照由 make corpus 刷新（fetch_corpus.py --snapshot 固化）。

SHELL := /bin/bash

.PHONY: install api web dev demo test lint cases smoke corpus clean

install:            ## 安装后端 + 前端依赖
	cd api && uv sync
	cd web && pnpm install

api:                ## 本地起 API（uvicorn :8100）
	cd api && uv run uvicorn app.main:app --port 8100 --reload

web:                ## 本地起前端（:3000）
	cd web && pnpm dev

demo:               ## 演示环境：恢复 data-real 快照（对比语料 v7/BERT/PreLN/GPT-2 + UDL）+ docker 起全栈
	@if [ -d data-real ] && [ ! -f data/.demo_loaded ]; then mkdir -p data && cp -r data-real/. data/ && touch data/.demo_loaded; echo '已恢复 data-real 演示快照'; fi
	docker compose up --build -d
	@echo "→ http://localhost:3000  （API http://localhost:8100/api/health）"

corpus:             ## 对比语料收录：BERT/PreLN/GPT-2 下载→入库→解析 + data-real 快照固化
	cd api && uv run python scripts/fetch_corpus.py --snapshot

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
	rm -rf data api/tests/_ses_*/ scripts/runs
	docker compose down -v 2>/dev/null || true


e2e:              ## Playwright UI 验收（自起 api:8100 + web:3000，5 用例）
	cd web && pnpm e2e
